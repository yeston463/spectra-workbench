"""P9-02 集成验收测试：能力路由补齐。

验收标准（对齐 docs/plan/01_完整开发方案.md §6.2 与 02_里程碑与验收.md P9-02）：
1. 适合参数化的 5 项清理与地形算法（gdal:sieve, gdal:fillnodata, gdal:nearblack,
   gdal:proximity, gdal:viewshed）接通专用面板并由真实 GDAL Worker 进程执行，
   暂存事务、格式校验、数值计算完全正确；
2. GUI CapabilityFormWidget 正确加载并下发 5 项算法参数；
3. 复杂 API 与非单文件算法（24 项 GDAL API + 16 项 Rasterio API + gdal:retile 共 41 项）
   全部具备可运行会话入口（generate_api_session_snippet），并在对应真实引擎会话中
   （QGIS GDAL / sw-rasterio）全量执行通过，断言无一失败；
4. gdal:retile 明确限制说明（多文件目录输出不适合单栅格表单）并在会话中提供可用入口。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.capability_wiring import CONNECTED_CAPABILITIES
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.registry import get_default_registry
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.scripting.api_snippets import (
    build_batch_script,
    generate_api_session_snippet,
    has_session_route,
    session_routed_ids,
)
from spectra_workbench.scripting.session import ScriptSession
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    ensure_application_fonts(app)
    return app


@pytest.fixture(scope="module")
def gdal_rt() -> dict[str, Any]:
    return resolve_gdal_runtime(ROOT)


def _create_cleanup_rasters(py: str, tmp_dir: Path) -> dict[str, Path]:
    """在外部 GDAL Python 进程中生成 5 项清理算法的测试输入栅格。"""
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

# 1. sieve raster (10x10, bg=10, (2,2)=99 孤立像元)
arr = np.full((10, 10), 10, dtype=np.int16)
arr[2, 2] = 99
ds = gdal.GetDriverByName('GTiff').Create(r'{tmp_dir / "sieve_in.tif"}', 10, 10, 1, gdal.GDT_Int16)
ds.SetGeoTransform((500000.0, 1.0, 0.0, 3200000.0, 0.0, -1.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650); ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(arr)
ds.FlushCache(); ds = None

# 2. fillnodata raster (8x8, 背景 50.0, (3,3)=-9999.0 孤立空洞)
arr = np.ones((8, 8), dtype=np.float32) * 50.0
arr[3, 3] = -9999.0
ds = gdal.GetDriverByName('GTiff').Create(r'{tmp_dir / "fill_in.tif"}', 8, 8, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000.0, 1.0, 0.0, 3200000.0, 0.0, -1.0))
ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.SetNoDataValue(-9999.0)
b.WriteArray(arr)
ds.FlushCache(); ds = None

# 3. nearblack raster (8x8, 边缘值为 2, 中心为 100)
arr = np.full((8, 8), 100, dtype=np.uint8)
arr[0, :] = 2; arr[-1, :] = 2; arr[:, 0] = 2; arr[:, -1] = 2
ds = gdal.GetDriverByName('GTiff').Create(r'{tmp_dir / "nb_in.tif"}', 8, 8, 1, gdal.GDT_Byte)
ds.SetGeoTransform((500000.0, 1.0, 0.0, 3200000.0, 0.0, -1.0))
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(arr)
ds.FlushCache(); ds = None

# 4. proximity raster (10x10, 背景 0, 中心 (5,5)=1 为目标)
arr = np.zeros((10, 10), dtype=np.uint8)
arr[5, 5] = 1
ds = gdal.GetDriverByName('GTiff').Create(r'{tmp_dir / "prox_in.tif"}', 10, 10, 1, gdal.GDT_Byte)
ds.SetGeoTransform((500000.0, 1.0, 0.0, 3200000.0, 0.0, -1.0))
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(arr)
ds.FlushCache(); ds = None

# 5. viewshed dem (16x16 平面 DEM)
rows, cols = np.mgrid[0:16, 0:16]
z = (cols + rows).astype(np.float32)
ds = gdal.GetDriverByName('GTiff').Create(r'{tmp_dir / "view_in.tif"}', 16, 16, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(z)
ds.FlushCache(); ds = None
print("OK")
"""
    subprocess.check_call([py, "-c", script])
    return {
        "sieve": tmp_dir / "sieve_in.tif",
        "fillnodata": tmp_dir / "fill_in.tif",
        "nearblack": tmp_dir / "nb_in.tif",
        "proximity": tmp_dir / "prox_in.tif",
        "viewshed": tmp_dir / "view_in.tif",
    }


