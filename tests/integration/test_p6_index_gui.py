"""P6-01 光谱指数桌面路径：真实 MainWindow + worker + QTest 提交。

数值验收：
- 表单选 NDVI、bandA=1/bandB=2、关闭物理量换算提交 → 成功并自动加载（图层计数 2）；
- 成果独立重开：单波段 Float32、合成值符合公式、NoData 语义正确。
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


def _make_two_band(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
from osgeo import gdal, osr
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, 3, 2, 2, gdal.GDT_Float32)
ds.SetGeoTransform((0.0, 10.0, 0.0, 20.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
a = [[200.0, 100.0, 30.0], [100.0, -1.0, 200.0]]
b = [[100.0, 100.0, -30.0], [100.0, 100.0, 100.0]]
import array
for band_idx, rows in ((1, a), (2, b)):
    flat = array.array("f", [v for row in rows for v in row])
    ds.GetRasterBand(band_idx).WriteRaster(0, 0, 3, 2, flat.tobytes(), buf_type=gdal.GDT_Float32)
ds.GetRasterBand(1).SetNoDataValue(-1.0)
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
print(json.dumps({{"dtype": gdal.GetDataTypeName(b.DataType), "nodata": b.GetNoDataValue(),
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


def _pump(qapp: QApplication, predicate: Any, timeout: float = 30.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{label}")


def test_sindex_form_submit_ndvi(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_two_band(gdal_rt, tmp_path / "twoband.tif")
    out = tmp_path / "ndvi.tif"
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
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开双波段影像")
        win.toolbox_panel.select_capability("gdal:sindex")
        qapp.processEvents()
        form = win.form_widget
        assert form.cmb_sindex.isVisibleTo(form)
        assert form.spin_band_a.isVisibleTo(form)
        assert form.spin_band_b.isVisibleTo(form)
        assert form.chk_use_physical.isVisibleTo(form)

        form.txt_input.setText(str(src))
        idx = form.cmb_sindex.findData("ndvi")
        assert idx >= 0
        form.cmb_sindex.setCurrentIndex(idx)
        form.spin_band_a.setValue(1)
        form.spin_band_b.setValue(2)
        # GUI 默认 usePhysical=True；本用例关闭以断言原始值公式
        form.chk_use_physical.setChecked(False)
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="sindex 任务完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error

        info = _inspect(gdal_rt, out)
        assert info["dtype"] == "Float32" and info["nodata"] == -9999.0
        assert info["arr"][0][0] == pytest.approx(1.0 / 3.0, abs=1e-6)
        assert info["arr"][0][2] == -9999.0  # 分母为零
        assert info["arr"][1][1] == -9999.0  # NoData 标签

        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="成果加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
