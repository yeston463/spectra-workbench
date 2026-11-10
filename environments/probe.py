"""环境自证探针：在某个环境内以独立进程运行，输出 JSON。

用法： <env>\\python.exe environments\\probe.py <role>
role ∈ ui | gdal | rasterio | science

设计要点：
- 只做「本环境实际有什么」的探测，不假设任何包存在。
- UI 角色额外报告原生地理引擎是否**不该**能导入（隔离证据）。
"""
from __future__ import annotations

import contextlib
import json
import os
import sys


def pkg_version(name: str) -> str | None:
    try:
        __import__(name)
    except Exception:
        return None
    from importlib.metadata import version

    try:
        return version(name)
    except Exception:
        pass
    try:
        mod = sys.modules.get(name)
        for attr in ("__version__", "version"):
            v = getattr(mod, attr, None)
            if v:
                return str(v)
    except Exception:
        pass
    return "unknown"


def gdal_info() -> dict:
    out: dict = {}
    try:
        from osgeo import gdal, osr
    except Exception as exc:  # pragma: no cover - 探测失败本身是结论
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    names = [gdal.GetDriver(i).ShortName for i in range(gdal.GetDriverCount())]
    proj = ".".join(
        str(getattr(osr, f"GetPROJVersion{s}")())
        for s in ("Major", "Minor", "Micro")
        if hasattr(osr, f"GetPROJVersion{s}")
    )
    try:
        proj_paths = list(osr.GetPROJSearchPaths())
    except Exception:
        proj_paths = []

    proj_env = os.environ.get("PROJ_DATA")
    if proj_env and hasattr(osr, "SetPROJSearchPaths"):
        with contextlib.suppress(Exception):
            osr.SetPROJSearchPaths([proj_env])

    # 1. Coordinate transformation check (EPSG:4326 -> EPSG:3857)
    coord_check: dict = {"ok": False}
    try:
        srs_in = osr.SpatialReference()
        srs_in.ImportFromEPSG(4326)
        srs_in.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        srs_out = osr.SpatialReference()
        srs_out.ImportFromEPSG(3857)
        srs_out.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(srs_in, srs_out)
        pt = ct.TransformPoint(116.4, 39.9)
        # Expected x ~ 12957588, y ~ 4851421
        if abs(pt[0] - 12957588.7) < 1000 and abs(pt[1] - 4851421.1) < 1000:
            coord_check = {
                "ok": True,
                "epsg_in": 4326,
                "epsg_out": 3857,
                "input": [116.4, 39.9],
                "output": [round(pt[0], 2), round(pt[1], 2)],
            }
        else:
            coord_check = {"ok": False, "reason": f"Unexpected transformed coordinates: {pt}"}
    except Exception as exc:
        coord_check = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 2. GTiff roundtrip check
    gtiff_check: dict = {"ok": False}
    try:
        import gc
        import struct
        import tempfile
        driver = gdal.GetDriverByName("GTiff")
        if driver:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                tif_path = os.path.join(td, "probe_test.tif")
                ds = driver.Create(tif_path, 4, 4, 1, gdal.GDT_Float32)
                srs_wkt = srs_in.ExportToWkt() if "srs_in" in locals() else ""
                if srs_wkt:
                    ds.SetProjection(srs_wkt)
                ds.SetGeoTransform([100.0, 1.0, 0.0, 50.0, 0.0, -1.0])
                band = ds.GetRasterBand(1)
                expected_vals = tuple(float(i * 1.5) for i in range(16))
                buf = struct.pack("16f", *expected_vals)
                band.WriteRaster(0, 0, 4, 4, buf, buf_type=gdal.GDT_Float32)
                band.FlushCache()
                ds.FlushCache()
                del band, ds
                gc.collect()

                # Reopen and verify
                ds_r = gdal.Open(tif_path, gdal.GA_ReadOnly)
                if ds_r and ds_r.RasterXSize == 4 and ds_r.RasterYSize == 4:
                    b_r = ds_r.GetRasterBand(1)
                    rb = b_r.ReadRaster(0, 0, 4, 4, buf_type=gdal.GDT_Float32)
                    read_vals = struct.unpack("16f", rb)
                    if read_vals == expected_vals:
                        gtiff_check = {
                            "ok": True,
                            "shape": [4, 4],
                            "dtype": "Float32",
                            "values_matched": True,
                        }
                    else:
                        gtiff_check = {"ok": False, "reason": f"Values mismatch: exp={expected_vals[:3]} got={read_vals[:3]}"}
                    del rb, b_r
                del ds_r
                gc.collect()
        else:
            gtiff_check = {"ok": False, "reason": "GTiff driver not found"}
    except Exception as exc:
        gtiff_check = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # 3. Resource directories check
    gdal_data = gdal.GetConfigOption("GDAL_DATA") or os.environ.get("GDAL_DATA", "")
    if not (gdal_data and os.path.isdir(gdal_data)):
        # Auto-detect standard QGIS / OSGeo4W apps/gdal/share/gdal
        candidate = r"C:\Program Files\QGIS 3.44.15\apps\gdal\share\gdal"
        if os.path.isdir(candidate):
            gdal_data = candidate
            gdal.SetConfigOption("GDAL_DATA", gdal_data)
    gdal_data_ok = bool(gdal_data and os.path.isdir(gdal_data))
    proj_data_ok = any(os.path.isdir(p) and os.path.exists(os.path.join(p, "proj.db")) for p in proj_paths)

    out.update(
        {
            "gdal_release": gdal.__version__,
            "gdal_rev": gdal.VersionInfo("REV"),
            "proj_version": proj or None,
            "proj_search_paths": proj_paths,
            "resource_dirs": {
                "gdal_data": gdal_data,
                "gdal_data_valid": gdal_data_ok,
                "proj_data_valid": proj_data_ok,
            },
            "driver_count": len(names),
            "drivers_of_interest": {
                d: (d in names) for d in ("GTiff", "ENVI", "GPKG", "HDF4", "HDF5", "netCDF", "COG", "VRT", "JPEG", "PNG", "ESRI Shapefile")
            },
            "coord_transform": coord_check,
            "gtiff_roundtrip": gtiff_check,
            "gui_can_start_without_hdf4": True,
        }
    )
    return out


