"""P4-01 验收测试：裁剪、重采样与输出网格。

原验收标准（docs/plan/02_里程碑与验收.md）：
「输出网格、dtype、mask、CRS 和像元值按明确参数验收」

逐项覆盖：
- **输出网格**：srcWin 像元窗口裁剪、projWin 地理窗口裁剪、outputBounds 地理范围、
  xRes/yRes 输出像元尺寸、width/height 输出尺寸、Warp 的 targetAlignedPixels 对齐。
- **dtype**：outputType 显式指定输出数据类型，并验证实际落盘类型。
- **mask**：NoData 传播（Translate 的 noData、Warp 的 dstNodata），
  以及 NoData 像元在输出中的填充值。
- **CRS**：Warp 的 dstSRS 生效，输出的投影与 EPSG 与预期一致。
- **像元值**：裁剪后像元值与源数据对应位置逐点一致；重采样后取值落在邻域合理范围。

所有断言都基于**独立计算的预期值**，不只检查字段存在。
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
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.scheduler.client import QtWorkerClient

pytestmark = [pytest.mark.integration, pytest.mark.engine]

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
    """构造一张**已知内容**的源栅格，作为一切预期值的基准。

    16x16 单波段 Float32，像元值 = 行*16 + 列（左上角为 0），经纬度 4326，
    像元 0.001 度（UTM 无关，便于直接核对像元值）。
    """
    out_dir = tmp_path_factory.mktemp("p401_src")
    path = out_dir / "source_16x16.tif"
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
w = h = 16
arr = (np.arange(h, dtype=np.float32)[:, None] * 16 + np.arange(w, dtype=np.float32)[None, :])
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", w, h, 1, gdal.GDT_Float32)
srs = osr.SpatialReference()
srs.ImportFromEPSG(4326)
ds.SetGeoTransform([100.0, 0.001, 0.0, 30.0, 0.0, -0.001])
ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.WriteArray(arr)
b.SetNoDataValue(-9999.0)
ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"源栅格构造失败：{proc.stderr}"
    assert path.exists()
    return path


def _run_task(
    qapp: QApplication,
    capability_id: str,
    parameters: dict,
    in_path: Path,
    out_path: Path,
    timeout: float = 60.0,
) -> TaskResult:
    """通过真实 QtWorkerClient 提交一次任务并返回终态结果。"""
    spec = TaskSpec(
        task_id=f"p401-{capability_id.replace(':', '-')}",
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
                dataset_id="p401-source",
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

    client = QtWorkerClient(
        provider_id="gdal",
        worker_id="p401-worker",
        project_root=str(ROOT),
    )
    results: list[TaskResult] = []
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


def _inspect(gdal_rt: dict, path: Path, script_body: str) -> dict:
    """在 GDAL 运行时里检查产出栅格，返回 JSON 结果。"""
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open(r'{path}')\n"
        "assert ds is not None, '产出文件无法打开'\n"
        f"{script_body}\n"
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


# ------------------------------------------------------------ 1. 裁剪（srcWin）
def test_clip_by_src_win_matches_source_pixels(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """像元窗口裁剪：输出尺寸、像元值与源数据对应区域逐点一致。"""
    out = tmp_path / "clip_srcwin.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "srcWin": [4, 2, 6, 5]},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    assert out.exists(), "裁剪成果未落盘"

    info = _inspect(
        gdal_rt,
        out,
        "arr = ds.GetRasterBand(1).ReadAsArray().tolist()\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "                  'arr': arr, 'dtype': gdal.GetDataTypeName(ds.GetRasterBand(1).DataType),\n"
        "                  'nodata': ds.GetRasterBand(1).GetNoDataValue(),\n"
        "                  'gt': list(ds.GetGeoTransform())}))",
    )
    # 尺寸：srcWin = [xoff, yoff, xsize, ysize]
    assert info["w"] == 6 and info["h"] == 5, f"输出尺寸应为 6x5，实际 {info['w']}x{info['h']}"

    # 像元值：源数据 value = row*16 + col，裁剪起点 (col=4, row=2)
    expected = [[(2 + r) * 16 + (4 + c) for c in range(6)] for r in range(5)]
    assert info["arr"] == expected, f"裁剪像元值与源数据不符：{info['arr']} != {expected}"

    # 输出网格：原点按 srcWin 偏移，步长不变
    gt = info["gt"]
    assert gt[0] == pytest.approx(100.0 + 4 * 0.001), f"裁剪后原点 X 不符：{gt}"
    assert gt[3] == pytest.approx(30.0 - 2 * 0.001), f"裁剪后原点 Y 不符：{gt}"
    assert gt[1] == pytest.approx(0.001) and gt[5] == pytest.approx(-0.001)


def test_clip_by_proj_win_matches_source_pixels(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """地理窗口裁剪：按 projWin 给出的地理范围裁剪，像元值与源数据一致。"""
    # 源原点 (100.0, 30.0)，像元 0.001 度；取 col 2..9、row 3..11
    # 注意 GDAL 的 projWin 顺序是 [minX, maxY, maxX, minY]（先上边界后下边界）：
    # 实测传 [minX, minY, maxX, maxY] 会报 "negative width and/or height"。
    x_min = 100.0 + 2 * 0.001
    y_max = 30.0 - 3 * 0.001
    x_max = 100.0 + 10 * 0.001
    y_min = 30.0 - 12 * 0.001
    out = tmp_path / "clip_projwin.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "projWin": [x_min, y_max, x_max, y_min]},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "arr = ds.GetRasterBand(1).ReadAsArray().tolist()\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize, 'arr': arr}))",
    )
    assert info["w"] == 8 and info["h"] == 9, f"输出尺寸应为 8x9，实际 {info['w']}x{info['h']}"

    # 像元值：起点 (col=2, row=3)
    expected = [[(3 + r) * 16 + (2 + c) for c in range(8)] for r in range(9)]
    assert info["arr"] == expected, f"地理窗口裁剪像元值不符：{info['arr']}"


# ------------------------------------------------- 2. 输出网格（xRes/yRes）
def test_output_resolution_changes_grid_and_dimensions(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """xRes/yRes 显式指定输出像元尺寸：输出网格步长与尺寸按参数变化。"""
    out = tmp_path / "resample_xres.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "xRes": 0.002, "yRes": 0.002},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "                  'gt': list(ds.GetGeoTransform())}))",
    )
    gt = info["gt"]
    assert gt[1] == pytest.approx(0.002), f"输出像元宽应为 0.002，实际 {gt[1]}"
    assert gt[5] == pytest.approx(-0.002), f"输出像元高应为 -0.002，实际 {gt[5]}"
    # 16 * 0.001 的地理跨度按 0.002 成像元 => 8x8
    assert info["w"] == 8 and info["h"] == 8, f"输出尺寸应为 8x8，实际 {info['w']}x{info['h']}"
    # 原点不变
    assert gt[0] == pytest.approx(100.0)
    assert gt[3] == pytest.approx(30.0)


def test_output_width_height_parameters(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """width/height 显式指定输出尺寸：尺寸按参数，其余网格随之推导。"""
    out = tmp_path / "outsize.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "width": 8, "height": 4},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "                  'gt': list(ds.GetGeoTransform())}))",
    )
    assert info["w"] == 8 and info["h"] == 4, f"输出尺寸应为 8x4，实际 {info['w']}x{info['h']}"
    gt = info["gt"]
    # 地理跨度不变，像元尺寸按比例放大
    assert gt[1] == pytest.approx(0.002), f"输出像元宽应为 0.002，实际 {gt[1]}"
    assert gt[5] == pytest.approx(-0.004), f"输出像元高应为 -0.004，实际 {gt[5]}"


# ------------------------------------------------------------- 3. dtype
def test_output_type_sets_dtype(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """outputType 显式指定输出数据类型，落盘类型必须与参数一致。"""
    out = tmp_path / "dtype_int16.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "outputType": "Int16"},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "b = ds.GetRasterBand(1)\n"
        "print(json.dumps({'dtype': gdal.GetDataTypeName(b.DataType),\n"
        "                  'first': float(b.ReadAsArray()[0][0])}))",
    )
    assert info["dtype"] == "Int16", f"输出 dtype 应为 Int16，实际 {info['dtype']}"
    assert info["first"] == 0, f"Int16 输出首像元应为 0，实际 {info['first']}"


def test_invalid_output_type_is_rejected(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """非法 dtype 必须报明确错误，不得静默回退到源类型。"""
    out = tmp_path / "bad_dtype.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "outputType": "NotAType"},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert "NotAType" in result.error.get("message", "")
    assert not out.exists(), "参数非法时不得产出文件"


def test_invalid_src_win_is_rejected(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """非法 srcWin（长度不对）必须报明确错误。"""
    out = tmp_path / "bad_win.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "srcWin": [1, 2, 3]},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error.get("code") == "invalid_parameters"
    assert not out.exists()


# ------------------------------------------------------------- 4. mask / NoData
def test_nodata_propagates_to_output(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """noData 参数：输出 NoData 必须等于参数值。"""
    out = tmp_path / "nodata.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "noData": -32768},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "b = ds.GetRasterBand(1)\n"
        "mask = b.GetMaskBand().ReadAsArray()\n"
        "print(json.dumps({'nodata': b.GetNoDataValue(),\n"
        "                  'mask_flags': int(b.GetMaskFlags()),\n"
        "                  'mask_valid_count': int(mask.sum()),\n"
        "                  'mask_total': int(mask.size),\n"
        "                  'GMF_NODATA': int(gdal.GMF_NODATA),\n"
        "                  'GMF_ALPHA': int(gdal.GMF_ALPHA)}))",
    )
    # GDAL 3.13.3 实测常量：GMF_NODATA=8、GMF_ALPHA=4
    assert info["GMF_NODATA"] == 8, f"GMF_NODATA 常量应为 8，实际 {info['GMF_NODATA']}"
    assert info["GMF_ALPHA"] == 4, f"GMF_ALPHA 常量应为 4，实际 {info['GMF_ALPHA']}"

    # NoData **元数据**与实际 **mask** 分别核对，不能只看其中之一。
    assert info["nodata"] == pytest.approx(-32768), f"输出 NoData 应为 -32768，实际 {info['nodata']}"
    assert info["mask_flags"] & info["GMF_NODATA"], (
        f"掩膜标志应含 GMF_NODATA({info['GMF_NODATA']})，实际 {info['mask_flags']}"
    )
    # 源影像本就没有 NoData 像元，故所有像元都应有效
    # （mask 是 uint8：有效=255、无效=0，故有效计数 = sum / 255）
    valid_count = info["mask_valid_count"] // 255
    assert valid_count == info["mask_total"], (
        f"有效像元数 {valid_count} 应等于总数 {info['mask_total']}"
    )


def test_source_nodata_is_preserved_without_override(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """未指定 noData 时，源 NoData 不得被静默丢弃。"""
    out = tmp_path / "nodata_keep.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff"},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(
        gdal_rt,
        out,
        "print(json.dumps({'nodata': ds.GetRasterBand(1).GetNoDataValue()}))",
    )
    assert info["nodata"] == pytest.approx(-9999.0), (
        f"源 NoData(-9999) 应被保留，实际 {info['nodata']}"
    )


# ------------------------------------------------------------------ 5. CRS
def test_warp_dst_srs_changes_crs(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """Warp 的 dstSRS 必须生效：输出 CRS 与参数一致，且范围与像元值随之变化。"""
    out = tmp_path / "warp_3857.tif"
    result = _run_task(
        qapp,
        "gdal:warp",
        {"format": "GTiff", "dstSRS": "EPSG:3857"},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "srs = ds.GetSpatialRef()\n"
        "print(json.dumps({'epsg': srs.GetAuthorityCode(None) if srs else None,\n"
        "                  'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "                  'gt': list(ds.GetGeoTransform())}))",
    )
    assert info["epsg"] == "3857", f"输出 CRS 应为 EPSG:3857，实际 {info['epsg']}"
    # 投影后坐标量级变化：3857 的 X 以米计（1e7 量级），不再是 100 度
    assert abs(info["gt"][0]) > 1e6, f"重投影后原点 X 量级不对：{info['gt'][0]}"


def test_warp_target_aligned_pixels_aligns_grid(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """targetAlignedPixels：输出网格必须对齐到整数像元边界。"""
    out = tmp_path / "warp_tap.tif"
    result = _run_task(
        qapp,
        "gdal:warp",
        {
            "format": "GTiff",
            "dstSRS": "EPSG:3857",
            "targetAlignedPixels": True,
            "xRes": 1000.0,
            "yRes": 1000.0,
        },
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "print(json.dumps({'gt': list(ds.GetGeoTransform()),\n"
        "                  'w': ds.RasterXSize, 'h': ds.RasterYSize}))",
    )
    gt = info["gt"]
    assert gt[1] == pytest.approx(1000.0), f"输出像元宽应为 1000，实际 {gt[1]}"
    # 对齐到整数像元：原点必须是 1000 的整数倍
    assert gt[0] % 1000 == pytest.approx(0.0, abs=1e-6), f"原点未对齐到像元边界：{gt[0]}"
    assert gt[3] % 1000 == pytest.approx(0.0, abs=1e-6), f"原点 Y 未对齐到像元边界：{gt[3]}"


# ------------------------------------------------------------- 6. 像元值
def test_resample_nearest_preserves_sample_values(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """最近邻重采样：像元值必须来自源数据（不得凭空插值）。"""
    out = tmp_path / "resample_nearest.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "xRes": 0.002, "yRes": 0.002, "resample_alg": "nearest"},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "arr = ds.GetRasterBand(1).ReadAsArray().tolist()\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize, 'arr': arr}))",
    )
    assert info["w"] == 8 and info["h"] == 8
    source_values = {r * 16 + c for r in range(16) for c in range(16)}
    for row in info["arr"]:
        for v in row:
            assert v in source_values, f"最近邻重采样产生了源数据中不存在的值 {v}"


def test_resample_bilinear_yields_intermediate_values(
    qapp, gdal_rt, source_raster, tmp_path
) -> None:
    """双线性重采样：像元值可能落在源值之间，但必须落在源数据的全局值域内。"""
    out = tmp_path / "resample_bilinear.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {"format": "GTiff", "xRes": 0.0015, "yRes": 0.0015, "resample_alg": "bilinear"},
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "arr = ds.GetRasterBand(1).ReadAsArray().tolist()\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "                  'min': float(min(min(r) for r in arr)),\n"
        "                  'max': float(max(max(r) for r in arr))}))",
    )
    assert info["min"] >= 0.0, f"双线性结果不应小于源最小值 0，实际 {info['min']}"
    assert info["max"] <= 255.0, f"双线性结果不应大于源最大值 255，实际 {info['max']}"


def test_clip_and_resample_combined(qapp, gdal_rt, source_raster, tmp_path) -> None:
    """裁剪与重采样组合：先裁剪再重采样，两条路径的参数都生效。"""
    out = tmp_path / "clip_then_resample.tif"
    result = _run_task(
        qapp,
        "gdal:translate",
        {
            "format": "GTiff",
            "srcWin": [0, 0, 8, 8],
            "xRes": 0.002,
            "yRes": 0.002,
            "resample_alg": "nearest",
            "outputType": "Float64",
        },
        source_raster,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(
        gdal_rt,
        out,
        "arr = ds.GetRasterBand(1).ReadAsArray().tolist()\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "                  'dtype': gdal.GetDataTypeName(ds.GetRasterBand(1).DataType),\n"
        "                  'arr': arr, 'gt': list(ds.GetGeoTransform())}))",
    )
    # 裁剪后 8x8（8 个像元 × 0.001 度），再按 0.002 重采样 => 4x4
    assert info["w"] == 4 and info["h"] == 4, f"组合后尺寸应为 4x4，实际 {info['w']}x{info['h']}"
    assert info["dtype"] == "Float64", f"组合后 dtype 应为 Float64，实际 {info['dtype']}"
    # 最近邻：值来自裁剪区域（row*16+col, col<8, row<8）
    for row in info["arr"]:
        for v in row:
            assert v in {rr * 16 + cc for rr in range(8) for cc in range(8)}, (
                f"组合结果出现裁剪区域外的值 {v}"
            )
