"""P6-02 FFT 去条纹端到端：真 worker + 独立 GDAL 重开验收。

数值验收（对应 BACKLOG P6-02「往返、频率定位、掩膜对称性、复数/奇偶尺寸通过；
保存掩膜可复现」）：
- 8×8 与 7×9 合成图 = 常量 10 + x 方向 cos(2π·2x/W) + y 方向 sin(2π·y/H)；
- 空 suppressBins：输出逐像元等于输入（往返）；
- 抑制 (0,2)：x 条纹消失，输出逐像元等于 10 + sin 分量（频率定位）；
- sidecar 自动补厄米伙伴 (0,W−2)，用 applied bins 重跑逐像元复现；
- 输出恒单波段 Float32 + NoData；NoData 标签/像元/计数一致；
- 输入为复数波段时明确拒绝，不静默丢弃虚部；
- ENVI 输出真实落盘 .dat/.hdr；PNG 在写盘前拒绝。
"""

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


def _pattern(rows: int, cols: int) -> list[list[float]]:
    return [
        [
            10.0
            + 3.0 * math.cos(2.0 * math.pi * 2.0 * x / cols)
            + 5.0 * math.sin(2.0 * math.pi * y / rows)
            for x in range(cols)
        ]
        for y in range(rows)
    ]


def _without_x_stripe(rows: int, cols: int) -> list[list[float]]:
    return [
        [10.0 + 5.0 * math.sin(2.0 * math.pi * y / rows) for _ in range(cols)] for y in range(rows)
    ]


