"""P6-04 Brovey/HSV 融合端到端：公式、命名、MS 重采样与拒绝路径。"""

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
    band = ds.GetRasterBand(i + 1)
    band.WriteArray(values[i])
    band.SetNoDataValue({NODATA})
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
    pan: Path,
    target: Path,
    *,
    timeout: float = 60.0,
) -> Any:
    spec = TaskSpec(
        task_id=f"p604fusion-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:pansharpen",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
        ),
        project_root=str(ROOT),
        workspace=str(target.parent),
        inputs={
            "input": DatasetRef(
                dataset_id="pan-in",
                kind=DatasetKind.raster,
                uri=pan.resolve().as_uri(),
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p604-fusion", project_root=str(ROOT))
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
    bands.append({{"description": b.GetDescription(), "arr": b.ReadAsArray().tolist()}})
print(json.dumps({{"w": ds.RasterXSize, "h": ds.RasterYSize, "bands": ds.RasterCount, "band_list": bands}}))
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
    path = out.with_name(out.name + ".pansharpen.json")
    assert path.exists(), f"缺少 sidecar: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("brovey", np.array([0.1333333333, 0.2666666667, 0.4])),
        ("hsv", np.array([0.2666666667, 0.5333333333, 0.8])),
    ],
)
def test_brovey_and_hsv_formulas(
    qapp: QApplication,
    gdal_rt: dict[str, Any],
    tmp_path: Path,
    method: str,
    expected: np.ndarray,
) -> None:
    pan = _make_raster(gdal_rt, tmp_path / f"pan_{method}.tif", np.full((8, 8), 0.8))
    ms_values = np.stack([np.full((8, 8), 0.2), np.full((8, 8), 0.4), np.full((8, 8), 0.6)])
    ms = _make_raster(gdal_rt, tmp_path / f"ms_{method}.tif", ms_values)
    out = tmp_path / f"fused_{method}.tif"

    result = _run(
        qapp,
        {
            "msFile": str(ms),
            "msBands": [1, 2, 3],
            "fusionMethod": method,
            "fusionResample": "bilinear",
        },
        pan,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["w"] == 8 and info["h"] == 8 and info["bands"] == 3
    assert [b["description"] for b in info["band_list"]] == [
        f"{method}_R",
        f"{method}_G",
        f"{method}_B",
    ]
    actual = np.asarray([b["arr"] for b in info["band_list"]], dtype=np.float64)
    for idx in range(3):
        assert float(np.max(np.abs(actual[idx] - expected[idx]))) <= 1e-6

    payload = _sidecar(out)
    assert payload["method"] == method
    assert payload["ms_bands"] == [1, 2, 3]
    assert payload["output_bands"] == [f"{method}_R", f"{method}_G", f"{method}_B"]
    assert "formula" in payload and payload["formula"]
    assert "NNDiffuse" in payload["applicability"]


def test_ms_is_resampled_to_pan_grid(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    pan = _make_raster(gdal_rt, tmp_path / "pan_hi.tif", np.full((8, 8), 0.8))
    ms_values = np.stack([np.full((4, 4), 0.2), np.full((4, 4), 0.4), np.full((4, 4), 0.6)])
    ms = _make_raster(
        gdal_rt,
        tmp_path / "ms_lo.tif",
        ms_values,
        gt=(100.0, 20.0, 0.0, 200.0, 0.0, -20.0),
    )
    out = tmp_path / "fused_resampled.tif"
    result = _run(
        qapp,
        {
            "msFile": str(ms),
            "msBands": [1, 2, 3],
            "fusionMethod": "brovey",
            "fusionResample": "bilinear",
        },
        pan,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["w"] == 8 and info["h"] == 8
    actual = np.asarray([b["arr"] for b in info["band_list"]], dtype=np.float64)
    expected = np.array([0.1333333333, 0.2666666667, 0.4])
    for idx in range(3):
        assert float(np.max(np.abs(actual[idx] - expected[idx]))) <= 1e-6


def test_fusion_rejections(qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path) -> None:
    pan = _make_raster(gdal_rt, tmp_path / "pan_rej.tif", np.full((4, 4), 0.8))
    ms_values = np.stack([np.full((4, 4), 0.2), np.full((4, 4), 0.4), np.full((4, 4), 0.6)])
    ms = _make_raster(gdal_rt, tmp_path / "ms_rej.tif", ms_values)
    out = tmp_path / "reject.tif"

    missing = _run(qapp, {"msFile": str(ms)}, pan, out)
    assert missing.status is TaskStatus.failed
    assert missing.error and missing.error["code"] == "invalid_parameters"
    assert not out.exists()

    duplicate = _run(qapp, {"msFile": str(ms), "msBands": [1, 1, 2]}, pan, out)
    assert duplicate.status is TaskStatus.failed
    assert duplicate.error and duplicate.error["code"] == "invalid_parameters"
    assert not out.exists()

    unknown = _run(
        qapp,
        {"msFile": str(ms), "msBands": [1, 2, 3], "fusionMethod": "nndiffuse"},
        pan,
        out,
    )
    assert unknown.status is TaskStatus.failed
    assert unknown.error and unknown.error["code"] == "invalid_parameters"
    assert not out.exists()

    multi_pan = _make_raster(gdal_rt, tmp_path / "pan_multi.tif", np.full((2, 4, 4), 0.8))
    wrong_pan = _run(qapp, {"msFile": str(ms), "msBands": [1, 2, 3]}, multi_pan, out)
    assert wrong_pan.status is TaskStatus.failed
    assert wrong_pan.error and wrong_pan.error["code"] == "pansharpen_error"
    assert "1 个波段" in wrong_pan.error["message"]
    assert not out.exists()

    ms_wrong_crs = _make_raster(gdal_rt, tmp_path / "ms_wrong_crs.tif", ms_values, epsg=4326)
    crs = _run(qapp, {"msFile": str(ms_wrong_crs), "msBands": [1, 2, 3]}, pan, out)
    assert crs.status is TaskStatus.failed
    assert crs.error and crs.error["code"] == "pansharpen_error"
    assert "CRS" in crs.error["message"]
    assert not out.exists()

    png = _run(
        qapp,
        {"msFile": str(ms), "msBands": [1, 2, 3], "format": "PNG"},
        pan,
        tmp_path / "fusion.png",
    )
    assert png.status is TaskStatus.failed
    assert png.error and png.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "fusion.png").exists()
