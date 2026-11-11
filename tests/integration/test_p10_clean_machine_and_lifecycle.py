"""P10-02 集成验收测试：安装/升级/卸载和干净机测试。

验收标准（docs/plan/02_里程碑与验收.md P10-02）：
- 无全局 Python、无开发目录、离线可启动并完成主要处理；
- 用户项目保留。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.package_onedir import (
    build_onedir_package,
    install_package,
    uninstall_package,
    upgrade_package,
)
from spectra_workbench.app.doctor import find_project_root

pytestmark = [pytest.mark.integration, pytest.mark.engine]

ROOT = find_project_root()
DIST_DIR = ROOT / "dist" / "SpectraWorkbench"
CLEAN_BASE = ROOT / "work" / "clean_machine_test"
INSTALLED_APP = CLEAN_BASE / "installed_app" / "SpectraWorkbench"
USER_WORKSPACE = CLEAN_BASE / "user_workspace"


@pytest.fixture(scope="session", autouse=True)
def ensure_onedir_and_installation() -> Path:
    """确保 onedir 发布包已构建，并预先安装至独立干净测试环境。"""
    if (
        not (DIST_DIR / "SpectraWorkbench.exe").exists()
        or not (DIST_DIR / "manifest.json").exists()
    ):
        build_onedir_package(DIST_DIR, mode="link")

    install_package(DIST_DIR, INSTALLED_APP, mode="link")

    USER_WORKSPACE.mkdir(parents=True, exist_ok=True)
    return INSTALLED_APP


def _get_scrubbed_clean_env(tmp_dir: Path, offline: bool = True) -> dict[str, str]:
    """构造彻底剥离全局 Python、Conda、QGIS、虚拟环境与网络代理的干净环境字典。"""
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    windir = os.environ.get("WINDIR", r"C:\Windows")

    # 仅保留 Windows 最基础核心系统路径，绝无任何 python/conda/qgis/dev 路径
    clean_path = f"{system_root}\\System32;{system_root};{system_root}\\System32\\Wbem"

    clean_env: dict[str, str] = {
        "SystemRoot": system_root,
        "windir": windir,
        "PATH": clean_path,
        "TEMP": str(tmp_dir),
        "TMP": str(tmp_dir),
        "USERPROFILE": str(tmp_dir),
        "APPDATA": str(tmp_dir / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(tmp_dir / "AppData" / "Local"),
        "SPECTRA_ONEDIR_ROOT": str(INSTALLED_APP),
    }

    if offline:
        # 注入无效本地代理与禁用网络，仿真完全离线环境
        clean_env["HTTP_PROXY"] = "http://127.0.0.1:99999"
        clean_env["HTTPS_PROXY"] = "http://127.0.0.1:99999"
        clean_env["GDAL_HTTP_TIMEOUT"] = "2"

    return clean_env


def test_p10_02_install_upgrade_uninstall_and_user_project_preservation() -> None:
    """验证完整的安装、升级、卸载生命周期，以及用户工程文件在升级与卸载过程中的 100% 安全保全。"""
    test_install_dir = CLEAN_BASE / "lifecycle_test" / "SpectraWorkbench"
    if test_install_dir.exists():
        import shutil

        shutil.rmtree(test_install_dir.parent, ignore_errors=True)

    # 1. 首次安装
    install_res = install_package(DIST_DIR, test_install_dir, mode="link")
    assert test_install_dir.is_dir()
    assert (test_install_dir / "SpectraWorkbench.exe").is_file()
    assert (test_install_dir / "install_manifest.json").is_file()
    assert install_res["version"] == "1.0.0"

    # 2. 模拟用户创建项目文件（在安装目录内部与独立外部工作区各放一份）
    internal_user_proj = test_install_dir / "user_projects" / "regional_analysis.spectra.json"
    internal_user_proj.parent.mkdir(parents=True, exist_ok=True)
    sample_proj_data = {
        "version": "1.0",
        "name": "用户高光谱典型矿物解译工程",
        "created_at": "2026-11-10T08:00:00Z",
        "layers": [{"source": "data/sample.tif", "name": "B1-Ramp"}],
        "notes": "这是用户在实地采集的重点研究数据，严禁被安装程序清理",
    }
    internal_user_proj.write_text(
        json.dumps(sample_proj_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    original_bytes = internal_user_proj.read_bytes()

    external_user_proj = USER_WORKSPACE / "field_survey_2026.spectra.json"
    external_user_proj.write_text(
        json.dumps(sample_proj_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    ext_bytes = external_user_proj.read_bytes()

    # 3. 模拟应用无缝升级到 v1.0.1
    upgrade_res = upgrade_package(DIST_DIR, test_install_dir, new_version="1.0.1", mode="link")
    assert upgrade_res["version"] == "1.0.1"

    # 验证升级后用户工程无损保留
    assert internal_user_proj.exists(), "安装目录内部用户工程在升级后必须保留"
    assert internal_user_proj.read_bytes() == original_bytes, "内部用户工程内容在升级后必须完全一致"
    assert external_user_proj.exists(), "外部用户工程在升级后必须保留"
    assert external_user_proj.read_bytes() == ext_bytes

    # 4. 执行应用安全卸载
    uninstall_res = uninstall_package(test_install_dir)
    assert uninstall_res["uninstalled"] is True

    # 验证应用程序二进制与运行库已彻底移除
    assert not (test_install_dir / "app").exists(), "app 运行库已清理"
    assert not (test_install_dir / "runtimes").exists(), "runtimes 引擎库已清理"
    assert not (test_install_dir / "SpectraWorkbench.exe").exists(), "GUI 启动器已清理"
    assert not (test_install_dir / "SpectraWorkbench-console.exe").exists(), "控制台启动器已清理"

    # 核心验收断言：用户项目文件依然完整保留！
    assert internal_user_proj.exists(), "【核心验收点】安装目录内部的用户项目在卸载后必须严格保全！"
    assert internal_user_proj.read_bytes() == original_bytes, "内部项目内容完好无损"
    assert external_user_proj.exists(), "外部用户项目必须保留"
    assert external_user_proj.read_bytes() == ext_bytes


def test_p10_02_clean_machine_scrubbed_env_and_no_dev_dir(tmp_path: Path) -> None:
    """验证在完全剥离外部 Python、Conda、QGIS 与开发目录的干净机环境下，自带诊断器与全引擎通过。"""
    clean_env = _get_scrubbed_clean_env(tmp_path, offline=True)

    # 验证环境变量中没有任何开发期或全局 Python 痕迹
    path_entries = clean_env["PATH"].lower().split(";")
    for entry in path_entries:
        assert "python" not in entry, f"干净 PATH 绝不包含全局 Python: {entry}"
        assert "conda" not in entry, f"干净 PATH 绝不包含 Conda: {entry}"
        assert "qgis" not in entry, f"干净 PATH 绝不包含 QGIS: {entry}"

    launcher_exe = INSTALLED_APP / "SpectraWorkbench-console.exe"
    assert launcher_exe.is_file(), f"未找到已安装控制台启动器: {launcher_exe}"

    # 运行自带 doctor 自检诊断
    cmd = [str(launcher_exe), "--doctor", "--json"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=clean_env,
        cwd=str(tmp_path),
        timeout=30,
    )

    assert proc.returncode == 0, (
        f"干净机启动 doctor 失败: stdout={proc.stdout}, stderr={proc.stderr}"
    )
    # 解析输出中的 JSON 报告
    report_data = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                report_data = json.loads(line)
                break
            except Exception:
                pass

    if report_data:
        assert report_data.get("overall_ok") is True, (
            f"Doctor 总体判定不通过: {report_data.get('errors')}"
        )
        assert report_data.get("gui_can_start") is True
        envs = report_data.get("envs", {})
        assert envs.get("ui", {}).get("status") == "ok"
        assert envs.get("gdal", {}).get("status") == "ok"
        assert envs.get("rasterio", {}).get("status") == "ok"
        assert envs.get("science", {}).get("status") == "ok"


def test_p10_02_offline_startup_and_main_gdal_processing(tmp_path: Path) -> None:
    """验证在离线、无开发目录环境下，自带 GDAL 运行时独立完成 DEM 渲染/坡度计算主要处理。"""
    clean_env = _get_scrubbed_clean_env(tmp_path, offline=True)

    gdal_py = INSTALLED_APP / "runtimes" / "gdal" / "python.exe"
    gdal_bin = INSTALLED_APP / "runtimes" / "gdal" / "bin"
    gdal_data = INSTALLED_APP / "runtimes" / "gdal" / "share" / "gdal"
    proj_data = INSTALLED_APP / "runtimes" / "gdal" / "share" / "proj"

    clean_env["PATH"] = f"{gdal_bin};{clean_env['PATH']}"
    clean_env["GDAL_DATA"] = str(gdal_data)
    clean_env["PROJ_DATA"] = str(proj_data)
    clean_env["PROJ_LIB"] = str(proj_data)

    dem_script = """
