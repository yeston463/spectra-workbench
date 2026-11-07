"""S5 P4-02 desktop end-to-end acceptance with the real MainWindow.

Every case drives the actual window, form, worker subprocess and task panel.
No provider calls are used as a substitute for the desktop path. On-disk
results are reopened with GDAL in a separate process to verify semantics.
"""

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


# --------------------------------------------------------------------- helpers
def _make_raster(
    gdal_rt: dict[str, Any],
    path: Path,
    *,
    bands: int = 1,
    dtype: str = "Float32",
    value: float = 10.0,
    gt: tuple[float, float, float, float, float, float] = (100.0, 1.0, 0.0, 30.0, 0.0, -1.0),
    epsg: int = 4326,
    nodata: float | None = None,
    alpha: bool = False,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    np_dtype = {"Byte": "uint8", "UInt16": "uint16", "Float32": "float32", "Float64": "float64"}[dtype]
    alpha_stmt = (
        f"    if i == {bands}:\n        b.SetColorInterpretation(gdal.GCI_AlphaBand)\n"
        if alpha
        else ""
    )
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
ds = gdal.GetDriverByName("GTiff").Create({str(path)!r}, 4, 4, {bands}, gdal.GDT_{dtype})
srs = osr.SpatialReference(); srs.ImportFromEPSG({epsg})
ds.SetGeoTransform({list(gt)!r})
ds.SetProjection(srs.ExportToWkt())
for i in range(1, {bands} + 1):
    b = ds.GetRasterBand(i)
    b.WriteArray(np.full((4, 4), {value!r} + (i - 1) * 10, dtype=np.{np_dtype}))
    {f"b.SetNoDataValue({nodata!r})" if nodata is not None else ""}
{alpha_stmt}ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"源栅格构造失败：{proc.stderr}"
    return path


def _make_large_raster(gdal_rt: dict[str, Any], path: Path, size: int = 4096) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
ds = gdal.GetDriverByName("GTiff").Create({str(path)!r}, {size}, {size}, 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform([100.0, 0.001, 0.0, 30.0, 0.0, -0.001])
ds.SetProjection(srs.ExportToWkt())
row = np.linspace(0.0, 1.0, {size}, dtype=np.float32)
ds.GetRasterBand(1).WriteArray(np.tile(row, ({size}, 1)))
ds = None
print("OK")
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"大栅格构造失败：{proc.stderr}"
    return path


def _inspect(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = f"""
import json
from osgeo import gdal
gdal.UseExceptions()
ds = gdal.Open({str(path)!r})
assert ds is not None, "成果无法重开"
print(json.dumps({{
    "driver": ds.GetDriver().ShortName,
    "w": ds.RasterXSize, "h": ds.RasterYSize, "bands": ds.RasterCount,
    "dtypes": [gdal.GetDataTypeName(ds.GetRasterBand(i).DataType) for i in range(1, ds.RasterCount + 1)],
    "nodata": [ds.GetRasterBand(i).GetNoDataValue() for i in range(1, ds.RasterCount + 1)],
    "ci": [int(ds.GetRasterBand(i).GetColorInterpretation()) for i in range(1, ds.RasterCount + 1)],
    "mask_flags": [int(ds.GetRasterBand(i).GetMaskFlags()) for i in range(1, ds.RasterCount + 1)],
    "arrays": [ds.GetRasterBand(i).ReadAsArray().tolist() for i in range(1, ds.RasterCount + 1)],
    "files": [str(p) for p in (ds.GetFileList() or [])],
}}))
"""
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"成果重开失败：{proc.stderr}"
    return json.loads(proc.stdout.strip())


def _pump(qapp: QApplication, predicate: Any, timeout: float = 60.0, label: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"等待超时（{timeout}s）：{label}")


def _close_window(qapp: QApplication, win: MainWindow) -> None:
    win.close()
    win.deleteLater()
    qapp.processEvents()


def _task_error_filter(record: list[tuple[str, str]]):
    def _record(code: str, message: str) -> None:
        # probe_* 是 P3-02 元数据面板对栅格无条件发起的查询类广播，与任务无关。
        if code.startswith(("probe_", "read_window_", "subdataset", "list_subdatasets")):
            return
        record.append((code, message))

    return _record


def _choose_format(form: Any, name: str) -> None:
    idx = form.cmb_format.findText(name, Qt.MatchFlag.MatchStartsWith)
    assert idx >= 0, f"格式下拉框缺少 {name}"
    form.cmb_format.setCurrentIndex(idx)


def _click_submit(form: Any) -> None:
    assert form.btn_submit.isEnabled(), "提交按钮必须可用"
    QTest.mouseClick(form.btn_submit, Qt.MouseButton.LeftButton)


# ------------------------------------------------------- 1. ordering + reopen
def test_desktop_merge_first_last_order_load_and_reopen(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    a = _make_raster(gdal_rt, tmp_path / "a.tif", value=10.0)
    b = _make_raster(
        gdal_rt, tmp_path / "b.tif", value=20.0, gt=(102.0, 1.0, 0.0, 28.0, 0.0, -1.0)
    )
    out_last = tmp_path / "merge_last.tif"
    out_first = tmp_path / "merge_first.tif"
    project = tmp_path / "desktop_merge.spectra.json"

    win = MainWindow(project_root=tmp_path)
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    win.gdal_client.sig_result.connect(results.append)
    win.gdal_client.sig_error.connect(_task_error_filter(errors))
    win2: MainWindow | None = None
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(a)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开源 A")

        win.toolbox_panel.select_capability("gdal:merge")
        qapp.processEvents()
        form = win.form_widget
        assert form.lst_inputs.isVisibleTo(form), "拼接必须显示多输入列表"
        form.lst_inputs.addItem(str(a))
        form.lst_inputs.addItem(str(b))

        # 默认 last：后加入的 B 胜出
        form.txt_output.setText(str(out_last))
        form.chk_overwrite.setChecked(True)
        _click_submit(form)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="last 优先级任务")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        info_last = _inspect(gdal_rt, out_last)
        assert info_last["bands"] == 1
        assert info_last["arrays"][0][2][2] == 20.0, "last 优先级重叠区应取后加入的 B"
        assert info_last["arrays"][0][2][0] == 10.0

        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="拼接成果自动加载")

        # 切换 first：先加入的 A 胜出
        idx = form.cmb_overlap_priority.findData("first")
        assert idx >= 0
        form.cmb_overlap_priority.setCurrentIndex(idx)
        form.txt_output.setText(str(out_first))
        _click_submit(form)
        _pump(qapp, lambda: len(results) >= 2 or bool(errors), label="first 优先级任务")
        assert not errors, errors
        assert results[1].status is TaskStatus.succeeded, results[1].error
        info_first = _inspect(gdal_rt, out_first)
        assert info_first["arrays"][0][2][2] == 10.0, "first 优先级重叠区应取先加入的 A"
        _pump(qapp, lambda: len(win.layer_panel.layers) == 3, label="第二个成果自动加载")

        loaded_paths = {ly.file_path for ly in win.layer_panel.layers}
        assert loaded_paths == {a.resolve(), out_last.resolve(), out_first.resolve()}

        win.project_path = project
        assert win.save_project_action() is True
        saved = json.loads(project.read_text(encoding="utf-8"))
        assert len(saved["layers"]) == 3

        win2 = MainWindow(project_root=tmp_path)
        try:
            assert win2.open_project(project) is True
            _pump(qapp, lambda: len(win2.layer_panel.layers) == 3, label="重开项目恢复图层")
            assert win2.missing_sources == []
            assert {ly.file_path for ly in win2.layer_panel.layers} == loaded_paths
        finally:
            _close_window(qapp, win2)
            win2 = None
    finally:
        if win2 is not None:
            _close_window(qapp, win2)
        _close_window(qapp, win)
    assert not win.gdal_client.is_alive


# ------------------------------------------- 2. mixed CRS keeps old output
def test_desktop_mixed_crs_rejected_and_old_output_preserved(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    a = _make_raster(gdal_rt, tmp_path / "wgs84.tif", value=10.0, epsg=4326)
    b = _make_raster(gdal_rt, tmp_path / "webmerc.tif", value=20.0, epsg=3857)
    out = _make_raster(gdal_rt, tmp_path / "existing.tif", value=99.0)
    before = out.read_bytes()

    win = MainWindow(project_root=tmp_path)
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    win.gdal_client.sig_result.connect(results.append)
    win.gdal_client.sig_error.connect(_task_error_filter(errors))
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(a)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开源 A")
        win.toolbox_panel.select_capability("gdal:merge")
        qapp.processEvents()
        form = win.form_widget
        form.lst_inputs.addItem(str(a))
        form.lst_inputs.addItem(str(b))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        _click_submit(form)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="混合 CRS 任务")
        assert not errors, errors
        assert results[0].status is TaskStatus.failed
        assert results[0].error and results[0].error.get("code") == "incompatible_sources"

        assert out.read_bytes() == before, "失败任务不得改动既有成果"
        info = _inspect(gdal_rt, out)
        assert info["w"] == 4 and info["h"] == 4
        assert info["arrays"][0][0][0] == 99.0
        assert len(win.layer_panel.layers) == 1, "失败成果不得进入图层树"
    finally:
        _close_window(qapp, win)
    assert not win.gdal_client.is_alive


# ----------------------------------------------- 3. ENVI new then overwrite
def test_desktop_envi_new_then_overwrite(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "two_bands.tif", bands=2, dtype="Byte", value=10.0)
    out = tmp_path / "envi_out.dat"
    win = MainWindow(project_root=tmp_path)
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    win.gdal_client.sig_result.connect(results.append)
    win.gdal_client.sig_error.connect(_task_error_filter(errors))
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(src)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开双波段源")
        win.toolbox_panel.select_capability("gdal:bandmanage")
        qapp.processEvents()
        form = win.form_widget
        assert form.cmb_format.isVisibleTo(form), "P4-02 输出格式选择必须可见"
        _choose_format(form, "ENVI")
        form.txt_input.setText(str(src))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form.txt_band_list.setText("2,1")
        _click_submit(form)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="ENVI 新成果")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        assert out.is_file() and out.with_suffix(".hdr").is_file(), "ENVI 必须成套提交 .dat/.hdr"
        info = _inspect(gdal_rt, out)
        assert info["driver"] == "ENVI" and info["bands"] == 2
        assert info["arrays"][0][0][0] == 20, "第一次提交应按 bandList=2,1 重排"
        assert info["arrays"][1][0][0] == 10

        # 同目标覆盖：新波段顺序必须替换旧内容，不能残留旧头文件语义
        # 成果自动加载会改写表单输入框，覆盖提交前必须重新指定源文件。
        form.txt_input.setText(str(src))
        form.txt_band_list.setText("1,2")
        _click_submit(form)
        _pump(qapp, lambda: len(results) >= 2 or bool(errors), label="ENVI 覆盖成果")
        assert not errors, errors
        assert results[1].status is TaskStatus.succeeded, results[1].error
        info2 = _inspect(gdal_rt, out)
        assert info2["arrays"][0][0][0] == 10
        assert info2["arrays"][1][0][0] == 20
    finally:
        _close_window(qapp, win)
    assert not win.gdal_client.is_alive


# ------------------------------------------- 4. alpha / mask format matrix
def test_desktop_format_matrix_alpha_and_float_preflight(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    rgba = _make_raster(
        gdal_rt, tmp_path / "rgba.tif", bands=4, dtype="Byte", value=5.0, alpha=True
    )
    float32 = _make_raster(gdal_rt, tmp_path / "float.tif", bands=1, dtype="Float32")

    win = MainWindow(project_root=tmp_path)
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    win.gdal_client.sig_result.connect(results.append)
    win.gdal_client.sig_error.connect(_task_error_filter(errors))
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(rgba)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开 RGBA 源")
        win.toolbox_panel.select_capability("gdal:bandmanage")
        qapp.processEvents()
        form = win.form_widget

        # PNG 接受 Byte RGBA，并保留 alpha color interpretation
        png = tmp_path / "rgba.png"
        _choose_format(form, "PNG")
        form.txt_input.setText(str(rgba))
        form.txt_output.setText(str(png))
        form.chk_overwrite.setChecked(True)
        _click_submit(form)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="PNG RGBA 任务")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        info = _inspect(gdal_rt, png)
        assert info["driver"] == "PNG" and info["bands"] == 4
        assert info["ci"][-1] == 6, "PNG 输出必须保留 alpha"

        # JPEG 不支持 alpha：GUI 必须在发出 TaskSpec 前拦截
        form.txt_input.setText(str(rgba))
        jpg = tmp_path / "rgba.jpg"
        _choose_format(form, "JPEG")
        form.txt_output.setText(str(jpg))
        with patch("spectra_workbench.ui.forms.param_form.QMessageBox.warning") as warning:
            _click_submit(form)
        assert warning.called, "JPEG + alpha 必须给出明确提示"
        message = str(warning.call_args.args[-1])
        assert "JPEG" in message
        assert not jpg.exists(), "被拦截的组合不得写出文件"
        qapp.processEvents()
        assert len(results) == 1, "被拦截的组合不得发出 TaskSpec"

        # ENVI 重新打开不保留 alpha color interpretation，同样在提交前拦截
        envi = tmp_path / "rgba.dat"
        form.txt_input.setText(str(rgba))
        _choose_format(form, "ENVI")
        form.txt_output.setText(str(envi))
        with patch("spectra_workbench.ui.forms.param_form.QMessageBox.warning") as warning_env:
            _click_submit(form)
        assert warning_env.called, "ENVI + alpha 必须给出明确提示"
        assert "ENVI" in str(warning_env.call_args.args[-1])
        assert not envi.exists()
        qapp.processEvents()
        assert len(results) == 1

        # Float32 -> PNG：同样在提交前拦截（元数据来自真实 worker 探测）
        float_png = tmp_path / "float.png"
        _choose_format(form, "PNG")
        form.txt_input.setText(str(float32))
        form.txt_output.setText(str(float_png))
        with patch("spectra_workbench.ui.forms.param_form.QMessageBox.warning") as warning2:
            _click_submit(form)
        assert warning2.called, "PNG + Float32 必须给出明确提示"
        assert "Float32" in str(warning2.call_args.args[-1])
        assert not float_png.exists()
        qapp.processEvents()
        assert len(results) == 1
    finally:
        _close_window(qapp, win)
    assert not win.gdal_client.is_alive


# ------------------------------------------- 5. real computation cancel/retry
def test_desktop_real_cancel_then_retry(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    src = _make_large_raster(gdal_rt, tmp_path / "large.tif", size=4096)
    out = tmp_path / "large_out.tif"

    win = MainWindow(project_root=tmp_path)
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    win.gdal_client.sig_result.connect(results.append)
    win.gdal_client.sig_error.connect(_task_error_filter(errors))
    cancel_requested = {"value": False}

    def _cancel_on_progress(ratio: float, phase: str, _message: str) -> None:
        if (
            phase == "processing"
            and ratio is not None
            and ratio >= 0.02
            and not cancel_requested["value"]
        ):
            cancel_requested["value"] = True
            QTest.mouseClick(win.task_panel.btn_cancel, Qt.MouseButton.LeftButton)

    win.gdal_client.sig_progress.connect(_cancel_on_progress)
    try:
        win.show()
        qapp.processEvents()
        win.toolbox_panel.select_capability("gdal:translate")
        qapp.processEvents()
        form = win.form_widget
        form.txt_input.setText(str(src))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        _click_submit(form)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), timeout=90.0, label="真实计算取消")
        assert not errors, errors
        assert cancel_requested["value"], "未能在真实 GDAL 计算期间触发取消"
        assert results[0].status is TaskStatus.cancelled, results[0].error
        assert results[0].error and results[0].error.get("code") == "task_cancelled"
        assert not out.exists(), "取消的任务不得提交目标文件"
        assert win.task_panel.btn_retry.isEnabled(), "取消后必须允许重试"

        QTest.mouseClick(win.task_panel.btn_retry, Qt.MouseButton.LeftButton)
        _pump(qapp, lambda: len(results) >= 2 or bool(errors), timeout=90.0, label="重试完成")
        assert not errors, errors
        retried = results[1]
        assert retried.status is TaskStatus.succeeded, retried.error
        assert retried.task_id == results[0].task_id, "重试必须复用同一 task_id"
        assert retried.attempt == 2
        assert out.is_file()
        info = _inspect(gdal_rt, out)
        assert info["w"] == 4096 and info["h"] == 4096
    finally:
        _close_window(qapp, win)
    assert not win.gdal_client.is_alive


