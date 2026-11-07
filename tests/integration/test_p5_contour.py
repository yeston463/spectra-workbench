"""P5-02 contour: analytic plane z=2x+3y, levels and vertex geometry verified.

数值验收（不能只检查文件存在）：
- 解析平面 z=2*col+3*row（8×8、像元 1、北向上仿射）→ interval=1 时
  ELEV 集合 = {1..35}，每个要素顶点要么在栅格边界上（等值线被裁剪到
  数据集范围），要么满足 |2*col + 3*row − ELEV| < 1e-4（像元中心空间）；
- offset 是等差序列的对齐点：interval=2 时 offset=0/1 产生偶/奇数等级；
- GeoJSON 单文件输出同性质；缺 interval / interval=0 / PNG 明确拒绝。
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

#: 解析平面仿射（与 fixture 一致）：地图 (x, y) → 像元中心 (col=x-0.5, row=7.5-y)
_WIDTH = 8
_HEIGHT = 8


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


def _make_plane(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
import array
from osgeo import gdal, osr
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, 8, 8, 1, gdal.GDT_Float32)
ds.SetGeoTransform((0.0, 1.0, 0.0, 8.0, 0.0, -1.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
vals = array.array("f", [2 * c + 3 * r for r in range(8) for c in range(8)])
ds.GetRasterBand(1).WriteRaster(0, 0, 8, 8, vals.tobytes(), buf_type=gdal.GDT_Float32)
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
        task_id=f"p502c-{uuid.uuid4().hex[:10]}",
        attempt=1,
        capability_id="gdal:contour",
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
    client = QtWorkerClient(provider_id="gdal", worker_id="p502-contour", project_root=str(ROOT))
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


def _inspect_contours(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = f"""
import json
from osgeo import ogr
ds = ogr.Open({str(path)!r})
assert ds is not None
lyr = ds.GetLayer(0)
srs = lyr.GetSpatialRef()
lines = []
for f in lyr:
    g = f.GetGeometryRef()
    pts = []
    for i in range(g.GetPointCount()):
        x, y, z = g.GetPoint(i)
        pts.append([x, y, z])
    lines.append({{"elev": f.GetField("ELEV"), "id": f.GetField("ID"), "points": pts}})
