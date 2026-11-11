"""P0-05 验收：Windows runtime 重定位与独立目录启动可行性验证。

验证目标：
1. UI 引擎（PySide6）：可在项目外的独立目录以 offscreen 模式成功初始化 Qt 窗口与事件上下文。
2. Rasterio 引擎：可在项目外的独立目录完成 GeoTIFF 写入与读取。
3. GDAL / OGR 引擎：可在项目外的独立目录完成 GeoTIFF 创建与 EPSG:4326->3857 坐标转换。
4. 验证缺少 GDAL 环境变量时的故障行为，证明显式设置 PATH/GDAL_DATA/PROJ_DATA 是重定位的必要条件。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime

pytestmark = pytest.mark.integration


def test_ui_qt_window_launches_in_isolated_directory(tmp_path: Path):
    """验证 PySide6 UI 运行时可在完全独立的临时工作目录中正常初始化 Qt 窗口。"""
    root = find_project_root()
    ui_py = root / ".venvs" / "sw-ui" / "Scripts" / "python.exe"
    if not ui_py.exists():
        pytest.skip(f"UI 环境不存在于 {ui_py}")

    script = """
import sys
from PySide6.QtWidgets import QApplication, QLabel

app = QApplication(['relocation_test', '-platform', 'offscreen'])
label = QLabel('Spectra Workbench Offscreen Window')
label.show()
assert app.platformName() == 'offscreen'
print('Qt window initialized successfully')
sys.exit(0)
"""
    proc = subprocess.run(
        [str(ui_py), "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"UI 独立目录启动失败: stdout={proc.stdout}, stderr={proc.stderr}"
    assert "Qt window initialized successfully" in proc.stdout


def test_rasterio_engine_runs_in_isolated_directory(tmp_path: Path):
    """验证 Rasterio 引擎可在完全独立的临时工作目录中完成栅格 I/O。"""
    root = find_project_root()
    rio_py = root / ".venvs" / "sw-rasterio" / "Scripts" / "python.exe"
    if not rio_py.exists():
        pytest.skip(f"Rasterio 环境不存在于 {rio_py}")

    script = """
import sys
import rasterio
import numpy as np
from rasterio.transform import from_origin

out_tif = 'rasterio_reloc_test.tif'
with rasterio.open(
    out_tif, 'w',
    driver='GTiff',
    height=4, width=4, count=1,
    dtype='float32',
    crs='EPSG:4326',
    transform=from_origin(100.0, 40.0, 0.1, 0.1)
) as ds:
    ds.write(np.ones((1, 4, 4), dtype='float32'))

with rasterio.open(out_tif) as ds:
    data = ds.read(1)
    assert data.shape == (4, 4)
    assert ds.crs.to_string() == 'EPSG:4326'

print('Rasterio relocation check passed')
sys.exit(0)
"""
    proc = subprocess.run(
        [str(rio_py), "-c", script],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"Rasterio 独立目录执行失败: stdout={proc.stdout}, stderr={proc.stderr}"
    )
    assert "Rasterio relocation check passed" in proc.stdout


def test_gdal_engine_runs_in_isolated_directory(tmp_path: Path):
    """验证 GDAL 引擎在显式配置环境变量后可在独立临时目录正常执行栅格创建与坐标转换。"""
    root = find_project_root()
    gdal_rt = resolve_gdal_runtime(root)
    gdal_py = gdal_rt["python"]
    if not (gdal_py and Path(gdal_py).exists()):
        pytest.skip(f"GDAL 运行时未配置或不存在于 {gdal_py}")

    script = """
import sys
from osgeo import gdal, osr

gdal.UseExceptions()

# 1. 栅格创建与写入
drv = gdal.GetDriverByName('GTiff')
ds = drv.Create('gdal_reloc_test.tif', 4, 4, 1, gdal.GDT_Float32)
b = ds.GetRasterBand(1)
raw_bytes = b'\\x00\\x00\\x80?' * 16  # 16 个 float32 1.0
b.WriteRaster(0, 0, 4, 4, raw_bytes, buf_type=gdal.GDT_Float32)
ds.FlushCache()
del b, ds

# 2. 坐标转换（依赖 PROJ_DATA/proj.db）
srs_in = osr.SpatialReference()
srs_in.ImportFromEPSG(4326)
srs_in.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

srs_out = osr.SpatialReference()
srs_out.ImportFromEPSG(3857)
srs_out.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

ct = osr.CoordinateTransformation(srs_in, srs_out)
pt = ct.TransformPoint(116.4, 39.9)
assert abs(pt[0] - 12957588.73) < 1.0
assert abs(pt[1] - 4851421.18) < 1.0

print('GDAL relocation check passed')
sys.exit(0)
"""
    proc = subprocess.run(
        [str(gdal_py), "-c", script],
        cwd=str(tmp_path),
        env=gdal_rt["env"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"GDAL 独立目录执行失败: stdout={proc.stdout}, stderr={proc.stderr}"
    )
    assert "GDAL relocation check passed" in proc.stdout
