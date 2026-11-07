"""P5-02 grid interpolation: analytic plane from scattered points."""

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


def _make_points(gdal_rt: dict[str, Any], path: Path) -> Path:
    """Points carrying value = 2*x + 3*y on a 0..4 x 0..2 plane."""
    script = f"""
from osgeo import ogr, osr
driver = ogr.GetDriverByName("GeoJSON")
ds = driver.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
layer = ds.CreateLayer("points", srs, ogr.wkbPoint)
layer.CreateField(ogr.FieldDefn("value", ogr.OFTReal))
for x, y in ((0, 0), (4, 0), (0, 2), (4, 2), (2, 1)):
    feat = ogr.Feature(layer.GetLayerDefn())
    geom = ogr.Geometry(ogr.wkbPoint)
    geom.AddPoint_2D(x, y)
    feat.SetGeometry(geom)
    feat.SetField("value", 2 * x + 3 * y)
    layer.CreateFeature(feat)
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
        task_id=f"p502g-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:grid",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(target.parent),
        inputs={
            "input": DatasetRef(
                dataset_id="pts-in",
                kind=DatasetKind.vector,
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p502-grid", project_root=str(ROOT))
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
print(json.dumps({{"w": ds.RasterXSize, "h": ds.RasterYSize,
                  "dtype": gdal.GetDataTypeName(b.DataType),
                  "arr": b.ReadAsArray().tolist()}}))
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


def test_grid_linear_reconstructs_analytic_plane(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_points(gdal_rt, tmp_path / "points.geojson")
    out = tmp_path / "grid.tif"
    result = _run(
        qapp,
        {
            "format": "GTiff",
            "algorithm": "linear",
            "zField": "value",
            "width": 4,
            "height": 2,
            "outputBounds": [0.0, 0.0, 4.0, 2.0],
        },
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["w"] == 4 and info["h"] == 2
    assert info["dtype"] == "Float64"
    assert info["arr"][0] == pytest.approx([5.5, 7.5, 9.5, 11.5], abs=1e-6)
    assert info["arr"][1] == pytest.approx([2.5, 4.5, 6.5, 8.5], abs=1e-6)


def test_grid_rejects_missing_size_and_png_default(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_points(gdal_rt, tmp_path / "points.geojson")
    no_size = _run(
        qapp,
        {"format": "GTiff", "algorithm": "linear", "zField": "value"},
        src,
        tmp_path / "no_size.tif",
    )
    assert no_size.status is TaskStatus.failed
    assert no_size.error and no_size.error["code"] == "invalid_parameters"
    assert not (tmp_path / "no_size.tif").exists()

    png = _run(
        qapp,
        {
            "format": "PNG",
            "algorithm": "linear",
            "zField": "value",
            "width": 4,
            "height": 2,
            "outputBounds": [0.0, 0.0, 4.0, 2.0],
        },
        src,
        tmp_path / "grid.png",
    )
    assert png.status is TaskStatus.failed
    assert png.error and png.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "grid.png").exists()
