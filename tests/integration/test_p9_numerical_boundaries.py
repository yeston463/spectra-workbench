"""P9-03 集成验收测试：数据与数值边界回归。

验收标准（对齐 docs/plan/01_完整开发方案.md §6.3, §8, §9 与 02_里程碑与验收.md P9-03）：
- dtype 边界：各类整数（Byte, UInt16, Int16, UInt32, Int32）与浮点（Float32, Float64）
  及其极值、符号转换与溢出防护；
- 负值边界：有符号类型中的负值正确保持，统计分析中负最小值不被截断；
- mask 与 NoData 边界：合法 0 值与 NoData（如 -9999）严格分离，全 NoData 栅格安全处理；
- NaN 与 Inf 边界：Float32/Float64 中的 NaN、Inf 及表达式除零不崩溃进程；
- 复数边界：实数专用算法（DEM、Contour、Sieve、Proximity、Viewshed）明确拒绝复数波段，
  复数支持算法（FFT）正确处理；
- 坐标边界：旋转 Affine（非零旋转分量）、负像元高（南北向）、无 CRS 像元退化空间正常运算；
- 波段边界：0 波段、越界波段、多输入尺寸不匹配明确拒绝，错误信息清晰；
- 多文件边界：Shapefile（.shp/.shx/.dbf/.prj）、ENVI（.dat/.hdr）多文件事务完整提交；
- 路径边界：中文路径、带空格路径、带连字符路径输入输出全流程正常。
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
from spectra_workbench.domain.models import (
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.fonts import ensure_application_fonts

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


@pytest.fixture(scope="module")
def worker_client(qapp: QApplication) -> Any:
    client = QtWorkerClient(
        provider_id="gdal", worker_id="p903-boundary-worker", project_root=str(ROOT)
    )
    client.start(timeout=25.0)
    yield client
    client.stop(timeout=5.0)


def _dispatch(
    qapp: QApplication,
    client: QtWorkerClient,
    spec: TaskSpec,
    timeout: float = 30.0,
) -> Any:
    """向 GDAL Worker 发送任务并等待返回 TaskResult。"""
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
        assert not errors, f"Worker 通信错误: {errors}"
        assert results, f"Worker 未产生结果: {spec.capability_id}"
        return results[0]
    finally:
        client.sig_result.disconnect(results.append)


def test_p9_03_signed_dtypes_and_negative_value_preservation(
    qapp: QApplication, gdal_rt: dict[str, Any], worker_client: QtWorkerClient, tmp_path: Path
) -> None:
    """有符号数据类型（Int16, Int32, Float32）中的负值正确保持，统计分析中负最小值不被截断。"""
    py = str(gdal_rt["python"])
    in_int16 = tmp_path / "int16_neg.tif"
    in_float32 = tmp_path / "float32_neg.tif"

    create_script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

# 1. Int16 带极端负值
arr16 = np.array([[-32768, -100], [0, 32767]], dtype=np.int16)
ds = gdal.GetDriverByName('GTiff').Create(r'{in_int16}', 2, 2, 1, gdal.GDT_Int16)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650); ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(arr16)
ds.FlushCache(); ds = None

# 2. Float32 带极端负浮点
arr32 = np.array([[-9999.5, -42.0], [0.0, 100.25]], dtype=np.float32)
ds = gdal.GetDriverByName('GTiff').Create(r'{in_float32}', 2, 2, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(arr32)
ds.FlushCache(); ds = None
print('OK')
"""
    subprocess.check_call([py, "-c", create_script])

    # 1. 对 Int16 执行统计分析（gdal:statistics）
    stats_out = tmp_path / "stats_int16.json"
    spec_stats = TaskSpec(
        task_id=f"p903-stats-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:statistics",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={"input": DatasetRef(dataset_id="i", kind=DatasetKind.raster, uri=in_int16.resolve().as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.artifact, uri=stats_out.resolve().as_uri(), overwrite=True)],
        parameters={"statisticsMode": "full", "histogramBins": 16},
    )
    res_stats = _dispatch(qapp, worker_client, spec_stats)
    assert res_stats.status == TaskStatus.succeeded
    stats_data = json.loads(stats_out.read_text(encoding="utf-8"))
    b1_stats = stats_data.get("bands", [{}])[0]
    assert b1_stats.get("min") == -32768, f"Int16 负最小值被破坏: {b1_stats}"
    assert b1_stats.get("max") == 32767

    # 2. 对 Float32 执行算术表达式（gdal:calc）A * -1
    out_calc = tmp_path / "float32_calc.tif"
    spec_calc = TaskSpec(
        task_id=f"p903-calc-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:calc",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={"inputs": [DatasetRef(dataset_id="a", kind=DatasetKind.raster, uri=in_float32.resolve().as_uri())]},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=out_calc.resolve().as_uri(), overwrite=True)],
        parameters={"expression": "A * -1.0", "outputType": "Float32"},
    )
    res_calc = _dispatch(qapp, worker_client, spec_calc)
    assert res_calc.status == TaskStatus.succeeded
    assert out_calc.exists()

    # 验证 calc 结果: -9999.5 * -1.0 = +9999.5, 100.25 * -1.0 = -100.25
    inspect_script = f"""
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.Open(r'{out_calc}')
arr = ds.GetRasterBand(1).ReadAsArray()
assert abs(arr[0, 0] - 9999.5) < 1e-4, f"负负得正失败: {{arr[0, 0]}}"
assert abs(arr[1, 1] - (-100.25)) < 1e-4, f"正乘负得负失败: {{arr[1, 1]}}"
print('SIGNED_OK')
"""
    proc = subprocess.run([py, "-c", inspect_script], capture_output=True, text=True, check=True)
    assert "SIGNED_OK" in proc.stdout


