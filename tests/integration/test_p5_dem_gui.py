"""P5-02 DEM desktop path: real MainWindow + worker + QTest submit."""

from __future__ import annotations

import json
import math
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


def _make_plane(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
n = 8
rows, cols = np.mgrid[0:n, 0:n]
z = (2.0 * cols + 3.0 * rows).astype(np.float32)
ds = gdal.GetDriverByName("GTiff").Create({str(path)!r}, n, n, 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetProjection(srs.ExportToWkt())
ds.SetGeoTransform((500000.0, 1.0, 0.0, 3200000.0, 0.0, -1.0))
ds.GetRasterBand(1).WriteArray(z)
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


def _center(gdal_rt: dict[str, Any], path: Path) -> float:
    script = f"""
import json
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.Open({str(path)!r})
assert ds is not None
print(json.dumps({{"center": float(ds.GetRasterBand(1).ReadAsArray()[4][4])}}))
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())["center"]


def _pump(qapp: QApplication, predicate: Any, timeout: float = 60.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{label}")


def test_dem_form_submit_and_png_preflight(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_plane(gdal_rt, tmp_path / "plane.tif")
    out = tmp_path / "slope.tif"
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
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开 DEM 源")
        win.toolbox_panel.select_capability("gdal:dem")
        qapp.processEvents()
        form = win.form_widget
        assert form.cmb_dem_processing.isVisibleTo(form), "地形模式控件应可见"
        assert form.spin_dem_zfactor.isVisibleTo(form), "zFactor 控件应可见"
        assert form.spin_dem_scale.isVisibleTo(form), "scale 控件应可见"

        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        idx = form.cmb_dem_processing.findData("slope")
        assert idx >= 0
        form.cmb_dem_processing.setCurrentIndex(idx)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="DEM 任务完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        expected = math.degrees(math.atan(math.sqrt(13.0)))
        assert _center(gdal_rt, out) == pytest.approx(expected, abs=1e-4)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="DEM 成果加载")

        # GUI must reject PNG before emitting a TaskSpec (DEM output is Float32).
        png = tmp_path / "slope.png"
        idx = form.cmb_format.findText("PNG", Qt.MatchFlag.MatchStartsWith)
        assert idx >= 0
        form.cmb_format.setCurrentIndex(idx)
        form.txt_output.setText(str(png))
        with patch("spectra_workbench.ui.forms.param_form.QMessageBox.warning") as warning:
            QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        assert warning.called, "DEM + PNG 应在提交前被拦截"
        assert "PNG" in str(warning.call_args.args[-1])
        assert not png.exists()
        qapp.processEvents()
        assert len(results) == 1, "被拦截的 DEM 任务不得发出"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
