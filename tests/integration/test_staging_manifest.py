"""P1-05 验收测试：Staging 清单持久化、跨卷提交事务与强杀崩溃恢复。

验收标准：
- 单文件、多文件成果持久化 manifest.json
- 提交前后状态可追踪（staging -> committing -> committed / failed）
- 跨卷或移动失败自动回滚，原目标完整恢复
- 进程强杀后由 recover_staging() 清除半成品并恢复备份，残缺文件绝不登记为 succeeded
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.processing.manifest import (
    StagingManifest,
    load_manifest,
    recover_staging,
    save_manifest,
)
from spectra_workbench.providers.base import ExecutionContext
from spectra_workbench.workers.process import WorkerProcess

pytestmark = [pytest.mark.integration, pytest.mark.engine]


def test_persistent_manifest_lifecycle_on_gdal_task():
    """验证真实 GDAL 任务在 staging 目录持久化 manifest.json，记录哈希并终态为 committed。"""
    root = find_project_root()
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        workspace = temp_root / "workspace"
        target_tif = temp_root / "manifest_test_output.tif"

        worker = WorkerProcess(provider_id="gdal", worker_id="test-manifest-worker", project_root=root)
        try:
            worker.start(timeout=15.0)

            spec = TaskSpec(
                task_id="t-manifest-01",
                capability_id="gdal:translate",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(temp_root),
                workspace=str(workspace),
                inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=target_tif.as_uri(), overwrite=True)],
                parameters={"format": "GTiff"},
            )
            res = worker.execute_task(spec, timeout=20.0)

            assert res.status is TaskStatus.succeeded
            assert target_tif.exists()

            # 验证 staging 目录下持久化的 manifest.json
            staging_dir = workspace / ".staging" / "t-manifest-01"
            manifest = load_manifest(staging_dir)
            assert manifest is not None
            assert manifest.task_id == "t-manifest-01"
            assert manifest.state == "committed"
            assert len(manifest.staged_files) >= 1
            staged_info = manifest.staged_files[0]
            assert staged_info["size"] > 500
            assert len(staged_info["sha256"]) == 64  # SHA-256 长度
            assert str(target_tif.resolve()) in [str(Path(p).resolve()) for p in manifest.committed_files]

            worker.stop()
        finally:
            worker.kill()


def test_cross_volume_or_move_failure_transactional_rollback():
    """验证提交过程中移动失败时，事务完整回滚并持久化 manifest 状态为 failed，原目标无损。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        staging_dir = temp_root / "staging"
        target_dir = temp_root / "target"
        staging_dir.mkdir()
        target_dir.mkdir()

        # 构造待提交的两个文件（模拟主文件与头文件）
        staged_dat = staging_dir / "product.dat"
        staged_hdr = staging_dir / "product.hdr"
        staged_dat.write_bytes(b"STAGED DATA CONTENT")
        staged_hdr.write_bytes(b"STAGED HEADER CONTENT")

        # 目标端已存在原有目标文件
        target_dat = target_dir / "product.dat"
        target_hdr = target_dir / "product.hdr"
        orig_dat_content = b"ORIGINAL PRODUCTION DATA - DO NOT DESTROY"
        orig_hdr_content = b"ORIGINAL PRODUCTION HDR"
        target_dat.write_bytes(orig_dat_content)
        target_hdr.write_bytes(orig_hdr_content)

        ctx = ExecutionContext(task_id="t-rollback-01", workspace=temp_root, staging_dir=staging_dir)

        # 模拟提交第二个文件时发生跨卷或 I/O 故障
        import shutil as real_shutil

        original_shutil_move = real_shutil.move

        def flaky_move(src: str, dst: str):
            # 仅在移动第二个文件（header）时模拟跨卷错误，允许第一个文件正常移动及回滚移回
            if Path(src) == staged_hdr and Path(dst) == target_hdr:
                raise OSError("Simulated cross-volume or disk full error on header")
            return original_shutil_move(src, dst)

        with (
            patch("spectra_workbench.providers.base.shutil.move", side_effect=flaky_move),
            pytest.raises(OSError, match="Simulated cross-volume"),
        ):
            ctx.commit_files([(staged_dat, target_dat), (staged_hdr, target_hdr)], overwrite=True)

        # 核心保证：
        # 1. 目标端原有文件必须 100% 完好无损
        assert target_dat.exists()
        assert target_dat.read_bytes() == orig_dat_content
        assert target_hdr.exists()
        assert target_hdr.read_bytes() == orig_hdr_content

        # 2. staging 端文件仍然保留
        assert staged_dat.exists()
        assert staged_hdr.exists()

        # 3. 持久化的 manifest.json 记录为 failed
        manifest = load_manifest(staging_dir)
        assert manifest is not None
        assert manifest.state == "failed"
        assert manifest.error is not None
        assert "Simulated cross-volume" in manifest.error.get("message", "")