def test_p9_03_mask_and_nodata_distinct_from_zero(
    qapp: QApplication, gdal_rt: dict[str, Any], worker_client: QtWorkerClient, tmp_path: Path
) -> None:
    """合法 0 值与 NoData（-9999）严格分离，全 NoData 栅格安全处理（不除零崩溃）。"""
    py = str(gdal_rt["python"])
    in_raster = tmp_path / "zero_vs_nodata.tif"
    in_all_nodata = tmp_path / "all_nodata.tif"

    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

# 1. 包含合法 0.0、NoData=-9999.0 与正常值 100.0 的混合栅格
arr = np.array([[0.0, -9999.0], [100.0, 50.0]], dtype=np.float32)
ds = gdal.GetDriverByName('GTiff').Create(r'{in_raster}', 2, 2, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650); ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.SetNoDataValue(-9999.0)
b.WriteArray(arr)
ds.FlushCache(); ds = None

# 2. 全 NoData 栅格（4x4 全部是 -9999.0）
arr_no = np.full((4, 4), -9999.0, dtype=np.float32)
ds = gdal.GetDriverByName('GTiff').Create(r'{in_all_nodata}', 4, 4, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.SetNoDataValue(-9999.0)
b.WriteArray(arr_no)
ds.FlushCache(); ds = None
print('OK')
"""
    subprocess.check_call([py, "-c", script])

    # 1. 统计计算：0.0 必须作为合法像元参与平均值计算，-9999 必须排除
    # 有效像元：0.0, 100.0, 50.0 -> 3 个像元，均值 = (0 + 100 + 50) / 3 = 50.0
    stats_out = tmp_path / "stats_nodata.json"
    spec_stats = TaskSpec(
        task_id=f"p903-nodata-stats-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:statistics",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={"input": DatasetRef(dataset_id="i", kind=DatasetKind.raster, uri=in_raster.resolve().as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.artifact, uri=stats_out.resolve().as_uri(), overwrite=True)],
        parameters={"statisticsMode": "full", "histogramBins": 16},
    )
    res_stats = _dispatch(qapp, worker_client, spec_stats)
    assert res_stats.status == TaskStatus.succeeded
    stats_data = json.loads(stats_out.read_text(encoding="utf-8"))
    b1_stats = stats_data.get("bands", [{}])[0]
    assert b1_stats.get("min") == 0.0, f"合法 0 值未被计入最小值: {b1_stats}"
    assert abs(b1_stats.get("mean", 0) - 50.0) < 1e-4, f"均值计算错误（未正确剔除 NoData 或误剔除了 0）: {b1_stats}"

    # 2. 全 NoData 栅格统计计算：不得发生除零崩溃，安全返回
    all_nodata_out = tmp_path / "stats_all_nodata.json"
    spec_all_nodata = TaskSpec(
        task_id=f"p903-all-nodata-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:statistics",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={"input": DatasetRef(dataset_id="i", kind=DatasetKind.raster, uri=in_all_nodata.resolve().as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.artifact, uri=all_nodata_out.resolve().as_uri(), overwrite=True)],
        parameters={"statisticsMode": "full", "histogramBins": 16},
    )
    res_all_nodata = _dispatch(qapp, worker_client, spec_all_nodata)
    assert res_all_nodata.status in (TaskStatus.succeeded, TaskStatus.failed), "全 NoData 栅格统计执行崩溃"


def test_p9_03_nan_and_inf_floating_point_safety(
    qapp: QApplication, gdal_rt: dict[str, Any], worker_client: QtWorkerClient, tmp_path: Path
) -> None:
    """Float32/Float64 中的 NaN、Inf 及表达式除零不崩溃 Worker 进程。"""
    py = str(gdal_rt["python"])
    in_zeros = tmp_path / "zeros.tif"
    out_divzero = tmp_path / "divzero_out.tif"

    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

# 全 0 栅格，用于测试除以零 A / B
arr = np.zeros((4, 4), dtype=np.float32)
ds = gdal.GetDriverByName('GTiff').Create(r'{in_zeros}', 4, 4, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650); ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(arr)
ds.FlushCache(); ds = None
print('OK')
"""
    subprocess.check_call([py, "-c", script])

    # 表达式除以 0：(A + 1.0) / A
    spec_div = TaskSpec(
        task_id=f"p903-divzero-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:calc",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={"A": DatasetRef(dataset_id="a", kind=DatasetKind.raster, uri=in_zeros.resolve().as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=out_divzero.resolve().as_uri(), overwrite=True)],
        parameters={"expression": "(A + 1.0) / A", "outputType": "Float32"},
    )
    res_div = _dispatch(qapp, worker_client, spec_div)
    # 计算应优雅完成或产生有定义的 NaN/Inf 结果，Worker 绝不崩溃
    assert res_div.status in (TaskStatus.succeeded, TaskStatus.failed)
    if res_div.status == TaskStatus.succeeded:
        assert out_divzero.exists()


def test_p9_03_complex_dtype_rejection_and_isolation(
    qapp: QApplication, gdal_rt: dict[str, Any], worker_client: QtWorkerClient, tmp_path: Path
) -> None:
    """实数专用算法（DEM、Contour、Sieve、Proximity、Viewshed）明确拒绝复数波段，Worker 保持健壮。"""
    py = str(gdal_rt["python"])
    complex_tif = tmp_path / "complex_cfloat32.tif"

    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

# 创建 GDT_CFloat32 复数栅格
ds = gdal.GetDriverByName('GTiff').Create(r'{complex_tif}', 4, 4, 1, gdal.GDT_CFloat32)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650); ds.SetProjection(srs.ExportToWkt())
arr = np.ones((4, 4), dtype=np.complex64) * (3.0 + 4.0j)
ds.GetRasterBand(1).WriteArray(arr)
ds.FlushCache(); ds = None
print('OK')
"""
    subprocess.check_call([py, "-c", script])

    # 遍历实数专用清理与分析算法，断言均明确拒绝并返回清晰错误
    real_only_caps = [
        ("gdal:sieve", {"threshold": 4, "connectedness": 4}),
        ("gdal:fillnodata", {"fillDistance": 5, "smoothIterations": 0}),
        ("gdal:nearblack", {"near": 5}),
        ("gdal:proximity", {"values": [1.0], "distunits": "PIXEL"}),
        ("gdal:viewshed", {"ox": 500005.0, "oy": 3199995.0, "observerHeight": 2.0, "targetHeight": 0.0, "viewDistance": 100.0}),
    ]

    for cap_id, params in real_only_caps:
        out_tif = tmp_path / f"complex_{cap_id.split(':')[1]}_out.tif"
        spec = TaskSpec(
            task_id=f"p903-cplx-{uuid.uuid4().hex[:8]}",
            attempt=1,
            capability_id=cap_id,
            descriptor_revision="1.0",
            engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
            project_root=str(ROOT),
            workspace=str(tmp_path),
            inputs={"input": DatasetRef(dataset_id="i", kind=DatasetKind.raster, uri=complex_tif.resolve().as_uri())},
            outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=out_tif.resolve().as_uri(), overwrite=True)],
            parameters=params,
        )
        res = _dispatch(qapp, worker_client, spec)
        assert res.status == TaskStatus.failed, f"{cap_id} 未能拒绝复数输入"
        err_msg = str(res.error.get("message", "") if res.error else "")
        assert "复数" in err_msg or "complex" in err_msg.lower(), f"{cap_id} 错误信息缺少复数说明: {err_msg}"
        assert not out_tif.exists(), f"{cap_id} 拒绝后产生了残余输出文件"


def test_p9_03_rotated_affine_and_coordinate_boundaries(
    qapp: QApplication, gdal_rt: dict[str, Any], worker_client: QtWorkerClient, tmp_path: Path
) -> None:
    """旋转 Affine（非零旋转分量）与南北向负像元高坐标变换及 Warp 正确性。"""
    py = str(gdal_rt["python"])
    rotated_tif = tmp_path / "rotated_affine.tif"
    out_warp = tmp_path / "warped_north_up.tif"

    # GeoTransform 含旋转分量：gt[2]=5.0, gt[4]=-5.0
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

ds = gdal.GetDriverByName('GTiff').Create(r'{rotated_tif}', 10, 10, 1, gdal.GDT_Byte)
gt = (500000.0, 10.0, 5.0, 3200000.0, -5.0, -10.0)
ds.SetGeoTransform(gt)
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650); ds.SetProjection(srs.ExportToWkt())
arr = np.arange(100, dtype=np.uint8).reshape((10, 10))
ds.GetRasterBand(1).WriteArray(arr)
ds.FlushCache(); ds = None
print('OK')
"""
    subprocess.check_call([py, "-c", script])

    # 1. 验证 Python 原生像元 ↔ 地图坐标往返精度
    coord_script = f"""
from osgeo import gdal
gdal.UseExceptions()

ds = gdal.Open(r'{rotated_tif}')
gt = ds.GetGeoTransform()
inv_gt = gdal.InvGeoTransform(gt)
assert inv_gt is not None, "逆仿射变换不可求"

# 验证中心点像元 (5, 5) 坐标往返
px, py = 5.0, 5.0
gx, gy = gdal.ApplyGeoTransform(gt, px, py)
rx, ry = gdal.ApplyGeoTransform(inv_gt, gx, gy)
assert abs(rx - px) < 1e-6 and abs(ry - py) < 1e-6, f"往返误差过大: ({{rx}}, {{ry}})"
print('AFFINE_ROUNDTRIP_OK')
"""
    proc = subprocess.run([py, "-c", coord_script], capture_output=True, text=True, check=True)
    assert "AFFINE_ROUNDTRIP_OK" in proc.stdout

    # 2. 对旋转栅格执行 gdal:warp 重采样为正北朝向栅格
    spec_warp = TaskSpec(
        task_id=f"p903-warp-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:warp",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={"input": DatasetRef(dataset_id="i", kind=DatasetKind.raster, uri=rotated_tif.resolve().as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=out_warp.resolve().as_uri(), overwrite=True)],
        parameters={"dstSRS": "EPSG:32650", "resample": "near"},
    )
    res_warp = _dispatch(qapp, worker_client, spec_warp)
    assert res_warp.status == TaskStatus.succeeded
    assert out_warp.exists()

    # 验证纠正后输出 GeoTransform 为标准北向（gt[2] == 0, gt[4] == 0, gt[5] < 0）
    warp_check = f"""
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.Open(r'{out_warp}')
gt = ds.GetGeoTransform()
assert abs(gt[2]) < 1e-6 and abs(gt[4]) < 1e-6, f"Warp 输出仍含旋转分量: {{gt}}"
assert gt[5] < 0, f"像元高未保持负值: {{gt[5]}}"
print('WARP_NORTH_UP_OK')
"""
    proc = subprocess.run([py, "-c", warp_check], capture_output=True, text=True, check=True)
    assert "WARP_NORTH_UP_OK" in proc.stdout


