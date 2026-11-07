"""P5-02 rasterize: known rectangles and attribute burn values."""

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


def _make_rectangles(gdal_rt: dict[str, Any], path: Path) -> Path:
    """Two 2x2 rectangles: x 0..2 value 3, x 2..4 value 9."""
    script = f"""
from osgeo import ogr, osr
driver = ogr.GetDriverByName("GeoJSON")
ds = driver.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
layer = ds.CreateLayer("rect", srs, ogr.wkbPolygon)
layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))
for x0, x1, value in ((0, 2, 3), (2, 4, 9)):
    feat = ogr.Feature(layer.GetLayerDefn())
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, 0), (x1, 0), (x1, 2), (x0, 2), (x0, 0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    feat.SetGeometry(poly)
    feat.SetField("value", value)
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
        task_id=f"p502r-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:rasterize",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(target.parent),
        inputs={
            "input": DatasetRef(
                dataset_id="vec-in",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p502-rasterize", project_root=str(ROOT))
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


def test_rasterize_burn_value_on_known_rectangles(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_rectangles(gdal_rt, tmp_path / "rect.geojson")
    out = tmp_path / "burn.tif"
    result = _run(
        qapp,
        {"format": "GTiff", "burnValues": [7], "xRes": 1.0, "yRes": 1.0},
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["w"] == 4 and info["h"] == 2
    assert info["crs"] is True
    assert info["arr"] == [[7.0] * 4, [7.0] * 4]


def test_rasterize_attribute_field_and_all_touched(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_rectangles(gdal_rt, tmp_path / "rect.geojson")
    out = tmp_path / "attr.tif"
    result = _run(
        qapp,
        {"format": "GTiff", "attribute": "value", "xRes": 1.0, "yRes": 1.0},
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["arr"] == [[3.0, 3.0, 9.0, 9.0], [3.0, 3.0, 9.0, 9.0]]

    touched = tmp_path / "touched.tif"
    result2 = _run(
        qapp,
        {
            "format": "GTiff",
            "burnValues": [7],
            "xRes": 1.0,
            "yRes": 1.0,
            "allTouched": True,
        },
        src,
        touched,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    assert _inspect(gdal_rt, touched)["arr"] == [[7.0] * 4, [7.0] * 4]


def test_rasterize_rejects_conflicts_and_missing_grid(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_rectangles(gdal_rt, tmp_path / "rect.geojson")
    conflict = _run(
        qapp,
        {"format": "GTiff", "burnValues": [7], "attribute": "value", "xRes": 1, "yRes": 1},
        src,
        tmp_path / "conflict.tif",
    )
    assert conflict.status is TaskStatus.failed
    assert conflict.error and conflict.error["code"] == "invalid_parameters"
    assert "互斥" in conflict.error["message"]

    no_grid = _run(qapp, {"format": "GTiff", "burnValues": [7]}, src, tmp_path / "no_grid.tif")
    assert no_grid.status is TaskStatus.failed
    assert no_grid.error and no_grid.error["code"] == "invalid_parameters"
    assert not (tmp_path / "no_grid.tif").exists()


def test_rasterize_png_requires_explicit_byte(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_rectangles(gdal_rt, tmp_path / "rect.geojson")
    rejected = _run(
        qapp,
        {"format": "PNG", "burnValues": [7], "xRes": 1, "yRes": 1},
        src,
        tmp_path / "default.png",
    )
    assert rejected.status is TaskStatus.failed
    assert rejected.error and rejected.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "default.png").exists()

    accepted = _run(
        qapp,
        {
            "format": "PNG",
            "burnValues": [7],
            "xRes": 1,
            "yRes": 1,
            "outputType": "Byte",
        },
        src,
        tmp_path / "byte.png",
    )
    assert accepted.status is TaskStatus.succeeded, accepted.error
    info = _inspect(gdal_rt, tmp_path / "byte.png")
    assert info["driver"] == "PNG" and info["dtype"] == "Byte"
    assert info["arr"] == [[7] * 4, [7] * 4]