print(json.dumps({{
    "driver": ds.GetDriver().GetName(),
    "geom_type": ogr.GeometryTypeToName(lyr.GetGeomType()),
    "crs": srs.GetAuthorityCode(None) if srs is not None else None,
    "lines": lines,
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


def _assert_analytic(info: dict[str, Any], *, expected_levels: set[float]) -> None:
    """逐顶点解析校验：内部顶点位于等值线上，边界顶点被裁剪到数据集范围。"""
    assert info["geom_type"] == "3D Line String"
    assert info["crs"] == "32650"
    elevs = {float(line["elev"]) for line in info["lines"]}
    assert elevs == expected_levels
    ids = [int(line["id"]) for line in info["lines"]]
    assert ids == sorted(ids), "ID 字段应按等级顺序递增"
    assert all(line["points"] for line in info["lines"]), "等值线不应有空几何"
    for line in info["lines"]:
        elev = float(line["elev"])
        assert info["lines"] is not None
        for x, y, z in line["points"]:
            assert z == pytest.approx(elev, abs=1e-4), "3D 顶点 Z 应等于 ELEV"
            on_boundary = min(abs(x - 0.0), abs(x - 8.0), abs(y - 0.0), abs(y - 8.0)) < 1e-9
            if on_boundary:
                continue
            col, row = x - 0.5, 7.5 - y
            assert abs(2 * col + 3 * row - elev) < 1e-4, (
                f"内部顶点应满足解析平面：({x}, {y}) vs ELEV={elev}"
            )


def test_contour_levels_and_vertices_match_analytic_plane(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_plane(gdal_rt, tmp_path / "plane.tif")
    out = tmp_path / "contour.gpkg"
    result = _run(qapp, {"format": "GPKG", "interval": 1.0}, src, out)
    assert result.status is TaskStatus.succeeded, result.error

    first = result.outputs[0]
    assert first["data_type"] == "vector"
    assert first["driver"] == "GPKG"
    assert first["feature_count"] == 35

    info = _inspect_contours(gdal_rt, out)
    # 平面 z=2c+3r 的取值范围 [0, 35]；interval=1、offset=0 → 等级 {1..35}
    # （0 级仅在 (0,0) 单点退化，无等值线）
    _assert_analytic(info, expected_levels={float(k) for k in range(1, 36)})


def test_contour_offset_aligns_level_sequence(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    # offset 是等差序列的**对齐点**（GDAL 会向两侧延伸覆盖数据范围），
    # 不是等级下限；interval=2 时 offset=0/1 产生偶/奇数等级，差异可解析断言。
    src = _make_plane(gdal_rt, tmp_path / "plane2.tif")

    even = tmp_path / "contour_even.gpkg"
    result = _run(qapp, {"format": "GPKG", "interval": 2.0}, src, even)
    assert result.status is TaskStatus.succeeded, result.error
    _assert_analytic(
        _inspect_contours(gdal_rt, even),
        expected_levels={float(k) for k in range(2, 36, 2)},
    )

    odd = tmp_path / "contour_odd.gpkg"
    result2 = _run(qapp, {"format": "GPKG", "interval": 2.0, "offset": 1.0}, src, odd)
    assert result2.status is TaskStatus.succeeded, result2.error
    _assert_analytic(
        _inspect_contours(gdal_rt, odd),
        expected_levels={float(k) for k in range(1, 36, 2)},
    )


def test_contour_geojson_single_file(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_plane(gdal_rt, tmp_path / "plane3.tif")
    out = tmp_path / "contour.geojson"
    result = _run(qapp, {"format": "GeoJSON", "interval": 1.0}, src, out)
    assert result.status is TaskStatus.succeeded, result.error
    assert out.exists()
    info = _inspect_contours(gdal_rt, out)
    assert info["driver"] == "GeoJSON"
    _assert_analytic(info, expected_levels={float(k) for k in range(1, 36)})


def test_contour_fixed_levels_replace_interval(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    # GDAL 3.13.3 实测：fixedLevels 非空时**完全替代** interval/base 等差序列
    # （interval 甚至可以不给）。ELEV 应恰为给定等级，顶点仍逐点位于解析等值线上。
    src = _make_plane(gdal_rt, tmp_path / "plane_fixed.tif")
    out = tmp_path / "contour_fixed.gpkg"
    result = _run(qapp, {"format": "GPKG", "fixedLevels": [2.5, 5.5]}, src, out)
    assert result.status is TaskStatus.succeeded, result.error
    _assert_analytic(_inspect_contours(gdal_rt, out), expected_levels={2.5, 5.5})

    # fixedLevels 为空列表：契约层拒绝（非空校验）
    empty = _run(qapp, {"format": "GPKG", "fixedLevels": []}, src, tmp_path / "empty.gpkg")
    assert empty.status is TaskStatus.failed
    assert empty.error and empty.error["code"] == "invalid_parameters"
    assert not (tmp_path / "empty.gpkg").exists()


def test_contour_shapefile_bundle_and_band(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_plane(gdal_rt, tmp_path / "plane_shp.tif")
    out = tmp_path / "contour.shp"
    result = _run(qapp, {"format": "ESRI Shapefile", "interval": 5.0}, src, out)
    assert result.status is TaskStatus.succeeded, result.error
    first = result.outputs[0]
    assert first["driver"] == "ESRI Shapefile"
    committed = {Path(u).suffix.lower() for u in first["committed_files"]}
    assert {".shp", ".shx", ".dbf"} <= committed
    info = _inspect_contours(gdal_rt, out)
    assert info["driver"] == "ESRI Shapefile"
    _assert_analytic(
        info,
        expected_levels={float(k) for k in range(5, 36, 5)},
    )


def test_contour_rejects_missing_or_bad_interval_and_raster_format(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_plane(gdal_rt, tmp_path / "plane4.tif")

    missing = _run(qapp, {"format": "GPKG"}, src, tmp_path / "missing.gpkg")
    assert missing.status is TaskStatus.failed
    assert missing.error and missing.error["code"] == "invalid_parameters"
    assert "interval" in missing.error["message"]
    assert not (tmp_path / "missing.gpkg").exists()

    zero = _run(qapp, {"format": "GPKG", "interval": 0.0}, src, tmp_path / "zero.gpkg")
    assert zero.status is TaskStatus.failed
    assert zero.error and zero.error["code"] == "invalid_parameters"
    assert not (tmp_path / "zero.gpkg").exists()

    rejected = _run(qapp, {"format": "PNG", "interval": 1.0}, src, tmp_path / "rej.png")
    assert rejected.status is TaskStatus.failed
    assert rejected.error and rejected.error["code"] == "unsupported_format_combination"
    assert not (tmp_path / "rej.png").exists()
