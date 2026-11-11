"""P6-04 栅格差值端到端：同网格严格比较、统计与拒绝路径。"""

from __future__ import annotations

import json
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
    values: np.ndarray,
    *,
    gt: tuple[float, ...] = GT,
    epsg: int = 32650,
    nodata: float = NODATA,
) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = np.array({values.tolist()!r}, dtype=np.float32)
if values.ndim == 2:
    values = values[None, ...]
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, values.shape[2], values.shape[1], values.shape[0], gdal.GDT_Float32)
ds.SetGeoTransform({gt!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG({epsg})
ds.SetSpatialRef(srs)
for i in range(values.shape[0]):
    ds.GetRasterBand(i + 1).WriteArray(values[i])
    ds.GetRasterBand(i + 1).SetNoDataValue({nodata})
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
    parameters: dict[str, Any],
    source: Path,
    target: Path,
    *,
    timeout: float = 60.0,
) -> Any:
    spec = TaskSpec(
        task_id=f"p604diff-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:rasterdiff",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p604-diff", project_root=str(ROOT))
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
    bands.append({{"nodata": b.GetNoDataValue(), "arr": b.ReadAsArray().tolist()}})
print(json.dumps({{"bands": ds.RasterCount, "band_list": bands}}))
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


def _sidecar(out: Path) -> dict[str, Any]:
    path = out.with_name(out.name + ".rasterdiff.json")
    assert path.exists(), f"缺少 sidecar: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_difference_and_absolute_with_stats(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    a = np.array(
        [
            [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]],
            [[10.0, 20.0, 30.0], [40.0, 50.0, 60.0]],
        ]
    )
    b = np.array(
        [
            [[0.5, 2.0, 1.0], [8.0, 3.0, 6.0]],
            [[5.0, 25.0, 20.0], [30.0, 60.0, 50.0]],
        ]
    )
    src = _make_raster(gdal_rt, tmp_path / "a.tif", a)
    ref = _make_raster(gdal_rt, tmp_path / "b.tif", b)

    for mode, expected in (
        ("difference", a - b),
        ("absolute", np.abs(a - b)),
    ):
        out = tmp_path / f"diff_{mode}.tif"
        result = _run(qapp, {"diffMode": mode, "referenceFile": str(ref)}, src, out)
        assert result.status is TaskStatus.succeeded, result.error
        info = _inspect(gdal_rt, out)
        assert info["bands"] == 2
        actual = np.asarray([band["arr"] for band in info["band_list"]])
        assert np.allclose(actual, expected, atol=1e-6)
        assert all(band["nodata"] == NODATA for band in info["band_list"])

        payload = _sidecar(out)
        assert payload["mode"] == mode
        assert len(payload["stats"]) == 2
        for index, stat in enumerate(payload["stats"]):
            values = expected[index].ravel()
            assert stat["n_valid"] == values.size
            assert stat["min"] == pytest.approx(float(values.min()))
            assert stat["max"] == pytest.approx(float(values.max()))
            assert stat["mean"] == pytest.approx(float(values.mean()))


def test_nodata_is_excluded_from_stats(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    a = np.array([[[1.0, NODATA], [3.0, 4.0]]])
    b = np.array([[[0.0, 1.0], [2.0, 3.0]]])
    src = _make_raster(gdal_rt, tmp_path / "a_nodata.tif", a)
    ref = _make_raster(gdal_rt, tmp_path / "b_nodata.tif", b)
    out = tmp_path / "diff_nodata.tif"
    result = _run(qapp, {"diffMode": "difference", "referenceFile": str(ref)}, src, out)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["band_list"][0]["arr"][0][1] == NODATA
    payload = _sidecar(out)
    assert payload["stats"][0]["n_valid"] == 3


def test_grid_band_and_format_rejections(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    a = np.ones((1, 2, 2))
    src = _make_raster(gdal_rt, tmp_path / "a_rej.tif", a)
    out = tmp_path / "reject.tif"

    shifted = _make_raster(
        gdal_rt,
        tmp_path / "b_shifted.tif",
        a,
        gt=(101.0, 10.0, 0.0, 200.0, 0.0, -10.0),
    )
    mismatch = _run(qapp, {"diffMode": "difference", "referenceFile": str(shifted)}, src, out)
    assert mismatch.status is TaskStatus.failed
    assert mismatch.error and mismatch.error["code"] == "rasterdiff_error"
    assert "GeoTransform" in mismatch.error["message"]
    assert not out.exists()

    one_band = _make_raster(gdal_rt, tmp_path / "b_oneband.tif", np.ones((1, 2, 2)))
    src_two = _make_raster(gdal_rt, tmp_path / "a_two.tif", np.ones((2, 2, 2)))
    band_mismatch = _run(
        qapp, {"diffMode": "difference", "referenceFile": str(one_band)}, src_two, out
    )
    assert band_mismatch.status is TaskStatus.failed
    assert band_mismatch.error and band_mismatch.error["code"] == "rasterdiff_error"
    assert "波段数" in band_mismatch.error["message"]
    assert not out.exists()

    png = _run(
        qapp,
        {"diffMode": "difference", "referenceFile": str(one_band), "format": "PNG"},
        src,
        tmp_path / "diff.png",
    )
    assert png.status is TaskStatus.failed
    assert png.error and png.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "diff.png").exists()

    missing = _run(qapp, {"diffMode": "difference"}, src, out)
    assert missing.status is TaskStatus.failed
    assert missing.error and missing.error["code"] == "invalid_parameters"
    assert not out.exists()
