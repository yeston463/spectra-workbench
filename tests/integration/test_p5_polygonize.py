"""P5-02 polygonize: labeled raster → vector polygons, 4/8 connectedness, rejections.

数值验收（不能只检查文件存在）：
- 已知标签图 [[3,3,9,9],[3,3,9,9]] → 恰 2 个多边形、DN∈{3,9}、面积各 4.0、SRS 32650；
- 对角同值像元 [[5,0],[0,5]] → 4 连通 2 个 DN=5 多边形、8 连通 1 个（8CONNECTED 实证）；
- OGR 重开校验：worker 在 commit 前后用 inspect_vector 真实读图层与要素。
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


def _make_labeled(gdal_rt: dict[str, Any], path: Path, rows: list[list[int]]) -> Path:
    script = f"""
import json
from osgeo import gdal, osr
rows = json.loads({json.dumps(rows)!r})
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, len(rows[0]), len(rows), 1, gdal.GDT_Byte)
ds.SetGeoTransform((0.0, 1.0, 0.0, float(len(rows)), 0.0, -1.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
for r, row in enumerate(rows):
    ds.GetRasterBand(1).WriteRaster(0, r, len(row), 1, bytes(row), buf_type=gdal.GDT_Byte)
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
        task_id=f"p502p-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:polygonize",
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
                kind=DatasetKind.vector,
                uri=target.resolve().as_uri(),
                overwrite=True,
            )
        ],
        parameters=parameters,
    )
    client = QtWorkerClient(provider_id="gdal", worker_id="p502-polygonize", project_root=str(ROOT))
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


def _inspect_vector(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = f"""
import json
from osgeo import ogr
ds = ogr.Open({str(path)!r})
assert ds is not None
layers = []
for idx in range(ds.GetLayerCount()):
    lyr = ds.GetLayer(idx)
    feats = []
    for f in lyr:
        g = f.GetGeometryRef()
        defn = lyr.GetLayerDefn()
        fields = {{defn.GetFieldDefn(i).GetName(): f.GetField(i) for i in range(defn.GetFieldCount())}}
        feats.append({{
            "fields": fields,
            "area": g.GetArea() if g is not None else None,
            "valid": g.IsValid() if g is not None else None,
        }})
    srs = lyr.GetSpatialRef()
    layers.append({{
        "name": lyr.GetName(),
        "geom_type": lyr.GetGeomType(),
        "feature_count": lyr.GetFeatureCount(),
        "crs": srs.GetAuthorityCode(None) if srs is not None else None,
        "features": feats,
    }})
print(json.dumps({{"driver": ds.GetDriver().GetName(), "layer_count": ds.GetLayerCount(), "layers": layers}}))
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


def test_polygonize_labeled_raster_matches_known_rectangles(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_labeled(gdal_rt, tmp_path / "labels.tif", [[3, 3, 9, 9], [3, 3, 9, 9]])
    out = tmp_path / "poly.gpkg"
    result = _run(qapp, {"format": "GPKG"}, src, out)
    assert result.status is TaskStatus.succeeded, result.error

    first = result.outputs[0]
    assert first["data_type"] == "vector"
    assert first["driver"] == "GPKG"

    info = _inspect_vector(gdal_rt, out)
    assert info["driver"] == "GPKG"
    assert info["layer_count"] == 1
    lyr = info["layers"][0]
    assert lyr["feature_count"] == 2
    assert lyr["crs"] == "32650"
    by_dn = {f["fields"]["DN"]: f for f in lyr["features"]}
    assert set(by_dn) == {3, 9}
    for feat in by_dn.values():
        assert feat["area"] == pytest.approx(4.0)
        assert feat["valid"] is True


def test_polygonize_connectedness_4_vs_8(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_labeled(gdal_rt, tmp_path / "diag.tif", [[5, 0], [0, 5]])

    out4 = tmp_path / "conn4.gpkg"
    result4 = _run(qapp, {"format": "GPKG", "connectedness": 4}, src, out4)
    assert result4.status is TaskStatus.succeeded, result4.error
    info4 = _inspect_vector(gdal_rt, out4)
    dn5_4 = [f for f in info4["layers"][0]["features"] if f["fields"]["DN"] == 5]
    assert len(dn5_4) == 2, "4 邻域下对角像元应各自成多边形"

    out8 = tmp_path / "conn8.gpkg"
    result8 = _run(qapp, {"format": "GPKG", "connectedness": 8}, src, out8)
    assert result8.status is TaskStatus.succeeded, result8.error
    info8 = _inspect_vector(gdal_rt, out8)
    dn5_8 = [f for f in info8["layers"][0]["features"] if f["fields"]["DN"] == 5]
    assert len(dn5_8) == 1, "8 邻域下对角像元应连通为同一个多边形"


def test_polygonize_geojson_single_file_and_default_field(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_labeled(gdal_rt, tmp_path / "labels2.tif", [[3, 3, 9, 9], [3, 3, 9, 9]])
    out = tmp_path / "poly.geojson"
    result = _run(qapp, {"format": "GeoJSON"}, src, out)
    assert result.status is TaskStatus.succeeded, result.error
    # 单文件成果：只允许 .geojson 一个文件落盘
    assert out.exists()
    info = _inspect_vector(gdal_rt, out)
    assert info["driver"] == "GeoJSON"
    lyr = info["layers"][0]
    assert lyr["feature_count"] == 2
    # 未给 fieldName 时默认 DN 字段
    assert all("DN" in f["fields"] for f in lyr["features"])


def test_polygonize_rejects_raster_format_and_vector_input(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_labeled(gdal_rt, tmp_path / "labels3.tif", [[3, 3, 9, 9], [3, 3, 9, 9]])

    rejected = _run(
        qapp, {"format": "GTiff", "fieldName": "DN"}, src, tmp_path / "rej.tif"
    )
    assert rejected.status is TaskStatus.failed
    assert rejected.error and rejected.error["code"] == "unsupported_format_combination"
    assert "GPKG" in rejected.error["message"]
    assert not (tmp_path / "rej.tif").exists()

    # 矢量文件作为输入：无法作为栅格打开
    vec_in = tmp_path / "in.geojson"
    script = f"""
from osgeo import ogr
ds = ogr.GetDriverByName("GeoJSON").CreateDataSource({str(vec_in)!r})
lyr = ds.CreateLayer("pt")
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt("POINT (1 1)"))
lyr.CreateFeature(f)
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
    bad_input = _run(qapp, {"format": "GPKG"}, vec_in, tmp_path / "bad.gpkg")
    assert bad_input.status is TaskStatus.failed
    assert bad_input.error and bad_input.error["code"] == "polygonize_error"
    assert not (tmp_path / "bad.gpkg").exists()


