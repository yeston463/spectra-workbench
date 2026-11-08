"""P6-06 辐射定标 / DOS1 / 多时相去云端到端验收。"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
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

pytestmark = [pytest.mark.integration, pytest.mark.engine]
ROOT = find_project_root()
NODATA = -9999.0
GT = (100.0, 10.0, 0.0, 200.0, 0.0, -10.0)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


@pytest.fixture(scope="module")
def gdal_rt() -> dict[str, Any]:
    return resolve_gdal_runtime(ROOT)


def _make_raster(
    gdal_rt: dict[str, Any],
    path: Path,
    values: list[np.ndarray] | np.ndarray,
    *,
    gt: tuple[float, ...] = GT,
    nodata: float = NODATA,
    set_nodata: bool = True,
) -> Path:
    if isinstance(values, np.ndarray):
        values = [values]
    flat = [np.asarray(v, dtype=np.float64).tolist() for v in values]
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = {flat!r}
drv = gdal.GetDriverByName("GTiff")
rows = len(values[0])
cols = len(values[0][0])
ds = drv.Create({str(path)!r}, cols, rows, len(values), gdal.GDT_Float32)
ds.SetGeoTransform({gt!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
for i in range(len(values)):
    band = ds.GetRasterBand(i + 1)
    band.WriteArray(np.array(values[i]))
    if {set_nodata}:
        band.SetNoDataValue({nodata})
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
    assert proc.returncode == 0, proc.stderr
    return path


def _run(
    qapp: QApplication,
    capability_id: str,
    parameters: dict[str, Any],
    source: Path,
    target: Path,
    *,
    timeout: float = 60.0,
) -> Any:
    spec = TaskSpec(
        task_id=f"p606-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(target.parent),
        inputs={
            "input": DatasetRef(
                dataset_id="ras-in",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p606", project_root=str(ROOT))
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda code, msg: errors.append((code, msg)))
    try:
        client.start(timeout=25.0)
        client.submit_task_async(spec, timeout=timeout)
        deadline = time.monotonic() + timeout
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert not errors, errors
        assert results, "任务未产生结果"
        return results[0]
    finally:
        client.stop(timeout=5.0)


def _inspect(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = f"""
import json
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.Open({str(path)!r})
assert ds is not None
bands = []
for i in range(1, ds.RasterCount + 1):
    b = ds.GetRasterBand(i)
    bands.append({{
        "dtype": gdal.GetDataTypeName(b.DataType),
        "nodata": b.GetNoDataValue(),
        "description": b.GetDescription(),
        "arr": b.ReadAsArray().tolist(),
    }})
