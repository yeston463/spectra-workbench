"""P6-02 FFT 滤波桌面路径：真实 MainWindow + worker + QTest 提交。

验收：
- 表单显示 fftBand / suppressBins 控件；
- 默认 GTiff 之外的 ENVI 选择真实下发并写出 .dat/.hdr（此前 sindex/fftfilter
  漏发 format，GUI 选择会被 Worker 静默忽略）；
- 抑制 (0,2) 后输出逐像元等于 10 + y 方向正弦（x 条纹被去除）；
- 掩膜 sidecar 与成果成套提交，成果自动加载为第二图层。
"""

from __future__ import annotations

import json
import math
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


def _pattern(rows: int, cols: int) -> list[list[float]]:
    return [
        [
            10.0
            + 3.0 * math.cos(2.0 * math.pi * 2.0 * x / cols)
            + 5.0 * math.sin(2.0 * math.pi * y / rows)
            for x in range(cols)
        ]
        for y in range(rows)
    ]


def _make_raster(gdal_rt: dict[str, Any], path: Path, rows: int, cols: int) -> Path:
    flat = [v for row in _pattern(rows, cols) for v in row]
    script = f"""
import array
from osgeo import gdal, osr
vals = {flat!r}
drv = gdal.GetDriverByName("GTiff")
ds = drv.Create({str(path)!r}, {cols}, {rows}, 1, gdal.GDT_Float32)
ds.SetGeoTransform((100.0, 10.0, 0.0, 200.0, 0.0, -10.0))
srs = osr.SpatialReference(); srs.ImportFromEPSG(32650)
ds.SetSpatialRef(srs)
band = ds.GetRasterBand(1)
band.WriteRaster(0, 0, {cols}, {rows}, array.array("f", vals).tobytes(), buf_type=gdal.GDT_Float32)
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
b = ds.GetRasterBand(1)
print(json.dumps({{
    "driver": ds.GetDriver().ShortName,
    "bands": ds.RasterCount,
    "dtype": gdal.GetDataTypeName(b.DataType),
    "nodata": b.GetNoDataValue(),
    "arr": b.ReadAsArray().tolist(),
}}))
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


def test_fft_form_submit_to_envi(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    rows, cols = 8, 8
    src = _make_raster(gdal_rt, tmp_path / "fft_src.tif", rows, cols)
    out = tmp_path / "fft_out.dat"

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
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开 8×8 影像")
        win.toolbox_panel.select_capability("gdal:fftfilter")
        qapp.processEvents()

        form = win.form_widget
        assert form.spin_fft_band.isVisibleTo(form)
        assert form.txt_fft_suppress.isVisibleTo(form)

        form.txt_input.setText(str(src))
        form.spin_fft_band.setValue(1)
        form.txt_fft_suppress.setText("0,2")
        envi_index = form.cmb_format.findText("ENVI (.dat/.hdr)")
        assert envi_index >= 0
        form.cmb_format.setCurrentIndex(envi_index)
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)

        QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="fftfilter 任务完成")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error

        info = _inspect(gdal_rt, out)
        assert info["driver"] == "ENVI"
        assert info["bands"] == 1 and info["dtype"] == "Float32"
        assert info["nodata"] == NODATA
        expected = np.asarray(
            [
                [10.0 + 5.0 * math.sin(2.0 * math.pi * y / rows) for _ in range(cols)]
                for y in range(rows)
            ],
            dtype=np.float64,
        )
        actual = np.asarray(info["arr"], dtype=np.float64)
        assert float(np.max(np.abs(actual - expected))) <= 1e-4

        sidecar = out.with_name(out.name + ".fft_mask.json")
        assert sidecar.exists()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["requested_suppressed_bins"] == [[0, 2]]
        assert {tuple(p) for p in payload["applied_suppressed_bins"]} >= {(0, 2), (0, cols - 2)}

        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="成果自动加载")
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
    assert not win.gdal_client.is_alive