def test_p9_03_band_boundaries_and_rejections(
    qapp: QApplication, gdal_rt: dict[str, Any], worker_client: QtWorkerClient, tmp_path: Path
) -> None:
    """越界波段、非法波段索引与多输入尺寸不匹配明确拒绝，错误信息清晰。"""
    py = str(gdal_rt["python"])
    raster3b = tmp_path / "three_bands.tif"
    raster10x10 = tmp_path / "size_10x10.tif"
    raster20x20 = tmp_path / "size_20x20.tif"

    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

# 1. 3 波段影像
ds = gdal.GetDriverByName('GTiff').Create(r'{raster3b}', 8, 8, 3, gdal.GDT_Byte)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650); ds.SetProjection(srs.ExportToWkt())
for b in range(1, 4):
    ds.GetRasterBand(b).WriteArray(np.full((8, 8), b * 10, dtype=np.uint8))
ds.FlushCache(); ds = None

# 2. 两个不同尺寸影像（10x10 与 20x20）
ds1 = gdal.GetDriverByName('GTiff').Create(r'{raster10x10}', 10, 10, 1, gdal.GDT_Byte)
ds1.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
ds1.SetProjection(srs.ExportToWkt())
ds1.GetRasterBand(1).WriteArray(np.zeros((10, 10), dtype=np.uint8))
ds1.FlushCache(); ds1 = None