import os, sys
from osgeo import gdal, osr

# 1. 验证 CRS 数据库自动绑定
if hasattr(osr, 'SetPROJSearchPaths') and os.environ.get('PROJ_DATA'):
    osr.SetPROJSearchPaths([os.environ['PROJ_DATA']])

# 2. 生成 16x16 坡度 DEM 仿真栅格
driver = gdal.GetDriverByName('GTiff')
dem_path = 'synthetic_dem.tif'
ds = driver.Create(dem_path, 16, 16, 1, gdal.GDT_Float32)
srs = osr.SpatialReference()
srs.ImportFromEPSG(32650) # WGS 84 / UTM zone 50N
ds.SetProjection(srs.ExportToWkt())
ds.SetGeoTransform([500000.0, 30.0, 0.0, 4400000.0, 0.0, -30.0])

import numpy as np
x = np.linspace(0, 100, 16, dtype=np.float32)
y = np.linspace(0, 50, 16, dtype=np.float32)
xx, yy = np.meshgrid(x, y)
dem_data = (xx ** 1.2 + yy ** 1.1).astype(np.float32)
ds.GetRasterBand(1).WriteArray(dem_data)
ds.FlushCache()
ds = None

# 3. 执行主要地形处理：生成山体阴影 (Hillshade)
hillshade_path = 'synthetic_hillshade.tif'
opt = gdal.DEMProcessingOptions(computeEdges=True, zFactor=1.0, scale=1.0, azimuth=315.0, altitude=45.0)
res = gdal.DEMProcessing(hillshade_path, dem_path, 'hillshade', options=opt)
assert res is not None
res.FlushCache()
res = None