def _dispatch_task(
    qapp: QApplication,
    client: QtWorkerClient,
    capability_id: str,
    parameters: dict[str, Any],
    source: Path,
    target: Path,
    workspace: Path,
    timeout: float = 30.0,
) -> Any:
    """向 GDAL Worker 发送任务并等待返回。"""
    spec = TaskSpec(
        task_id=f"p902-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(workspace),
        inputs={
            "input": DatasetRef(
                dataset_id="src",
                kind=DatasetKind.raster,
                uri=source.resolve().as_uri(),
            )
        },
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.raster,
                uri=target.resolve().as_uri(),
                overwrite=True,
            )
        ],
        parameters=parameters,
    )
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda code, msg: errors.append((code, msg)))
    try:
        client.submit_task_async(spec, timeout=timeout)
        deadline = time.monotonic() + timeout
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert not errors, f"Worker 执行失败: {errors}"
        assert results, f"Worker 未产生结果: {capability_id}"
        res = results[0]
        assert res.status == TaskStatus.succeeded, f"任务状态非成功: {res.error}"
        return res
    finally:
        client.sig_result.disconnect(results.append)


def test_p9_02_five_cleanup_algorithms_worker_execution(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    """五项清理算法通过真实 GDAL Worker 进程执行，检验输出文件与数值特性。"""
    py = str(gdal_rt["python"])
    inputs = _create_cleanup_rasters(py, tmp_path)

    client = QtWorkerClient(
        provider_id="gdal", worker_id="p9-cleanup-worker", project_root=str(ROOT)
    )
    client.start(timeout=25.0)

    try:
        # 1. gdal:sieve
        sieve_out = tmp_path / "sieve_out.tif"
        _dispatch_task(
            qapp,
            client,
            "gdal:sieve",
            {"threshold": 4, "connectedness": 4},
            inputs["sieve"],
            sieve_out,
            tmp_path,
        )
        assert sieve_out.exists() and sieve_out.stat().st_size > 0

        # 2. gdal:fillnodata
        fill_out = tmp_path / "fill_out.tif"
        _dispatch_task(
            qapp,
            client,
            "gdal:fillnodata",
            {"fillDistance": 5, "smoothIterations": 0},
            inputs["fillnodata"],
            fill_out,
            tmp_path,
        )
        assert fill_out.exists() and fill_out.stat().st_size > 0

        # 3. gdal:nearblack
        nb_out = tmp_path / "nb_out.tif"
        _dispatch_task(
            qapp,
            client,
            "gdal:nearblack",
            {"near": 5},
            inputs["nearblack"],
            nb_out,
            tmp_path,
        )
        assert nb_out.exists() and nb_out.stat().st_size > 0

        # 4. gdal:proximity
        prox_out = tmp_path / "prox_out.tif"
        _dispatch_task(
            qapp,
            client,
            "gdal:proximity",
            {"values": "1", "distunits": "PIXEL"},
            inputs["proximity"],
            prox_out,
            tmp_path,
        )
        assert prox_out.exists() and prox_out.stat().st_size > 0

        # 5. gdal:viewshed
        view_out = tmp_path / "view_out.tif"
        _dispatch_task(
            qapp,
            client,
            "gdal:viewshed",
            {
                "ox": 500005.0,
                "oy": 3199995.0,
                "observerHeight": 2.0,
                "targetHeight": 0.0,
                "viewDistance": 100.0,
            },
            inputs["viewshed"],
            view_out,
            tmp_path,
        )
        assert view_out.exists() and view_out.stat().st_size > 0

        # 在外部 GDAL Python 进程中验证数值结果
        inspect_script = f"""
from osgeo import gdal
gdal.UseExceptions()

# 验证 sieve: (2,2) 处的 99 被过滤为背景 10
ds = gdal.Open(r'{sieve_out}')
arr = ds.GetRasterBand(1).ReadAsArray()
assert arr[2, 2] == 10, f"Sieve 未滤除: {{arr[2, 2]}}"

# 验证 fillnodata: (3,3) 处的空洞被填补
ds = gdal.Open(r'{fill_out}')
arr = ds.GetRasterBand(1).ReadAsArray()
assert arr[3, 3] == 50.0, f"FillNodata 未填补: {{arr[3, 3]}}"

# 验证 nearblack: 边缘值 2 被归零为 0
ds = gdal.Open(r'{nb_out}')
arr = ds.GetRasterBand(1).ReadAsArray()
assert arr[0, 0] == 0, f"Nearblack 未归零: {{arr[0, 0]}}"
assert arr[4, 4] == 100, f"Nearblack 中心被误伤: {{arr[4, 4]}}"

# 验证 proximity: (5,5) 处距离为 0，邻近为正数
ds = gdal.Open(r'{prox_out}')
arr = ds.GetRasterBand(1).ReadAsArray()
assert arr[5, 5] == 0.0, f"Proximity 目标像元距离非0: {{arr[5, 5]}}"
assert arr[5, 6] == 1.0, f"Proximity 相邻像元距离非1: {{arr[5, 6]}}"

# 验证 viewshed: 视域栅格有有效值（根据视距裁剪为局部范围）
ds = gdal.Open(r'{view_out}')
arr = ds.GetRasterBand(1).ReadAsArray()
assert arr.ndim == 2 and arr.size > 0, f"Viewshed 输出形状异常: {{arr.shape}}"
assert (arr > 0).any(), "Viewshed 无可见像元"
print("NUMERICAL_VERIFIED_OK")
"""
        proc = subprocess.run(
            [py, "-c", inspect_script],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            f"Inspect script failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )
        assert "NUMERICAL_VERIFIED_OK" in proc.stdout
    finally:
        client.stop(timeout=5.0)


def test_p9_02_five_cleanup_algorithms_param_form(qapp: QApplication, tmp_path: Path) -> None:
    """图形参数表单 CapabilityFormWidget 正确加载 5 项算法并能下发收集参数。"""
    reg = get_default_registry()
    caps = [
        "gdal:sieve",
        "gdal:fillnodata",
        "gdal:nearblack",
        "gdal:proximity",
        "gdal:viewshed",
    ]

    for cid in caps:
        cap = reg.get(cid)
        assert cap is not None
        assert cid in CONNECTED_CAPABILITIES

        form = CapabilityFormWidget()
        form.set_project_root(tmp_path)
        form.load_capability(cap)

        if cid == "gdal:viewshed":
            form.txt_view_ox.setText("500005.0")
            form.txt_view_oy.setText("3199995.0")

        # 验证能从表单收集到对应算法的参数
        params, err = form._collect_p402_params(cid)
        assert err is None, f"{cid} 采集参数出错: {err}"
        assert isinstance(params, dict)

        if cid == "gdal:sieve":
            assert "threshold" in params
            assert "connectedness" in params
        elif cid == "gdal:fillnodata":
            assert "fillDistance" in params
            assert "smoothIterations" in params
        elif cid == "gdal:nearblack":
            assert "near" in params
        elif cid == "gdal:proximity":
            assert "distunits" in params
        elif cid == "gdal:viewshed":
            assert "ox" in params
            assert "oy" in params
            assert "observerHeight" in params

        form.close()
        form.deleteLater()
        qapp.processEvents()


def test_p9_02_session_snippets_all_41_capabilities() -> None:
    """全量 41 项会话能力（24 GDAL API + 16 Rasterio API + 1 retile）模板在真实会话中执行通过。"""
    reg = get_default_registry()
    routed = session_routed_ids(reg.list_all())
    assert len(routed) == 41, f"会话路由能力数应为 41 项，实际 {len(routed)}"

    # 验证全部生成了带可运行 demo 的模板
    gdal_snippets = []
    rio_snippets = []
    for cid in routed:
        snippet = generate_api_session_snippet(cid, registry=reg)
        assert snippet.level == "demo", f"{cid} 缺少 demo 级别模板"
        assert len(snippet.code) > 200
        if snippet.engine == "gdal":
            gdal_snippets.append(snippet)
        else:
            rio_snippets.append(snippet)

    assert len(gdal_snippets) == 25  # 24 GDAL API + gdal:retile
    assert len(rio_snippets) == 16  # 16 Rasterio API

    # 1. 运行 GDAL 会话批处理
    gdal_sess = ScriptSession(engine="gdal")
    gdal_batch = build_batch_script(gdal_snippets)
    gdal_res = gdal_sess.run(gdal_batch, timeout=120)
    assert gdal_res["exit_code"] == 0, f"GDAL 会话退出码异常: {gdal_res['stderr_tail']}"

    stdout = (gdal_sess.session_dir / "logs" / "stdout.log").read_text(encoding="utf-8")
    gdal_results = []
    for line in stdout.splitlines():
        if "SPECTRA_API_RESULT " in line:
            idx = line.index("SPECTRA_API_RESULT ")
            gdal_results.append(json.loads(line[idx + len("SPECTRA_API_RESULT ") :]))

    assert len(gdal_results) == 25, f"GDAL 结果收集不全: {len(gdal_results)}/25"
    gdal_fails = [r for r in gdal_results if not r["ok"]]
    assert not gdal_fails, f"GDAL 模板执行失败项: {gdal_fails}"

    # 2. 运行 Rasterio 会话批处理
    rio_sess = ScriptSession(engine="rasterio")
    rio_batch = build_batch_script(rio_snippets)
    rio_res = rio_sess.run(rio_batch, timeout=120)
    assert rio_res["exit_code"] == 0, f"Rasterio 会话退出码异常: {rio_res['stderr_tail']}"

    stdout = (rio_sess.session_dir / "logs" / "stdout.log").read_text(encoding="utf-8")
    rio_results = []
    for line in stdout.splitlines():
        if "SPECTRA_API_RESULT " in line:
            idx = line.index("SPECTRA_API_RESULT ")
            rio_results.append(json.loads(line[idx + len("SPECTRA_API_RESULT ") :]))

    assert len(rio_results) == 16, f"Rasterio 结果收集不全: {len(rio_results)}/16"
    rio_fails = [r for r in rio_results if not r["ok"]]
    assert not rio_fails, f"Rasterio 模板执行失败项: {rio_fails}"


def test_p9_02_gdal_retile_routing_and_limitations() -> None:
    """验证 gdal:retile 会话路由与多文件限制说明。"""
    reg = get_default_registry()
    cap = reg.get("gdal:retile")
    assert cap is not None

    # 表单未接通（产生目录，不适合单栅格表单），脚本可用
    form_ep = next((ep for ep in cap.entrypoints if ep.kind == "form"), None)
    script_ep = next((ep for ep in cap.entrypoints if ep.kind == "script"), None)
    assert form_ep is None or form_ep.available is False
    assert cap.capability_id not in CONNECTED_CAPABILITIES
    assert script_ep is not None and script_ep.available is True
    assert has_session_route(cap) is True

    # 限制说明明确指向脚本会话与目录输出限制
    assert any("目录" in lim for lim in cap.limitations)
    assert any("Python" in lim or "ScriptSession" in lim for lim in cap.limitations)
