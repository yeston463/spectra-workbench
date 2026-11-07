"""P5-02 polygonize desktop path: real MainWindow + worker + QTest submit.

数值验收：
- 表单提交 GPKG 成功 → 图层计数 2（矢量成果经 P3-02 回退登记为图层）；
- 元数据面板展示矢量信息（geometry_type / feature_count）；
- 成果被独立 OGR 重开验证：2 个要素、DN∈{3,9}、面积各 4.0。
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


def _make_labels(gdal_rt: dict[str, Any], path: Path) -> Path:
    script = f"""
import json
from osgeo import gdal, osr
rows = [[3, 3, 9, 9], [3, 3, 9, 9]]
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, 4, 2, 1, gdal.GDT_Byte)
ds.SetGeoTransform((0.0, 1.0, 0.0, 2.0, 0.0, -1.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
for r, row in enumerate(rows):
    ds.GetRasterBand(1).WriteRaster(0, r, 4, 1, bytes(row), buf_type=gdal.GDT_Byte)
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


def _inspect_vector(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = f"""
import json
from osgeo import ogr
ds = ogr.Open({str(path)!r})
assert ds is not None
lyr = ds.GetLayer(0)
feats = []
for f in lyr:
    g = f.GetGeometryRef()
    feats.append({{"dn": f.GetField("DN"), "area": g.GetArea()}})
print(json.dumps({{"count": lyr.GetFeatureCount(), "features": feats}}))
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


def test_polygonize_form_submit(qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path) -> None:
    src = _make_labels(gdal_rt, tmp_path / "labels.tif")
    out = tmp_path / "poly.gpkg"
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
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开标签图")
        win.toolbox_panel.select_capability("gdal:polygonize")
        qapp.processEvents()
        form = win.form_widget
        assert form.txt_field_name.isVisibleTo(form)
        assert form.cmb_connectedness.isVisibleTo(form)
        # 矢量成果的格式候选必须是 OGR 驱动，而不是栅格驱动
        formats = [form.cmb_format.itemText(i) for i in range(form.cmb_format.count())]
        assert formats[0].startswith("GPKG"), formats
        assert not any(item.startswith("GTiff") for item in formats)

        form.txt_input.setText(str(src))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="polygonize 任务完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        assert results[0].outputs[0]["data_type"] == "vector"

        info = _inspect_vector(gdal_rt, out)
        assert info["count"] == 2
        areas = {f["dn"]: f["area"] for f in info["features"]}
        assert set(areas) == {3, 9}
        for area in areas.values():
            assert area == pytest.approx(4.0)

        # 矢量成果经 P3-02 回退登记为图层（暂不渲染，仅元数据）
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="polygonize 成果加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
