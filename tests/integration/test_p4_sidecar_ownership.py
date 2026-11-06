"""S3: generic sidecar ownership regressions.

Phase 1 is conservative: if an existing target owns a sidecar that this run
does not generate, refuse the overwrite before commit. No wildcard deletion.
VRT input dependencies must never be treated as target sidecars.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

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
def gdal_rt() -> dict:
    return resolve_gdal_runtime(ROOT)


def _make_raster(gdal_rt: dict, path: Path, value: float = 111.0, gt_x: float = 100.0) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", 2, 2, 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform(({gt_x!r}, 1.0, 0.0, 30.0, 0.0, -1.0))
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(np.full((2, 2), {value!r}, dtype=np.float32))
ds.GetRasterBand(1).SetNoDataValue(-9999)
ds = None
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


def _make_envi(gdal_rt: dict, path: Path, value: float = 111.0) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
ds = gdal.GetDriverByName("ENVI").Create(r"{path}", 4, 4, 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform((100.0, 1.0, 0.0, 30.0, 0.0, -1.0))
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(np.full((4, 4), {value!r}, dtype=np.float32))
ds = None
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


def _run_task(
    qapp: QApplication,
    capability_id: str,
    parameters: dict,
    inputs: list[Path],
    out_path: Path,
    *,
    timeout: float = 40.0,
) -> object:
    spec = TaskSpec(
        task_id=f"s3-{uuid.uuid4().hex[:12]}",
        attempt=1,
        capability_id=capability_id,
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(ROOT),
        workspace=str(out_path.parent),
        inputs={
            "inputs": [
                DatasetRef(dataset_id=f"src-{i}", kind=DatasetKind.raster, uri=p.resolve().as_uri())
                for i, p in enumerate(inputs)
            ]
        },
        outputs=[
            OutputPlan(
                key="output",
                kind=DatasetKind.raster,
                uri=out_path.resolve().as_uri(),
                driver="GTiff",
                overwrite=True,
            )
        ],
        parameters=parameters,
    )
    client = QtWorkerClient(provider_id="gdal", worker_id="s3-worker", project_root=str(ROOT))
    results = []
    errors: list[tuple[str, str]] = []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda c, m: errors.append((c, m)))
    try:
        client.start(timeout=20.0)
        client.submit_task_async(spec, timeout=timeout)
        deadline = time.monotonic() + timeout
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert not errors, errors
        assert results, f"任务在 {timeout}s 内未产生结果"
        return results[0]
    finally:
        client.stop(timeout=5.0)


def _inspect(gdal_rt: dict, path: Path) -> dict:
    script = f"""
import json
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.Open(r"{path}")
assert ds is not None
print(json.dumps({{"driver": ds.GetDriver().ShortName, "w": ds.RasterXSize,
                  "h": ds.RasterYSize, "value": float(ds.GetRasterBand(1).ReadAsArray()[0][0])}}))
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


@pytest.mark.parametrize("sidecar", [".aux.xml", ".ovr", ".msk", ".tfw"])
def test_gtiff_stale_sidecar_refuses_overwrite(
    qapp, gdal_rt, tmp_path, sidecar: str
) -> None:
    target = _make_raster(gdal_rt, tmp_path / "existing.tif", 111.0)
    marker = (
        target.with_suffix(sidecar)
        if sidecar == ".tfw"
        else Path(f"{target}{sidecar}")
    )
    marker.write_bytes(b"OLD SIDECAR MARKER")
    a = _make_raster(gdal_rt, tmp_path / "a.tif", 5.0, 200.0)
    b = _make_raster(gdal_rt, tmp_path / "b.tif", 7.0, 202.0)

    result = _run_task(qapp, "gdal:merge", {"format": "GTiff"}, [a, b], target)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "stale_sidecar_conflict"
    assert marker.read_bytes() == b"OLD SIDECAR MARKER"
    assert _inspect(gdal_rt, target)["value"] == 111.0


def test_envi_overwrite_without_stale_sidecar_succeeds(qapp, gdal_rt, tmp_path) -> None:
    target = _make_envi(gdal_rt, tmp_path / "existing.dat", 111.0)
    a = _make_raster(gdal_rt, tmp_path / "a.tif", 5.0, 200.0)
    b = _make_raster(gdal_rt, tmp_path / "b.tif", 7.0, 202.0)
    result = _run_task(
        qapp, "gdal:merge", {"format": "ENVI"}, [a, b], target
    )
    assert result.status is TaskStatus.succeeded, result.error
    assert target.with_suffix(".hdr").exists()
    assert _inspect(gdal_rt, target)["driver"].upper() == "ENVI"


def test_envi_stale_overview_refuses_overwrite(qapp, gdal_rt, tmp_path) -> None:
    target = _make_envi(gdal_rt, tmp_path / "existing.dat", 111.0)
    stale = tmp_path / "existing.dat.ovr"
    stale.write_bytes(b"OLD ENVI OVERVIEW")
    a = _make_raster(gdal_rt, tmp_path / "a.tif", 5.0, 200.0)
    b = _make_raster(gdal_rt, tmp_path / "b.tif", 7.0, 202.0)
    result = _run_task(qapp, "gdal:merge", {"format": "ENVI"}, [a, b], target)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "stale_sidecar_conflict"
    assert stale.read_bytes() == b"OLD ENVI OVERVIEW"


def test_chinese_space_target_with_stale_sidecar_refuses(qapp, gdal_rt, tmp_path) -> None:
    target = _make_raster(gdal_rt, tmp_path / "成果 数据.tif", 111.0)
    stale = tmp_path / "成果 数据.tif.aux.xml"
    stale.write_bytes(b"OLD")
    a = _make_raster(gdal_rt, tmp_path / "a.tif", 5.0, 200.0)
    b = _make_raster(gdal_rt, tmp_path / "b.tif", 7.0, 202.0)
    result = _run_task(qapp, "gdal:merge", {"format": "GTiff"}, [a, b], target)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "stale_sidecar_conflict"
    assert stale.exists()


def test_corrupt_target_without_sidecars_can_be_replaced(qapp, gdal_rt, tmp_path) -> None:
    """目标不可读但没有任何可识别 sidecar 时，允许 overwrite 替换主文件。"""
    target = tmp_path / "corrupt.tif"
    target.write_bytes(b"not a raster")
    a = _make_raster(gdal_rt, tmp_path / "a.tif", 5.0, 200.0)
    b = _make_raster(gdal_rt, tmp_path / "b.tif", 7.0, 202.0)
    result = _run_task(qapp, "gdal:merge", {"format": "GTiff"}, [a, b], target)
    assert result.status is TaskStatus.succeeded, result.error
    assert _inspect(gdal_rt, target)["value"] == 5.0


def test_vrt_sources_are_not_deleted_or_treated_as_sidecars(qapp, gdal_rt, tmp_path) -> None:
    source_a = _make_raster(gdal_rt, tmp_path / "a.tif", 5.0, 100.0)
    source_b = _make_raster(gdal_rt, tmp_path / "b.tif", 7.0, 102.0)
    target = tmp_path / "mosaic.vrt"
    first = _run_task(qapp, "gdal:buildvrt", {}, [source_a, source_b], target)
    assert first.status is TaskStatus.succeeded, first.error
    second = _run_task(qapp, "gdal:buildvrt", {}, [source_a, source_b], target)
    assert second.status is TaskStatus.succeeded, second.error
    assert source_a.exists() and source_b.exists()
    assert _inspect(gdal_rt, source_a)["value"] == 5.0
    assert _inspect(gdal_rt, source_b)["value"] == 7.0