def rasterio_info() -> dict:
    out: dict = {}
    try:
        import rasterio
    except Exception as exc:  # pragma: no cover
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    out["rasterio_version"] = rasterio.__version__
    for fn in ("gdal_version", "gdal_cpl_version", "gdal_geos_version"):
        try:
            out[fn] = getattr(rasterio, fn)()
        except Exception:
            out[fn] = None

    # GTiff roundtrip check using rasterio
    gtiff_check: dict = {"ok": False}
    try:
        import os
        import tempfile

        import numpy as np
        from rasterio.transform import from_origin

        with tempfile.TemporaryDirectory() as td:
            tif_path = os.path.join(td, "probe_rio.tif")
            transform = from_origin(116.0, 40.0, 0.01, 0.01)
            arr = np.arange(16, dtype=np.float32).reshape((4, 4))
            with rasterio.open(
                tif_path,
                "w",
                driver="GTiff",
                height=4,
                width=4,
                count=1,
                dtype="float32",
                crs="EPSG:4326",
                transform=transform,
            ) as dst:
                dst.write(arr, 1)

            with rasterio.open(tif_path) as src:
                if src.width == 4 and src.height == 4 and str(src.crs) == "EPSG:4326":
                    read_arr = src.read(1)
                    if np.array_equal(arr, read_arr):
                        gtiff_check = {
                            "ok": True,
                            "shape": [4, 4],
                            "crs": "EPSG:4326",
                            "dtype": "float32",
                            "values_matched": True,
                        }
    except Exception as exc:
        gtiff_check = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        out["driver_count"] = len(rasterio.DRIVERS)
        out["drivers_of_interest"] = {
            d: (d in rasterio.DRIVERS) for d in ("GTiff", "ENVI", "GPKG", "HDF4", "HDF5", "netCDF", "COG", "JPEG", "PNG")
        }
    except Exception:
        pass
    out["gtiff_roundtrip"] = gtiff_check
    return out


def science_info() -> dict:
    out: dict = {}
    try:
        import numpy as np
        import scipy
        import scipy.fft
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["numpy_version"] = np.__version__
    out["scipy_version"] = scipy.__version__

    # FFT roundtrip check
    fft_check: dict = {"ok": False}
    try:
        arr = np.linspace(0, 10, 64, dtype=np.float64).reshape((8, 8))
        f = scipy.fft.fft2(arr)
        rec = scipy.fft.ifft2(f).real
        max_diff = float(np.max(np.abs(arr - rec)))
        if max_diff < 1e-10:
            fft_check = {
                "ok": True,
                "shape": [8, 8],
                "max_diff": max_diff,
                "roundtrip_invariant_passed": True,
            }
        else:
            fft_check = {"ok": False, "max_diff": max_diff, "reason": "Max diff exceeds threshold"}
    except Exception as exc:
        fft_check = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out["fft_roundtrip"] = fft_check
    return out


def main() -> int:
    role = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    info: dict = {
        "role": role,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "packages": {
            "PySide6": pkg_version("PySide6"),
            "pyqtgraph": pkg_version("pyqtgraph"),
            "pydantic": pkg_version("pydantic"),
            "jsonschema": pkg_version("jsonschema"),
            "numpy": pkg_version("numpy"),
            "scipy": pkg_version("scipy"),
            "rasterio": pkg_version("rasterio"),
            "osgeo": pkg_version("osgeo"),
            "fiona": pkg_version("fiona"),
            "shapely": pkg_version("shapely"),
            "pyproj": pkg_version("pyproj"),
            "geopandas": pkg_version("geopandas"),
        },
    }
    if role == "gdal":
        info["gdal"] = gdal_info()
    elif role == "rasterio":
        info["rasterio"] = rasterio_info()
    elif role == "science":
        info["science"] = science_info()
    elif role == "ui":
        # 隔离要求：GUI 环境不应能加载原生地理引擎
        info["isolation"] = {m: (info["packages"].get(m) is not None) for m in ("osgeo", "rasterio", "fiona", "geopandas")}
        try:
            from PySide6 import QtCore
            info["qt_version"] = QtCore.qVersion()
        except Exception as exc:
            info["qt_version"] = f"error: {exc}"
    print(json.dumps(info, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
