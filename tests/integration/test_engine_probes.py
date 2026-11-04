"""P0-04 验收：跨进程引擎探针与环境集成测试。

使用 `@pytest.mark.integration` 标记，用于验证独立子进程中的地理引擎环境。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spectra_workbench.app.doctor import find_project_root, probe_role

pytestmark = pytest.mark.integration


def test_gdal_external_runtime_integration():
    """验证 GDAL 引擎子进程能够成功执行 GTiff 往返与坐标变换。"""
    root = find_project_root()
    probe_script = root / "environments" / "probe.py"
    qgis_python = Path(r"C:\Program Files\QGIS 3.44.15\apps\Python312\python.exe")
    qgis_bin = Path(r"C:\Program Files\QGIS 3.44.15\bin")
    qgis_gdal_data = Path(r"C:\Program Files\QGIS 3.44.15\apps\gdal\share\gdal")
    qgis_proj_data = Path(r"C:\Program Files\QGIS 3.44.15\share\proj")

    if not qgis_python.exists():
        pytest.skip("GDAL QGIS runtime not found on this machine")

    extra_env = {
        "PATH": f"{qgis_bin}",
        "GDAL_DATA": str(qgis_gdal_data),
        "PROJ_DATA": str(qgis_proj_data),
    }

    code, data, err = probe_role(qgis_python, probe_script, "gdal", extra_env=extra_env)
    assert code == 0, f"GDAL probe exited with code {code}: {err}"
    assert data["role"] == "gdal"
    assert data["packages"]["osgeo"] is not None

    gdal_info = data.get("gdal", {})
    assert gdal_info.get("gtiff_roundtrip", {}).get("ok") is True
    assert gdal_info.get("coord_transform", {}).get("ok") is True
    assert gdal_info.get("resource_dirs", {}).get("gdal_data_valid") is True
    assert gdal_info.get("resource_dirs", {}).get("proj_data_valid") is True


def test_rasterio_isolated_venv_integration():
    """验证 Rasterio 独立 venv 子进程能够成功执行 GTiff 往返。"""
    root = find_project_root()
    probe_script = root / "environments" / "probe.py"
    rio_python = root / ".venvs" / "sw-rasterio" / "Scripts" / "python.exe"

    if not rio_python.exists():
        pytest.skip("Rasterio venv not found (.venvs/sw-rasterio)")

    code, data, err = probe_role(rio_python, probe_script, "rasterio")
    assert code == 0, f"Rasterio probe exited with code {code}: {err}"
    assert data["role"] == "rasterio"
    assert data["packages"]["rasterio"] is not None

    rio_info = data.get("rasterio", {})
    assert rio_info.get("gtiff_roundtrip", {}).get("ok") is True


def test_science_isolated_venv_integration():
    """验证 Science 独立 venv 子进程能够成功执行 FFT 往返变换。"""
    root = find_project_root()
    probe_script = root / "environments" / "probe.py"
    sci_python = root / ".venvs" / "sw-science" / "Scripts" / "python.exe"

    if not sci_python.exists():
        pytest.skip("Science venv not found (.venvs/sw-science)")

    code, data, err = probe_role(sci_python, probe_script, "science")
    assert code == 0, f"Science probe exited with code {code}: {err}"
    assert data["role"] == "science"
    assert data["packages"]["numpy"] is not None
    assert data["packages"]["scipy"] is not None

    sci_info = data.get("science", {})
    assert sci_info.get("fft_roundtrip", {}).get("ok") is True