def test_recovery_after_forced_kill_during_commit():
    """验证 Worker 在提交过程中被强杀（模拟断电/崩溃）后，recover_staging() 能正确恢复。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        staging_dir = temp_root / "staging"
        target_dir = temp_root / "target"
        staging_dir.mkdir()
        target_dir.mkdir()

        # 模拟目标端原本存有的关键数据
        target_file = target_dir / "existing_critical.tif"
        original_bytes = b"ORIGINAL PRE-CRASH DATA"
        target_file.write_bytes(original_bytes)

        # 模拟 staging 目录处于 committing 阶段但未完成（强杀遗留）
        # 伴随同目录备份已创建
        backup_file = target_dir / ".existing_critical.tif.bak_test123"
        backup_file.write_bytes(original_bytes)
        # 且 target_file 处可能已有未写完的残缺半成品
        target_file.write_bytes(b"PARTIAL TRUNCATED JUNK DATA")

        # 构造崩溃时的未完成清单
        incomplete_manifest = StagingManifest(
            task_id="t-crash-01",
            attempt=1,
            state="committing",
            commit_pairs=[{"staged": str(staging_dir / "staged.tif"), "target": str(target_file)}],
            backups=[{"target": str(target_file), "backup": str(backup_file)}],
            staged_files=[{"path": str(staging_dir / "staged.tif"), "size": 100, "sha256": "abc"}],
        )
        save_manifest(staging_dir, incomplete_manifest)

        # 执行恢复操作
        recovery_report = recover_staging(staging_dir)

        assert recovery_report["recovered"] is True
        assert recovery_report["action"] == "reverted_uncommitted_state"

        # 核心保证：
        # 1. 目标端残缺半成品必须被清除，原有数据从备份中还原！
        assert target_file.exists()
        assert target_file.read_bytes() == original_bytes, "恢复后目标内容未能还原原始数据！"
        # 2. 临时备份文件应被消耗并还原
        assert not backup_file.exists()

        # 3. 清单持久化状态更新为 'recovered'，绝不被登记为 succeeded
        manifest = load_manifest(staging_dir)
        assert manifest is not None
        assert manifest.state == "recovered"
        assert "Recovered" in (manifest.recovery_notes or "")


def test_incomplete_staged_outputs_never_registered_as_succeeded():
    """验证任务计算异常失败时，残缺的 staging 成果绝不登记为 succeeded，TaskResult 状态为 failed。"""
    root = find_project_root()
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        workspace = temp_root / "workspace"
        target_tif = temp_root / "must_not_exist.tif"

        worker = WorkerProcess(provider_id="gdal", worker_id="test-fail-manifest", project_root=root)
        try:
            worker.start(timeout=15.0)

            # 故意传入非法格式参数触发 GDAL 内部报错
            spec = TaskSpec(
                task_id="t-invalid-format",
                capability_id="gdal:translate",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(temp_root),
                workspace=str(workspace),
                inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=target_tif.as_uri(), overwrite=True)],
                parameters={"format": "NON_EXISTENT_FORMAT_XYZ"},
            )
            res = worker.execute_task(spec, timeout=20.0)

            # 必须为 failed，绝对不能为 succeeded
            assert res.status is TaskStatus.failed
            assert not target_tif.exists()
            assert res.error is not None

            worker.stop()
        finally:
            worker.kill()


CHILD_KILL_SCRIPT = r"""
import sys, time
from pathlib import Path
from unittest.mock import patch
from spectra_workbench.providers.base import ExecutionContext
import spectra_workbench.providers.base as base

