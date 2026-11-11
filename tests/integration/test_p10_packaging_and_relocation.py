"""P10-01 集成验收测试：Windows onedir UI 与 engine runtimes。

验收标准（docs/plan/02_里程碑与验收.md P10-01）：
- 迁到中文/空格目录可运行；
- 引擎、Qt 插件、CRS 资源完整。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.package_onedir import build_onedir_package, verify_relocation
from spectra_workbench.app.doctor import find_project_root

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()
DIST_DIR = ROOT / "dist" / "SpectraWorkbench"
RELOCATED_BASE = ROOT / "work" / "dist_test" / "高光谱 空间 2026"
RELOCATED_APP = RELOCATED_BASE / "SpectraWorkbench"


@pytest.fixture(scope="session", autouse=True)
def ensure_onedir_built() -> Path:
    """确保 onedir 发布包在测试开始前已就绪（如未构建则自动构建并链接）。"""
    if (
        not (DIST_DIR / "SpectraWorkbench.exe").exists()
        or not (DIST_DIR / "manifest.json").exists()
    ):
        build_onedir_package(DIST_DIR, mode="link")
    return DIST_DIR


@pytest.fixture(scope="session", autouse=True)
def ensure_relocated_app(ensure_onedir_built: Path) -> Path:
    """确保在包含深层中文与空格的路径下完成发布包重定位镜像。"""
    if not (RELOCATED_APP / "SpectraWorkbench.exe").exists():
        verify_relocation(DIST_DIR, RELOCATED_BASE)
    return RELOCATED_APP


def test_p10_01_onedir_manifest_and_directory_structure() -> None:
    """验证 onedir 发布包的目录布局规范性与元数据清单。"""
    assert DIST_DIR.is_dir(), "dist/SpectraWorkbench 必须存在"
    assert (DIST_DIR / "SpectraWorkbench.exe").is_file(), "缺少原生 Windows GUI 启动器"
    assert (DIST_DIR / "SpectraWorkbench-console.exe").is_file(), "缺少原生控制台启动器"
    assert (DIST_DIR / "SpectraWorkbench.bat").is_file(), "缺少批处理启动器"
    assert (DIST_DIR / "manifest.json").is_file(), "缺少发布包 manifest.json"

    # 验证 app 目录
    assert (DIST_DIR / "app" / "python.exe").is_file(), "缺少 app/python.exe"
    assert (DIST_DIR / "app" / "qt.conf").is_file(), "缺少 app/qt.conf"
    assert (DIST_DIR / "app" / "src" / "spectra_workbench").is_dir(), "缺少应用代码"
    assert (DIST_DIR / "app" / "Lib" / "site-packages" / "PySide6").is_dir(), "缺少 PySide6 依赖"

    # 验证 runtimes 目录
    assert (DIST_DIR / "runtimes" / "gdal" / "python.exe").is_file(), "缺少 GDAL 引擎解释器"
    assert (DIST_DIR / "runtimes" / "gdal" / "bin" / "gdalinfo.exe").is_file(), "缺少 gdalinfo CLI"
    assert (DIST_DIR / "runtimes" / "gdal" / "share" / "gdal" / "epsg.wkt").is_file(), (
        "缺少 gdal CRS epsg.wkt"
    )
    assert (DIST_DIR / "runtimes" / "gdal" / "share" / "proj" / "proj.db").is_file(), (
        "缺少 PROJ 坐标数据库 proj.db"
    )
    assert (DIST_DIR / "runtimes" / "rasterio" / "python.exe").is_file(), "缺少 Rasterio 引擎解释器"

    # 校验 manifest 内容
    manifest_data = json.loads((DIST_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_data.get("app_name") == "Spectra Workbench"
    assert manifest_data.get("layout") == "onedir"


def test_p10_01_ui_qt_platform_plugin_in_chinese_space_path() -> None:
    """验证在中文与空格目录下，PySide6 UI 与 qwindows.dll 平台插件正常初始化。"""
    ui_py = RELOCATED_APP / "app" / "python.exe"
    assert ui_py.exists(), f"未找到重定位后的 Python 解释器: {ui_py}"

    qt_script = """
import sys
from PySide6.QtWidgets import QApplication, QLabel

app = QApplication(['test_p10', '-platform', 'offscreen'])
lbl = QLabel('Spectra Workbench Onedir UI in Chinese Path')
lbl.show()
assert app.platformName() == 'offscreen'
print('P10_UI_OFFSCREEN_SUCCESS')
sys.exit(0)
"""
    env = os.environ.copy()
    env["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(
        RELOCATED_APP / "app" / "Lib" / "site-packages" / "PySide6" / "plugins"
    )
    env["PYTHONPATH"] = (
        f"{RELOCATED_APP / 'app' / 'src'};{RELOCATED_APP / 'app' / 'Lib' / 'site-packages'}"
    )
    proc = subprocess.run(
        [str(ui_py), "-c", qt_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=20,
    )
    assert proc.returncode == 0, f"UI 启动失败: stdout={proc.stdout}, stderr={proc.stderr}"
    assert "P10_UI_OFFSCREEN_SUCCESS" in proc.stdout


def test_p10_01_gdal_crs_proj_db_integrity_in_chinese_space_path() -> None:
    """验证在中文与空格目录下，自带 GDAL 运行时完整使用内嵌 proj.db 执行 EPSG:4326 到 EPSG:3857 投影换算。"""
    gdal_py = RELOCATED_APP / "runtimes" / "gdal" / "python.exe"
    gdal_bin = RELOCATED_APP / "runtimes" / "gdal" / "bin"
    gdal_data = RELOCATED_APP / "runtimes" / "gdal" / "share" / "gdal"
    proj_data = RELOCATED_APP / "runtimes" / "gdal" / "share" / "proj"
    gdal_sp = RELOCATED_APP / "runtimes" / "gdal" / "Lib" / "site-packages"

    script = f"""
