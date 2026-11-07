"""P5-04 ogr:vectortranslate 桌面路径：真实 MainWindow + worker + QTest。

数值验收：
- 表单输入矢量（GeoJSON 网格），where 过滤 + GPKG 提交成功、自动加载（图层计数 2）；
- 成果被独立 OGR 重开验证：恰 1 个要素、字段值正确；
- 格式候选为矢量驱动四项。
"""

from __future__ import annotations

import json
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
from spectra_workbench.domain.models import TaskStatus
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


def _make_grid(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
from osgeo import ogr, osr
drv = ogr.GetDriverByName("GeoJSON")
ds = drv.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
lyr = ds.CreateLayer("grid", srs, ogr.wkbPoint)
lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
for i in range(5):
    f = ogr.Feature(lyr.GetLayerDefn())
    g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(float(i), float(i))
    f.SetGeometry(g)
    f.SetField("v", i)
    f.SetField("name", "g%02d" % i)
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
    return path


def _inspect(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = f"""
import json
from osgeo import ogr
ds = ogr.Open({str(path)!r})
assert ds is not None
lyr = ds.GetLayer(0)
vals = []
for f in lyr:
    vals.append(f.GetField("v"))
print(json.dumps({{"count": lyr.GetFeatureCount(), "vals": vals}}))
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


def _pump(qapp: QApplication, predicate: Any, timeout: float = 30.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{label}")


def test_vectortranslate_form_submit_with_where_filter(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_grid(gdal_rt, tmp_path / "grid.geojson")
    out = tmp_path / "filtered.gpkg"
    win = MainWindow(project_root=tmp_path)
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    win.gdal_client.sig_result.connect(results.append)

    def _record_error(code: str, message: str) -> None:
        if code.startswith(("probe_", "read_window_", "subdataset", "list_subdatasets")):
            return
        errors.append((code, message))

    win.gdal_client.sig_error.connect(_record_error)
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(src)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开网格矢量")
        win.toolbox_panel.select_capability("ogr:vectortranslate")
        qapp.processEvents()
        form = win.form_widget
        assert form.txt_vt_where.isVisibleTo(form)
        assert form.txt_vt_sql.isVisibleTo(form)
        assert form.txt_vt_spat.isVisibleTo(form)
        assert form.txt_vt_dst_srs.isVisibleTo(form)
        formats = [form.cmb_format.itemText(i) for i in range(form.cmb_format.count())]
        assert formats == [
            "GPKG (GeoPackage)",
            "GeoJSON (.geojson)",
            "Shapefile (.shp)",
            "FlatGeobuf (.fgb)",
        ], formats

        form.txt_input.setText(str(src))
        form.txt_vt_where.setText("v = 3")
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="vectortranslate 任务完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        assert results[0].outputs[0]["data_type"] == "vector"

        info = _inspect(gdal_rt, out)
        assert info["count"] == 1
        assert info["vals"] == [3]

        # 成果自动加载（矢量图层经 P3-02 回退登记）
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="成果加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
