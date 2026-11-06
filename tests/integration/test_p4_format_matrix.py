"""S4 format matrix integration: supported outputs and early rejection."""

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


def _make_raster(
    gdal_rt: dict,
    path: Path,
    *,
    bands: int = 1,
    dtype: str = "Byte",
    nodata: float | None = None,
    alpha: bool = False,
) -> Path:
    alpha_stmt = ""
    if alpha:
        alpha_stmt = (
            f"    if i == {bands}:\n"
            "        b.SetColorInterpretation(gdal.GCI_AlphaBand)"
        )
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
np_dtype = {{"Byte": "uint8", "Float32": "float32"}}[{dtype!r}]
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", 2, 2, {bands}, gdal.GDT_{dtype})
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform((100.0, 1.0, 0.0, 30.0, 0.0, -1.0))
ds.SetProjection(srs.ExportToWkt())
for i in range(1, {bands} + 1):
    b = ds.GetRasterBand(i)
    b.WriteArray(np.full((2, 2), 5 + (i - 1) * 10, dtype=np_dtype))
    {f"b.SetNoDataValue({nodata!r})" if nodata is not None else ""}
{alpha_stmt}
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
        task_id=f"s4-{uuid.uuid4().hex[:12]}",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="s4-worker", project_root=str(ROOT))
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
print(json.dumps({{
    "driver": ds.GetDriver().ShortName,
    "w": ds.RasterXSize, "h": ds.RasterYSize, "bands": ds.RasterCount,
    "types": [gdal.GetDataTypeName(ds.GetRasterBand(i).DataType) for i in range(1, ds.RasterCount + 1)],
    "nodata": [ds.GetRasterBand(i).GetNoDataValue() for i in range(1, ds.RasterCount + 1)],
    "ci": [int(ds.GetRasterBand(i).GetColorInterpretation()) for i in range(1, ds.RasterCount + 1)],
    "flags": [int(ds.GetRasterBand(i).GetMaskFlags()) for i in range(1, ds.RasterCount + 1)],
    "files": [str(p) for p in (ds.GetFileList() or [])],
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


def test_png_byte_rgb_succeeds_with_sidecar(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "rgb.tif", bands=3, dtype="Byte")
    out = tmp_path / "out.png"
    result = _run_task(qapp, "gdal:bandmanage", {"format": "PNG"}, [src], out)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["driver"] == "PNG" and info["bands"] == 3
    assert info["types"] == ["Byte", "Byte", "Byte"]
    assert any(str(p).endswith(".aux.xml") for p in info["files"])


def test_jpeg_byte_rgb_succeeds(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "rgb.tif", bands=3, dtype="Byte")
    out = tmp_path / "out.jpg"
    result = _run_task(qapp, "gdal:bandmanage", {"format": "JPEG"}, [src], out)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["driver"] == "JPEG" and info["bands"] == 3


def test_png_rgba_alpha_preserved(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "rgba.tif", bands=4, dtype="Byte", alpha=True)
    out = tmp_path / "out.png"
    result = _run_task(qapp, "gdal:bandmanage", {"format": "PNG"}, [src], out)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["bands"] == 4
    assert info["ci"][-1] == 6, "PNG 输出应保留 alpha color interpretation"


def test_envi_alpha_rejected_before_write(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "rgba.tif", bands=4, dtype="Byte", alpha=True)
    out = tmp_path / "out.dat"
    result = _run_task(qapp, "gdal:bandmanage", {"format": "ENVI"}, [src], out)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "unsupported_format_combination"
    assert not out.exists()


def test_png_float32_rejected_before_write(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "float.tif", bands=1, dtype="Float32")
    out = tmp_path / "out.png"
    result = _run_task(qapp, "gdal:bandmanage", {"format": "PNG"}, [src], out)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "unsupported_format_combination"
    assert not out.exists()


def test_jpeg_alpha_rejected_before_write(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "rgba.tif", bands=4, dtype="Byte", alpha=True)
    out = tmp_path / "out.jpg"
    result = _run_task(qapp, "gdal:bandmanage", {"format": "JPEG"}, [src], out)
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "unsupported_format_combination"
    assert not out.exists()


def test_png_dst_nodata_rejected_before_write(qapp, gdal_rt, tmp_path) -> None:
    a = _make_raster(gdal_rt, tmp_path / "a.tif", bands=1, dtype="Byte", nodata=0)
    b = _make_raster(gdal_rt, tmp_path / "b.tif", bands=1, dtype="Byte", nodata=0)
    out = tmp_path / "out.png"
    result = _run_task(
        qapp,
        "gdal:merge",
        {"format": "PNG", "srcNodata": 0, "vrtNodata": 0, "dstNodata": 0},
        [a, b],
        out,
    )
    assert result.status is TaskStatus.failed
    assert result.error and result.error.get("code") == "unsupported_format_combination"
    assert not out.exists()


def test_cog_float32_with_nodata_succeeds(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "float.tif", bands=1, dtype="Float32", nodata=-9999)
    out = tmp_path / "out_cog.tif"
    result = _run_task(
        qapp,
        "gdal:bandmanage",
        {"format": "COG"},
        [src],
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    assert out.exists()
