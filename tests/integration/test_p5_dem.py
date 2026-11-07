"""P5-02 DEM terrain analysis: numerical validation on an analytic plane."""

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


def _make_plane(gdal_rt: dict[str, Any], path: Path, size: int = 8) -> Path:
    """z = 2*col + 3*row on a 1x1 degree-ish plane in EPSG:32650."""
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
n = {size}
rows, cols = np.mgrid[0:n, 0:n]
z = (2.0 * cols + 3.0 * rows).astype(np.float32)
ds = gdal.GetDriverByName("GTiff").Create({str(path)!r}, n, n, 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetProjection(srs.ExportToWkt())
ds.SetGeoTransform((500000.0, 1.0, 0.0, 3200000.0, 0.0, -1.0))
ds.GetRasterBand(1).WriteArray(z)
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
        task_id=f"p502-{uuid.uuid4().hex[:10]}",
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
                dataset_id="dem-in",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p502-dem", project_root=str(ROOT))
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
b = ds.GetRasterBand(1)
print(json.dumps({{
    "driver": ds.GetDriver().ShortName,
    "w": ds.RasterXSize, "h": ds.RasterYSize, "bands": ds.RasterCount,
    "dtype": gdal.GetDataTypeName(b.DataType),
    "nodata": b.GetNoDataValue(),
    "crs": bool(ds.GetProjectionRef()),
    "arr": b.ReadAsArray().tolist(),
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


def test_dem_slope_and_aspect_match_analytic_plane(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_plane(gdal_rt, tmp_path / "plane.tif")
    expected_slope = math.degrees(math.atan(math.sqrt(13.0)))

    slope = _run(qapp, "gdal:dem", {"format": "GTiff", "processing": "slope"}, src, tmp_path / "slope.tif")
    assert slope.status is TaskStatus.succeeded, slope.error
    slope_info = _inspect(gdal_rt, tmp_path / "slope.tif")
    assert slope_info["dtype"] == "Float32" and slope_info["bands"] == 1
    assert slope_info["crs"] is True
    assert slope_info["arr"][4][4] == pytest.approx(expected_slope, abs=1e-4)
    assert slope_info["arr"][4][1] == pytest.approx(expected_slope, abs=1e-4)

    aspect = _run(qapp, "gdal:dem", {"format": "GTiff", "processing": "aspect"}, src, tmp_path / "aspect.tif")
    assert aspect.status is TaskStatus.succeeded, aspect.error
    aspect_info = _inspect(gdal_rt, tmp_path / "aspect.tif")
    assert aspect_info["arr"][4][4] == pytest.approx(326.3099365234375, abs=1e-4)

    # scale must really participate: halving the vertical scale doubles the gradient.
    expected_scaled = math.degrees(math.atan(math.sqrt(13.0) / 0.5))
    scaled = _run(
        qapp,
        "gdal:dem",
        {"format": "GTiff", "processing": "slope", "scale": 0.5},
        src,
        tmp_path / "slope_scale.tif",
    )
    assert scaled.status is TaskStatus.succeeded, scaled.error
    assert _inspect(gdal_rt, tmp_path / "slope_scale.tif")["arr"][4][4] == pytest.approx(
        expected_scaled, abs=1e-4
    )


def test_dem_hillshade_and_rejections(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_plane(gdal_rt, tmp_path / "plane.tif")

    hs = _run(
        qapp,
        "gdal:dem",
        {"format": "GTiff", "processing": "hillshade"},
        src,
        tmp_path / "hillshade.tif",
    )
    assert hs.status is TaskStatus.succeeded, hs.error
    hs_info = _inspect(gdal_rt, tmp_path / "hillshade.tif")
    assert hs_info["arr"][4][4] == pytest.approx(219.0, abs=1e-6)

    # zFactor is a hillshade-only option; doubling elevations changes the shading.
    hs2 = _run(
        qapp,
        "gdal:dem",
        {"format": "GTiff", "processing": "hillshade", "zFactor": 2.0},
        src,
        tmp_path / "hillshade_z2.tif",
    )
    assert hs2.status is TaskStatus.succeeded, hs2.error
    assert _inspect(gdal_rt, tmp_path / "hillshade_z2.tif")["arr"][4][4] == pytest.approx(
        200.0, abs=1e-6
    )

    color_relief = _run(
        qapp,
        "gdal:dem",
        {"format": "GTiff", "processing": "color-relief"},
        src,
        tmp_path / "color.tif",
    )
    assert color_relief.status is TaskStatus.failed
    assert color_relief.error and color_relief.error["code"] == "invalid_parameters"
    assert "颜色表" in color_relief.error["message"]
    assert not (tmp_path / "color.tif").exists()

    png = _run(
        qapp,
        "gdal:dem",
        {"format": "PNG", "processing": "slope"},
        src,
        tmp_path / "slope.png",
    )
    assert png.status is TaskStatus.failed
    assert png.error and png.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "slope.png").exists()