# --------------------------------------- 6. VRT moved / missing source error
def test_desktop_vrt_moved_and_project_missing_source(
    qapp: QApplication, gdal_rt: dict[str, Any], tmp_path: Path
) -> None:
    a = _make_raster(gdal_rt, tmp_path / "vrt_a.tif", value=10.0)
    b = _make_raster(
        gdal_rt, tmp_path / "vrt_b.tif", value=20.0, gt=(102.0, 1.0, 0.0, 28.0, 0.0, -1.0)
    )
    bundle = tmp_path / "bundle"
    out = bundle / "mosaic.vrt"
    project = tmp_path / "vrt_project.spectra.json"

    win = MainWindow(project_root=tmp_path)
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    notifications: list[tuple[Any, str, str]] = []
    win.gdal_client.sig_result.connect(results.append)
    win.gdal_client.sig_error.connect(_task_error_filter(errors))
    win._notify = lambda icon, title, message: notifications.append((icon, title, message))  # type: ignore[method-assign]
    win2: MainWindow | None = None
    try:
        win.show()
        qapp.processEvents()
        win.open_dataset(a)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="打开 VRT 源 A")
        win.toolbox_panel.select_capability("gdal:buildvrt")
        qapp.processEvents()
        form = win.form_widget
        form.lst_inputs.addItem(str(a))
        form.lst_inputs.addItem(str(b))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        _click_submit(form)
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="VRT 构建")
        assert not errors, errors
        assert results[0].status is TaskStatus.succeeded, results[0].error
        assert out.is_file()
        assert 'relativeToVRT="1"' in out.read_text(encoding="utf-8")
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="VRT 自动加载")

        win.project_path = project
        assert win.save_project_action() is True

        # 只移动 VRT 到更深的目录：相对源引用失效，GUI 必须明确报错且不新增图层
        moved_dir = tmp_path / "elsewhere" / "deep"
        moved_dir.mkdir(parents=True)
        moved_vrt = moved_dir / "mosaic.vrt"
        out.rename(moved_vrt)
        layer_count = len(win.layer_panel.layers)
        win.open_dataset(moved_vrt)
        _pump(
            qapp,
            lambda: bool(notifications),
            timeout=20.0,
            label="移动后的 VRT 打开失败提示",
        )
        _icon, title, message = notifications[-1]
        assert "数据打开失败" in title
        assert "mosaic.vrt" in message or "源" in message
        qapp.processEvents()
        assert len(win.layer_panel.layers) == layer_count, "坏 VRT 不得登记为新图层"

        # 原项目引用的 VRT 已搬走：重开必须进入缺失源列表，不能静默成功
        win2 = MainWindow(project_root=tmp_path)
        try:
            assert win2.open_project(project) is True
            _pump(qapp, lambda: len(win2.layer_panel.layers) == 1, label="重开项目加载其余源")
            assert len(win2.missing_sources) == 1
            assert win2.missing_sources[0].name == "mosaic"
            assert "待重新关联 1" in win2.status_bar.currentMessage()
            assert all(ly.file_path != out.resolve() for ly in win2.layer_panel.layers)
        finally:
            _close_window(qapp, win2)
            win2 = None
    finally:
        if win2 is not None:
            _close_window(qapp, win2)
        _close_window(qapp, win)
    assert not win.gdal_client.is_alive
