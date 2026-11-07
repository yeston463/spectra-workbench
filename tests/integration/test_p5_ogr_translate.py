"""P5-04 ogr:vectortranslate：已知坐标、空间关系与字段值验证。

验收对应：
- 独立已知坐标：UTM 50N (500000, 3000000) → EPSG:4326 = (117.0, 27.1224696415507)，
  运行时实测（独立 osr 计算与 VectorTranslate 输出一致）；
- 轴顺序明确：输出几何 X=经度、Y=纬度（传统 GIS 顺序），断言 X≈117 而非 27；
- 空间关系：spat 过滤框（源 CRS 单位）选出预期子集；
- 字段值：where 子句与 select 字段列表逐值断言；
- 假重投影防护：源图层无 SRS 时明确拒绝（实测 ogr2ogr 只改标签不换算坐标）。
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


def _run_script(gdal_rt: dict[str, Any], script: str) -> None:
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, proc.stderr


def _make_utm_point(gdal_rt: dict[str, Any], path: Path, *, with_srs: bool) -> Path:
    script = f"""
from osgeo import ogr, osr
drv = ogr.GetDriverByName("GeoJSON")
ds = drv.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650) if {with_srs} else None
lyr = ds.CreateLayer("utm", srs, ogr.wkbPoint)
lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("v", 7)
g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(500000.0, 3000000.0)
f.SetGeometry(g); lyr.CreateFeature(f)
ds = None
print("OK")
"""
    _run_script(gdal_rt, script)
    return path


def _make_grid_gpkg(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
from osgeo import ogr, osr
drv = ogr.GetDriverByName("GPKG")
ds = drv.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
lyr = ds.CreateLayer("grid", srs, ogr.wkbPoint)
lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
i = 0
for y in range(5):
    for x in range(5):
        f = ogr.Feature(lyr.GetLayerDefn())
        g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(float(x), float(y))
        f.SetGeometry(g)
        f.SetField("v", i)
        f.SetField("name", "g%02d" % i)
        lyr.CreateFeature(f)
        i += 1
ds = None
print("OK")
"""
    _run_script(gdal_rt, script)
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
        task_id=f"p504-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="ogr:vectortranslate",
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
                kind=DatasetKind.vector,
                uri=target.resolve().as_uri(),
                overwrite=True,
            )
        ],
        parameters=parameters,
    )
    client = QtWorkerClient(provider_id="gdal", worker_id="p504-vt", project_root=str(ROOT))
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
from osgeo import ogr
ds = ogr.Open({str(path)!r})
assert ds is not None
lyr = ds.GetLayer(0)
rows = []
for f in lyr:
    g = f.GetGeometryRef()
    defn = lyr.GetLayerDefn()
    fields = {{defn.GetFieldDefn(i).GetName(): f.GetField(i) for i in range(defn.GetFieldCount())}}
    rows.append({{
        "fields": fields,
        "x": g.GetX() if g is not None else None,
        "y": g.GetY() if g is not None else None,
    }})
