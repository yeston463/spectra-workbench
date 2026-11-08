"""P6-04 桌面路径：GCP 配准、栅格差值、Brovey/HSV 融合的真实 MainWindow + QTest。"""

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
GT = (0.0, 1.0, 0.0, 300.0, 0.0, -1.0)


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


def _plane(x: float, y: float) -> float:
    return 10.0 + 0.1 * x + 0.2 * y


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
values = np.array({values.tolist()!r}, dtype=np.float32)
if values.ndim == 2:
    values = values[None, ...]
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, values.shape[2], values.shape[1], values.shape[0], gdal.GDT_Float32)
ds.SetGeoTransform({gt!r})
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
for i in range(values.shape[0]):
    ds.GetRasterBand(i + 1).WriteArray(values[i])
    ds.GetRasterBand(i + 1).SetNoDataValue({NODATA})
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
    bands.append(ds.GetRasterBand(i).ReadAsArray().tolist())
print(json.dumps({{"w": ds.RasterXSize, "h": ds.RasterYSize, "bands": ds.RasterCount, "arr": bands}}))
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


def _window_with_errors(
    qapp: QApplication, project_root: Path
) -> tuple[MainWindow, list[Any], list[tuple[str, str]]]:
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


def test_gcp_registration_form(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    ref_values = np.array(
        [[_plane(x + 0.5, 300.0 - (y + 0.5)) for x in range(10)] for y in range(10)]
    )
    ref = _make_raster(gdal_rt, tmp_path / "ref_gui.tif", ref_values)
    src_gt = (-5.0, 1.0, 0.0, 315.0, 0.0, -1.0)
    src_values = np.array(
        [
            [_plane(-5.0 + x + 0.5, 315.0 - (y + 0.5)) for x in range(12)]
            for y in range(12)
        ]
    )
    source = _make_raster(gdal_rt, tmp_path / "src_gui.tif", src_values, gt=src_gt)
    points = []
    for py in range(1, 12, 3):
        for px in range(1, 12, 3):
            points.append(
                {
                    "id": f"p{px}_{py}",
                    "pixel": px + 0.5,
                    "line": py + 0.5,
                    "x": src_gt[0] + src_gt[1] * (px + 0.5) + src_gt[2] * (py + 0.5),
                    "y": src_gt[3] + src_gt[4] * (px + 0.5) + src_gt[5] * (py + 0.5),
                    "role": "control" if (px + py) % 2 else "check",
                }
            )
    gcp = tmp_path / "gcps_gui.json"
    gcp.write_text(
        json.dumps(
            {"type": "spectra_gcp_set", "schema_version": "1.0", "srs": "EPSG:32650", "points": points}
        ),
        encoding="utf-8",
    )
    out = tmp_path / "registered_gui.tif"

    win, results, errors = _window_with_errors(qapp, tmp_path)
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(source)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开源影像")
        win.toolbox_panel.select_capability("gdal:gcpregister")
        qapp.processEvents()
        form = win.form_widget
        assert form.cmb_gcp_transform.isVisibleTo(form)
        assert form.txt_gcp_file.isVisibleTo(form)
        assert form.txt_gcp_reference.isVisibleTo(form)
        form.txt_input.setText(str(source))
        form.txt_gcp_file.setText(str(gcp))
        form.txt_gcp_reference.setText(str(ref))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="GCP 配准完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        info = _inspect(gdal_rt, out)
        assert info["w"] == 10 and info["h"] == 10
        assert out.with_name(out.name + ".gcpregistration.json").exists()
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="配准成果加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive


def test_rasterdiff_form(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    a = np.array([[[1.0, 2.0], [3.0, 4.0]]])
    b = np.array([[[0.0, 2.0], [5.0, 1.0]]])
    src = _make_raster(gdal_rt, tmp_path / "a_gui.tif", a)
    ref = _make_raster(gdal_rt, tmp_path / "b_gui.tif", b)
    out = tmp_path / "diff_gui.tif"

    win, results, errors = _window_with_errors(qapp, tmp_path)
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(src)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开差值输入")
        win.toolbox_panel.select_capability("gdal:rasterdiff")
        qapp.processEvents()
        form = win.form_widget
        assert form.cmb_diff_mode.isVisibleTo(form)
        assert form.txt_diff_reference.isVisibleTo(form)
        form.txt_input.setText(str(src))
        form.txt_diff_reference.setText(str(ref))
        form.cmb_diff_mode.setCurrentIndex(form.cmb_diff_mode.findData("absolute"))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="差值完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        info = _inspect(gdal_rt, out)
        assert np.allclose(np.asarray(info["arr"][0]), np.abs(a[0] - b[0]))
        assert out.with_name(out.name + ".rasterdiff.json").exists()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive


def test_pansharpen_form(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    pan = _make_raster(gdal_rt, tmp_path / "pan_gui.tif", np.full((6, 6), 0.8))
    ms_values = np.stack(
        [np.full((6, 6), 0.2), np.full((6, 6), 0.4), np.full((6, 6), 0.6)]
    )
    ms = _make_raster(gdal_rt, tmp_path / "ms_gui.tif", ms_values)
    out = tmp_path / "fused_gui.tif"

    win, results, errors = _window_with_errors(qapp, tmp_path)
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(pan)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开全色影像")
        win.toolbox_panel.select_capability("gdal:pansharpen")
        qapp.processEvents()
        form = win.form_widget
        assert form.txt_fusion_ms.isVisibleTo(form)
        assert form.txt_fusion_ms_bands.isVisibleTo(form)
        assert form.cmb_fusion_method.isVisibleTo(form)
        form.txt_input.setText(str(pan))
        form.txt_fusion_ms.setText(str(ms))
        form.txt_fusion_ms_bands.setText("1,2,3")
        form.cmb_fusion_method.setCurrentIndex(form.cmb_fusion_method.findData("brovey"))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="融合完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        info = _inspect(gdal_rt, out)
        assert info["bands"] == 3
        assert out.with_name(out.name + ".pansharpen.json").exists()
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="融合成果加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