def _make_raster(
    gdal_rt: dict[str, Any],
    path: Path,
    rows: int,
    cols: int,
    *,
    nodata: float | None = None,
    nodata_pixel: tuple[int, int] | None = None,
    complex_band: bool = False,
) -> Path:
    if complex_band:
        pairs = [[[1.0, 2.0] for _ in range(cols)] for _ in range(rows)]
        script = f"""
import numpy as np
from osgeo import gdal, osr
pairs = np.array({pairs!r}, dtype=np.float32).reshape({rows}, {cols}, 2)
arr = pairs[..., 0] + 1j * pairs[..., 1]
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, {cols}, {rows}, 1, gdal.GDT_CFloat32)
ds.SetGeoTransform((100.0, 10.0, 0.0, 200.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
ds.GetRasterBand(1).WriteArray(arr.astype(np.complex64))
ds = None
print("OK")
"""
    else:
        values = _pattern(rows, cols)
        if nodata_pixel is not None:
            y, x = nodata_pixel
            values[y][x] = float(nodata)
        flat = [v for row in values for v in row]
        script = f"""
import array
from osgeo import gdal, osr
vals = {flat!r}
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, {cols}, {rows}, 1, gdal.GDT_Float32)
ds.SetGeoTransform((100.0, 10.0, 0.0, 200.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
band = ds.GetRasterBand(1)
band.WriteRaster(0, 0, {cols}, {rows}, array.array("f", vals).tobytes(), buf_type=gdal.GDT_Float32)
if {nodata!r} is not None:
    band.SetNoDataValue(float({nodata!r}))
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
        task_id=f"p602-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:fftfilter",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p602-fft", project_root=str(ROOT))
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


def _mask_payload(out: Path) -> dict[str, Any]:
    sidecar = out.with_name(out.name + ".fft_mask.json")
    assert sidecar.exists(), f"缺少掩膜 sidecar: {sidecar}"
    return json.loads(sidecar.read_text(encoding="utf-8"))


def _assert_close(
    actual: list[list[float]], expected: list[list[float]], atol: float = 1e-4
) -> None:
    a = np.asarray(actual, dtype=np.float64)
    e = np.asarray(expected, dtype=np.float64)
    assert a.shape == e.shape
    diff = float(np.max(np.abs(a - e)))
    assert diff <= atol, f"逐像元最大误差 {diff} 超过容差 {atol}"


@pytest.mark.parametrize(("rows", "cols"), [(8, 8), (7, 9)])
def test_empty_suppression_is_identity_roundtrip(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path, rows: int, cols: int
) -> None:
    src = _make_raster(gdal_rt, tmp_path / f"src_{rows}x{cols}.tif", rows, cols)
    out = tmp_path / f"roundtrip_{rows}x{cols}.tif"

    result = _run(qapp, {"fftBand": 1}, src, out)
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(gdal_rt, out)
    assert info["bands"] == 1 and info["dtype"] == "Float32" and info["nodata"] == NODATA
    _assert_close(info["arr"], _pattern(rows, cols))

    payload = _mask_payload(out)
    assert payload["requested_suppressed_bins"] == []
    assert payload["applied_suppressed_bins"] == []
    assert payload["hermitian_symmetric"] is True
    assert payload["size"] == [rows, cols]


@pytest.mark.parametrize(("rows", "cols"), [(8, 8), (7, 9)])
def test_known_stripe_frequency_is_localized_and_removed(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path, rows: int, cols: int
) -> None:
    src = _make_raster(gdal_rt, tmp_path / f"stripe_{rows}x{cols}.tif", rows, cols)
    out = tmp_path / f"destripe_{rows}x{cols}.tif"

    result = _run(qapp, {"fftBand": 1, "suppressBins": [[0, 2]]}, src, out)
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(gdal_rt, out)
    # x 方向周期 2 的余弦被抑制后，只剩常量 + y 方向正弦。
    _assert_close(info["arr"], _without_x_stripe(rows, cols))

    payload = _mask_payload(out)
    assert payload["requested_suppressed_bins"] == [[0, 2]]
    applied = {tuple(pair) for pair in payload["applied_suppressed_bins"]}
    assert (0, 2) in applied
    assert (0, cols - 2) in applied, "必须自动补入厄米对称伙伴 (0, W-2)"
    assert payload["hermitian_symmetric"] is True


def test_saved_mask_reproduces_the_same_output(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    rows, cols = 7, 9
    src = _make_raster(gdal_rt, tmp_path / "repro_src.tif", rows, cols)
    first = tmp_path / "repro_first.tif"
    second = tmp_path / "repro_second.tif"

    result = _run(qapp, {"fftBand": 1, "suppressBins": [[0, 2]]}, src, first)
    assert result.status is TaskStatus.succeeded, result.error
    payload = _mask_payload(first)

    replay_bins = payload["applied_suppressed_bins"]
    result2 = _run(qapp, {"fftBand": 1, "suppressBins": replay_bins}, src, second)
    assert result2.status is TaskStatus.succeeded, result2.error

    a = np.asarray(_inspect(gdal_rt, first)["arr"], dtype=np.float64)
    b = np.asarray(_inspect(gdal_rt, second)["arr"], dtype=np.float64)
    assert np.allclose(a, b, atol=1e-6), "用 sidecar 的 applied bins 重跑必须逐像元复现"

    payload2 = _mask_payload(second)
    assert {tuple(p) for p in payload2["applied_suppressed_bins"]} == {
        tuple(p) for p in replay_bins
    }


def test_nodata_pixel_and_label_stay_consistent(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    rows, cols = 8, 8
    src = _make_raster(
        gdal_rt,
        tmp_path / "nodata_src.tif",
        rows,
        cols,
        nodata=NODATA,
        nodata_pixel=(3, 3),
    )
    out = tmp_path / "nodata_out.tif"

    result = _run(qapp, {"fftBand": 1, "suppressBins": [[0, 2]]}, src, out)
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(gdal_rt, out)
    assert info["nodata"] == NODATA
    assert info["arr"][3][3] == NODATA, "源 NoData 像元必须回填输出 NoData"
    flat = np.asarray(info["arr"], dtype=np.float64).ravel()
    assert int(np.count_nonzero(flat == NODATA)) == 1
    assert np.all(np.isfinite(flat)), "输出不应出现 NaN/Inf"


def test_envi_output_is_really_written_and_png_is_rejected(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "fmt_src.tif", 8, 8)

    envi_out = tmp_path / "fft_envi.dat"
    result = _run(qapp, {"fftBand": 1, "format": "ENVI", "suppressBins": [[0, 2]]}, src, envi_out)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, envi_out)
    assert info["driver"] == "ENVI"
    assert envi_out.with_suffix(".hdr").exists(), "ENVI 必须同时写出 .hdr"
    assert _mask_payload(envi_out)["size"] == [8, 8]

    png_out = tmp_path / "fft.png"
    png = _run(qapp, {"fftBand": 1, "format": "PNG"}, src, png_out)
    assert png.status is TaskStatus.failed
    assert png.error and png.error["code"] == "unsupported_format_combination"
    assert not png_out.exists()


def test_rejections_missing_band_out_of_range_and_complex_input(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "reject_src.tif", 8, 8)

    missing = _run(qapp, {}, src, tmp_path / "missing.tif")
    assert missing.status is TaskStatus.failed
    assert missing.error and missing.error["code"] == "invalid_parameters"
    assert "fftBand" in missing.error["message"]
    assert not (tmp_path / "missing.tif").exists()

    out_of_range = _run(qapp, {"fftBand": 2}, src, tmp_path / "oob.tif")
    assert out_of_range.status is TaskStatus.failed
    assert out_of_range.error and out_of_range.error["code"] == "fftfilter_error"
    assert "超出范围" in out_of_range.error["message"]
    assert not (tmp_path / "oob.tif").exists()

    malformed = _run(
        qapp, {"fftBand": 1, "suppressBins": [[0, 1.5]]}, src, tmp_path / "bad_bins.tif"
    )
    assert malformed.status is TaskStatus.failed
    assert malformed.error and malformed.error["code"] == "invalid_parameters"
    assert not (tmp_path / "bad_bins.tif").exists()

    complex_src = _make_raster(gdal_rt, tmp_path / "complex_src.tif", 8, 8, complex_band=True)
    complex_out = _run(qapp, {"fftBand": 1}, complex_src, tmp_path / "complex_out.tif")
    assert complex_out.status is TaskStatus.failed
    assert complex_out.error and complex_out.error["code"] == "fftfilter_error"
    assert "复数" in complex_out.error["message"]
    assert not (tmp_path / "complex_out.tif").exists()


def test_overwrite_replaces_old_mask_sidecar(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "ow_src.tif", 8, 8)
    out = tmp_path / "ow_out.tif"

    first = _run(qapp, {"fftBand": 1, "suppressBins": [[0, 2]]}, src, out)
    assert first.status is TaskStatus.succeeded, first.error
    assert _mask_payload(out)["applied_suppressed_bins"]

    second = _run(qapp, {"fftBand": 1}, src, out)
    assert second.status is TaskStatus.succeeded, second.error
    assert _mask_payload(out)["applied_suppressed_bins"] == [], "覆盖后不得残留旧掩膜"