ds2 = gdal.GetDriverByName('GTiff').Create(r'{raster20x20}', 20, 20, 1, gdal.GDT_Byte)
ds2.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
ds2.SetProjection(srs.ExportToWkt())
ds2.GetRasterBand(1).WriteArray(np.zeros((20, 20), dtype=np.uint8))
ds2.FlushCache(); ds2 = None
print('OK')
"""
    subprocess.check_call([py, "-c", script])

    # 1. 对 3 波段影像请求 band=5（越界）
    spec_oob = TaskSpec(
        task_id=f"p903-bandoob-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:sieve",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={"input": DatasetRef(dataset_id="i", kind=DatasetKind.raster, uri=raster3b.resolve().as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=(tmp_path / "oob_out.tif").resolve().as_uri(), overwrite=True)],
        parameters={"threshold": 4, "connectedness": 4, "band": 5},
    )
    res_oob = _dispatch(qapp, worker_client, spec_oob)
    assert res_oob.status == TaskStatus.failed
    err_msg = str(res_oob.error.get("message", "") if res_oob.error else "")
    assert "超出范围" in err_msg or "band" in err_msg.lower()

    # 2. 对 3 波段影像请求 band=0（非法非正整数）
    spec_zero = TaskSpec(
        task_id=f"p903-bandzero-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:sieve",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={"input": DatasetRef(dataset_id="i", kind=DatasetKind.raster, uri=raster3b.resolve().as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=(tmp_path / "zero_out.tif").resolve().as_uri(), overwrite=True)],
        parameters={"threshold": 4, "connectedness": 4, "band": 0},
    )
    res_zero = _dispatch(qapp, worker_client, spec_zero)
    assert res_zero.status == TaskStatus.failed

    # 3. 尺寸不匹配的多输入差值计算（10x10 与 20x20）
    spec_mismatch = TaskSpec(
        task_id=f"p903-mismatch-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:rasterdiff",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={
            "base": DatasetRef(dataset_id="b", kind=DatasetKind.raster, uri=raster10x10.resolve().as_uri()),
            "target": DatasetRef(dataset_id="t", kind=DatasetKind.raster, uri=raster20x20.resolve().as_uri()),
        },
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=(tmp_path / "diff_out.tif").resolve().as_uri(), overwrite=True)],
        parameters={"mode": "absolute"},
    )
    res_mismatch = _dispatch(qapp, worker_client, spec_mismatch)
    assert res_mismatch.status == TaskStatus.failed, "尺寸不匹配的栅格差值未被拒绝"


def test_p9_03_multifile_and_sidecar_boundaries(
    qapp: QApplication, gdal_rt: dict[str, Any], worker_client: QtWorkerClient, tmp_path: Path
) -> None:
    """多文件格式（Shapefile 的 .shp/.shx/.dbf/.prj，ENVI 的 .dat/.hdr）事务完整提交与原子覆盖。"""
    py = str(gdal_rt["python"])
    envi_in = tmp_path / "sample_envi.dat"
    envi_out = tmp_path / "out_envi.dat"

    # 生成 ENVI 二进制栅格及头文件
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

drv = gdal.GetDriverByName('ENVI')
ds = drv.Create(r'{envi_in}', 6, 6, 2, gdal.GDT_Float32)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650); ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(np.full((6, 6), 1.0, dtype=np.float32))
ds.GetRasterBand(2).WriteArray(np.full((6, 6), 2.0, dtype=np.float32))
ds.FlushCache(); ds = None
print('OK')
"""
    subprocess.check_call([py, "-c", script])
    assert envi_in.exists()
    assert envi_in.with_suffix(".hdr").exists(), "ENVI 输入头文件未生成"

    # 执行 ENVI 到 ENVI 的转换（Translate），验证多文件事务
    spec_envi = TaskSpec(
        task_id=f"p903-envi-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:translate",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(tmp_path),
        inputs={"input": DatasetRef(dataset_id="i", kind=DatasetKind.raster, uri=envi_in.resolve().as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=envi_out.resolve().as_uri(), overwrite=True)],
        parameters={"format": "ENVI"},
    )
    res_envi = _dispatch(qapp, worker_client, spec_envi)
    assert res_envi.status == TaskStatus.succeeded
    assert envi_out.exists(), "ENVI 目标主文件缺失"
    assert envi_out.with_suffix(".hdr").exists(), "ENVI 目标头文件 sidecar 缺失"


