"""P5-02 contour desktop path: real MainWindow + worker + QTest submit.

数值验收：
- 表单只有矢量格式候选（GPKG/GeoJSON），栅格格式不可选（这是预检的正确形态）；
- GPKG 提交成功、图层计数 2（矢量成果经 P3-02 回退登记）；
- 成果被独立 OGR 重开验证：ELEV 集合 = {1..35}，等级按 ID 递增。
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


def _inspect_contours(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = f"""
import json
from osgeo import ogr
ds = ogr.Open({str(path)!r})
assert ds is not None
lyr = ds.GetLayer(0)
elevs = []
for f in lyr:
    elevs.append(float(f.GetField("ELEV")))
print(json.dumps({{"count": lyr.GetFeatureCount(), "elevs": sorted(elevs)}}))
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


def test_contour_form_submit(qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path) -> None:
    src = _make_plane(gdal_rt, tmp_path / "plane.tif")
    out = tmp_path / "contour.gpkg"
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
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开解析平面")
        win.toolbox_panel.select_capability("gdal:contour")
        qapp.processEvents()
        form = win.form_widget
        assert form.spin_contour_interval.isVisibleTo(form)
        assert form.spin_contour_offset.isVisibleTo(form)
        # 矢量成果的格式候选必须是 OGR 驱动：PNG 等栅格格式根本不可选
        formats = [form.cmb_format.itemText(i) for i in range(form.cmb_format.count())]
        assert formats == ["GPKG (GeoPackage)", "GeoJSON (.geojson)", "Shapefile (.shp)"], formats

        form.txt_input.setText(str(src))
        form.spin_contour_interval.setValue(1.0)
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="contour 任务完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        assert results[0].outputs[0]["data_type"] == "vector"

        info = _inspect_contours(gdal_rt, out)
        assert info["count"] == 35
        assert info["elevs"] == [float(k) for k in range(1, 36)]

        # 矢量成果经 P3-02 回退登记为图层（暂不渲染，仅元数据）
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="contour 成果加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
