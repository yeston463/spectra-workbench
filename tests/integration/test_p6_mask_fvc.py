"""P6-05 掩膜/FVC 端到端：真 worker + 独立 GDAL 重开验收。

验收（对应 BACKLOG P6-05）：
- ``gdal:mask``：1/0 语义 Byte 掩膜，阈值闭区间、invert 仅翻转有效源像元，
  sidecar 完成 n_true/n_false 审计；
- ``gdal:fvc``：percentile/values 两种端元模式、分级边界 classes、crop 是
  显式选项且越界质量标记恒在 sidecar（n_below_soil/n_above_veg）、maskFile
  与 `gdal:mask` 串联、像元面积与分级统计可独立核算；
- 拒绝路径明确且不落盘。
"""

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


def _ndvi_plane(rows: int = 4, cols: int = 5) -> np.ndarray:
    y, x = np.mgrid[0:rows, 0:cols]
    return 0.1 + 0.02 * x + 0.01 * y


def _make_raster(
    gdal_rt: dict[str, Any],
    path: Path,
    values: np.ndarray,
    *,
    gt: tuple[float, ...] = GT,
    nodata: float = NODATA,
    set_nodata: bool = True,
) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = np.array({values.astype(np.float64).tolist()!r}, dtype=np.float32)
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, values.shape[1], values.shape[0], 1, gdal.GDT_Float32)
ds.SetGeoTransform({gt!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
band = ds.GetRasterBand(1)
band.WriteArray(values)
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
        task_id=f"p605-{uuid.uuid4().hex[:10]}",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p605", project_root=str(ROOT))
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
        [str(gdal_rt["python"]), "-c" if False else "-c", script],
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


def test_mask_binary_byte_semantics(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    values = _ndvi_plane()
    src = _make_raster(gdal_rt, tmp_path / "ndvi_mask_src.tif", values)

    out = tmp_path / "mask_025_035.tif"
    result = _run(
        qapp,
        "gdal:mask",
        {"maskBand": 1, "maskLow": 0.18, "maskHigh": 0.35},
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["bands"] == 1
    band = info["band_list"][0]
    assert band["dtype"] == "Byte"
    assert band["nodata"] is None, "二值掩膜显式不设 NoData"
    arr = np.asarray(band["arr"], dtype=np.int64)
    expected = ((values >= 0.18) & (values <= 0.35)).astype(np.int64)
    assert np.array_equal(arr, expected)
    payload = _sidecar(out, ".mask.json")
    assert payload["semantics"] == "1=mask true, 0=mask false; no nodata set"
    assert payload["n_true"] == int(expected.sum())
    assert payload["n_false"] == int(expected.size - expected.sum())

    invert_out = tmp_path / "mask_invert.tif"
    result2 = _run(
        qapp,
        "gdal:mask",
        {"maskLow": 0.18, "maskHigh": 0.35, "maskInvert": True},
        src,
        invert_out,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    arr2 = np.asarray(_inspect(gdal_rt, invert_out)["band_list"][0]["arr"])
    assert np.array_equal(arr2, 1 - expected)
    payload2 = _sidecar(invert_out, ".mask.json")
    assert payload2["n_true"] == payload["n_false"]


def test_fvc_values_mode_edges_stats_and_flags(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    values = _ndvi_plane()
    src = _make_raster(gdal_rt, tmp_path / "ndvi_src.tif", values)
    out = tmp_path / "fvc_values.tif"

    result = _run(
        qapp,
        "gdal:fvc",
        {
            "endmemberMode": "values",
            "fvcSoil": 0.15,
            "fvcVeg": 0.55,
            "fvcClassEdges": [0.3, 0.6],
            "crop": True,
        },
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["bands"] == 1
    fvc_band = info["band_list"][0]
    assert fvc_band["dtype"] == "Float32" and fvc_band["nodata"] == NODATA
    assert fvc_band["description"] == "fvc"

    raw = (values - 0.15) / (0.55 - 0.15)
    expected_fvc = np.clip(raw, 0.0, 1.0)
    expected_class = np.searchsorted([0.3, 0.6], expected_fvc, side="right") + 1
    actual_fvc = np.asarray(fvc_band["arr"])
    assert np.allclose(actual_fvc, expected_fvc, atol=1e-6)

    class_path = out.with_name(out.name + ".fvc_class.tif")
    assert class_path.exists(), "分级类别栅格必须随事务提交"
    class_info = _inspect(gdal_rt, class_path)
    assert class_info["bands"] == 1
    class_band = class_info["band_list"][0]
    assert class_band["dtype"] == "Byte" and class_band["nodata"] == 0.0
    assert class_band["description"] == "fvc_class"
    actual_class = np.asarray(class_band["arr"], dtype=np.int64)
    assert np.array_equal(actual_class, expected_class)

    payload = _sidecar(out, ".fvc.json")
    assert payload["endmember_mode"] == "values"
    assert payload["endmembers"] == {"soil": 0.15, "veg": 0.55}
    assert payload["crop"] is True
    assert payload["class_edges"] == [0.3, 0.6]
    counts = payload["class_counts"]
    assert [entry["class"] for entry in counts] == [1, 2, 3]
    for entry in counts:
        idx = entry["class"] - 1
        assert entry["count"] == int(np.count_nonzero(expected_class == idx + 1))
    # 像元面积：10m×10m
    assert payload["pixel_area"] == pytest.approx(100.0)
    for entry in counts:
        assert entry["pixel_area"] == pytest.approx(entry["count"] * 100.0)


def test_fvc_percentile_mode_and_crop_flags(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    values = _ndvi_plane()
    # 制造两个低于 soil 端元的像元（保留 20 个有效像元）
    src = _make_raster(gdal_rt, tmp_path / "ndvi_pct_src.tif", values)
    out = tmp_path / "fvc_pct.tif"
    result = _run(
        qapp,
        "gdal:fvc",
        {
            "endmemberMode": "percentile",
            "lowPercentile": 2.0,
            "highPercentile": 98.0,
            "crop": True,
        },
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    payload = _sidecar(out, ".fvc.json")
    assert payload["endmember_mode"] == "percentile"
    expected_soil = float(np.percentile(values.ravel(), 2.0))
    expected_veg = float(np.percentile(values.ravel(), 98.0))
    assert payload["endmembers"]["soil"] == pytest.approx(expected_soil, rel=1e-6)
    assert payload["endmembers"]["veg"] == pytest.approx(expected_veg, rel=1e-6)
    assert payload["n_valid"] == 20

    # crop=false：保留原始数值并报告越界
    out2 = tmp_path / "fvc_pct_raw.tif"
    result2 = _run(
        qapp,
        "gdal:fvc",
        {"endmemberMode": "percentile", "crop": False},
        src,
        out2,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    info2 = _inspect(gdal_rt, out2)
    raw = (values - payload["endmembers"]["soil"]) / (
        payload["endmembers"]["veg"] - payload["endmembers"]["soil"]
    )
    assert np.allclose(np.asarray(info2["band_list"][0]["arr"]), raw, atol=1e-6)
    payload2 = _sidecar(out2, ".fvc.json")
    assert payload2["crop"] is False
    # 独立核算：越界标记 = raw<0 / raw>1 的真实计数（98% 分位在 20 像元内
    # 不是最大值，因此会有 1 个像元高于 veg 端元）
    below = int(np.count_nonzero(raw < 0.0))
    above = int(np.count_nonzero(raw > 1.0))
    assert payload2["n_below_soil"] == below
    assert payload2["n_above_veg"] == above
    assert payload2["n_above_veg"] > 0


def test_mask_file_chain_with_gdal_mask(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    values = _ndvi_plane()
    src = _make_raster(gdal_rt, tmp_path / "ndvi_chain_src.tif", values)
    mask_out = tmp_path / "chain_mask.tif"
    mask_result = _run(
        qapp,
        "gdal:mask",
        {"maskLow": 0.18, "maskHigh": 0.35},
        src,
        mask_out,
    )
    assert mask_result.status is TaskStatus.succeeded, mask_result.error

    fvc_out = tmp_path / "chain_fvc.tif"
    fvc_result = _run(
        qapp,
        "gdal:fvc",
        {
            "endmemberMode": "percentile",
            "maskFile": str(mask_out),
            "crop": True,
        },
        src,
        fvc_out,
    )
    assert fvc_result.status is TaskStatus.succeeded, fvc_result.error
    payload = _sidecar(fvc_out, ".fvc.json")
    assert payload["mask_file"] == str(mask_out)
    valid = np.asarray(
        _inspect(gdal_rt, mask_out)["band_list"][0]["arr"], dtype=np.int64
    ) == 1
    assert payload["n_valid"] == int(valid.sum())
    expected_soil = float(np.percentile(values[valid], 2.0))
    assert payload["endmembers"]["soil"] == pytest.approx(expected_soil, rel=1e-6)


def test_fvc_and_mask_rejections(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    values = _ndvi_plane()
    src = _make_raster(gdal_rt, tmp_path / "reject_src.tif", values)
    out = tmp_path / "reject_out.tif"

    missing_soil = _run(
        qapp,
        "gdal:fvc",
        {"endmemberMode": "values", "fvcVeg": 0.55},
        src,
        out,
    )
    assert missing_soil.status is TaskStatus.failed
    assert missing_soil.error and missing_soil.error["code"] == "invalid_parameters"
    assert not out.exists()

    bad_edges = _run(
        qapp,
        "gdal:fvc",
        {"fvcClassEdges": [0.5, 0.4]},
        src,
        out,
    )
    assert bad_edges.status is TaskStatus.failed
    assert bad_edges.error and bad_edges.error["code"] == "invalid_parameters"
    assert not out.exists()

    png = _run(
        qapp,
        "gdal:fvc",
        {"endmemberMode": "percentile", "format": "PNG"},
        src,
        tmp_path / "fvc.png",
    )
    assert png.status is TaskStatus.failed
    assert png.error and png.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "fvc.png").exists()

    unknown_format = _run(
        qapp,
        "gdal:mask",
        {"maskLow": 0.0, "format": "NOT_A_FORMAT"},
        src,
        out,
    )
    assert unknown_format.status is TaskStatus.failed
    assert unknown_format.error
    assert unknown_format.error["code"] == "unsupported_format_combination"
    assert not out.exists()

    bad_band = _run(
        qapp,
        "gdal:mask",
        {"maskBand": 5, "maskLow": 0.0},
        src,
        out,
    )
    assert bad_band.status is TaskStatus.failed
    assert bad_band.error and bad_band.error["code"] == "mask_error"
    assert not out.exists()
