"""P6-05 桌面路径：掩膜与 FVC 的真实 MainWindow + worker + QTest。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.models import TaskStatus
from spectra_workbench.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.engine, pytest.mark.gui]
ROOT = find_project_root()
NODATA = -9999.0
GT = (100.0, 10.0, 0.0, 200.0, 0.0, -10.0)


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


def _ndvi_plane(rows: int = 4, cols: int = 5) -> np.ndarray:
    y, x = np.mgrid[0:rows, 0:cols]
    return 0.1 + 0.02 * x + 0.01 * y


def _make_raster(
    gdal_rt: dict[str, Any],
    path: Path,
    values: np.ndarray,
    *,
    gt: tuple[float, ...] = GT,
) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = np.array({values.astype(np.float64).tolist()!r}, dtype=np.float32)
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, values.shape[1], values.shape[0], 1, gdal.GDT_Float32)
ds.SetGeoTransform({gt!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
band = ds.GetRasterBand(1)
band.WriteArray(values)
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
    bands.append({{"dtype": gdal.GetDataTypeName(b.DataType), "arr": b.ReadAsArray().tolist()}})
print(json.dumps({{"bands": ds.RasterCount, "band_list": bands}}))
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c" if False else "-c", script],
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


def _window_with_errors(
    qapp: QApplication, project_root: Path
) -> tuple[Any, list[Any], list[tuple[str, str]]]:
    win = MainWindow(project_root=project_root)
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    win.gdal_client.sig_result.connect(results.append)

    def _record(code: str, message: str) -> None:
        if code.startswith(("probe_", "read_window_", "subdataset", "list_subdatasets")):
            return
        errors.append((code, message))

    win.gdal_client.sig_error.connect(_record)
    return win, results, errors


def test_mask_form(qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path) -> None:
    values = _ndvi_plane()
    src = _make_raster(gdal_rt, tmp_path / "mask_gui_src.tif", values)
    out = tmp_path / "mask_gui_out.tif"

    win, results, errors = _window_with_errors(qapp, tmp_path)
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(src)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开 NDVI 影像")
        win.toolbox_panel.select_capability("gdal:mask")
        qapp.processEvents()
        form = win.form_widget
        assert form.spin_mask_band.isVisibleTo(form)
        assert form.txt_mask_low.isVisibleTo(form)
        assert form.txt_mask_high.isVisibleTo(form)
        assert form.chk_mask_invert.isVisibleTo(form)
        form.txt_input.setText(str(src))
        form.txt_mask_low.setText("0.18")
        form.txt_mask_high.setText("0.35")
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="掩膜完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        info = _inspect(gdal_rt, out)
        assert info["bands"] == 1 and info["band_list"][0]["dtype"] == "Byte"
        expected = ((values >= 0.18) & (values <= 0.35)).astype(int)
        assert np.array_equal(np.asarray(info["band_list"][0]["arr"]), expected)
        assert out.with_name(out.name + ".mask.json").exists()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive


def test_fvc_form(qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path) -> None:
    values = _ndvi_plane()
    src = _make_raster(gdal_rt, tmp_path / "fvc_gui_src.tif", values)
    out = tmp_path / "fvc_gui_out.tif"

    win, results, errors = _window_with_errors(qapp, tmp_path)
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(src)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开 NDVI 影像")
        win.toolbox_panel.select_capability("gdal:fvc")
        qapp.processEvents()
        form = win.form_widget
        assert form.spin_fvc_band.isVisibleTo(form)
        assert form.cmb_fvc_mode.isVisibleTo(form)
        assert form.spin_fvc_low.isVisibleTo(form)
        assert form.spin_fvc_high.isVisibleTo(form)
        assert form.txt_fvc_mask.isVisibleTo(form)
        assert form.txt_fvc_class_edges.isVisibleTo(form)
        form.txt_input.setText(str(src))
        form.txt_fvc_class_edges.setText("0.3,0.6")
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="FVC 完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        info = _inspect(gdal_rt, out)
        assert info["bands"] == 1 and info["band_list"][0]["dtype"] == "Float32"
        class_path = out.with_name(out.name + ".fvc_class.tif")
        assert class_path.exists()
        class_info = _inspect(gdal_rt, class_path)
        assert class_info["band_list"][0]["dtype"] == "Byte"
        assert out.with_name(out.name + ".fvc.json").exists()
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="FVC 成果加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