def _make_two_band(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
from osgeo import gdal, osr
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, 4, 2, 2, gdal.GDT_Byte)
ds.SetGeoTransform((0.0, 1.0, 0.0, 2.0, 0.0, -1.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
bands = [[[3, 3, 9, 9], [3, 3, 9, 9]], [[7, 7, 7, 7], [8, 8, 8, 8]]]
for b, rows in enumerate(bands, start=1):
    for r, row in enumerate(rows):
        ds.GetRasterBand(b).WriteRaster(0, r, 4, 1, bytes(row), buf_type=gdal.GDT_Byte)
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


def test_polygonize_shapefile_bundle_commits_all_sidecars(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_labeled(gdal_rt, tmp_path / "labels4.tif", [[3, 3, 9, 9], [3, 3, 9, 9]])
    out = tmp_path / "polyshp.shp"
    result = _run(qapp, {"format": "ESRI Shapefile"}, src, out)
    assert result.status is TaskStatus.succeeded, result.error

    first = result.outputs[0]
    assert first["data_type"] == "vector"
    assert first["driver"] == "ESRI Shapefile"
    # 多文件 bundle：主文件 + .shx/.dbf（.prj/.cpg 视驱动行为）全部登记提交
    committed = {Path(u).suffix.lower() for u in first["committed_files"]}
    assert ".shp" in committed and ".shx" in committed and ".dbf" in committed
    for suffix in (".shp", ".shx", ".dbf"):
        assert out.with_suffix(suffix).exists(), f"{suffix} 未提交"
    # 旧成果残留 sidecar 不被遗留：预置一个旧 .qix（新成果不产生该文件），
    # 覆盖提交后必须随事务删除，而不是与新 .shp 混用
    stale_qix = tmp_path / "polyshp.qix"
    stale_qix.write_bytes(b"stale-spatial-index")
    result2 = _run(qapp, {"format": "Shapefile"}, src, out)
    assert result2.status is TaskStatus.succeeded, result2.error
    assert not stale_qix.exists(), "旧 .qix 应随覆盖提交删除"

    info = _inspect_vector(gdal_rt, out)
    assert info["driver"] == "ESRI Shapefile"
    lyr = info["layers"][0]
    assert lyr["feature_count"] == 2
    by_dn = {f["fields"]["DN"]: f for f in lyr["features"]}
    assert set(by_dn) == {3, 9}
    for feat in by_dn.values():
        assert feat["area"] == pytest.approx(4.0)


def test_polygonize_band_selection_and_range_rejection(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_two_band(gdal_rt, tmp_path / "twoband.tif")

    # band=2：使用第二波段 [[7,7,7,7],[8,8,8,8]] 而不是默认第一波段
    out = tmp_path / "band2.gpkg"
    result = _run(qapp, {"format": "GPKG", "band": 2}, src, out)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect_vector(gdal_rt, out)
    by_dn = {f["fields"]["DN"]: f for f in info["layers"][0]["features"]}
    assert set(by_dn) == {7, 8}
    for feat in by_dn.values():
        assert feat["area"] == pytest.approx(4.0)

    # band 超出范围：明确拒绝且不写目标
    rejected = _run(qapp, {"format": "GPKG", "band": 3}, src, tmp_path / "band3.gpkg")
    assert rejected.status is TaskStatus.failed
    assert rejected.error and rejected.error["code"] == "invalid_parameters"
    assert "3" in rejected.error["message"]
    assert not (tmp_path / "band3.gpkg").exists()

    # band=0 在契约层被拒绝（正整数校验）
    zero = _run(qapp, {"format": "GPKG", "band": 0}, src, tmp_path / "band0.gpkg")
    assert zero.status is TaskStatus.failed
    assert zero.error and zero.error["code"] == "invalid_parameters"
    assert not (tmp_path / "band0.gpkg").exists()
