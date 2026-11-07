"""P5-02 rasterize desktop path: real MainWindow + worker + QTest submit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

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


def _make_rectangles(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
from osgeo import ogr, osr
driver = ogr.GetDriverByName("GeoJSON")
ds = driver.CreateDataSource({str(path)!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
layer = ds.CreateLayer("rect", srs, ogr.wkbPolygon)
layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))
for x0, x1, value in ((0, 2, 3), (2, 4, 9)):
    feat = ogr.Feature(layer.GetLayerDefn())
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, 0), (x1, 0), (x1, 2), (x0, 2), (x0, 0)):
        ring.AddPoint_2D(x, y)
    poly = ogr.Geometry(ogr.wkbPolygon)
    poly.AddGeometry(ring)
    feat.SetGeometry(poly)
    feat.SetField("value", value)
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
b = ds.GetRasterBand(1)
print(json.dumps({{"w": ds.RasterXSize, "h": ds.RasterYSize,
                  "arr": b.ReadAsArray().tolist()}}))
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


def test_rasterize_form_submit_and_png_preflight(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_rectangles(gdal_rt, tmp_path / "rect.geojson")
    out = tmp_path / "burn.tif"
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
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开矢量源")
        win.toolbox_panel.select_capability("gdal:rasterize")
        qapp.processEvents()
        form = win.form_widget
        assert form.txt_burn_values.isVisibleTo(form)
        assert form.txt_attribute.isVisibleTo(form)
        assert form.chk_all_touched.isVisibleTo(form)
        assert form.spin_xres.isVisibleTo(form), "rasterize 需要输出像元宽"

        form.txt_input.setText(str(src))
        form.txt_burn_values.setText("7")
        form.spin_xres.setValue(1.0)
        form.spin_yres.setValue(1.0)
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="rasterize 任务完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        info = _inspect(gdal_rt, out)
        assert info["w"] == 4 and info["h"] == 2
        assert info["arr"] == [[7.0] * 4, [7.0] * 4]
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="rasterize 成果加载")

        # PNG with the default Float64 output must be blocked before TaskSpec.
        png = tmp_path / "burn.png"
        form.txt_input.setText(str(src))
        form.txt_output.setText(str(png))
        idx = form.cmb_format.findText("PNG", Qt.MatchFlag.MatchStartsWith)
        assert idx >= 0
        form.cmb_format.setCurrentIndex(idx)
        with patch("spectra_workbench.ui.forms.param_form.QMessageBox.warning") as warning:
            QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        assert warning.called, "rasterize + PNG 默认 Float64 应在提交前被拦截"
        assert "PNG" in str(warning.call_args.args[-1])
        assert not png.exists()
        qapp.processEvents()
        assert len(results) == 1
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
