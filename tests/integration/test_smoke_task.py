"""P1-03 验收测试：ProviderAdapter 与真实 smoke 作业。

验收标准：
- Qt 提交真实格式转换（ENVI -> GeoTIFF）
- Worker 在独立 GDAL 运行环境中执行
- 成果文件在目标路径可重新打开验证（完整像元比较、CRS、transform、dtype、NoData、元数据）
- 过程日志记录 GDAL 引擎版本（3.13.3）
- 消息符合 JSON Schema 契约，staging 临时目录正常闭合
- 覆盖保护：overwrite=false 拒绝冲突且内容不变；允许覆盖原子替换；commit 失败回滚原目标
- 多文件完整性：ENVI 格式主文件 .dat 与头文件 .hdr 统一提交，目标端可直接读取
- 路径解析契约：相对路径严格按 TaskSpec.project_root 解析，不受 worker 当前 cwd 影响
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.providers.base import ExecutionContext
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.workers.process import WorkerProcess

pytestmark = [pytest.mark.integration, pytest.mark.engine]

EXPECTED_FIRST_ROW = [
    0.0, 17.0, 34.0, 51.0, 68.0, 85.0, 102.0, 119.0,
    136.0, 153.0, 170.0, 187.0, 204.0, 221.0, 238.0, 255.0
]


def _get_qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def test_gdal_smoke_task_via_qt_client():
    """P1-03 核心验收：Qt 提交真实格式转换，GDAL Worker 执行，成果全量数值核验，日志含引擎版本。"""
    root = find_project_root()
    qapp = _get_qapp()

    # 1. 确认输入样品质（A01-1 单波段水平线性渐变 16x16 Float32 ENVI 格式）
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"
    assert in_dat.exists(), f"测试输入文件不存在: {in_dat}"

    with tempfile.TemporaryDirectory() as temp_dir:
        workspace_dir = Path(temp_dir)
        out_tif = workspace_dir / "converted_ramp.tif"

        # 2. 构造 TaskSpec
        spec = TaskSpec(
            task_id="smoke-task-p1-03",
            attempt=1,
            capability_id="gdal:translate",
            descriptor_revision="1.0",
            engine=EngineConstraint(
                provider_id="gdal",
                runtime_id="gdal-qgis",
                expected_version="3.13.3",
            ),
            project_root=str(root),
            workspace=str(workspace_dir),
            inputs={
                "input": DatasetRef(
                    dataset_id="ramp-fixture",
                    uri=in_dat.resolve().as_uri(),
                    kind=DatasetKind.raster,
                )
            },
            outputs=[
                OutputPlan(
                    key="output",
                    kind=DatasetKind.raster,
                    uri=out_tif.resolve().as_uri(),
                    driver="GTiff",
                    overwrite=True,
                )
            ],
            parameters={"format": "GTiff"},
        )

        # 3. 初始化并启动 QtWorkerClient
        client = QtWorkerClient(
            provider_id="gdal",
            worker_id="smoke-qt-worker-01",
            project_root=root,
        )

        collected_logs: list[tuple[str, str, str]] = []
        collected_progress: list[tuple[float, str, str]] = []
        result_holder: list[TaskResult] = []
        error_holder: list[tuple[str, str]] = []

        client.sig_log.connect(lambda lvl, msg, strm: collected_logs.append((lvl, msg, strm)))
        client.sig_progress.connect(lambda r, p, m: collected_progress.append((r, p, m)))
        client.sig_result.connect(lambda res: result_holder.append(res))
        client.sig_error.connect(lambda c, m: error_holder.append((c, m)))

        try:
            ready_env = client.start(timeout=15.0)
            assert ready_env.payload["runtime_id"] == "gdal-worker"

            # 4. Qt 异步提交任务并驱动 Qt 事件循环
            client.submit_task_async(spec, timeout=30.0)

            start_wait = time.monotonic()
            while not result_holder and not error_holder and time.monotonic() - start_wait < 30.0:
                qapp.processEvents()
                time.sleep(0.05)

            assert not error_holder, f"任务上报异常: {error_holder}"
            assert result_holder, "任务在 30 秒内未产生结果"
            result = result_holder[0]

            # 5. 校验任务终态与成果描述
            assert result.status is TaskStatus.succeeded
            assert result.task_id == "smoke-task-p1-03"
            assert len(result.outputs) >= 1
            out_record = result.outputs[0]
            assert out_record["driver"] == "GTiff"
            assert out_record["width"] == 16
            assert out_record["height"] == 16
            assert out_record["bands"] == 1

            # 6. 校验日志与引擎版本记录
            assert result.engine_runtime.get("provider_id") == "gdal"
            gdal_ver = result.engine_runtime.get("version")
            assert gdal_ver == "3.13.3"

            log_texts = [msg for _, msg, _ in collected_logs]
            has_version_in_logs = any("3.13.3" in m for m in log_texts)
            assert has_version_in_logs, f"过程日志未包含 GDAL 版本信息: {log_texts}"

            # 7. 成果深度数值核验（全量像元比较、CRS、transform、dtype）
            assert out_tif.exists(), f"成果文件未落盘: {out_tif}"

            gdal_rt = resolve_gdal_runtime(root)
            gdal_py = str(gdal_rt["python"])
            env = gdal_rt["env"]

            verify_script = (
                "import sys, json; from osgeo import gdal\n"
                f"ds = gdal.Open(r'{out_tif}')\n"
                "assert ds is not None\n"
                "w, h, b_cnt = ds.RasterXSize, ds.RasterYSize, ds.RasterCount\n"
                "gt = ds.GetGeoTransform()\n"
                "proj = ds.GetProjection()\n"
                "band = ds.GetRasterBand(1)\n"
                "dt = gdal.GetDataTypeName(band.DataType)\n"
                "arr = band.ReadAsArray().tolist()\n"
                "min_v, max_v = band.ComputeRasterMinMax()\n"
                "print(json.dumps({\n"
                "    'width': w, 'height': h, 'bands': b_cnt,\n"
                "    'gt': list(gt), 'has_proj': bool(proj and 'UTM' in proj),\n"
                "    'dtype': dt, 'min': min_v, 'max': max_v,\n"
                "    'first_row': arr[0], 'last_row': arr[-1]\n"
                "}))\n"
            )
            p = subprocess.run([gdal_py, "-c", verify_script], capture_output=True, text=True, env=env)
            assert p.returncode == 0, f"GDAL 深度校验失败: {p.stderr}"
            probe_data = json.loads(p.stdout.strip())

            # 严格数值与空间模型契约断言
            assert probe_data["width"] == 16 and probe_data["height"] == 16
            assert probe_data["bands"] == 1
            assert probe_data["dtype"] == "Float32"
            assert probe_data["gt"] == [400000.0, 30.0, 0.0, 4400000.0, 0.0, -30.0]
            assert probe_data["has_proj"] is True
            assert probe_data["min"] == 0.0 and probe_data["max"] == 255.0
            assert probe_data["first_row"] == EXPECTED_FIRST_ROW
            assert probe_data["last_row"] == EXPECTED_FIRST_ROW

            # Rasterio 交叉验证
            rio_py = root / ".venvs" / "sw-rasterio" / "Scripts" / "python.exe"
            if rio_py.exists():
                rio_script = (
                    "import rasterio, json\n"
                    f"with rasterio.open(r'{out_tif}') as src:\n"
                    "    arr = src.read(1)\n"
                    "    print(json.dumps({\n"
                    "        'width': src.width, 'height': src.height, 'count': src.count,\n"
                    "        'dtype': str(src.dtypes[0]), 'min': float(arr.min()), 'max': float(arr.max()),\n"
                    "        'first_row': arr[0].tolist()\n"
                    "    }))\n"
                )
                rp = subprocess.run([str(rio_py), "-c", rio_script], capture_output=True, text=True)
                assert rp.returncode == 0, f"Rasterio 校验失败: {rp.stderr}"
                rio_data = json.loads(rp.stdout.strip())
                assert rio_data["width"] == 16 and rio_data["height"] == 16
                assert rio_data["dtype"] == "float32"
                assert rio_data["first_row"] == EXPECTED_FIRST_ROW

            # 8. 校验 staging 目录闭合情况
            staging_dir = workspace_dir / ".staging" / "smoke-task-p1-03"
            if staging_dir.exists():
                remaining_staged = list(staging_dir.glob("staging_*"))
                assert not remaining_staged, f"Staging 目录残留未提交文件: {remaining_staged}"

            # 9. 停止 Worker 进程
            exit_code = client.stop(timeout=5.0)
            assert exit_code == 0
            assert not client.is_alive
        finally:
            client.kill()


def test_output_overwrite_protection_preserves_existing_target():
    """验证 Defect 1：当 overwrite=False 时严格拒绝冲突，既有文件内容绝对不被破坏；允许覆盖时正常更新。"""
    root = find_project_root()
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        protected_target = temp_root / "existing_protected.tif"
        sentinel_content = b"SENTINEL BYTES - MUST NOT BE OVERWRITTEN"
        protected_target.write_bytes(sentinel_content)

        worker = WorkerProcess(provider_id="gdal", worker_id="test-overwrite-guard", project_root=root)
        try:
            worker.start(timeout=15.0)

            # 1. 尝试以 overwrite=False 提交
            spec_conflict = TaskSpec(
                task_id="t-conflict-01",
                capability_id="gdal:translate",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(temp_root),
                workspace=str(temp_root / "workspace"),
                inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=protected_target.as_uri(), overwrite=False)],
                parameters={"format": "GTiff"},
            )
            res_conflict = worker.execute_task(spec_conflict, timeout=20.0)

            # 判定：任务失败，原目标文件完整存活
            assert res_conflict.status is TaskStatus.failed
            assert res_conflict.error and res_conflict.error.get("code") == "output_already_exists"
            assert protected_target.exists()
            assert protected_target.read_bytes() == sentinel_content

            # 2. 以 overwrite=True 重新提交
            spec_overwrite = TaskSpec(
                task_id="t-conflict-02",
                capability_id="gdal:translate",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(temp_root),
                workspace=str(temp_root / "workspace"),
                inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=protected_target.as_uri(), overwrite=True)],
                parameters={"format": "GTiff"},
            )
            res_overwrite = worker.execute_task(spec_overwrite, timeout=20.0)

            # 判定：任务成功，原目标被合法替换为新成果
            assert res_overwrite.status is TaskStatus.succeeded
            assert protected_target.exists()
            assert protected_target.read_bytes() != sentinel_content
            assert protected_target.stat().st_size > 500

            worker.stop()
        finally:
            worker.kill()


def test_envi_dataset_sidecar_commit_and_reopen():
    """验证 Defect 2：ENVI 格式主文件 .dat 与伴生头文件 .hdr 统一提交，目标端可完整重新打开。"""
    root = find_project_root()
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        envi_target = temp_root / "converted_envi.dat"

        worker = WorkerProcess(provider_id="gdal", worker_id="test-envi-sidecars", project_root=root)
        try:
            worker.start(timeout=15.0)

            spec = TaskSpec(
                task_id="t-envi-01",
                capability_id="gdal:translate",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(temp_root),
                workspace=str(temp_root / "workspace"),
                inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=envi_target.as_uri(), driver="ENVI", overwrite=True)],
                parameters={"format": "ENVI"},
            )
            res = worker.execute_task(spec, timeout=20.0)

            assert res.status is TaskStatus.succeeded
            # 必须验证最终目标端 .dat 和 .hdr 均存在！
            assert envi_target.exists(), "最终目标 .dat 不存在"
            hdr_target = envi_target.with_suffix(".hdr")
            assert hdr_target.exists(), "最终目标 .hdr 头文件缺失未被提交！"

            # 验证 staging 目录中不残留 .hdr
            staging_dir = temp_root / "workspace" / ".staging" / "t-envi-01"
            if staging_dir.exists():
                staged_hdrs = list(staging_dir.glob("*.hdr"))
                assert not staged_hdrs, f"Staging 依然残留未移动的头文件: {staged_hdrs}"

            # 重新打开核验
            gdal_rt = resolve_gdal_runtime(root)
            check_script = (
                "import sys; from osgeo import gdal\n"
                f"ds = gdal.Open(r'{envi_target}')\n"
                "assert ds is not None\n"
                "assert ds.RasterXSize == 16 and ds.RasterYSize == 16\n"
                "print('ENVI_REOPEN_OK')\n"
            )
            p = subprocess.run([str(gdal_rt["python"]), "-c", check_script], env=gdal_rt["env"], capture_output=True, text=True)
            assert p.returncode == 0, f"ENVI 重新打开失败: {p.stderr}"
            assert "ENVI_REOPEN_OK" in p.stdout

            worker.stop()
        finally:
            worker.kill()


def test_project_relative_output_resolution_independent_of_cwd():
    """验证 Defect 3：相对路径输出严格按 TaskSpec.project_root 解析，不受 worker 当前 cwd 影响。"""
    root = find_project_root()
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        user_project = temp_root / "declared_project"
        user_project.mkdir()
        isolated_worker_cwd = temp_root / "arbitrary_worker_cwd"
        isolated_worker_cwd.mkdir()

        prev_cwd = Path.cwd()
        os.chdir(isolated_worker_cwd)

        worker = WorkerProcess(provider_id="gdal", worker_id="test-rel-path-cwd", project_root=root)
        try:
            worker.start(timeout=15.0)

            rel_output = "results/subfolder/relative_test.tif"
            spec = TaskSpec(
                task_id="t-rel-01",
                capability_id="gdal:translate",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(user_project),
                workspace=str(temp_root / "workspace"),
                inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=rel_output, overwrite=True)],
                parameters={"format": "GTiff"},
            )
            res = worker.execute_task(spec, timeout=20.0)

            assert res.status is TaskStatus.succeeded

            # 产出文件必须在 project_root 下，绝对不能在 worker_cwd 下！
            expected_file = user_project / rel_output
            unexpected_file = isolated_worker_cwd / rel_output

            assert expected_file.exists(), f"文件未生成在声明的项目目录下: {expected_file}"
            assert not unexpected_file.exists(), f"文件被错误写入 worker 当前工作目录下: {unexpected_file}"

            worker.stop()
        finally:
            worker.kill()
            os.chdir(prev_cwd)


def test_commit_failure_preserves_original_target_and_staged():
    """验证 Defect 1 边界：当提交过程中注入 move 异常时，回滚机制安全保留原目标与 staging 内容。"""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        existing = temp_root / "existing_original.bin"
        staged = temp_root / "replacement.bin"
        original_data = b"ORIGINAL PRODUCTION DATA - CRITICAL"
        staged_data = b"NEW COMPUTED DATA"

        existing.write_bytes(original_data)
        staged.write_bytes(staged_data)

        ctx = ExecutionContext(task_id="review-commit-failure", workspace=temp_root, staging_dir=temp_root / "staging")
        caught = None
        with patch("spectra_workbench.providers.base.shutil.move", side_effect=OSError("injected move failure")):
            try:
                ctx.commit_file(staged, existing, overwrite=True)
            except OSError as exc:
                caught = str(exc)

        assert caught == "injected move failure"
        # 核心断言：原目标绝不能被 unlink，必须原封不动恢复！
        assert existing.exists(), "提交失败后原目标文件丢失！"
        assert existing.read_bytes() == original_data, "提交失败后原目标内容被修改！"
        assert staged.exists(), "提交失败后 staging 文件丢失！"
        assert staged.read_bytes() == staged_data


def test_input_output_same_file_rejected():
    """验证输入与输出指向同一物理文件时安全拒绝，防止原地覆盖破坏数据源。"""
    root = find_project_root()
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        worker = WorkerProcess(provider_id="gdal", worker_id="test-same-file-rejection", project_root=root)
        try:
            worker.start(timeout=15.0)

            spec = TaskSpec(
                task_id="t-samefile-01",
                capability_id="gdal:translate",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(temp_root),
                workspace=str(temp_root / "workspace"),
                inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=in_dat.as_uri(), overwrite=True)],
                parameters={"format": "GTiff"},
            )
            res = worker.execute_task(spec, timeout=20.0)

            assert res.status is TaskStatus.failed
            assert res.error and res.error.get("code") == "input_output_conflict"

            worker.stop()
        finally:
            worker.kill()


def test_gdal_warp_reprojection_numerical_and_crs():
    """P2 真实重投影数值与 CRS 测试：请求 dstSRS=EPSG:3857，验证输出 CRS 确为 3857 且坐标系变换生效。"""
    root = find_project_root()
    rt = resolve_gdal_runtime(root)
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        target_tif = temp_root / "warped_webmercator_3857.tif"

        worker = WorkerProcess(provider_id="gdal", worker_id="test-warp-crs", project_root=root)
        try:
            worker.start(timeout=15.0)

            spec = TaskSpec(
                task_id="warp-3857-smoke",
                capability_id="gdal:warp",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(temp_root),
                workspace=str(temp_root / "workspace"),
                inputs={"input": DatasetRef(dataset_id="source", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=target_tif.as_uri(), driver="GTiff", overwrite=True)],
                parameters={"dstSRS": "EPSG:3857", "resampleAlg": "bilinear"},
            )
            res = worker.execute_task(spec, timeout=20.0)

            assert res.status is TaskStatus.succeeded, f"Warp 任务执行失败: {res.error}"
            assert target_tif.exists(), "Warp 目标输出文件未生成"

            # 跨进程使用实际 GDAL 运行时校验成果 CRS 与坐标范围
            check_script = (
                "import json, sys; from osgeo import gdal; "
                "ds = gdal.Open(sys.argv[1]); "
                "s = ds.GetSpatialRef(); "
                "gt = ds.GetGeoTransform(); "
                "print(json.dumps({'epsg': s.GetAuthorityCode(None) if s else None, 'gt': gt, 'xsize': ds.RasterXSize, 'ysize': ds.RasterYSize}))"
            )
            proc = subprocess.run(
                [str(rt["python"]), "-c", check_script, str(target_tif)],
                env=rt["env"],
                capture_output=True,
                text=True,
                timeout=15.0,
            )
            assert proc.returncode == 0, proc.stderr
            info = json.loads(proc.stdout)

            # 严格数值与坐标断言：必须完成从 UTM 50N 到 Web Mercator (3857) 的重投影
            assert info["epsg"] == "3857", f"CRS 重投影失败：期望 3857，实际为 {info['epsg']}"
            gt = info["gt"]
            # UTM 50N 坐标范围在 ~400000 左右，Web Mercator 在 ~12894453 左右
            assert gt[0] > 10_000_000.0, f"X 坐标变换未生效: {gt[0]}"
            assert gt[3] > 4_000_000.0, f"Y 坐标变换未生效: {gt[3]}"
            assert info["xsize"] > 0 and info["ysize"] > 0

            worker.stop()
        finally:
            worker.kill()


def test_gdal_worker_unsupported_capability_rejection():
    """负向准入测试：未接入的算法（如 gdal:grid）安全拒绝，不执行 Translate，不产生目标文件。"""
    root = find_project_root()
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        target_tif = temp_root / "should_not_exist.tif"

        worker = WorkerProcess(provider_id="gdal", worker_id="test-unsupported-cap", project_root=root)
        try:
            worker.start(timeout=15.0)

            spec = TaskSpec(
                task_id="unsupported-grid-test",
                capability_id="gdal:grid",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
                project_root=str(temp_root),
                workspace=str(temp_root / "workspace"),
                inputs={"input": DatasetRef(dataset_id="source", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=target_tif.as_uri(), driver="GTiff")],
                parameters={"algorithm": "invdist"},
            )
            res = worker.execute_task(spec, timeout=20.0)

            assert res.status is TaskStatus.failed
            assert res.error and res.error.get("code") == "unsupported_capability"
            assert not target_tif.exists(), "未支持的能力不应写入目标成果"

            worker.stop()
        finally:
            worker.kill()


def test_rasterio_worker_convert_and_unsupported_rejection():
    """Rasterio Worker 端到端与能力准入测试：启动 Worker 进程，执行 convert 闭环并拒绝未实现项。"""
    root = find_project_root()
    in_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        target_tif = temp_root / "rio_converted.tif"
        bad_tif = temp_root / "bad_warp.tif"

        worker = WorkerProcess(provider_id="rasterio", worker_id="test-rio-e2e", project_root=root)
        try:
            worker.start(timeout=15.0)
            assert worker.is_alive, "Rasterio Worker 启动失败"

            # 1. 成功路径：rasterio:convert
            spec_ok = TaskSpec(
                task_id="rio-convert-ok",
                capability_id="rasterio:convert",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="rasterio", runtime_id="sw-rasterio", expected_version="1.5.2"),
                project_root=str(temp_root),
                workspace=str(temp_root / "workspace"),
                inputs={"input": DatasetRef(dataset_id="source", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=target_tif.as_uri(), driver="GTiff", overwrite=True)],
                parameters={},
            )
            res_ok = worker.execute_task(spec_ok, timeout=20.0)
            assert res_ok.status is TaskStatus.succeeded, f"Rasterio convert 失败: {res_ok.error}"
            assert target_tif.exists(), "Rasterio convert 输出未生成"

            # 2. 负向路径：未实现的 rasterio:warp 必须被安全拒绝
            spec_bad = TaskSpec(
                task_id="rio-warp-unsupported",
                capability_id="rasterio:warp",
                descriptor_revision="1.0",
                engine=EngineConstraint(provider_id="rasterio", runtime_id="sw-rasterio", expected_version="1.5.2"),
                project_root=str(temp_root),
                workspace=str(temp_root / "workspace"),
                inputs={"input": DatasetRef(dataset_id="source", kind=DatasetKind.raster, uri=in_dat.as_uri())},
                outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=bad_tif.as_uri(), driver="GTiff")],
                parameters={},
            )
            res_bad = worker.execute_task(spec_bad, timeout=20.0)
            assert res_bad.status is TaskStatus.failed
            assert res_bad.error and res_bad.error.get("code") == "unsupported_capability"
            assert not bad_tif.exists(), "未支持的 Rasterio 操作不应创建目标文件"

            worker.stop()
        finally:
            worker.kill()