# 4. 校验成果
hs_ds = gdal.Open(hillshade_path)
assert hs_ds.RasterXSize == 16 and hs_ds.RasterYSize == 16
hs_arr = hs_ds.GetRasterBand(1).ReadAsArray()
assert hs_arr.min() >= 0 and hs_arr.max() <= 255
assert hs_ds.GetSpatialRef().GetAuthorityCode(None) == '32650'

print('P10_GDAL_HILLSHADE_OFFLINE_SUCCESS')
"""
    proc = subprocess.run(
        [str(gdal_py), "-c", dem_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=clean_env,
        cwd=str(tmp_path),
        timeout=25,
    )
    assert proc.returncode == 0, (
        f"GDAL 离线主要处理失败: stdout={proc.stdout}, stderr={proc.stderr}"
    )
    assert "P10_GDAL_HILLSHADE_OFFLINE_SUCCESS" in proc.stdout


def test_p10_02_offline_rasterio_processing(tmp_path: Path) -> None:
    """验证在离线、无开发目录环境下，自带 Rasterio 运行时独立完成多波段栅格读写与 NDVI 计算。"""
    clean_env = _get_scrubbed_clean_env(tmp_path, offline=True)

    rio_py = INSTALLED_APP / "runtimes" / "rasterio" / "python.exe"
    rio_sp = INSTALLED_APP / "runtimes" / "rasterio" / "Lib" / "site-packages"

    clean_env["PYTHONPATH"] = str(rio_sp)

    rio_script = """
import sys
import rasterio
import numpy as np

# 1. 写入包含 Red 与 NIR 的多波段仿真影像
out_ms = 'synthetic_multispectral.tif'
profile = {
    'driver': 'GTiff',
    'height': 16,
    'width': 16,
    'count': 4,
    'dtype': 'float32',
    'crs': 'EPSG:4326',
    'transform': rasterio.transform.from_origin(116.0, 40.0, 0.01, 0.01)
}

red_band = np.full((16, 16), 0.15, dtype=np.float32)
nir_band = np.full((16, 16), 0.45, dtype=np.float32)

with rasterio.open(out_ms, 'w', **profile) as ds:
    ds.write(red_band, 1) # Band 1: Blue
    ds.write(red_band, 2) # Band 2: Green
    ds.write(red_band, 3) # Band 3: Red
    ds.write(nir_band, 4) # Band 4: NIR

# 2. 读取并计算 NDVI
with rasterio.open(out_ms) as ds:
    red = ds.read(3)
    nir = ds.read(4)
    ndvi = (nir - red) / (nir + red)
    expected_ndvi = (0.45 - 0.15) / (0.45 + 0.15) # 0.5
    assert np.allclose(ndvi, expected_ndvi, atol=1e-5)
    assert ds.crs.to_string() == 'EPSG:4326'

print('P10_RASTERIO_NDVI_OFFLINE_SUCCESS')
"""
    proc = subprocess.run(
        [str(rio_py), "-c", rio_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=clean_env,
        cwd=str(tmp_path),
        timeout=25,
    )
    assert proc.returncode == 0, (
        f"Rasterio 离线处理失败: stdout={proc.stdout}, stderr={proc.stderr}"
    )
    assert "P10_RASTERIO_NDVI_OFFLINE_SUCCESS" in proc.stdout


def test_p10_02_offline_headless_ui_startup(tmp_path: Path) -> None:
    """验证在干净机、完全离线环境下，原生 PE 启动器在 headless 模式下顺利拉起主窗口与核心组件并安全退出。"""
    clean_env = _get_scrubbed_clean_env(tmp_path, offline=True)

    launcher_exe = INSTALLED_APP / "SpectraWorkbench-console.exe"

    cmd = [str(launcher_exe), "--headless", "--test-init"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=clean_env,
        cwd=str(tmp_path),
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"干净机离线 UI 启动失败: stdout={proc.stdout}, stderr={proc.stderr}"
    )
