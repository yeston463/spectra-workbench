"""P5-03 分页属性表：worker 侧按范围读取的数值验收。

不能只检查「能读到数据」：
- 分页边界：150 要素按页 100 读取 → 恰 [0..99] 与 [100..149]；
- 空几何（NULL geometry）与多部件（MultiPolygon 2 部件）逐要素如实标注；
- 编码保留：GBK 编码的 Shapefile（.cpg）中文字段名/字段值往返一致；
- 大图层无一次性全读：单页 count>1000 直接拒绝；payload 恰为请求页大小。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
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


def _run_script(gdal_rt: dict[str, Any], script: str) -> None:
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, proc.stderr


def _make_points_gpkg(gdal_rt: dict[str, Any], path: Path, total: int = 150) -> Path:
    script = f"""
import json
from osgeo import ogr, osr
drv = ogr.GetDriverByName("GPKG")
ds = drv.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
lyr = ds.CreateLayer("points", srs, ogr.wkbPoint)
lyr.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))
lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
for i in range({total}):
    f = ogr.Feature(lyr.GetLayerDefn())
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(float(i % 15), float(i // 15))
    f.SetGeometry(g)
    f.SetField("value", i)
    f.SetField("name", "p%03d" % i)
    lyr.CreateFeature(f)
ds = None
print("OK")
"""
    _run_script(gdal_rt, script)
    return path


def _make_mixed_gpkg(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
from osgeo import ogr, osr
drv = ogr.GetDriverByName("GPKG")
ds = drv.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
lyr = ds.CreateLayer("mixed", srs, ogr.wkbUnknown)
lyr.CreateField(ogr.FieldDefn("tag", ogr.OFTString))

# 正常多边形
f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("tag", "normal")
ring = ogr.Geometry(ogr.wkbLinearRing)
for x, y in ((0, 0), (1, 0), (1, 1), (0, 1), (0, 0)):
    ring.AddPoint_2D(x, y)
poly = ogr.Geometry(ogr.wkbPolygon); poly.AddGeometry(ring)
f.SetGeometry(poly); lyr.CreateFeature(f)

# 空几何（NULL）
f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("tag", "null_geom")
lyr.CreateFeature(f)

# 多部件：MultiPolygon 2 部件
f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("tag", "multipart")
mp = ogr.Geometry(ogr.wkbMultiPolygon)
for x0 in (0.0, 2.0):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, 0), (x0 + 1, 0), (x0 + 1, 1), (x0, 1), (x0, 0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon); poly.AddGeometry(ring)
    mp.AddGeometry(poly)
f.SetGeometry(mp); lyr.CreateFeature(f)

# 显式空多边形
f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("tag", "empty_geom")
empty = ogr.Geometry(ogr.wkbPolygon); empty.Empty()
f.SetGeometry(empty); lyr.CreateFeature(f)
ds = None
print("OK")
"""
    _run_script(gdal_rt, script)
    return path


def _make_gbk_shapefile(gdal_rt: dict[str, Any], path: Path) -> Path:
    rows = [("地块甲", 1), ("地块乙", 2), ("地块丙", 3)]
    script = f"""
from osgeo import ogr, osr
drv = ogr.GetDriverByName("ESRI Shapefile")
ds = drv.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
lyr = ds.CreateLayer("区域", srs, ogr.wkbPolygon, options=["ENCODING=GBK"])
lyr.CreateField(ogr.FieldDefn("名称", ogr.OFTString))
lyr.CreateField(ogr.FieldDefn("数值", ogr.OFTInteger))
rows = {rows!r}
for name, value in rows:
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("名称", name)
    f.SetField("数值", value)
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((0, 0), (1, 0), (1, 1), (0, 1), (0, 0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon); poly.AddGeometry(ring)
    f.SetGeometry(poly)
    lyr.CreateFeature(f)
ds = None
print("OK")
"""
    _run_script(gdal_rt, script)
    return path


def _page(client: QtWorkerClient, path: Path, **kwargs: Any) -> dict[str, Any]:
    return client.read_vector_page(path, **kwargs)


def test_vector_page_boundaries_on_150_points(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_points_gpkg(gdal_rt, tmp_path / "points.gpkg", total=150)
    client = QtWorkerClient(provider_id="gdal", worker_id="p503-page", project_root=str(ROOT))
    try:
        client.start(timeout=25.0)
        page0 = _page(client, src, start=0, count=100)
        assert page0["returned_count"] == 100
        assert page0["feature_count"] == 150
        assert page0["feature_count_expensive"] is False
        # GPKG 的 FID 来自 SQLite rowid，1-based
        assert [f["fid"] for f in page0["features"]] == list(range(1, 101))
        assert page0["features"][0]["fields"] == {"value": 0, "name": "p000"}
        assert page0["features"][42]["fields"] == {"value": 42, "name": "p042"}

        page1 = _page(client, src, start=100, count=100)
        assert page1["returned_count"] == 50
        assert [f["fid"] for f in page1["features"]] == list(range(101, 151))
        assert page1["features"][-1]["fields"]["name"] == "p149"

        # 起点正好在末尾：0 条，不是错误
        tail = _page(client, src, start=150, count=100)
        assert tail["returned_count"] == 0
        assert tail["features"] == []

        # 越过末尾：0 条
        beyond = _page(client, src, start=500, count=100)
        assert beyond["returned_count"] == 0

        # 小页逐页拼接 = 全表（范围查询正确性；FID 1-based）
        collected: list[int] = []
        start = 0
        while True:
            page = _page(client, src, start=start, count=60)
            collected.extend(f["fid"] for f in page["features"])
            if page["returned_count"] < 60:
                break
            start += 60
        assert collected == list(range(1, 151))
    finally:
        client.stop(timeout=5.0)


def test_vector_page_rejects_oversized_and_bad_range(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_points_gpkg(gdal_rt, tmp_path / "points2.gpkg", total=10)
    client = QtWorkerClient(provider_id="gdal", worker_id="p503-guard", project_root=str(ROOT))
    try:
        client.start(timeout=25.0)
        # 单页超过硬上限：拒绝（从接口上杜绝一次性全读）
        with pytest.raises(Exception, match="不允许一次性取全表"):
            _page(client, src, start=0, count=1001)
        with pytest.raises(Exception, match="count 必须"):
            _page(client, src, start=0, count=0)
        with pytest.raises(Exception, match="start 必须"):
            _page(client, src, start=-5, count=10)
        with pytest.raises(Exception, match="超出范围"):
            _page(client, src, layer_index=5, start=0, count=10)
    finally:
        client.stop(timeout=5.0)


def test_vector_page_marks_null_and_multipart_geometry(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_mixed_gpkg(gdal_rt, tmp_path / "mixed.gpkg")
    client = QtWorkerClient(provider_id="gdal", worker_id="p503-geom", project_root=str(ROOT))
    try:
        client.start(timeout=25.0)
        page = _page(client, src, start=0, count=100)
        assert page["returned_count"] == 4
        by_tag = {f["fields"]["tag"]: f for f in page["features"]}

        normal = by_tag["normal"]
        assert normal["geometry_type"] == "Polygon"
        assert normal["empty_geometry"] is False
        assert normal["part_count"] == 1

        null_geom = by_tag["null_geom"]
        assert null_geom["geometry_type"] is None
        assert null_geom["empty_geometry"] is True

        multipart = by_tag["multipart"]
        assert multipart["geometry_type"] == "Multi Polygon"
        assert multipart["empty_geometry"] is False
        assert multipart["part_count"] == 2

        empty_geom = by_tag["empty_geom"]
        assert empty_geom["geometry_type"] == "Polygon"
        assert empty_geom["empty_geometry"] is True
    finally:
        client.stop(timeout=5.0)


def test_vector_page_preserves_gbk_encoding(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_gbk_shapefile(gdal_rt, tmp_path / "gbk.shp")
    assert (tmp_path / "gbk.cpg").exists(), "ENCODING=GBK 应写出 .cpg"
    assert (tmp_path / "gbk.cpg").read_text(encoding="ascii").strip().upper() == "GBK"

    client = QtWorkerClient(provider_id="gdal", worker_id="p503-gbk", project_root=str(ROOT))
    try:
        client.start(timeout=25.0)
        page = _page(client, src, start=0, count=100)
        assert page["returned_count"] == 3
        assert [f["fields"]["名称"] for f in page["features"]] == ["地块甲", "地块乙", "地块丙"]
        assert [f["fields"]["数值"] for f in page["features"]] == [1, 2, 3]
        # 字段名（DBF 头）同样按 .cpg 解码
        assert [f["name"] for f in page["fields"]] == ["名称", "数值"]
    finally:
        client.stop(timeout=5.0)


def test_worker_returns_layer_and_crs_metadata(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_points_gpkg(gdal_rt, tmp_path / "points3.gpkg", total=3)
    client = QtWorkerClient(provider_id="gdal", worker_id="p503-meta", project_root=str(ROOT))
    try:
        client.start(timeout=25.0)
        page = _page(client, src, start=0, count=10)
        assert page["layer_name"] == "points"
        assert page["layer_count"] == 1
        assert page["driver"] == "GPKG"
        assert page["crs_epsg"] == 32650
        assert [f["name"] for f in page["fields"]] == ["value", "name"]
        payload = json.dumps(page, ensure_ascii=False)
        assert len(payload) < 8_000, "单页 payload 应保持小（分页，不携带 WKT 全量几何）"
    finally:
        client.stop(timeout=5.0)
