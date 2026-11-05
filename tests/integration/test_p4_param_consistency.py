"""P4-01 第 26 轮：GUI → TaskSpec → Worker → 实际落盘的参数一致性回归。

第 25 轮复核发现的四类静默失效，本文件逐项固化：

1. **Warp 的源窗口裁剪被忽略**：表单在 Warp 模式下暴露像元裁剪，Worker 却
   不处理 srcWin/projWin → 用户填了窗口仍产出整图且状态 succeeded。
   现在 Warp 采用两步语义（先按源窗口裁剪，再重投影）。
2. **GUI 双线性实际执行最近邻**：GUI 发 `resampling`、Worker 读 `resample_alg`。
   现在两者共用同一份契约（canonical 名 `resampleAlg`）。
3. **dstNodata=0 被丢弃**：`or` 短路吞掉合法的零值。
   现在所有可选数值参数按「是否为 None」判断。
4. **非法参数被截断或静默回退**：小数窗口被 `int()` 截断、未知算法静默回退
   最近邻。现在一律返回 invalid_parameters。

关键：本文件**不只测参数采集辅助函数**，而是提交 GUI 真实发出的完整 TaskSpec，
由真实 GDAL worker 执行，再独立重开产出文件核对**实际像元与元数据**。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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
from spectra_workbench.processing.raster_params import ParameterError, normalize_raster_params
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
    out_dir = tmp_path_factory.mktemp("p401_c_src")
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


# --------------------------------------------------------------- 工具
def _run_task(
    qapp: QApplication,
    capability_id: str,
    parameters: dict,
    in_path: Path,
    out_path: Path,
    timeout: float = 60.0,
):
    """通过真实 QtWorkerClient 提交任务并返回终态结果。"""
    spec = TaskSpec(
        task_id=f"p401c-{capability_id.replace(':', '-')}",
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
                dataset_id="p401c-source",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p401c-worker", project_root=str(ROOT))
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


def _inspect(gdal_rt: dict, path: Path) -> dict:
    """独立重开产出文件，读取像元与元数据。"""
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open(r'{path}')\n"
        "assert ds is not None, '产出文件无法打开'\n"
        "b = ds.GetRasterBand(1)\n"
        "arr = b.ReadAsArray()\n"
        "mask = b.GetMaskBand().ReadAsArray()\n"
        "print(json.dumps({\n"
        "    'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "    'arr': arr.tolist(),\n"
        "    'mask_first': int(mask[0][0]),\n"
        "    'mask_flags': int(b.GetMaskFlags()),\n"
        "    'nodata': b.GetNoDataValue(),\n"
        "    'dtype': gdal.GetDataTypeName(b.DataType),\n"
        "    'gt': list(ds.GetGeoTransform()),\n"
        "    'GMF_NODATA': int(gdal.GMF_NODATA), 'GMF_ALPHA': int(gdal.GMF_ALPHA),\n"
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


def _form(qapp: QApplication, tmp_path: Path, capability_id: str):
    """构造一个加载了指定能力的表单，并接入任务提交信号。"""
    from spectra_workbench.registry.catalog import get_default_registry

    form = CapabilityFormWidget()
    form.set_project_root(tmp_path)
    cap = get_default_registry().get(capability_id)
    assert cap is not None, f"能力注册表中找不到 {capability_id}"
    form.load_capability(cap)
    emitted: list = []
    form.sig_task_submitted.connect(emitted.append)
    return form, emitted


# ------------------------------------------------- 1. Warp 的源窗口裁剪语义
def test_warp_applies_source_window_clip(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """Warp + srcWin：必须真正裁剪，产出尺寸等于窗口尺寸（不是整图）。"""
    out = tmp_path / "warp_clip.tif"
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
    # 像元值应对应源数据 (row 3..11, col 2..9)
    expected = [[(3 + r) * 16 + (2 + c) for c in range(8)] for r in range(9)]
    assert info["arr"] == expected, f"裁剪像元值不符：{info['arr']}"


def test_warp_source_window_and_output_bounds_conflict_rejected(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """源窗口（源像元/源地理）与 outputBounds（目标 CRS 范围）语义不同，不能同时给。"""
    out = tmp_path / "conflict.tif"
    result = _run_task(
        qapp,
        "gdal:warp",
        {
            "format": "GTiff",
            "dst_crs": "EPSG:4326",
            "srcWin": [2, 3, 8, 9],
            "outputBounds": [100.0, 29.0, 101.0, 30.0],
        },
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert not out.exists(), "参数冲突时不得产出文件"


def test_warp_src_win_and_proj_win_conflict_rejected(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """srcWin 与 projWin 都是源窗口语义，不能同时指定。"""
    out = tmp_path / "conflict2.tif"
    result = _run_task(
        qapp,
        "gdal:warp",
        {
            "format": "GTiff",
            "dst_crs": "EPSG:4326",
            "srcWin": [2, 3, 8, 9],
            "projWin": [100.0, 29.9, 100.01, 30.0],
        },
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"


# ------------------------------------------- 2. GUI 重采样参数落到实际像元
def test_gui_bilinear_reaches_worker_as_bilinear(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """GUI 选双线性：落盘结果必须等于显式 bilinear，且不等于 nearest。

    复现第 25 轮缺陷：GUI 发 `resampling`，Worker 读 `resample_alg`，
    双线性被静默执行成最近邻。
    """
    form, emitted = _form(qapp, tmp_path, "gdal:warp")
    try:
        form.txt_input.setText(str(source_raster))
        form.txt_output.setText(str(tmp_path / "gui_bilinear.tif"))
        form.cmb_dst_crs.setCurrentText("EPSG:4326")
        form.spin_xres.setValue(0.0015)
        form.spin_yres.setValue(0.0015)
        # 显式选择双线性（不依赖默认项）
        idx = form.cmb_resampling.findData("bilinear")
        assert idx >= 0, "下拉框中必须有 bilinear 选项"
        form.cmb_resampling.setCurrentIndex(idx)
        form._on_submit_clicked()
        assert emitted, "表单应发出任务"
        gui_params = dict(emitted[-1].parameters)
    finally:
        form.close()
        form.deleteLater()

    # GUI 发出的参数必须使用 canonical 名
    assert gui_params.get("resampleAlg") == "bilinear", f"GUI 应发 canonical 名：{gui_params}"
    assert "resampling" not in gui_params, "不应再使用与 Worker 不一致的旧字段名"

    gui_out = tmp_path / "gui_bilinear.tif"
    result = _run_task(qapp, "gdal:warp", gui_params, source_raster, gui_out)
    assert result.status is TaskStatus.succeeded, result.error
    gui_info = _inspect(gdal_rt, gui_out)

    near_out = tmp_path / "ref_nearest.tif"
    bilinear_out = tmp_path / "ref_bilinear.tif"
    assert (
        _run_task(
            qapp,
            "gdal:warp",
            {"format": "GTiff", "dst_crs": "EPSG:4326", "xRes": 0.0015, "yRes": 0.0015,
             "resample_alg": "nearest"},
            source_raster,
            near_out,
        ).status
        is TaskStatus.succeeded
    )
    assert (
        _run_task(
            qapp,
            "gdal:warp",
            {"format": "GTiff", "dst_crs": "EPSG:4326", "xRes": 0.0015, "yRes": 0.0015,
             "resample_alg": "bilinear"},
            source_raster,
            bilinear_out,
        ).status
        is TaskStatus.succeeded
    )
    near_info = _inspect(gdal_rt, near_out)
    bilinear_info = _inspect(gdal_rt, bilinear_out)

    # 前提：两种参考算法本身确有区别
    assert near_info["arr"] != bilinear_info["arr"], "用例前提失败：两种算法结果相同"
    # GUI 结果必须与双线性一致、与最近邻不同
    assert gui_info["arr"] == bilinear_info["arr"], "GUI 双线性未落到实际输出"
    assert gui_info["arr"] != near_info["arr"], "GUI 双线性被静默执行成最近邻"


def test_gui_default_resampling_is_bilinear(qapp, tmp_path) -> None:
    """GUI 默认重采样算法保持 bilinear（改造前的行为，不得顺带变更）。"""
    form, _ = _form(qapp, tmp_path, "gdal:warp")
    try:
        assert form.cmb_resampling.currentData() == "bilinear", (
            f"默认重采样算法应为 bilinear，实际 {form.cmb_resampling.currentData()}"
        )
    finally:
        form.close()
        form.deleteLater()


def test_gui_offers_only_supported_algorithms(qapp, tmp_path) -> None:
    """下拉框只提供契约声明的**实际支持**的算法（不得出现 Worker 不认的项）。"""
    from spectra_workbench.processing.raster_params import RESAMPLE_ALGORITHMS

    form, _ = _form(qapp, tmp_path, "gdal:warp")
    try:
        offered = {
            form.cmb_resampling.itemData(i) for i in range(form.cmb_resampling.count())
        }
        assert offered, "下拉框不得为空"
        assert offered <= set(RESAMPLE_ALGORITHMS), f"下拉框含未支持算法：{offered}"
    finally:
        form.close()
        form.deleteLater()


# ------------------------------------------------- 3. 合法零值
def test_zero_dst_nodata_is_honoured(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """dstNodata=0 是合法请求：落盘 NoData 必须是 0（此前被 or 丢弃，落到 -9999）。"""
    out = tmp_path / "zero_nodata.tif"
    result = _run_task(
        qapp,
        "gdal:warp",
        {"format": "GTiff", "dst_crs": "EPSG:4326", "dstNodata": 0},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["nodata"] == 0.0, f"落盘 NoData 应为 0，实际 {info['nodata']}"


def test_zero_nodata_differs_from_unspecified(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """请求零为 NoData 与未请求时语义不同：落盘 NoData 必须分别是 0 与源值。"""
    zero_out = tmp_path / "nd_zero.tif"
    default_out = tmp_path / "nd_default.tif"
    assert (
        _run_task(
            qapp,
            "gdal:translate",
            {"format": "GTiff", "noData": 0},
            source_raster,
            zero_out,
        ).status
        is TaskStatus.succeeded
    )
    assert (
        _run_task(
            qapp,
            "gdal:translate",
            {"format": "GTiff"},
            source_raster,
            default_out,
        ).status
        is TaskStatus.succeeded
    )
    zero_info = _inspect(gdal_rt, zero_out)
    default_info = _inspect(gdal_rt, default_out)
    assert zero_info["nodata"] == 0.0, f"请求 0 时应为 0，实际 {zero_info['nodata']}"
    assert default_info["nodata"] == pytest.approx(-9999.0), (
        f"未请求时应保留源 NoData，实际 {default_info['nodata']}"
    )
    # NoData 元数据与实际 mask 必须分别核对
    assert zero_info["mask_flags"] & zero_info["GMF_NODATA"], (
        f"掩膜标志应含 GMF_NODATA({zero_info['GMF_NODATA']})，实际 {zero_info['mask_flags']}"
    )


# --------------------------------------------- 4. 非法参数严格拒绝
def test_fractional_src_win_is_rejected(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """小数窗口必须报错，不得 int() 静默截断。"""
    out = tmp_path / "frac.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "srcWin": [1.9, 2.9, 3.8, 4.8]},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert not out.exists(), "非法参数时不得产出文件"


def test_unknown_resampling_algorithm_is_rejected(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """未知重采样算法必须报错，不得静默回退最近邻。"""
    out = tmp_path / "unknown_alg.tif"
    result = _run_task(
        qapp,
        "gdal:warp",
        {
            "format": "GTiff",
            "dst_crs": "EPSG:4326",
            "resample_alg": "review_unknown_algorithm",
            "width": 8,
            "height": 8,
        },
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert "review_unknown_algorithm" in result.error.get("message", "")
    assert not out.exists()


def test_unknown_parameter_name_is_rejected(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """拼错的参数名必须报错，避免「参数没生效但任务成功」。"""
    out = tmp_path / "typo.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "srcWn": [0, 0, 4, 4]},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert not out.exists()


def test_alias_conflict_is_rejected(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """同一参数的两个别名给出不同值时必须报错，不得无声择一。"""
    out = tmp_path / "conflict_alias.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {
            "format": "GTiff",
            "resample_alg": "bilinear",
            "resampling": "nearest",
            "xRes": 0.002,
        },
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert not out.exists()


# --------------------------------------- 契约模块自身的单元级行为
def test_normalize_rejects_unknown_algorithm() -> None:
    with pytest.raises(ParameterError):
        normalize_raster_params({"resample_alg": "not_an_algorithm"})


def test_normalize_keeps_zero_nodata() -> None:
    out = normalize_raster_params({"dstNodata": 0})
    assert out["dstNodata"] == 0.0


def test_normalize_accepts_known_aliases() -> None:
    assert normalize_raster_params({"resampling": "bilinear"})["resampleAlg"] == "bilinear"
    assert normalize_raster_params({"resample_alg": "near"})["resampleAlg"] == "nearest"


def test_normalize_rejects_conflicting_aliases() -> None:
    with pytest.raises(ParameterError):
        normalize_raster_params({"resample_alg": "bilinear", "resampling": "nearest"})


def test_normalize_rejects_fractional_integers() -> None:
    with pytest.raises(ParameterError):
        normalize_raster_params({"srcWin": [1.9, 2.9, 3.8, 4.8]})


def test_normalize_rejects_non_finite() -> None:
    with pytest.raises(ParameterError):
        normalize_raster_params({"xRes": float("inf")})