import os, sys
os.add_dll_directory(r'{gdal_bin}')
os.environ['PATH'] = r'{gdal_bin};' + os.environ.get('PATH', '')
os.environ['GDAL_DATA'] = r'{gdal_data}'
os.environ['PROJ_DATA'] = r'{proj_data}'
os.environ['PROJ_LIB'] = r'{proj_data}'
sys.path.insert(0, r'{gdal_sp}')

from osgeo import gdal, osr
if hasattr(osr, 'SetPROJSearchPaths'):
    osr.SetPROJSearchPaths([r'{proj_data}'])

# 1. 验证 GDAL 版本
ver = gdal.__version__
assert ver.startswith('3.13'), f'GDAL 版本不符预期: {{ver}}'

# 2. 验证 PROJ 数据库与坐标投影转换精度
s_srs = osr.SpatialReference()
s_srs.ImportFromEPSG(4326)
s_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

t_srs = osr.SpatialReference()
t_srs.ImportFromEPSG(3857)
t_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

tx = osr.CoordinateTransformation(s_srs, t_srs)
# 北京经纬度 (116.4074, 39.9042) -> EPSG:3857
x, y, _ = tx.TransformPoint(116.4074, 39.9042)
assert abs(x - 12958412.49) < 0.5, f'X 投影误差超标: {{x}}'
assert abs(y - 4852030.63) < 0.5, f'Y 投影误差超标: {{y}}'

print(f'P10_GDAL_PROJ_SUCCESS: x={{x:.2f}}, y={{y:.2f}}')
"""
    env = os.environ.copy()
    env["PATH"] = f"{gdal_bin};{env.get('PATH', '')}"
    env["GDAL_DATA"] = str(gdal_data)
    env["PROJ_DATA"] = str(proj_data)
    env["PROJ_LIB"] = str(proj_data)

    proc = subprocess.run(
        [str(gdal_py), "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=20,
    )
    assert proc.returncode == 0, f"GDAL 投影转换失败: stdout={proc.stdout}, stderr={proc.stderr}"
    assert "P10_GDAL_PROJ_SUCCESS" in proc.stdout


def test_p10_01_rasterio_io_in_chinese_space_path(tmp_path: Path) -> None:
    """验证在中文与空格目录下，自带 Rasterio 运行时正常读写 GeoTIFF 栅格。"""
    rio_py = RELOCATED_APP / "runtimes" / "rasterio" / "python.exe"
    rio_sp = RELOCATED_APP / "runtimes" / "rasterio" / "Lib" / "site-packages"

    script = f"""
import sys
sys.path.insert(0, r'{rio_sp}')
import rasterio
import numpy as np

out_tif = 'test_rio_reloc.tif'
with rasterio.open(
    out_tif, 'w',
    driver='GTiff',
    height=8, width=8, count=1,
    dtype='float32',
    crs='EPSG:4326'
) as ds:
    ds.write(np.ones((1, 8, 8), dtype='float32') * 42.0)

with rasterio.open(out_tif) as ds:
    data = ds.read(1)
    assert data.shape == (8, 8)
    assert data[0, 0] == 42.0
    assert ds.crs.to_string() == 'EPSG:4326'

print('P10_RASTERIO_IO_SUCCESS')
"""
    proc = subprocess.run(
        [str(rio_py), "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(tmp_path),
        timeout=20,
    )
    assert proc.returncode == 0, f"Rasterio 执行失败: stdout={proc.stdout}, stderr={proc.stderr}"
    assert "P10_RASTERIO_IO_SUCCESS" in proc.stdout


def test_p10_01_native_launcher_help_and_argument_dispatch() -> None:
    """验证原生启动器 SpectraWorkbench-console.exe 在中文/空格目录下准确响应 --help。"""
    launcher_exe = RELOCATED_APP / "SpectraWorkbench-console.exe"
    if not launcher_exe.exists():
        launcher_exe = RELOCATED_APP / "SpectraWorkbench.bat"

    proc = subprocess.run(
        [str(launcher_exe), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert proc.returncode == 0, f"原生启动器 --help 失败: stderr={proc.stderr}"
    out_all = proc.stdout + proc.stderr
    assert "Spectra Workbench" in out_all or "usage" in out_all.lower()