srs = lyr.GetSpatialRef()
print(json.dumps({{
    "driver": ds.GetDriver().GetName(),
    "count": lyr.GetFeatureCount(),
    "crs": srs.GetAuthorityCode(None) if srs is not None else None,
    "field_names": [lyr.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(lyr.GetLayerDefn().GetFieldCount())],
    "rows": rows,
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


def test_translate_reprojects_known_coordinates_with_explicit_axis_order(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_utm_point(gdal_rt, tmp_path / "utm.geojson", with_srs=True)
    out = tmp_path / "wgs84.gpkg"
    result = _run(qapp, {"format": "GPKG", "dstSRS": "EPSG:4326"}, src, out)
    assert result.status is TaskStatus.succeeded, result.error

    info = _inspect(gdal_rt, out)
    assert info["driver"] == "GPKG"
    assert info["crs"] == "4326"
    assert info["count"] == 1
    row = info["rows"][0]
    # 轴顺序明确：传统 GIS 顺序，X=经度（≈117），Y=纬度（≈27.12）
    assert row["x"] == pytest.approx(117.0, abs=1e-6)
    assert row["y"] == pytest.approx(27.1224696415507, abs=1e-6)
    assert row["fields"]["v"] == 7


def test_translate_rejects_reprojection_without_source_srs(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    # 注意：GeoJSON 驱动会给无 SRS 图层自动补 WGS84（RFC 7946 假设），
    # 无法构造「无空间参考」的源——用 GPKG 的 undefined SRS (srs_id=0)。
    src = tmp_path / "nosrs.gpkg"
    script = f"""
from osgeo import ogr
drv = ogr.GetDriverByName("GPKG")
ds = drv.CreateDataSource({str(src)!r})
lyr = ds.CreateLayer("nosrs", None, ogr.wkbPoint)
lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("v", 7)
g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(500000.0, 3000000.0)
f.SetGeometry(g); lyr.CreateFeature(f)
ds = None
print("OK")
"""
    _run_script(gdal_rt, script)
    out = tmp_path / "fake.gpkg"
    result = _run(qapp, {"format": "GPKG", "dstSRS": "EPSG:4326"}, src, out)
    assert result.status is TaskStatus.failed
    assert result.error and result.error["code"] == "invalid_parameters"
    assert "无空间参考" in result.error["message"]
    assert not out.exists(), "假重投影不得落盘"


def test_translate_where_select_and_spatial_filter(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_grid_gpkg(gdal_rt, tmp_path / "grid.gpkg")

    # where：字段值验证（25 点中 v>=20 恰 5 个）
    out_where = tmp_path / "where.gpkg"
    result = _run(qapp, {"format": "GPKG", "where": "v >= 20"}, src, out_where)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out_where)
    assert info["count"] == 5
    assert sorted(r["fields"]["v"] for r in info["rows"]) == [20, 21, 22, 23, 24]
    assert info["field_names"] == ["v", "name"]

    # select：字段子集验证
    out_select = tmp_path / "select.gpkg"
    result2 = _run(qapp, {"format": "GPKG", "select": ["name"]}, src, out_select)
    assert result2.status is TaskStatus.succeeded, result2.error
    info2 = _inspect(gdal_rt, out_select)
    assert info2["field_names"] == ["name"]
    assert info2["count"] == 25

    # spat：空间关系验证（bbox (2,2,3,3) 命中 x/y ∈ {2,3} 的 4 个点，源 CRS 单位=米）
    out_spat = tmp_path / "spat.gpkg"
    result3 = _run(qapp, {"format": "GPKG", "spat": [2.0, 2.0, 3.0, 3.0]}, src, out_spat)
    assert result3.status is TaskStatus.succeeded, result3.error
    info3 = _inspect(gdal_rt, out_spat)
    assert info3["count"] == 4
    assert {r["fields"]["name"] for r in info3["rows"]} == {"g12", "g13", "g17", "g18"}

    # 反向 bbox：明确拒绝（min > max）
    bad = _run(
        qapp,
        {"format": "GPKG", "spat": [3.0, 3.0, 2.0, 2.0]},
        src,
        tmp_path / "bad.gpkg",
    )
    assert bad.status is TaskStatus.failed
    assert bad.error and bad.error["code"] == "invalid_parameters"
    assert not (tmp_path / "bad.gpkg").exists()


def test_translate_sql_query_by_field_values(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_grid_gpkg(gdal_rt, tmp_path / "grid2.gpkg")
    out = tmp_path / "sql.gpkg"
    result = _run(
        qapp,
        {"format": "GPKG", "sql": "SELECT name, v FROM grid WHERE v < 5 ORDER BY v"},
        src,
        out,
    )
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["count"] == 5
    assert info["field_names"] == ["name", "v"]
    assert [r["fields"]["v"] for r in info["rows"]] == [0, 1, 2, 3, 4]


def test_translate_format_passthrough_and_flatgeobuf(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_grid_gpkg(gdal_rt, tmp_path / "grid3.gpkg")

    # 无 dstSRS：坐标原样透传（不重投影），SRS 保持源 CRS
    out_same = tmp_path / "same.gpkg"
    result = _run(qapp, {"format": "GPKG"}, src, out_same)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out_same)
    assert info["crs"] == "32650"
    assert info["rows"][0]["x"] == 0.0 and info["rows"][0]["y"] == 0.0
    assert info["count"] == 25

    # FlatGeobuf 单文件输出（驱动实测可用；句柄关闭后才落盘）
    out_fgb = tmp_path / "out.fgb"
    result2 = _run(qapp, {"format": "FlatGeobuf", "where": "v = 11"}, src, out_fgb)
    assert result2.status is TaskStatus.succeeded, result2.error
    assert result2.outputs[0]["driver"] == "FlatGeobuf"
    info2 = _inspect(gdal_rt, out_fgb)
    assert info2["driver"] == "FlatGeobuf"
    assert info2["count"] == 1
    assert info2["rows"][0]["fields"]["v"] == 11
