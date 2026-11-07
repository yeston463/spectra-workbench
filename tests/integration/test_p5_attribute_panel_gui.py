"""P5-03 属性表面板桌面路径：真实 MainWindow + worker + 分页交互。

数值验收：
- 150 点 GPKG：属性表恰显示第一页 100 行、表头含字段名、页码「1 / 2」、
  要素数「150」；点击下一页后首行 value=100（分页真实翻页）；
- 混合几何 fixture：「几何」列如实标注空几何与多部件（×2）；
- 全程只按页索取（面板不请求全表）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.engine, pytest.mark.gui]

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
f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("tag", "multipart")
mp = ogr.Geometry(ogr.wkbMultiPolygon)
for x0 in (0.0, 2.0):
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, 0), (x0 + 1, 0), (x0 + 1, 1), (x0, 1), (x0, 0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon); poly.AddGeometry(ring)
    mp.AddGeometry(poly)
f.SetGeometry(mp); lyr.CreateFeature(f)
f = ogr.Feature(lyr.GetLayerDefn()); f.SetField("tag", "null_geom")
lyr.CreateFeature(f)
ds = None
print("OK")
"""
    _run_script(gdal_rt, script)
    return path


def _pump(qapp: QApplication, predicate: Any, timeout: float = 30.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{label}")


def _cell(win: MainWindow, row: int, col: int) -> str:
    model = win.attribute_panel.table.model()
    return str(model.index(row, col).data() or "")


def test_attribute_panel_pages_through_150_points(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_points_gpkg(gdal_rt, tmp_path / "points.gpkg", total=150)
    win = MainWindow(project_root=tmp_path)
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(src)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开点图层")
        win.tab_right.setCurrentWidget(win.attribute_panel)
        model = win.attribute_panel.table.model()
        _pump(qapp, lambda: model.rowCount() == 100, label="第一页 100 行")

        # 表头：FID | 几何 | value | name
        headers = [
            model.headerData(c, Qt.Orientation.Horizontal)
            for c in range(model.columnCount())
        ]
        assert headers == ["FID", "几何", "value", "name"], headers
        assert model.rowCount() == 100
        assert win.attribute_panel.lbl_count.text() == "要素数：150"
        assert win.attribute_panel.lbl_page.text() == "第 1 / 2 页"
        assert not win.attribute_panel.btn_prev.isEnabled()
        assert win.attribute_panel.btn_next.isEnabled()
        assert _cell(win, 0, 0) == "1" and _cell(win, 0, 2) == "0"
        assert _cell(win, 99, 3) == "p099"

        # 翻页：真实按钮点击 → 第二页 50 行
        QTest.mouseClick(win.attribute_panel.btn_next, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: model.rowCount() == 50, label="第二页 50 行")
        assert win.attribute_panel.lbl_page.text() == "第 2 / 2 页"
        assert _cell(win, 0, 2) == "100"
        assert _cell(win, 49, 3) == "p149"
        assert not win.attribute_panel.btn_next.isEnabled()
        assert win.attribute_panel.btn_prev.isEnabled()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive


def test_attribute_panel_marks_empty_and_multipart_geometry(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_mixed_gpkg(gdal_rt, tmp_path / "mixed.gpkg")
    win = MainWindow(project_root=tmp_path)
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(src)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开混合几何图层")
        win.tab_right.setCurrentWidget(win.attribute_panel)
        model = win.attribute_panel.table.model()
        _pump(qapp, lambda: model.rowCount() == 2, label="混合几何 2 行")

        assert win.attribute_panel.lbl_count.text() == "要素数：2"
        assert _cell(win, 0, 1) == "Multi Polygon ×2"
        assert _cell(win, 0, 2) == "multipart"
        assert _cell(win, 1, 1) == "空几何"
        assert _cell(win, 1, 2) == "null_geom"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
