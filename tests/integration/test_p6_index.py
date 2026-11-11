"""P6-01 光谱指数：物理量/原值对应、合成值与无效区符合公式。

fixture（3×2 双波段 Float32，EPSG:32650）：
- 波段 A（NIR，scale=0.1，NoData=-1）：[[200, 100, 30], [100, -1, 200]]
- 波段 B（Red，scale=0.2，无 NoData）：[[100, 100, -30], [100, 100, 100]]

数值验收：
- NDVI 原值（usePhysical=false）：(200−100)/300=1/3、(100−100)/200=0（合法零）、
  A+B=0 → NoData（数学无效）、A=-1（NoData 标签）→ NoData（输入无效）；
- NDVI 物理量（usePhysical=true，A=×0.1、B=×0.2）：同一像元数值不同
  （如 (0,0) 原值 1/3 → 物理量 0.0），证明波段元数据真实参与计算；
- SAVI：(100/300.5)×1.5≈0.499168（L=0.5）；
- NDWI 公式与拒绝路径：缺 bandA/bandB、bandA==bandB、越界波段、未知指数、PNG。
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


def _make_two_band(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
from osgeo import gdal, osr
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, 3, 2, 2, gdal.GDT_Float32)
ds.SetGeoTransform((0.0, 10.0, 0.0, 20.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
a = [[200.0, 100.0, 30.0], [100.0, -1.0, 200.0]]
b = [[100.0, 100.0, -30.0], [100.0, 100.0, 100.0]]
import array
for band_idx, rows, scale in ((1, a, 0.1), (2, b, 0.2)):
    band = ds.GetRasterBand(band_idx)
    flat = array.array("f", [v for row in rows for v in row])
    band.WriteRaster(0, 0, 3, 2, flat.tobytes(), buf_type=gdal.GDT_Float32)
    band.SetScale(scale)
band = ds.GetRasterBand(1)
band.SetNoDataValue(-1.0)
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
        task_id=f"p601-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:sindex",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p601-sindex", project_root=str(ROOT))
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


NODATA = -9999.0


def test_ndvi_raw_values_match_formula(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_two_band(gdal_rt, tmp_path / "twoband.tif")
    out = tmp_path / "ndvi_raw.tif"
    result = _run(
        qapp,
        {"sindex": "ndvi", "bandA": 1, "bandB": 2, "usePhysical": False},
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["bands"] == 1 and info["dtype"] == "Float32"
    assert info["nodata"] == NODATA
    # 合成值逐像元符合公式：(A-B)/(A+B)
    assert info["arr"][0][0] == pytest.approx(1.0 / 3.0, abs=1e-6)
    assert info["arr"][0][1] == pytest.approx(0.0, abs=1e-6)  # 合法零值不是 NoData
    assert info["arr"][0][2] == NODATA  # 分母为零 → 数学无效
    assert info["arr"][1][0] == pytest.approx(0.0, abs=1e-6)
    assert info["arr"][1][1] == NODATA  # A=-1 命中 NoData 标签 → 输入无效
    assert info["arr"][1][2] == pytest.approx(1.0 / 3.0, abs=1e-6)


def test_ndvi_physical_values_follow_band_metadata(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_two_band(gdal_rt, tmp_path / "twoband2.tif")
    out = tmp_path / "ndvi_phys.tif"
    result = _run(qapp, {"sindex": "ndvi", "bandA": 1, "bandB": 2}, src, out)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    # 物理值 = 原值 × scale（A×0.1、B×0.2）；NoData 检测仍按原始值（A=-1）
    # (0,0): (20-20)/40 = 0；(1,0): (10-20)/30 = -1/3；(2,0): (3+6)/(3-6) = -3
    assert info["arr"][0][0] == pytest.approx(0.0, abs=1e-6)
    assert info["arr"][0][1] == pytest.approx(-1.0 / 3.0, abs=1e-6)
    assert info["arr"][0][2] == pytest.approx(-3.0, abs=1e-6)
    assert info["arr"][1][0] == pytest.approx(-1.0 / 3.0, abs=1e-6)
    assert info["arr"][1][1] == NODATA
    assert info["arr"][1][2] == pytest.approx(0.0, abs=1e-6)
    # 与原值结果不同：波段元数据真实参与（物理量/原值对应）
    raw = (
        _inspect(gdal_rt, tmp_path / "ndvi_raw.tif")
        if (tmp_path / "ndvi_raw.tif").exists()
        else None
    )
    if raw is not None:
        assert raw["arr"][0][0] != info["arr"][0][0]


def test_ndwi_and_savi_formulas(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_two_band(gdal_rt, tmp_path / "twoband3.tif")

    out_ndwi = tmp_path / "ndwi.tif"
    result = _run(
        qapp,
        {
            "sindex": "ndwi",
            "bandA": 2,
            "bandB": 1,
            "usePhysical": False,
        },
        src,
        out_ndwi,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out_ndwi)
    # NDWI = (Green−NIR)/(Green+NIR)：bandA=2 作 Green、bandB=1 作 NIR
    assert info["arr"][0][0] == pytest.approx(-1.0 / 3.0, abs=1e-6)

    out_savi = tmp_path / "savi.tif"
    result2 = _run(
        qapp,
        {
            "sindex": "savi",
            "bandA": 1,
            "bandB": 2,
            "usePhysical": False,
        },
        src,
        out_savi,
    )
    assert result2.status is TaskStatus.succeeded, result2.error
    info2 = _inspect(gdal_rt, out_savi)
    # SAVI = ((200-100)/(200+100+0.5))×1.5
    assert info2["arr"][0][0] == pytest.approx((100.0 / 300.5) * 1.5, abs=1e-6)

    # saviL 自定义：L=0 → SAVI 退化为 NDVI
    out_l0 = tmp_path / "savi_l0.tif"
    result3 = _run(
        qapp,
        {
            "sindex": "savi",
            "bandA": 1,
            "bandB": 2,
            "saviL": 0.0,
            "usePhysical": False,
        },
        src,
        out_l0,
    )
    assert result3.status is TaskStatus.succeeded, result3.error
    info3 = _inspect(gdal_rt, out_l0)
    assert info3["arr"][0][0] == pytest.approx(1.0 / 3.0, abs=1e-6)


def test_sindex_rejections(qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path) -> None:
    src = _make_two_band(gdal_rt, tmp_path / "twoband4.tif")

    missing = _run(qapp, {"sindex": "ndvi"}, src, tmp_path / "missing.tif")
    assert missing.status is TaskStatus.failed
    assert missing.error and missing.error["code"] == "invalid_parameters"
    assert "bandA" in missing.error["message"]
    assert not (tmp_path / "missing.tif").exists()

    same = _run(
        qapp,
        {"sindex": "ndvi", "bandA": 2, "bandB": 2},
        src,
        tmp_path / "same.tif",
    )
    assert same.status is TaskStatus.failed
    assert same.error and same.error["code"] == "invalid_parameters"
    assert not (tmp_path / "same.tif").exists()

    out_of_range = _run(
        qapp,
        {"sindex": "ndvi", "bandA": 1, "bandB": 9},
        src,
        tmp_path / "oob.tif",
    )
    assert out_of_range.status is TaskStatus.failed
    assert out_of_range.error and out_of_range.error["code"] == "sindex_error"
    assert "超出范围" in out_of_range.error["message"]
    assert not (tmp_path / "oob.tif").exists()

    unknown = _run(
        qapp,
        {"sindex": "evi2", "bandA": 1, "bandB": 2},
        src,
        tmp_path / "unknown.tif",
    )
    assert unknown.status is TaskStatus.failed
    assert unknown.error and unknown.error["code"] == "invalid_parameters"
    assert "未知光谱指数" in unknown.error["message"]
    assert not (tmp_path / "unknown.tif").exists()

    png = _run(
        qapp,
        {"sindex": "ndvi", "bandA": 1, "bandB": 2, "format": "PNG"},
        src,
        tmp_path / "out.png",
    )
    assert png.status is TaskStatus.failed
    assert png.error and png.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "out.png").exists()
