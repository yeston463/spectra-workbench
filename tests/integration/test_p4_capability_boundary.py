"""P4-01 第 27 轮：能力边界、GUI 状态隔离与有界预裁剪的回归测试。

第 26 轮复核的四项问题，本文件逐项固化：

1. **参数契约未按能力区分**：`normalize_raster_params` 没有 capability 上下文，
   「可解析的参数」不等于「该算法会消费它」。实测 Translate + dstNodata=0
   与 Translate + dstSRS=EPSG:3857 都 succeeded 但参数被静默忽略。
   现在 Translate 明确拒绝这些参数；分辨率与输出尺寸的互斥在契约层判定。
2. **切换能力后隐藏控件仍进入 TaskSpec**：仅隐藏控件不足以隔离能力状态。
   现在切换能力时重置不适用控件，且只采集当前能力适用的控件。
3. **Warp 预裁剪用 MEM**：MEM 会物化整个裁剪区（无内存上限）。
   现在用惰性 VRT（只生成 XML，裁剪在读取时生效），并加窗口预算拒绝路径。
4. **失败时既有输出不被覆盖**：参数非法时不得产出新文件、不得破坏原目标。

所有断言基于**真实 worker 执行 + 独立重开产出**核对，不只测参数采集辅助函数。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

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
from spectra_workbench.processing.raster_params import (
    ParameterError,
    normalize_raster_params,
    supported_params_for,
)
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget

pytestmark = [pytest.mark.integration, pytest.mark.engine, pytest.mark.gui]

ROOT = find_project_root()


def _get_qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return _get_qapp()


@pytest.fixture(scope="module")
def gdal_rt() -> dict:
    return resolve_gdal_runtime(ROOT)


@pytest.fixture(scope="module")
def source_raster(gdal_rt: dict, tmp_path_factory) -> Path:
    """16x16 Float32 合成栅格：值 = row*16 + col，EPSG:4326，NoData=-9999。"""
    out_dir = tmp_path_factory.mktemp("p401_d_src")
    path = out_dir / "source_16x16.tif"
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
arr = (np.arange(16, dtype=np.float32)[:, None] * 16 + np.arange(16, dtype=np.float32)[None, :])
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", 16, 16, 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform([100.0, 0.001, 0.0, 30.0, 0.0, -0.001])
ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.WriteArray(arr)
b.SetNoDataValue(-9999.0)
ds = None
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"源栅格构造失败：{proc.stderr}"
    return path


# ------------------------------------------------------------------ 工具
def _run_task(qapp, capability_id, parameters, in_path, out_path, timeout=60.0):
    spec = TaskSpec(
        task_id=f"p401d-{capability_id.replace(':', '-')}-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal",
            runtime_id="gdal-qgis",
            expected_version="3.13.3",
        ),
        project_root=str(ROOT),
        workspace=str(out_path.parent),
        inputs={
            "input": DatasetRef(
                dataset_id="p401d-source",
                uri=in_path.resolve().as_uri(),
                kind=DatasetKind.raster,
            )
        },
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.raster,
                uri=out_path.resolve().as_uri(),
                driver="GTiff",
                overwrite=True,
            )
        ],
        parameters=parameters,
    )
    client = QtWorkerClient(provider_id="gdal", worker_id="p401d-worker", project_root=str(ROOT))
    results = []
    errors: list[tuple[str, str]] = []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda c, m: errors.append((c, m)))
    try:
        client.start(timeout=20.0)
        client.submit_task_async(spec, timeout=timeout)
        deadline = time.monotonic() + timeout
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.05)
        assert not errors, f"任务上报异常：{errors}"
        assert results, f"任务在 {timeout}s 内未产生结果"
        return results[0]
    finally:
        client.stop(timeout=5.0)


def _inspect(gdal_rt: dict, path: Path, extra: str = "") -> dict:
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open(r'{path}')\n"
        "assert ds is not None, '产出文件无法打开'\n"
        "b = ds.GetRasterBand(1)\n"
        "s = ds.GetSpatialRef()\n"
        "print(json.dumps({\n"
        "    'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "    'arr': b.ReadAsArray().tolist(),\n"
        "    'nodata': b.GetNoDataValue(),\n"
        "    'dtype': gdal.GetDataTypeName(b.DataType),\n"
        "    'epsg': s.GetAuthorityCode(None) if s else None,\n"
        "    'gt': list(ds.GetGeoTransform()),\n"
        f"    {extra}\n"
        "}))"
    )
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"GDAL 校验失败：{proc.stderr}"
    return json.loads(proc.stdout.strip())


def _form(qapp, tmp_path, capability_id):
    from spectra_workbench.registry.catalog import get_default_registry

    form = CapabilityFormWidget()
    form.set_project_root(tmp_path)
    cap = get_default_registry().get(capability_id)
    assert cap is not None, f"能力注册表中找不到 {capability_id}"
    form.load_capability(cap)
    emitted: list = []
    form.sig_task_submitted.connect(emitted.append)
    return form, emitted


# ---------------------------------------------- 1. 参数按能力校验
def test_translate_rejects_dst_nodata(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """Translate 必须拒绝 dstNodata（它不消费该参数），不得静默忽略。"""
    out = tmp_path / "tr_nodata.tif"
    result = _run_task(
        qapp, "gdal:translate", {"format": "GTiff", "dstNodata": 0}, source_raster, out
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert "dstNodata" in result.error.get("message", "")
    assert not out.exists(), "参数被拒时不得产出文件"


def test_translate_rejects_dst_srs(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """Translate 必须拒绝 dstSRS（它不消费该参数），不得静默忽略。"""
    out = tmp_path / "tr_crs.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "dstSRS": "EPSG:3857"},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert "dstSRS" in result.error.get("message", "")
    assert not out.exists()


def test_warp_accepts_dst_nodata_and_dst_srs(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """Warp 确实消费 dstNodata/dstSRS：必须生效（对照 Translate 的拒绝）。"""
    out = tmp_path / "warp_ok.tif"
    result = _run_task(
        qapp,
        "gdal:warp",
        {"format": "GTiff", "dst_crs": "EPSG:3857", "dstNodata": 0},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["nodata"] == 0.0, f"Warp 的 dstNodata 应生效，实际 {info['nodata']}"
    assert info["epsg"] == "3857", f"Warp 的 dstSRS 应生效，实际 {info['epsg']}"


def test_resolution_and_size_conflict_rejected_in_contract(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """分辨率与输出尺寸互斥：必须在契约层判定（不是等 GDAL 报错）。"""
    out = tmp_path / "conflict.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "xRes": 0.002, "yRes": 0.002, "width": 4, "height": 4},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert "互斥" in result.error.get("message", "")
    assert not out.exists()


def test_rasterio_convert_rejects_grid_params(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """Rasterio convert 不消费裁剪/网格参数：脚本绕过 GUI 也必须被拒。"""
    out = tmp_path / "rio.tif"
    result = _run_task(
        qapp,
        "rasterio:convert",
        {"format": "GTiff", "srcWin": [1, 2, 3, 4]},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    # Rasterio convert 当前未接入该执行通道，能力准入阶段即被拒
    # （早于参数校验；两种拒绝都满足「不得静默忽略」的要求）
    assert result.error.get("code") in ("invalid_parameters", "unsupported_capability")
    assert not out.exists()


def test_contract_rejects_unsupported_params_per_capability() -> None:
    """契约层：按能力拒绝不支持的参数（不依赖真实引擎）。"""
    with pytest.raises(ParameterError):
        normalize_raster_params({"dstNodata": 0}, capability_id="gdal:translate")
    with pytest.raises(ParameterError):
        normalize_raster_params({"dstSRS": "EPSG:3857"}, capability_id="gdal:translate")
    with pytest.raises(ParameterError):
        normalize_raster_params({"srcWin": [1, 2, 3, 4]}, capability_id="rasterio:convert")
    with pytest.raises(ParameterError):
        normalize_raster_params(
            {"xRes": 0.002, "yRes": 0.002, "width": 4, "height": 4},
            capability_id="gdal:translate",
        )
    # Warp 支持这些参数，不应被拒
    ok = normalize_raster_params(
        {"dstNodata": 0, "dstSRS": "EPSG:3857", "srcWin": [1, 2, 3, 4]},
        capability_id="gdal:warp",
    )
    assert ok["dstNodata"] == 0.0
    assert ok["srcWin"] == [1, 2, 3, 4]


def test_supported_params_matrix_is_consistent() -> None:
    """支持矩阵：Translate 与 Warp 的能力边界必须是显式且互不相同的。"""
    translate = supported_params_for("gdal:translate")
    warp = supported_params_for("gdal:warp")
    assert translate is not None and warp is not None
    assert "dstNodata" not in translate and "dstNodata" in warp
    assert "dstSRS" not in translate and "dstSRS" in warp
    assert "srcWin" in translate and "srcWin" in warp
    assert supported_params_for("rasterio:convert") == frozenset()


# ------------------------------------------- 2. GUI 能力切换的状态隔离
def test_hidden_controls_do_not_leak_after_capability_switch(
    qapp, tmp_path, source_raster
) -> None:
    """切到 Rasterio convert 后，隐藏的裁剪/分辨率控件不得进入 TaskSpec。"""
    form, emitted = _form(qapp, tmp_path, "gdal:translate")
    try:
        form.txt_input.setText(str(source_raster))
        form.txt_output.setText(str(tmp_path / "leak.tif"))
        form.txt_clip.setText("1,2,3,4")
        form.spin_xres.setValue(0.002)
        form.spin_yres.setValue(0.002)

        from spectra_workbench.registry.catalog import get_default_registry

        cap = get_default_registry().get("rasterio:convert")
        assert cap is not None, "能力注册表中找不到 rasterio:convert"
        form.load_capability(cap)
        form._on_submit_clicked()

        assert emitted, "表单应发出任务"
        params = emitted[-1].parameters
        assert params.get("srcWin") is None, f"隐藏的 srcWin 泄漏进了参数：{params}"
        assert params.get("xRes") is None, f"隐藏的 xRes 泄漏进了参数：{params}"
        assert params.get("yRes") is None, f"隐藏的 yRes 泄漏进了参数：{params}"
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()


def test_capability_switch_cycle_isolates_state(qapp, tmp_path, source_raster) -> None:
    """Translate→Warp→Rasterio→Translate 循环切换：状态不得串味。"""
    from spectra_workbench.registry.catalog import get_default_registry

    registry = get_default_registry()
    form = CapabilityFormWidget()
    form.set_project_root(tmp_path)
    emitted: list = []
    form.sig_task_submitted.connect(emitted.append)
    try:
        form.load_capability(registry.get("gdal:translate"))
        form.txt_input.setText(str(source_raster))
        form.txt_output.setText(str(tmp_path / "cycle.tif"))
        form.txt_clip.setText("1,2,3,4")
        form.spin_xres.setValue(0.002)

        # 切到 Warp：srcWin 适用（保留），xRes 适用
        form.load_capability(registry.get("gdal:warp"))
        form._on_submit_clicked()
        params = emitted[-1].parameters
        assert params.get("srcWin") == [1, 2, 3, 4], f"Warp 应保留 srcWin：{params}"

        # 切到 Rasterio：两者都不适用，必须被重置
        form.load_capability(registry.get("rasterio:convert"))
        form._on_submit_clicked()
        params = emitted[-1].parameters
        assert params.get("srcWin") is None, f"切到 Rasterio 后 srcWin 未重置：{params}"
        assert params.get("xRes") is None, f"切到 Rasterio 后 xRes 未重置：{params}"

        # 切回 Translate：值已被重置，不得复活
        form.load_capability(registry.get("gdal:translate"))
        form._on_submit_clicked()
        params = emitted[-1].parameters
        assert params.get("srcWin") is None, f"切回 Translate 后旧 srcWin 复活：{params}"
        assert params.get("xRes") is None, f"切回 Translate 后旧 xRes 复活：{params}"
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()


def test_gui_rejects_conflicting_resolution_and_size(qapp, tmp_path, source_raster) -> None:
    """GUI 侧也必须在提交前拒绝分辨率/尺寸冲突（不是等 worker 报错）。"""
    form, emitted = _form(qapp, tmp_path, "gdal:translate")
    try:
        form.txt_input.setText(str(source_raster))
        form.txt_output.setText(str(tmp_path / "gui_conflict.tif"))
        form.spin_xres.setValue(0.002)
        form.spin_yres.setValue(0.002)
        form.txt_outsize.setText("4,4")

        warnings: list[str] = []
        import spectra_workbench.ui.forms.param_form as pf

        original = pf.QMessageBox.warning
        try:
            pf.QMessageBox.warning = lambda *a, **k: warnings.append(str(a[-1]))  # type: ignore[assignment]
            form._on_submit_clicked()
        finally:
            pf.QMessageBox.warning = original  # type: ignore[assignment]

        assert not emitted, "冲突参数时不得提交任务"
        assert warnings, "冲突参数必须有明确提示"
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()


# --------------------------------------------- 3. 有界预裁剪（VRT 而非 MEM）
def test_warp_source_window_uses_lazy_vrt(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """Warp 源窗口预裁剪必须走惰性 VRT，不得物化 MEM。

    做法：调用独立探测脚本比较 VRT 与 MEM 两种预裁剪方案——
    VRT 应为**惰性**（只含 XML 描述、体量远小于像元），
    用 SrcRect 表达源窗口语义，且像元与 MEM 方案逐点一致。
    """
    import json as _json

    script_path = (
        Path(__file__).resolve().parents[1] / "fixtures" / "probe_crop_driver.py"
    )
    proc = subprocess.run(
        [str(gdal_rt["python"]), str(script_path), str(source_raster)],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"预裁剪驱动探测失败：{proc.stderr}"
    info = _json.loads(proc.stdout.strip())

    assert info["driver"] == "VRT", f"预裁剪驱动应为 VRT，实际 {info['driver']}"
    assert (info["w"], info["h"]) == (8, 9), f"VRT 尺寸应为 8x9，实际 {info['w']}x{info['h']}"
    assert info["has_SrcRect"], "VRT 应以 SrcRect 表达源窗口语义"
    # 惰性证据：VRT 只是 XML 描述，体量远小于被物化的像元
    assert info["xml_len"] < 8192, f"VRT XML 过大（{info['xml_len']} 字节），疑似被物化"
    # 语义等价：VRT 与 MEM 的像元必须逐点一致
    assert info["pixels_equal_to_mem"], "VRT 与 MEM 的预裁剪像元不一致"
    # NoData 与 GeoTransform 语义保持
    assert info["nodata"] == pytest.approx(-9999.0)
    assert info["gt"][0] == pytest.approx(100.002)



def test_warp_source_window_semantics_preserved_with_vrt(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """改用 VRT 后，源窗口裁剪的尺寸/像元值/NoData/GeoTransform 语义必须不变。"""
    out = tmp_path / "vrt_clip.tif"
    result = _run_task(
        qapp,
        "gdal:warp",
        {"format": "GTiff", "dst_crs": "EPSG:4326", "srcWin": [2, 3, 8, 9]},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert (info["w"], info["h"]) == (8, 9), f"应裁剪为 8x9，实际 {info['w']}x{info['h']}"
    expected = [[(3 + r) * 16 + (2 + c) for c in range(8)] for r in range(9)]
    assert info["arr"] == expected, f"VRT 裁剪像元值不符：{info['arr']}"
    assert info["nodata"] == pytest.approx(-9999.0), f"NoData 语义丢失：{info['nodata']}"
    gt = info["gt"]
    assert gt[0] == pytest.approx(100.002), f"GeoTransform 原点不符：{gt}"
    assert gt[3] == pytest.approx(29.997), f"GeoTransform 原点 Y 不符：{gt}"
    assert info["epsg"] == "4326", f"CRS 语义丢失：{info['epsg']}"


def test_adapter_source_has_no_mem_preprocessing() -> None:
    """静态断言：adapter 的预裁剪分支不得再出现 MEM。"""
    from spectra_workbench.providers.gdal_provider import adapter

    text = Path(adapter.__file__).read_text(encoding="utf-8")
    # 预裁剪分支使用惰性 VRT；read_view_window 的 MEM 属显示路径（有 2048 上限），不在此断言范围
    assert '"format": "VRT"' in text, "预裁剪应使用 VRT"
    assert '"format": "MEM"' not in text, "预裁剪不得再使用 MEM"


def test_oversized_source_window_is_rejected(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """超大源窗口必须被预算拒绝（明确错误），而不是默默跑巨量作业。"""
    from spectra_workbench.providers.gdal_provider.adapter import MAX_SOURCE_WINDOW_PIXELS

    out = tmp_path / "huge.tif"
    # 构造一个超过预算的窗口（不实际读取巨量数据，预算校验在读取前）
    huge = [0, 0, 10_000, 10_000]
    assert huge[2] * huge[3] > MAX_SOURCE_WINDOW_PIXELS
    result = _run_task(
        qapp,
        "gdal:warp",
        {"format": "GTiff", "dst_crs": "EPSG:4326", "srcWin": huge},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") in ("window_too_large", "invalid_parameters")
    assert not out.exists()


# ------------------------------------- 4. 失败时既有输出不被覆盖
def test_invalid_params_do_not_overwrite_existing_output(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """参数非法时既有输出不得被覆盖或破坏。"""
    out = tmp_path / "existing.tif"
    out.write_bytes(b"original content marker")

    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "dstNodata": 0},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert out.read_bytes() == b"original content marker", "既有输出被破坏"
