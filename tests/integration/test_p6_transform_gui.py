"""P6-03 PCA 与缨帽变换桌面路径：真实 MainWindow + worker + QTest。

验收：
- PCA 表单显示模式/成分数/矩阵/变换文件控件；正变换后自动加载；
- 正变换 sidecar 可直接作为逆变换输入，逆变换重建原波段数；
- 缨帽表单显示传感器/6 波段输入/来源说明；提交后自动加载 3 波段成果。
"""

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


def _pca_bands(rows: int, cols: int) -> np.ndarray:
    y, x = np.mgrid[0:rows, 0:cols]
    band1 = 10.0 + 2.0 * x + 0.5 * y
    band2 = 20.0 - x + 0.2 * y
    band3 = 0.5 * band1 + 0.5 * band2 + 1.0
    return np.stack([band1, band2, band3])


def _tc_bands(rows: int, cols: int) -> np.ndarray:
    y, x = np.mgrid[0:rows, 0:cols]
    return np.stack(
        [
            0.10 + 0.010 * x + 0.005 * y,
            0.20 + 0.008 * x - 0.004 * y,
            0.30 - 0.012 * x + 0.006 * y,
            0.40 + 0.015 * x + 0.003 * y,
            0.50 - 0.006 * x + 0.009 * y,
            0.60 + 0.004 * x - 0.007 * y,
        ]
    )


def _make_raster(
    gdal_rt: dict[str, Any], path: Path, values: np.ndarray
) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
values = np.array({values.astype(np.float64).tolist()!r}, dtype=np.float32)
rows, cols = values.shape[1], values.shape[2]
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, cols, rows, values.shape[0], gdal.GDT_Float32)
ds.SetGeoTransform((100.0, 10.0, 0.0, 200.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
for i in range(values.shape[0]):
    band = ds.GetRasterBand(i + 1)
    band.WriteArray(values[i])
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
    bands.append({{
        "dtype": gdal.GetDataTypeName(b.DataType),
        "nodata": b.GetNoDataValue(),
        "description": b.GetDescription(),
        "arr": b.ReadAsArray().tolist(),
    }})
print(json.dumps({{"bands": ds.RasterCount, "band_list": bands}}))
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


def test_pca_forward_then_inverse_from_gui(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(
        gdal_rt, tmp_path / "pca_gui_src.tif", _pca_bands(5, 4)
    )
    components = tmp_path / "pca_gui_components.tif"
    inverse = tmp_path / "pca_gui_inverse.tif"

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
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开 3 波段影像")
        win.toolbox_panel.select_capability("gdal:pca")
        qapp.processEvents()
        form = win.form_widget
        assert form.cmb_pca_mode.isVisibleTo(form)
        assert form.spin_pca_components.isVisibleTo(form)
        assert form.cmb_pca_matrix.isVisibleTo(form)
        assert form.txt_pca_transform.isVisibleTo(form)
        assert form.btn_browse_pca_transform.isVisibleTo(form)

        form.txt_input.setText(str(src))
        form.cmb_pca_mode.setCurrentIndex(form.cmb_pca_mode.findData("forward"))
        form.spin_pca_components.setValue(2)
        form.txt_output.setText(str(components))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="PCA forward 完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="主成分自动加载")

        info = _inspect(gdal_rt, components)
        assert info["bands"] == 2
        assert [b["description"] for b in info["band_list"]] == ["PC1", "PC2"]
        transform = components.with_name(components.name + ".pca.json")
        assert transform.exists()

        results.clear()
        form.txt_input.setText(str(components))
        form.cmb_pca_mode.setCurrentIndex(form.cmb_pca_mode.findData("inverse"))
        form.spin_pca_components.setValue(2)
        form.txt_pca_transform.setText(str(transform))
        form.txt_output.setText(str(inverse))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="PCA inverse 完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error

        restored = _inspect(gdal_rt, inverse)
        assert restored["bands"] == 3
        expected = _pca_bands(5, 4).reshape(3, -1)
        actual = np.asarray([b["arr"] for b in restored["band_list"]]).reshape(3, -1)
        assert float(np.max(np.abs(actual - expected))) <= 1e-3
        _pump(qapp, lambda: len(win.layer_panel.layers) == 3, label="重建成果自动加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive


def test_tasseled_cap_from_gui(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(
        gdal_rt, tmp_path / "tc_gui_src.tif", _tc_bands(4, 3)
    )
    out = tmp_path / "tc_gui_out.tif"

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
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开 6 波段影像")
        win.toolbox_panel.select_capability("gdal:tasseledcap")
        qapp.processEvents()
        form = win.form_widget
        assert form.cmb_tc_sensor.isVisibleTo(form)
        assert form.txt_tc_bands.isVisibleTo(form)
        assert form.lbl_tc_source.isVisibleTo(form)
        assert "Baig" in form.lbl_tc_source.text() or "doi.org" in form.lbl_tc_source.text()

        form.txt_input.setText(str(src))
        index = form.cmb_tc_sensor.findData("landsat8_oli")
        assert index >= 0
        form.cmb_tc_sensor.setCurrentIndex(index)
        form.txt_tc_bands.setText("1,2,3,4,5,6")
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="缨帽任务完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error

        info = _inspect(gdal_rt, out)
        assert info["bands"] == 3
        assert [b["description"] for b in info["band_list"]] == [
            "brightness",
            "greenness",
            "wetness",
        ]
        assert out.with_name(out.name + ".tasseledcap.json").exists()
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="缨帽成果自动加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
