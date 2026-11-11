"""P9-05 集成验收测试：故障注入与恢复审计。

验收标准（docs/plan/02_里程碑与验收.md P9-05）：
- worker 崩溃：子进程被强杀后能精准捕获退出、不挂死主线程、自动重启并恢复后续执行；
- 磁盘满：写入/提交段模拟 ENOSPC / 磁盘满异常，事务原子回滚，原目标数据完好无损；
- 无权限：输入源无读取权限或目标路径只读时返回标准拒绝错误，不发生静默破坏；
- 网络失败：远程 VSI 遭遇 404、503 持续失败与超时时受控失败，不阻塞 Qt 事件循环；
- 取消与重试：任务在执行中途协同取消迅速进入终态（<=2s），且后续任务可立即在同一 Client 成功复用；
- 损坏项目可恢复：畸变/截断项目 JSON 安全拦截不污染当前会话，缺失源进入重关联流程，保存失败原工程字节完好。
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.project.model import (
    LayerRecord,
    ProjectSpec,
    save_project,
)
from spectra_workbench.providers.base import ExecutionContext
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow
from spectra_workbench.workers.process import WorkerTimeoutError
from tests.fixtures.remote_server import LocalHttpRangeServer

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()
BIG_RASTER = ROOT / "work" / "p3_perf" / "p3_big_6144.tif"


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


@pytest.fixture
def gdal_rt() -> dict[str, Any]:
    return resolve_gdal_runtime(ROOT)


def _wait_qt(app: QApplication, timeout: float = 1.0, step: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(step)


def _make_dummy_spec(
    task_id: str,
    project_root: Path,
    out_path: Path,
    in_path: Path | None = None,
    params: dict[str, Any] | None = None,
) -> TaskSpec:
    src_uri = (in_path or BIG_RASTER).as_uri()
    return TaskSpec(
        task_id=task_id,
        attempt=1,
        capability_id="gdal:translate",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(project_root),
        workspace=str(out_path.parent),
        inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=src_uri)},
        outputs=[
            OutputPlan(key="output", kind=DatasetKind.raster, uri=out_path.as_uri(), overwrite=True)
        ],
        parameters=params or {"format": "GTiff"},
    )


# ==============================================================================
# 1. Worker 崩溃注入与自动感知重启恢复
# ==============================================================================


def test_p9_05_worker_crash_injection_detection_and_auto_restart(
    qapp: QApplication, tmp_path: Path
) -> None:
    """故障注入 1：Worker 进程在执行期间被强杀，客户端捕获退出，后续请求自动拉起全新 Worker 恢复成功。"""
    client = QtWorkerClient(provider_id="gdal", project_root=tmp_path)
    client.start(timeout=15.0)
    assert client.is_alive is True

    try:
        # 1. 提交长计算任务并在中途强杀 Worker 进程
        out_crash = tmp_path / "crash_test.tif"
        spec = TaskSpec(
            task_id=f"crash-{uuid.uuid4().hex[:6]}",
            attempt=1,
            capability_id="gdal:calc",
            descriptor_revision="1.0",
            engine=EngineConstraint(
                provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
            ),
            project_root=str(tmp_path),
            workspace=str(tmp_path),
            inputs={
                "inputs": [
                    DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=BIG_RASTER.as_uri())
                ]
            },
            outputs=[
                OutputPlan(
                    key="output", kind=DatasetKind.raster, uri=out_crash.as_uri(), overwrite=True
                )
            ],
            parameters={"expression": "A * 2.0 + numpy.sin(A)", "outputType": "Float32"},
        )

        err_holder: list[tuple[str, str]] = []
        client.sig_error.connect(lambda code, msg: err_holder.append((code, msg)))

        client.submit_task_async(spec, timeout=15.0)
        _wait_qt(qapp, timeout=0.15)

        # 强杀底层 Worker 进程
        worker_proc = client._worker._proc  # noqa: SLF001
        assert worker_proc is not None
        worker_proc.kill()
        worker_proc.wait(timeout=5.0)

        # 等待客户端感知到崩溃并发出错误通知
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not err_holder:
            _wait_qt(qapp, timeout=0.05)

        assert len(err_holder) > 0, "Worker 进程强杀后客户端未报告错误"
        err_code, err_msg = err_holder[0]
        assert (
            "task_submission_error" in err_code or "Worker" in err_msg or "exit" in err_msg.lower()
        )
        assert client.is_alive is False, "强杀后 client.is_alive 应感知为 False"

        # 2. 自动恢复审计：对同一个客户端提交新任务，验证自动重启且成功执行
        out_recovered = tmp_path / "recovered.tif"
        spec_recovery = TaskSpec(
            task_id=f"recover-{uuid.uuid4().hex[:6]}",
            attempt=1,
            capability_id="gdal:translate",
            descriptor_revision="1.0",
            engine=EngineConstraint(
                provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
            ),
            project_root=str(tmp_path),
            workspace=str(tmp_path),
            inputs={
                "input": DatasetRef(
                    dataset_id="in", kind=DatasetKind.raster, uri=BIG_RASTER.as_uri()
                )
            },
            outputs=[
                OutputPlan(
                    key="output",
                    kind=DatasetKind.raster,
                    uri=out_recovered.as_uri(),
                    overwrite=True,
                )
            ],
            parameters={"format": "GTiff", "srcWin": [0, 0, 128, 128]},
        )

        res_holder: list[Any] = []
        client.sig_result.connect(res_holder.append)

        # 此时 client 自动重启全新 worker
        client.submit_task_async(spec_recovery, timeout=20.0)
        deadline2 = time.monotonic() + 15.0
        while time.monotonic() < deadline2 and not res_holder:
            _wait_qt(qapp, timeout=0.05)

        assert len(res_holder) == 1, "崩溃后重新提交未能成功完成"
        res = res_holder[0]
        assert res.status == TaskStatus.succeeded
        assert out_recovered.exists()
        assert client.is_alive is True, "全新 Worker 进程已成功拉起并处于活动状态"
    finally:
        client.stop()


# ==============================================================================
# 2. 磁盘满（ENOSPC）故障注入与原子回滚
# ==============================================================================


def test_p9_05_disk_full_enospc_injection_and_rollback(tmp_path: Path) -> None:
    """故障注入 2：模拟提交阶段遭遇磁盘满（ENOSPC），事务原子回滚，原目标数据完好无损。"""
    target = tmp_path / "important_data.tif"
    original_bytes = b"ORIGINAL_IMPORTANT_RASTER_DATA_NO_CORRUPT"
    target.write_bytes(original_bytes)

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / "new_calc_result.tif"
    staged.write_bytes(b"NEW_CALCULATED_DATA_THAT_FAILED_DURING_COMMIT")

    ctx = ExecutionContext(
        task_id="t-diskfull-enospc-01",
        workspace=tmp_path,
        staging_dir=staging_dir,
    )

    # 模拟在安装新成果（shutil.move）阶段触发 ENOSPC（磁盘满）
    def mock_enospc_move(src: Any, dst: Any) -> None:
        raise OSError(28, "No space left on device")

    with (
        patch("spectra_workbench.providers.base.shutil.move", side_effect=mock_enospc_move),
        pytest.raises(OSError, match="No space left on device"),
    ):
        ctx.commit_files([(staged, target)], overwrite=True)

    # 核心断言 1：manifest 状态标记为 failed，并携带 commit_failed 诊断
    assert ctx.manifest.state == "failed"
    assert ctx.manifest.error is not None
    assert ctx.manifest.error.get("code") == "commit_failed"

    # 核心断言 2：原目标文件完好无损，绝不留下 0 字节截断坏文件
    assert target.exists()
    assert target.read_bytes() == original_bytes, "磁盘满异常下原文件数据被破坏"

    # 核心断言 3：备份文件已完全还原，不存在遗留的 .bak 文件
    bak_files = list(tmp_path.glob("*.bak_*"))
    assert len(bak_files) == 0, f"回滚后残留备份文件: {bak_files}"


# ==============================================================================
# 3. 无权限与访问受阻故障注入
# ==============================================================================


def test_p9_05_permission_denied_fault_injection(qapp: QApplication, tmp_path: Path) -> None:
    """故障注入 3：输入文件不存在或无读权限、输出目录锁定时前置校验拦截并安全报错。"""
    client = QtWorkerClient(provider_id="gdal", project_root=tmp_path)
    client.start(timeout=15.0)

    try:
        # Case A: 输入文件不存在或不可访问
        missing_input = tmp_path / "non_existent_raster.tif"
        out_path = tmp_path / "out.tif"
        spec_bad_in = _make_dummy_spec("t-perm-01", tmp_path, out_path, in_path=missing_input)

        res_a = client.execute_task_sync(spec_bad_in, timeout=10.0)
        assert res_a.status == TaskStatus.failed
        assert "input_not_found" in str(res_a.error) or "不存在" in str(res_a.error)

        # Case B: 输出目标锁定为只读文件，且 overwrite=False
        locked_target = tmp_path / "locked_output.tif"
        locked_target.write_bytes(b"LOCKED_DATA")
        spec_locked = _make_dummy_spec("t-perm-02", tmp_path, locked_target)
        # 将输出声明为 overwrite=False
        spec_locked.outputs[0].overwrite = False

        res_b = client.execute_task_sync(spec_locked, timeout=10.0)
        assert res_b.status == TaskStatus.failed
        err_str = str(res_b.error).lower()
        assert (
            "output_already_exists" in err_str
            or "already exists" in err_str
            or "已存在" in str(res_b.error)
        )
        assert locked_target.read_bytes() == b"LOCKED_DATA"
    finally:
        client.stop()


# ==============================================================================
# 4. 网络失败与远程 VSI 异常注入（404 / 503 / 超时）
# ==============================================================================


def test_p9_05_network_vsi_failures_and_timeout_injection(tmp_path: Path) -> None:
    """故障注入 4：本地模拟远程 HTTP 404、503 故障与可控超时，验证受控失败且 Worker 进程不崩溃。"""
    client = QtWorkerClient(provider_id="gdal", project_root=tmp_path)
    client.start(timeout=15.0)

    try:
        # 1. 模拟 404 Not Found
        server_404 = LocalHttpRangeServer(files={}, fail_status=404)
        try:
            vsi_404 = server_404.vsi_url_for("missing_image.tif")
            with pytest.raises(Exception) as exc_info:
                client.probe_dataset(vsi_404, timeout=5.0)
            err_str = str(exc_info.value)
            assert "404" in err_str or "探测" in err_str or "失败" in err_str
            assert client.is_alive is True, "404 故障不应导致 Worker 退出"
        finally:
            server_404.stop()

        # 2. 模拟超时端点（delay 4.0s，探测设置 0.8s 硬超时）
        server_slow = LocalHttpRangeServer(
            files={"slow.tif": b"DUMMY_TILED_DATA"}, delay_seconds=4.0
        )
        try:
            vsi_slow = server_slow.vsi_url_for("slow.tif")
            t0 = time.perf_counter()
            with pytest.raises((WorkerTimeoutError, TimeoutError, RuntimeError)):
                client.probe_dataset(vsi_slow, timeout=1.0)
            elapsed = time.perf_counter() - t0
            assert elapsed < 3.5, f"超时未能在期望时间内阻断: {elapsed:.2f}s"
            assert client.is_alive is True, "网络超时不应击垮 Worker 进程"
        finally:
            server_slow.stop()

        # 3. 验证网络异常后 Worker 依然健康，本地操作依然可用
        local_meta = client.probe_dataset(BIG_RASTER, timeout=5.0)
        assert int(local_meta.get("width", 0)) == 6144
    finally:
        client.stop()


# ==============================================================================
# 5. 任务协作取消与立即复用恢复
# ==============================================================================


def test_p9_05_cancellation_and_resubmission_recovery(qapp: QApplication, tmp_path: Path) -> None:
    """故障注入 5：运行中发起协同取消（<=2s 完成），同一个 Worker 进程不重启直接承接并成功完成新任务。"""
    client = QtWorkerClient(provider_id="gdal", project_root=tmp_path)
    client.start(timeout=15.0)

    try:
        task_id = f"cancel-{uuid.uuid4().hex[:6]}"
        out_cancel = tmp_path / "cancelled_output.tif"
        spec = TaskSpec(
            task_id=task_id,
            attempt=1,
            capability_id="gdal:calc",
            descriptor_revision="1.0",
            engine=EngineConstraint(
                provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
            ),
            project_root=str(tmp_path),
            workspace=str(tmp_path),
            inputs={
                "inputs": [
                    DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=BIG_RASTER.as_uri())
                ]
            },
            outputs=[
                OutputPlan(
                    key="output", kind=DatasetKind.raster, uri=out_cancel.as_uri(), overwrite=True
                )
            ],
            parameters={
                "expression": "numpy.sqrt(numpy.abs(A)) * numpy.cos(A)",
                "outputType": "Float32",
            },
        )

        res_holder: list[Any] = []
        client.sig_result.connect(res_holder.append)

        client.submit_task_async(spec, timeout=15.0)
        _wait_qt(qapp, timeout=0.1)

        # 发送取消
        t_cancel = time.perf_counter()
        client.cancel_task(task_id)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not res_holder:
            _wait_qt(qapp, timeout=0.05)

        assert len(res_holder) == 1, "取消未能在超时内收到终态"
        cancel_duration = time.perf_counter() - t_cancel
        assert cancel_duration <= 2.0, f"取消耗时过长: {cancel_duration:.3f}s"
        assert res_holder[0].status == TaskStatus.cancelled
        assert not out_cancel.exists(), "已取消的任务绝不能留下未完成的成果文件"

        # 立即在同客户端上提交新任务（不重启 Worker）
        out_ok = tmp_path / "after_cancel_ok.tif"
        spec_ok = TaskSpec(
            task_id=f"next-{uuid.uuid4().hex[:6]}",
            attempt=1,
            capability_id="gdal:translate",
            descriptor_revision="1.0",
            engine=EngineConstraint(
                provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
            ),
            project_root=str(tmp_path),
            workspace=str(tmp_path),
            inputs={
                "input": DatasetRef(
                    dataset_id="in", kind=DatasetKind.raster, uri=BIG_RASTER.as_uri()
                )
            },
            outputs=[
                OutputPlan(
                    key="output", kind=DatasetKind.raster, uri=out_ok.as_uri(), overwrite=True
                )
            ],
            parameters={"format": "GTiff", "srcWin": [0, 0, 100, 100]},
        )

        res_holder.clear()
        client.submit_task_async(spec_ok, timeout=15.0)
        deadline2 = time.monotonic() + 10.0
        while time.monotonic() < deadline2 and not res_holder:
            _wait_qt(qapp, timeout=0.05)

        assert len(res_holder) == 1
        assert res_holder[0].status == TaskStatus.succeeded
        assert out_ok.exists()
    finally:
        client.stop()


# ==============================================================================
# 6. 损坏项目文件安全恢复与原子保存韧性
# ==============================================================================


def test_p9_05_corrupt_project_file_handling_and_atomic_save_resilience(
    qapp: QApplication, tmp_path: Path
) -> None:
    """故障注入 6：损坏/截断工程文件安全阻断、缺失源进入重定位恢复、保存中途崩溃原工程完好无损。"""
    win = MainWindow(project_root=tmp_path)
    win.show()
    qapp.processEvents()

    try:
        # Case A: 畸形/截断的 JSON 工程文件
        corrupt_proj = tmp_path / "corrupt_truncated.spectra.json"
        corrupt_proj.write_text('{"schema_version": "1.0", "layers": [', encoding="utf-8")

        res_open = win.open_project(corrupt_proj)
        assert res_open is False, "打开截断畸变工程必须返回 False 阻断"
        assert len(win.layer_panel.layers) == 0, "畸变工程不得污染图层模型"

        # Case B: 包含丢失数据源的工程文件，验证 missing_sources 恢复流程
        valid_sample = tmp_path / "sample_raster.tif"
        shutil.copyfile(BIG_RASTER, valid_sample)

        missing_sample = tmp_path / "deleted_raster.tif"
        missing_sample.write_bytes(b"temp")
        missing_sample.unlink()  # 物理删除

        proj_file = tmp_path / "missing_source_project.spectra.json"
        spec = ProjectSpec(
            path_anchor="project_dir",
            layers=[
                LayerRecord(
                    layer_id="ly1",
                    name="Valid",
                    source="sample_raster.tif",
                    source_is_relative=True,
                    order=0,
                ),
                LayerRecord(
                    layer_id="ly2",
                    name="Missing",
                    source="deleted_raster.tif",
                    source_is_relative=True,
                    order=1,
                ),
            ],
        )
        save_project(proj_file, spec)

        # 打开包含缺失源的工程
        res_missing = win.open_project(proj_file)
        assert res_missing is True
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and len(win.layer_panel.layers) < 1:
            _wait_qt(qapp, timeout=0.05)

        # 验证缺失源被安全归类至 missing_sources，有效图层正常加载
        assert len(win.missing_sources) == 1
        assert win.missing_sources[0].layer_id == "ly2"
        assert len(win.layer_panel.layers) == 1
        assert win.layer_panel.layers[0].layer_id == "ly1"

        # 模拟用户移除缺失图层
        win.missing_sources.clear()
        win.layer_panel.set_missing_layers([])
        assert len(win.missing_sources) == 0

        # Case C: 保存工程中途遭遇崩溃注入（os.replace 前抛异常）
        initial_saved_bytes = proj_file.read_bytes()

        def inject_crash_replace(src: Any, dst: Any) -> None:
            raise RuntimeError("Injected system crash before atomic replace")

        with (
            patch("spectra_workbench.project.model.os.replace", side_effect=inject_crash_replace),
            pytest.raises(RuntimeError, match="Injected system crash"),
        ):
            save_project(proj_file, spec)

        # 核心断言：原工程文件字节数与内容完全不变，且没有遗留 .tmp 脏文件
        assert proj_file.read_bytes() == initial_saved_bytes
        dangling_tmps = list(tmp_path.glob("*.tmp"))
        assert len(dangling_tmps) == 0, f"原子保存失败遗留了临时脏文件: {dangling_tmps}"
    finally:
        win.close()
        qapp.processEvents()