root = Path(sys.argv[1]); point = sys.argv[2]
staging = root / 'staging'; target = root / 'original.bin'; staged = staging / 'replacement.bin'
staging.mkdir(exist_ok=True)
target.write_bytes(b'ORIGINAL REVIEW FILE')
staged.write_bytes(b'NEW REVIEW FILE')
real_save = base.save_manifest
def persist_and_pause(directory, manifest):
    if point == 'before_commit_marker' and manifest.state == 'committed':
        (root / 'paused').write_text(point)
        while True: time.sleep(0.05)
    result = real_save(directory, manifest)
    if point == 'before_backup' and manifest.state == 'committing' and not manifest.backups:
        (root / 'paused').write_text(point)
        while True: time.sleep(0.05)
    return result

ctx = ExecutionContext(task_id='review-real-process-kill', workspace=root, staging_dir=staging)
with patch.object(base, 'save_manifest', side_effect=persist_and_pause):
    ctx.commit_files([(staged, target)], overwrite=True)
"""


def test_real_subprocess_kill_before_backup_preserves_untouched_target():
    """验证真实子进程在规划落盘后、物理备份创建前被强杀：恢复时绝不误删原文件。"""
    root = find_project_root()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "src")
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD_KILL_SCRIPT, str(temp_root), "before_backup"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            deadline = time.monotonic() + 10
            while not (temp_root / "paused").exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (temp_root / "paused").exists(), f"子进程未能在预期点暂停: {proc.communicate(timeout=2)}"

            # 强杀真实执行提交的子进程
            proc.kill()
            proc.wait(timeout=5)

            # 执行灾后恢复
            recovery = recover_staging(temp_root / "staging")
            assert recovery["recovered"] is True

            target = temp_root / "original.bin"
            # 核心断言：原文件必须存在且内容完好无损（绝不被当成半成品误删）
            assert target.exists()
            assert target.read_bytes() == b"ORIGINAL REVIEW FILE"
            assert str(target) in recovery.get("preserved_originals", [])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_real_subprocess_kill_before_commit_marker_restores_from_backup():
    """验证真实子进程在移动成果后、committed 落盘前被强杀：备份未被提前删除，成功还原原文件。"""
    root = find_project_root()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(root / "src")
        env["PYTHONIOENCODING"] = "utf-8"

        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD_KILL_SCRIPT, str(temp_root), "before_commit_marker"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            deadline = time.monotonic() + 10
            while not (temp_root / "paused").exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert (temp_root / "paused").exists(), f"子进程未能在预期点暂停: {proc.communicate(timeout=2)}"

            # 强杀真实执行提交的子进程
            proc.kill()
            proc.wait(timeout=5)

            # 执行灾后恢复
            recovery = recover_staging(temp_root / "staging")
            assert recovery["recovered"] is True

            target = temp_root / "original.bin"
            # 核心断言：原文件必须被还原（因为事务未完成，不能保留新文件）
            assert target.exists()
            assert target.read_bytes() == b"ORIGINAL REVIEW FILE"
            assert str(target) in recovery.get("restored_backups", [])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_restore_error_preserves_staging_and_reports_recovery_failed():
    """验证恢复操作遭遇异常（如权限不足）时，不吞异常，保留 staging 计算文件与备份，清单标记 recovery_failed。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        staging = temp_root / "staging"
        staging.mkdir(parents=True)
        target = temp_root / "original.bin"
        backup = temp_root / "original.backup"
        staged = staging / "replacement.bin"

        target.write_bytes(b"PARTIAL")
        backup.write_bytes(b"ORIGINAL")
        staged.write_bytes(b"NEW")

        manifest = StagingManifest(
            task_id="test-restore-err",
            state="committing",
            commit_pairs=[{"staged": str(staged), "target": str(target)}],
            backups=[{"target": str(target), "backup": str(backup)}],
            staged_files=[{"path": str(staged)}],
        )
        save_manifest(staging, manifest)

        orig_replace = os.replace

        def deny_restore(src, dst):
            if Path(src) == backup and Path(dst) == target:
                raise PermissionError("Simulated restoration permission denied")
            return orig_replace(src, dst)

        with patch("spectra_workbench.processing.manifest.os.replace", side_effect=deny_restore):
            recovery = recover_staging(staging)

        # 核心断言：
        # 1. 恢复报告 recovered 为 False，明确报告 recovery_failed
        assert recovery["recovered"] is False
        assert recovery["action"] == "recovery_failed"
        assert len(recovery["errors"]) > 0

        # 2. staging 文件与备份必须完整保留供人工排查或安全重试
        assert staged.exists()
        assert backup.exists()

        # 3. 磁盘清单状态必须为 recovery_failed，绝不被谎报为 recovered
        persisted = load_manifest(staging)
        assert persisted is not None
        assert persisted.state == "recovery_failed"


