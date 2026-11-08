"""P6-03 缨帽变换端到端：真 worker + 独立 GDAL 重开验收。

数值验收（对应 BACKLOG P6-03「缨帽系数来源、传感器匹配可检查」）：
- 3 个已登记传感器（Landsat 5 TM / 7 ETM+ / 8 OLI）分别按公开系数逐像元计算；
- 输出 3 波段 brightness/greenness/wetness + NoData；
- 数据集元数据与 sidecar 同时记录传感器、产品级别、波段顺序、完整系数与来源 URL；
- tcBands 的波段映射真实生效（打乱输入波段顺序后结果仍按角色计算）；
- 未知传感器、缺 tcBands、重复/越界波段、PNG 输出均明确拒绝且不落盘。
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

# 测试侧独立硬编码的公开系数（与 registry 分开维护，形成交叉校验）。
COEFFICIENTS = {
    "landsat5_tm": np.array(
        [
            [0.2043, 0.4158, 0.5524, 0.5741, 0.3124, 0.2303],
            [-0.1603, -0.2819, -0.4934, 0.7940, -0.0002, -0.1446],
            [0.0315, 0.2021, 0.3102, 0.1594, -0.6806, -0.6109],
        ]
    ),
    "landsat7_etm": np.array(
        [
            [0.3561, 0.3972, 0.3904, 0.6966, 0.2286, 0.1596],
            [-0.3344, -0.3544, -0.4556, 0.6966, -0.0242, -0.2630],
            [0.2626, 0.2141, 0.0926, 0.0656, -0.7629, -0.5388],
        ]
    ),
    "landsat8_oli": np.array(
        [
            [0.3029, 0.2786, 0.4733, 0.5599, 0.5080, 0.1872],
            [-0.2941, -0.2430, -0.5424, 0.7276, 0.0713, -0.1608],
            [0.1511, 0.1973, 0.3283, 0.3407, -0.7117, -0.4559],
        ]
    ),
}


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


def _bands(rows: int, cols: int) -> np.ndarray:
    y, x = np.mgrid[0:rows, 0:cols]
    return np.stack(
        [
            0.10 + 0.010 * x + 0.005 * y,
            0.20 + 0.008 * x - 0.004 * y,
            0.30 - 0.012 * x + 0.006 * y,
            0.40 + 0.015 * x + 0.003 * y,
            0.50 - 0.006 * x + 0.009 * y,
            0.60 + 0.004 * x - 0.007 * y,
        ]
    ).astype(np.float64)


def _make_raster(
    gdal_rt: dict[str, Any],
    path: Path,
    *,
    rows: int = 4,
    cols: int = 3,
    nodata_pixel: tuple[int, int] | None = None,
) -> Path:
    values = _bands(rows, cols)
    if nodata_pixel is not None:
        y, x = nodata_pixel
        values[2, y, x] = NODATA
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = np.array({values.tolist()!r}, dtype=np.float32)
rows, cols = values.shape[1], values.shape[2]
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, cols, rows, values.shape[0], gdal.GDT_Float32)
ds.SetGeoTransform((100.0, 10.0, 0.0, 200.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
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
    source: Path,
    target: Path,
    *,
    timeout: float = 60.0,
) -> Any:
    spec = TaskSpec(
        task_id=f"p603tc-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:tasseledcap",
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
    client = QtWorkerClient(
        provider_id="gdal", worker_id="p603-tc", project_root=str(ROOT)
    )
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
    "driver": ds.GetDriver().ShortName,
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


def _sidecar(out: Path) -> dict[str, Any]:
    path = out.with_name(out.name + ".tasseledcap.json")
    assert path.exists(), f"缺少 sidecar: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("sensor", ["landsat5_tm", "landsat7_etm", "landsat8_oli"])
def test_tasseled_cap_known_coefficients(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path, sensor: str
) -> None:
    rows, cols = 4, 3
    src = _make_raster(gdal_rt, tmp_path / f"{sensor}_src.tif", rows=rows, cols=cols)
    out = tmp_path / f"{sensor}_tc.tif"

    result = _run(
        qapp,
        {"tcSensor": sensor, "tcBands": [1, 2, 3, 4, 5, 6]},
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(gdal_rt, out)
    assert info["bands"] == 3
    assert [b["description"] for b in info["band_list"]] == [
        "brightness",
        "greenness",
        "wetness",
    ]
    assert all(b["dtype"] == "Float32" and b["nodata"] == NODATA for b in info["band_list"])

    data = _bands(rows, cols).reshape(6, -1)
    expected = COEFFICIENTS[sensor] @ data
    actual = np.asarray([b["arr"] for b in info["band_list"]]).reshape(3, -1)
    assert float(np.max(np.abs(actual - expected))) <= 1e-6

    assert info["metadata"]["TC_SENSOR"] == sensor
    assert info["metadata"]["TC_BAND_ORDER"] == "Blue,Green,Red,NIR,SWIR1,SWIR2"
    assert info["metadata"]["TC_INPUT_BANDS"] == "1,2,3,4,5,6"
    assert info["metadata"]["TC_SOURCE_URL"].startswith("http")

    payload = _sidecar(out)
    assert payload["sensor"] == sensor
    assert payload["band_order"] == ["Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2"]
    assert payload["input_bands"] == [1, 2, 3, 4, 5, 6]
    assert payload["output_bands"] == ["brightness", "greenness", "wetness"]
    assert np.allclose(np.asarray(payload["coefficients"]), COEFFICIENTS[sensor])
    assert payload["source_citation"] and payload["source_url"].startswith("http")
    assert payload["product_level"]


def test_tasseled_cap_band_mapping_is_explicit(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    # 输入波段顺序打乱为 Red, Blue, Green, NIR, SWIR1, SWIR2；
    # tcBands 必须按角色重新映射，结果应与标准顺序一致。
    rows, cols = 4, 3
    standard = _bands(rows, cols)
    shuffled = standard[[2, 0, 1, 3, 4, 5]]
    script_values = shuffled.tolist()
    src = tmp_path / "tc_shuffled_src.tif"
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = np.array({script_values!r}, dtype=np.float32)
rows, cols = values.shape[1], values.shape[2]
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(src)!r}, cols, rows, 6, gdal.GDT_Float32)
ds.SetGeoTransform((100.0, 10.0, 0.0, 200.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
for i in range(6):
    ds.GetRasterBand(i + 1).WriteArray(values[i])
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

    out = tmp_path / "tc_shuffled_out.tif"
    result = _run(
        qapp,
        {"tcSensor": "landsat8_oli", "tcBands": [2, 3, 1, 4, 5, 6]},
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    actual = np.asarray([b["arr"] for b in _inspect(gdal_rt, out)["band_list"]]).reshape(3, -1)
    expected = COEFFICIENTS["landsat8_oli"] @ standard.reshape(6, -1)
    assert float(np.max(np.abs(actual - expected))) <= 1e-6
    assert _sidecar(out)["input_bands"] == [2, 3, 1, 4, 5, 6]


def test_tasseled_cap_nodata_is_preserved(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    rows, cols = 4, 3
    src = _make_raster(
        gdal_rt, tmp_path / "tc_nodata_src.tif", rows=rows, cols=cols, nodata_pixel=(1, 2)
    )
    out = tmp_path / "tc_nodata_out.tif"
    result = _run(
        qapp,
        {"tcSensor": "landsat8_oli", "tcBands": [1, 2, 3, 4, 5, 6]},
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    for band in _inspect(gdal_rt, out)["band_list"]:
        assert band["arr"][1][2] == NODATA
    assert _sidecar(out)["n_valid"] == rows * cols - 1


def test_tasseled_cap_rejections(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "tc_reject_src.tif", rows=4, cols=3)
    out = tmp_path / "tc_reject_out.tif"

    missing = _run(qapp, {"tcSensor": "landsat8_oli"}, src, out)
    assert missing.status is TaskStatus.failed
    assert missing.error and missing.error["code"] == "invalid_parameters"
    assert not out.exists()

    unknown = _run(
        qapp, {"tcSensor": "sentinel2_msi", "tcBands": [1, 2, 3, 4, 5, 6]}, src, out
    )
    assert unknown.status is TaskStatus.failed
    assert unknown.error and unknown.error["code"] == "invalid_parameters"
    assert not out.exists()

    duplicate = _run(
        qapp, {"tcSensor": "landsat8_oli", "tcBands": [1, 1, 2, 3, 4, 5]}, src, out
    )
    assert duplicate.status is TaskStatus.failed
    assert duplicate.error and duplicate.error["code"] == "invalid_parameters"
    assert not out.exists()

    out_of_range = _run(
        qapp, {"tcSensor": "landsat8_oli", "tcBands": [1, 2, 3, 4, 5, 7]}, src, out
    )
    assert out_of_range.status is TaskStatus.failed
    assert out_of_range.error and out_of_range.error["code"] == "tasseledcap_error"
    assert not out.exists()

    png = _run(
        qapp,
        {"tcSensor": "landsat8_oli", "tcBands": [1, 2, 3, 4, 5, 6], "format": "PNG"},
        src,
        tmp_path / "tc.png",
    )
    assert png.status is TaskStatus.failed
    assert png.error and png.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "tc.png").exists()