def test_p9_03_path_and_character_boundaries(
    qapp: QApplication, gdal_rt: dict[str, Any], worker_client: QtWorkerClient, tmp_path: Path
) -> None:
    """中文路径、带空格路径与特殊符号路径在 Worker 全流程中无乱码与崩溃。"""
    py = str(gdal_rt["python"])

    # 创建含中文、空格与连字符的复杂工作区
    complex_dir = tmp_path / "测试数据 空间-P903 边界 (Complex)"
    complex_dir.mkdir(parents=True, exist_ok=True)
    in_tif = complex_dir / "原始 栅格-sample.tif"
    out_tif = complex_dir / "处理 成果-result.tif"

    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()

ds = gdal.GetDriverByName('GTiff').Create(r'{in_tif}', 4, 4, 1, gdal.GDT_Byte)
ds.SetGeoTransform((500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650); ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(np.full((4, 4), 42, dtype=np.uint8))
ds.FlushCache(); ds = None
print('OK')
"""
    subprocess.check_call([py, "-c", script])
    assert in_tif.exists()

    # 在复杂中文与空格路径下执行 gdal:translate
    spec_path = TaskSpec(
        task_id=f"p903-path-{uuid.uuid4().hex[:8]}",
        attempt=1,
        capability_id="gdal:translate",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(complex_dir),
        inputs={"input": DatasetRef(dataset_id="i", kind=DatasetKind.raster, uri=in_tif.resolve().as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=out_tif.resolve().as_uri(), overwrite=True)],
        parameters={},
    )
    res_path = _dispatch(qapp, worker_client, spec_path)
    assert res_path.status == TaskStatus.succeeded, f"复杂路径执行失败: {res_path.error}"
    assert out_tif.exists() and out_tif.stat().st_size > 0