def test_recovered_manifest_is_read_only_terminal_and_replay_preserves_new_files():
    """验证 recovered 是只读终态：恢复完成后在同路径新建独立文件，再次恢复绝不误删该文件。"""
    class SimulatedAbruptExit(BaseException):
        pass

    import spectra_workbench.providers.base as provider_base

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        staging = temp_root / "staging"
        staging.mkdir(parents=True)
        staged = staging / "created.bin"
        target = temp_root / "created.bin"
        staged.write_bytes(b"TRANSACTION OUTPUT")

        real_save = provider_base.save_manifest

        def stop_before_marker(directory, manifest):
            if manifest.state == "committed":
                raise SimulatedAbruptExit("stop before commit decision落盘")
            return real_save(directory, manifest)

        ctx = ExecutionContext(task_id="t-terminal-recovered", workspace=temp_root, staging_dir=staging)
        with (
            patch.object(provider_base, "save_manifest", side_effect=stop_before_marker),
            pytest.raises(SimulatedAbruptExit),
        ):
            ctx.commit_files([(staged, target)])

        # 首次恢复：清理未提交的半成品
        first_recovery = recover_staging(staging)
        assert first_recovery["recovered"] is True
        assert not target.exists()

        # 在相同目标路径创建内容完全不同的独立成果
        target.write_bytes(b"LATER INDEPENDENT FILE")

        # 二次重放恢复
        second_recovery = recover_staging(staging)

        # 核心断言：
        # 1. recovered 必须作为只读终态，直接返回 already_recovered
        assert second_recovery["recovered"] is True
        assert second_recovery["action"] == "already_recovered"

        # 2. 后来新建的独立文件绝不被删除或修改
        assert target.exists()
        assert target.read_bytes() == b"LATER INDEPENDENT FILE"


def test_post_commit_cleanup_failure_retains_committed_targets_and_state():
    """验证 committed 持久化落盘是单向不可逆决断点：提交后清理或日志保存失败绝不回滚成果。"""
    import spectra_workbench.providers.base as provider_base

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        staging = temp_root / "staging"
        staging.mkdir(parents=True)
        target = temp_root / "existing.bin"
        staged = staging / "replacement.bin"
        target.write_bytes(b"ORIGINAL PRE-COMMIT DATA")
        staged.write_bytes(b"NEW COMMITTED OUTPUT")

        real_save = provider_base.save_manifest
        committed_save_count = 0

        def fail_on_second_committed_save(directory, manifest):
            nonlocal committed_save_count
            if manifest.state == "committed":
                committed_save_count += 1
                if committed_save_count == 2:
                    raise OSError("Injected post-commit cleanup metadata error")
            return real_save(directory, manifest)

        ctx = ExecutionContext(task_id="t-post-commit-err", workspace=temp_root, staging_dir=staging)

        # 执行提交，注入后清理阶段的异常
        with (
            patch.object(provider_base, "save_manifest", side_effect=fail_on_second_committed_save),
            pytest.raises(OSError, match="Injected post-commit cleanup metadata error"),
        ):
            ctx.commit_files([(staged, target)], overwrite=True)

        # 核心断言：
        # 1. 提交决断点已过，新成果必须完好保留在目标路径，绝不被移回 staging 或删除！
        assert target.exists()
        assert target.read_bytes() == b"NEW COMMITTED OUTPUT"

        # 2. 状态保持 committed 或 cleanup_pending，绝对不能变成 failed！
        disk_manifest = load_manifest(staging)
        assert disk_manifest is not None
        assert disk_manifest.state in {"committed", "cleanup_pending"}
        assert disk_manifest.state != "failed"

        # 3. 再次执行 recover_staging 也绝不触碰已提交的成果
        recovery = recover_staging(staging)
        assert recovery["recovered"] is True
        assert recovery["action"] == "already_committed"
        assert target.read_bytes() == b"NEW COMMITTED OUTPUT"