print(json.dumps({{
    "w": ds.RasterXSize, "h": ds.RasterYSize, "bands": ds.RasterCount,
    "metadata": ds.GetMetadata() or {{}},
    "band_list": bands,
}}))
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def _sidecar(out: Path, suffix: str) -> dict[str, Any]:
    path = out.with_name(out.name + suffix)
    assert path.exists(), f"缺少 sidecar: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_calibrate_physical_and_toa_reflectance(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    dn = np.array([[0.0, 25.0], [50.0, 100.0]])
    src = _make_raster(gdal_rt, tmp_path / "calib_src.tif", [dn])
    out = tmp_path / "calibrated.tif"

    # physical：physical = DN*gain + offset（DN=0 是合法值，不是 NoData）
    gain, offset = 0.1, -2.0
    result = _run(
        qapp,
        "gdal:calibrate",
        {
            "calibrateMode": "physical",
            "calibrateGains": [gain],
            "calibrateOffsets": [offset],
        },
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["bands"] == 1
    expected = dn * gain + offset
    assert np.allclose(info["band_list"][0]["arr"], expected, atol=1e-6)

    payload = _sidecar(out, ".calibrate.json")
    assert payload["spec_kind"] == "linear_gain_offset"
    assert payload["formula"] == "physical = DN * gain + offset"
    assert payload["gains"] == [gain]
    assert payload["offsets"] == [offset]

    # toa_reflectance：rho = pi*L*d^2/(esun*cos(theta_z))，d=1
    out2 = tmp_path / "calibrated_toa.tif"
    result2 = _run(
        qapp,
        "gdal:calibrate",
        {
            "calibrateMode": "toa_reflectance",
            "calibrateEsun": [1500.0],
            "sunZenAngle": 30.0,
        },
        src,
        out2,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    info2 = _inspect(gdal_rt, out2)
    cos_theta_z = math.cos(math.radians(30.0))
    factor = math.pi / (1500.0 * cos_theta_z)
    expected2 = factor * dn
    assert np.allclose(info2["band_list"][0]["arr"], expected2, atol=1e-6)
    payload2 = _sidecar(out2, ".calibrate.json")
    assert payload2["spec_kind"] == "toa_reflectance"
    assert payload2["sun_zen_angle"] == 30.0
    assert payload2["esun"] == [1500.0]


def test_dos1_dark_object_subtraction(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    dn = np.array([[0.0, 10.0], [50.0, 100.0]])
    src = _make_raster(gdal_rt, tmp_path / "dos_src.tif", [dn])
    out = tmp_path / "dos_out.tif"
    haze_dn, esun, sun_zen = 2.0, 1500.0, 30.0
    gain, offset, d = 0.1, -2.0, 1.0

    result = _run(
        qapp,
        "gdal:dos1",
        {
            "hazeDN": haze_dn,
            "dosEsun": esun,
            "sunZenAngle": sun_zen,
            "dosGain": gain,
            "dosOffset": offset,
            "earthSunDistance": d,
        },
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    # 独立推导：rho = pi*(L - L1haze)*d^2 / (ESUN*cos(theta_z))，其中
    # L1haze = gain*hazeDN + offset - 0.01*ESUN*cos(theta_z)/(pi*d^2)
    cos_theta_z = math.cos(math.radians(sun_zen))
    numerator = math.pi * (d**2) / (esun * cos_theta_z)
    one_percent = 0.01 * esun * cos_theta_z / (math.pi * d**2)
    L = gain * dn + offset
    L1haze = gain * haze_dn + offset - one_percent
    expected = numerator * (L - L1haze)
    actual = np.asarray(info["band_list"][0]["arr"], dtype=np.float64)
    assert np.allclose(actual, expected, atol=1e-6)

    payload = _sidecar(out, ".dos1.json")
    assert payload["atmos_method"] == "chavez_dos1_1988"
    assert payload["haze_dn"] == haze_dn
    assert payload["esun"] == esun
    assert payload["sun_zen_angle_deg"] == sun_zen
    assert payload["one_percent_radiance"] == pytest.approx(one_percent, rel=1e-9)
    assert payload["n_valid"] == 4  # 输入本身无 NoData
    assert "Chavez" in payload["citation"]


def test_cloudreplace_primary_secondary_and_dilation(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    primary = [
        np.array([[10.0, 10.0], [10.0, 10.0]]),
        np.array([[20.0, 20.0], [20.0, 20.0]]),
    ]
    secondary = [
        np.array([[5.0, 5.0], [5.0, 5.0]]),
        np.array([[15.0, 15.0], [15.0, 15.0]]),
    ]
    pr = _make_raster(gdal_rt, tmp_path / "primary.tif", primary)
    se = _make_raster(gdal_rt, tmp_path / "secondary.tif", secondary)
    mask = np.array([[0.0, 1.0], [0.0, 0.0]])
    msk = _make_raster(gdal_rt, tmp_path / "mask.tif", [mask], nodata=0.0)
    out = tmp_path / "cloud_free.tif"

    result = _run(
        qapp,
        "gdal:cloudreplace",
        {
            "secondaryFile": str(se),
            "cloudMaskFile": str(msk),
            "primaryTime": "2026-05-01",
            "secondaryTime": "2026-05-29",
            "maskDilatePixels": 0,
        },
        pr,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    actual = np.asarray([band["arr"] for band in info["band_list"]])
    expected = np.where(mask == 1, secondary, primary)
    assert np.allclose(actual, expected, atol=1e-6)
    payload = _sidecar(out, ".cloudreplace.json")
    assert payload["primary_time"] == "2026-05-01"
    assert payload["secondary_time"] == "2026-05-29"
    assert payload["n_cloud"] == 1
    assert payload["n_clean"] == 3
    assert payload["pixel_source_rule"].startswith("clean: primary")

    out2 = tmp_path / "cloud_free_dilate.tif"
    result2 = _run(
        qapp,
        "gdal:cloudreplace",
        {
            "secondaryFile": str(se),
            "cloudMaskFile": str(msk),
            "maskDilatePixels": 1,
        },
        pr,
        out2,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    payload2 = _sidecar(out2, ".cloudreplace.json")
    assert payload2["n_cloud"] == 4  # 3x3 全膨胀覆盖整幅
    info2 = _inspect(gdal_rt, out2)
    actual2 = np.asarray([band["arr"] for band in info2["band_list"]])
    assert np.allclose(actual2, secondary, atol=1e-6)


def test_radiometric_and_cloud_rejections(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    dn = np.array([[0.0, 10.0], [50.0, 100.0]])
    src = _make_raster(gdal_rt, tmp_path / "rej_src.tif", [dn])
    out = tmp_path / "reject.tif"

    # calibrate：physical 缺 gains
    missing = _run(qapp, "gdal:calibrate", {"calibrateMode": "physical"}, src, out)
    assert missing.status is TaskStatus.failed
    assert missing.error and missing.error["code"] == "calibrate_error"
    assert not out.exists()

    # calibrate：esun 数量不匹配
    out2 = tmp_path / "reject2.tif"
    bad_esun = _run(
        qapp,
        "gdal:calibrate",
        {
            "calibrateMode": "toa_reflectance",
            "calibrateEsun": [1500.0, 1800.0],
            "sunZenAngle": 30.0,
        },
        src,
        out2,
    )
    assert bad_esun.status is TaskStatus.failed
    assert bad_esun.error and bad_esun.error["code"] == "invalid_parameters"
    assert not out2.exists()

    # dos1：缺必填参数
    out3 = tmp_path / "reject3.tif"
    missing_dos = _run(qapp, "gdal:dos1", {"dosBand": 1}, src, out3)
    assert missing_dos.status is TaskStatus.failed
    assert missing_dos.error and missing_dos.error["code"] == "invalid_parameters"
    assert not out3.exists()

    # cloudreplace：缺必填参数
    out4 = tmp_path / "reject4.tif"
    missing_cloud = _run(qapp, "gdal:cloudreplace", {}, src, out4)
    assert missing_cloud.status is TaskStatus.failed
    assert missing_cloud.error and missing_cloud.error["code"] == "invalid_parameters"
    assert not out4.exists()

    # cloudreplace：maskDilatePixels=0 是合法（不膨胀）
    out5 = tmp_path / "reject5.tif"
    secondary_ok = np.array([[9.0, 9.0], [9.0, 9.0]])
    sec_ok = _make_raster(gdal_rt, tmp_path / "sec_ok.tif", [secondary_ok])
    mask_ok = np.array([[0.0, 0.0], [0.0, 0.0]])
    msk_ok = _make_raster(gdal_rt, tmp_path / "msk_ok.tif", [mask_ok], nodata=0.0)
    keep = _run(
        qapp,
        "gdal:cloudreplace",
        {"secondaryFile": str(sec_ok), "cloudMaskFile": str(msk_ok), "maskDilatePixels": 0},
        src,
        out5,
    )
    assert keep.status is TaskStatus.succeeded, keep.error
    info5 = _inspect(gdal_rt, out5)
    assert np.allclose(info5["band_list"][0]["arr"], dn)
