"""P5-02 grid desktop path: real MainWindow + worker + QTest submit."""

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


def _make_points(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
from osgeo import ogr, osr
driver = ogr.GetDriverByName("GeoJSON")
ds = driver.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
layer = ds.CreateLayer("points", srs, ogr.wkbPoint)
layer.CreateField(ogr.FieldDefn("value", ogr.OFTReal))
for x, y in ((0, 0), (4, 0), (0, 2), (4, 2), (2, 1)):
    feat = ogr.Feature(layer.GetLayerDefn())
    geom = ogr.Geometry(ogr.wkbPoint)
    geom.AddPoint_2D(x, y)
    feat.SetGeometry(geom)
    feat.SetField("value", 2 * x + 3 * y)
    layer.CreateFeature(feat)
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
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.Open({str(path)!r})
assert ds is not None
print(json.dumps({{"w": ds.RasterXSize, "h": ds.RasterYSize,
                  "arr": ds.GetRasterBand(1).ReadAsArray().tolist()}}))
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


def _pump(qapp: QApplication, predicate: Any, timeout: float = 60.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{label}")


def test_grid_form_submit(qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path) -> None:
    src = _make_points(gdal_rt, tmp_path / "points.geojson")
    out = tmp_path / "grid.tif"
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
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开点图层")
        win.toolbox_panel.select_capability("gdal:grid")
        qapp.processEvents()
        form = win.form_widget
        assert form.cmb_grid_algorithm.isVisibleTo(form)
        assert form.txt_zfield.isVisibleTo(form)
        assert form.txt_outsize.isVisibleTo(form)

        form.txt_input.setText(str(src))
        form.txt_zfield.setText("value")
        idx = form.cmb_grid_algorithm.findData("linear")
        assert idx >= 0
        form.cmb_grid_algorithm.setCurrentIndex(idx)
        form.txt_outsize.setText("4,2")
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="grid 任务完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        info = _inspect(gdal_rt, out)
        assert info["w"] == 4 and info["h"] == 2
        assert info["arr"][0] == pytest.approx([5.5, 7.5, 9.5, 11.5], abs=1e-6)
        assert info["arr"][1] == pytest.approx([2.5, 4.5, 6.5, 8.5], abs=1e-6)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="grid 成果加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
