"""P4-04 GUI 端到端验收：表单 → TaskSpec → 真实 worker → 落盘/读回。

后端语义由 test_p4_expression_ops.py 与 test_p4_calc_statistics.py 覆盖；
本文件专门证明这些能力可从桌面表单实际提交，且统计 JSON 不会被误当栅格加载。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.capability_wiring import (
    CONNECTED_CAPABILITIES,
    is_multi_input,
)
from spectra_workbench.domain.models import DatasetKind, TaskStatus
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget
from spectra_workbench.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.engine, pytest.mark.gui]

ROOT = find_project_root()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def gdal_rt() -> dict[str, Any]:
    return resolve_gdal_runtime(ROOT)


def _make_raster(
    gdal_rt: dict[str, Any],
    path: Path,
    values: list[list[float]],
    nodata: float | None = None,
) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
arr = np.array({values!r}, dtype=np.float32)
ds = gdal.GetDriverByName("GTiff").Create({str(path)!r}, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform([100.0, 1.0, 0.0, 30.0, 0.0, -1.0])
ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.WriteArray(arr)
if {nodata!r} is not None:
    b.SetNoDataValue({nodata!r})
b.FlushCache()
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
    assert proc.returncode == 0, f"源栅格构造失败：{proc.stderr}"
    return path


def _inspect(gdal_rt: dict[str, Any], path: Path) -> dict[str, Any]:
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open({str(path)!r})\n"
        "assert ds is not None, '产出无法打开'\n"
        "b = ds.GetRasterBand(1)\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "  'nodata': b.GetNoDataValue(), 'data': b.ReadAsArray().tolist(),\n"
        "  'mask': b.GetMaskBand().ReadAsArray().tolist(),\n"
        "  'driver': ds.GetDriver().ShortName}))"
    )
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"成果重开失败：{proc.stderr}"
    return json.loads(proc.stdout.strip())


def _load_capability(form: CapabilityFormWidget, capability_id: str) -> None:
    from spectra_workbench.registry.catalog import get_default_registry

    cap = get_default_registry().get(capability_id)
    assert cap is not None, f"注册表缺少能力 {capability_id}"
    form.load_capability(cap)


def _form_for(tmp_path: Path, capability_id: str) -> tuple[CapabilityFormWidget, list[Any]]:
    form = CapabilityFormWidget()
    form.set_project_root(tmp_path)
    _load_capability(form, capability_id)
    emitted: list[Any] = []
    form.sig_task_submitted.connect(emitted.append)
    return form, emitted


def _close_form(qapp: QApplication, form: CapabilityFormWidget) -> None:
    form.close()
    form.deleteLater()
    qapp.processEvents()


def _execute(qapp: QApplication, spec: Any, timeout: float = 60.0) -> Any:
    client = QtWorkerClient(
        provider_id="gdal",
        worker_id=f"p404gui-{uuid.uuid4().hex[:8]}",
        project_root=str(ROOT),
    )
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda code, msg: errors.append((code, msg)))
    try:
        client.start(timeout=25.0)
        client.submit_task_async(spec, timeout=timeout)
        deadline = time.monotonic() + timeout
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert not errors, f"任务上报异常：{errors}"
        assert results, "任务未在时限内产生结果"
        return results[0]
    finally:
        client.stop(timeout=5.0)


def _wait_results(
    qapp: QApplication,
    results: list[Any],
    errors: list[tuple[str, str]],
    *,
    count: int,
    timeout: float = 60.0,
) -> None:
    deadline = time.monotonic() + timeout
    while len(results) < count and not errors and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    assert not errors, f"任务上报异常：{errors}"
    assert len(results) >= count, f"只收到 {len(results)}/{count} 个任务结果"


def _set_stats_mode(form: CapabilityFormWidget, mode: str) -> None:
    index = form.cmb_stats_mode.findData(mode)
    assert index >= 0
    form.cmb_stats_mode.setCurrentIndex(index)


def _set_output_type(form: CapabilityFormWidget, type_name: str) -> None:
    index = form.cmb_out_type.findText(type_name)
    assert index >= 0, f"输出类型下拉框没有 {type_name}"
    form.cmb_out_type.setCurrentIndex(index)


def test_gui_calc_divzero_mask_and_valid_zero(qapp, gdal_rt, tmp_path) -> None:
    a = _make_raster(
        gdal_rt,
        tmp_path / "calc_a.tif",
        [[10, 20, 0], [-9999, 40, 30000]],
        nodata=-9999.0,
    )
    b = _make_raster(gdal_rt, tmp_path / "calc_b.tif", [[5, 10, 0], [0, 20, 30000]])
    out = tmp_path / "gui_calc.tif"
    form, emitted = _form_for(tmp_path, "gdal:calc")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(a))
        form.lst_inputs.addItem(str(b))
        form.txt_calc.setText("(A-B)/(A+B)")
        form.txt_dst_nodata.setText("-9999")
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form._on_submit_clicked()
        assert emitted, "有效表达式应从表单发出任务"
        spec = emitted[-1]
        assert spec.parameters["calc"] == "(A-B)/(A+B)"
        assert spec.parameters["dstNodata"] == -9999.0
        assert len(spec.inputs["inputs"]) == 2
        assert spec.outputs[0].kind is DatasetKind.raster
    finally:
        _close_form(qapp, form)

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["driver"] == "GTiff"
    assert info["nodata"] == pytest.approx(-9999.0)
    assert info["data"][0][0] == pytest.approx(1.0 / 3.0, abs=1e-6)
    assert info["data"][0][1] == pytest.approx(1.0 / 3.0, abs=1e-6)
    assert info["data"][0][2] == pytest.approx(-9999.0), "0/0 必须转为输出 NoData"
    assert info["data"][1][0] == pytest.approx(-9999.0), "输入 NoData 必须传播"
    assert info["data"][1][1] == pytest.approx(1.0 / 3.0, abs=1e-6)
    assert info["data"][1][2] == pytest.approx(0.0), "有效计算结果 0 不得被当成 NoData"
    assert info["mask"][0][2] == 0 and info["mask"][1][0] == 0
    assert info["mask"][1][2] != 0, "有效零值的 mask 必须保持有效"
    calc = result.outputs[0]["calc"]
    assert calc["n_math_invalid"] == 1
    assert calc["n_input_invalid"] == 1


def test_gui_calc_parse_error_returns_failure_without_output(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "parse_src.tif", [[1, 2]])
    out = tmp_path / "parse_out.tif"
    form, emitted = _form_for(tmp_path, "gdal:calc")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(src))
        form.txt_calc.setText("(A+")
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form._on_submit_clicked()
        spec = emitted[-1]
    finally:
        _close_form(qapp, form)

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.failed
    assert result.error["code"] == "invalid_expression"
    assert "语法" in result.error["message"]
    assert not out.exists(), "解析失败不得产出文件"


def test_gui_calc_integer_overflow_is_rejected(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "overflow_src.tif", [[30000, 30000]])
    out = tmp_path / "overflow_out.tif"
    form, emitted = _form_for(tmp_path, "gdal:calc")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(src))
        form.txt_calc.setText("A+A")
        _set_output_type(form, "Int16")
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form._on_submit_clicked()
        spec = emitted[-1]
        assert spec.parameters["outputType"] == "Int16"
    finally:
        _close_form(qapp, form)

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.failed
    assert result.error["code"] == "invalid_expression"
    assert "回绕" in result.error["message"]
    assert not out.exists(), "整型溢出不得静默回绕并产出文件"


def test_gui_statistics_full_report_excludes_nodata(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(
        gdal_rt,
        tmp_path / "stats_full_src.tif",
        [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10, 11], [12, 13, 14, -9999]],
        nodata=-9999.0,
    )
    out = tmp_path / "stats_full.json"
    form, emitted = _form_for(tmp_path, "gdal:statistics")
    try:
        form.txt_input.setText(str(src))
        _set_stats_mode(form, "full")
        form.spin_hist_bins.setValue(4)
        form.spin_full_limit.setValue(100)
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form._on_submit_clicked()
        spec = emitted[-1]
        assert spec.parameters["statisticsMode"] == "full"
        assert spec.parameters["histogramBins"] == 4
        assert spec.outputs[0].kind is DatasetKind.artifact
    finally:
        _close_form(qapp, form)

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.succeeded, result.error
    assert result.outputs[0]["kind"] == "artifact"
    assert result.outputs[0]["driver"] == "JSON"
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["mode"] == "full"
    assert report["source"] == "full_raster"
    assert report["stride"] == 1
    assert report["sampled_is_estimate"] is False
    assert report["pixel_count"] == 16
    band = report["bands"][0]
    assert band["valid_count"] == 15 and band["total_count"] == 16
    assert band["min"] == 0.0 and band["max"] == 14.0
    assert band["mean"] == pytest.approx(7.0)
    assert sum(band["histogram"]["counts"]) == 15
    assert len(band["histogram"]["edges"]) == 5
    assert not Path(f"{src}.aux.xml").exists(), "只读统计不得写 PAM sidecar"


def test_gui_statistics_sampled_mode_is_explicitly_estimated(qapp, gdal_rt, tmp_path) -> None:
    values = [[(row * 64 + col) % 100 for col in range(64)] for row in range(64)]
    src = _make_raster(gdal_rt, tmp_path / "stats_sampled_src.tif", values)
    out = tmp_path / "stats_sampled.json"
    form, emitted = _form_for(tmp_path, "gdal:statistics")
    try:
        form.txt_input.setText(str(src))
        _set_stats_mode(form, "sampled")
        form.spin_hist_bins.setValue(8)
        form.spin_max_samples.setValue(64)
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form._on_submit_clicked()
        spec = emitted[-1]
        assert spec.parameters["maxSamples"] == 64
    finally:
        _close_form(qapp, form)

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.succeeded, result.error
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["mode"] == "sampled"
    assert report["source"] == "sampled_grid"
    assert report["stride"] == 8
    assert report["sampled_is_estimate"] is True
    assert report["pixel_count"] == 4096
    assert (report["read_width"], report["read_height"]) == (8, 8)
    assert report["bands"][0]["valid_count"] == 64
    assert sum(report["bands"][0]["histogram"]["counts"]) == 64


def test_gui_statistics_full_limit_is_enforced(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "stats_limit_src.tif", [[1, 2], [3, 4]])
    out = tmp_path / "stats_limit.json"
    form, emitted = _form_for(tmp_path, "gdal:statistics")
    try:
        form.txt_input.setText(str(src))
        _set_stats_mode(form, "full")
        form.spin_full_limit.setValue(2)
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form._on_submit_clicked()
        spec = emitted[-1]
        assert spec.parameters["fullLimit"] == 2
    finally:
        _close_form(qapp, form)

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.failed
    assert result.error["code"] == "invalid_parameters"
    assert "超过上限" in result.error["message"]
    assert not out.exists(), "全量统计超限不得静默降级或产出报告"


def test_gui_statistics_rejects_non_json_output_without_modal(qapp, gdal_rt, tmp_path, monkeypatch) -> None:
    src = _make_raster(gdal_rt, tmp_path / "stats_suffix_src.tif", [[1, 2]])
    form, emitted = _form_for(tmp_path, "gdal:statistics")
    warnings: list[str] = []
    import spectra_workbench.ui.forms.param_form as param_form

    monkeypatch.setattr(
        param_form.QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append(str(args[-1])),
    )
    try:
        form.txt_input.setText(str(src))
        form.txt_output.setText(str(tmp_path / "stats.txt"))
        form._on_submit_clicked()
    finally:
        _close_form(qapp, form)

    assert not emitted, "非 JSON 统计输出不得提交给 worker"
    assert any(".json" in message for message in warnings)


def test_main_window_loads_calc_raster_but_not_statistics_json(qapp, gdal_rt, tmp_path) -> None:
    src = _make_raster(gdal_rt, tmp_path / "window_src.tif", [[1, 2]])
    calc_out = tmp_path / "window_calc.tif"
    stats_out = tmp_path / "window_stats.json"
    win = MainWindow(project_root=tmp_path)
    opened: list[str] = []
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    win.open_dataset = lambda path, *args, **kwargs: opened.append(str(path))  # type: ignore[method-assign]
    client = win.gdal_client
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda code, msg: errors.append((code, msg)))
    try:
        _load_capability(win.form_widget, "gdal:calc")
        win.form_widget.lst_inputs.clear()
        win.form_widget.lst_inputs.addItem(str(src))
        win.form_widget.txt_calc.setText("A+1")
        win.form_widget.txt_output.setText(str(calc_out))
        win.form_widget.chk_overwrite.setChecked(True)
        win.form_widget._on_submit_clicked()
        _wait_results(qapp, results, errors, count=1)
        assert results[0].status is TaskStatus.succeeded, results[0].error
        assert [Path(p).resolve() for p in opened] == [calc_out.resolve()]
        assert _inspect(gdal_rt, calc_out)["data"] == [[2.0, 3.0]]

        _load_capability(win.form_widget, "gdal:statistics")
        win.form_widget.txt_input.setText(str(src))
        _set_stats_mode(win.form_widget, "full")
        win.form_widget.txt_output.setText(str(stats_out))
        win.form_widget.chk_overwrite.setChecked(True)
        win.form_widget._on_submit_clicked()
        _wait_results(qapp, results, errors, count=2)
        assert results[1].status is TaskStatus.succeeded, results[1].error
        assert len(opened) == 1, "JSON 统计报告不得被当成栅格打开"
        assert "非栅格图层" in win.status_bar.currentMessage()
        assert json.loads(stats_out.read_text(encoding="utf-8"))["mode"] == "full"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()

    assert not client.is_alive, "关窗后 GDAL worker 必须停止"


# --------------------------------------------- 能力接通 / 显隐 / 状态隔离
def test_p404_capabilities_are_connected_and_controls_switch(qapp, tmp_path) -> None:
    """两个能力必须可提交，且表达式用列表、统计用单输入行，控件互不串场。"""
    for cid in ("gdal:calc", "gdal:statistics"):
        assert cid in CONNECTED_CAPABILITIES, f"{cid} 未接入 GUI"
    assert is_multi_input("gdal:calc"), "表达式需可排序多输入列表（A/B/C…）"
    assert not is_multi_input("gdal:statistics"), "统计是单输入能力"

    calc_form, _ = _form_for(tmp_path, "gdal:calc")
    try:
        assert calc_form.grp_inputs.isVisibleTo(calc_form)
        assert not calc_form.lbl_input.isVisibleTo(calc_form)
        assert calc_form.txt_calc.isVisibleTo(calc_form)
        assert not calc_form.lbl_stats_mode.isVisibleTo(calc_form)
    finally:
        _close_form(qapp, calc_form)

    stats_form, _ = _form_for(tmp_path, "gdal:statistics")
    try:
        assert stats_form.lbl_input.isVisibleTo(stats_form)
        assert not stats_form.grp_inputs.isVisibleTo(stats_form)
        assert stats_form.cmb_stats_mode.isVisibleTo(stats_form)
        assert not stats_form.lbl_calc.isVisibleTo(stats_form)
    finally:
        _close_form(qapp, stats_form)


def test_gui_calc_allows_single_source(qapp, gdal_rt, tmp_path) -> None:
    """表达式允许恰好 1 个源（A*2），与拼接类 >=2 源的约束不同。"""
    src = _make_raster(gdal_rt, tmp_path / "single_src.tif", [[2, 4]])
    form, emitted = _form_for(tmp_path, "gdal:calc")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(src))
        form.txt_calc.setText("A*2")
        form.txt_output.setText(str(tmp_path / "single_calc.tif"))
        form._on_submit_clicked()
    finally:
        _close_form(qapp, form)

    assert emitted, "单源表达式必须允许提交"
    spec = emitted[-1]
    assert len(spec.inputs["inputs"]) == 1
    assert spec.parameters["calc"] == "A*2"
    assert spec.parameters["format"] == "GTiff"
    assert spec.outputs[0].kind is DatasetKind.raster


def test_gui_calc_rejects_empty_expression(qapp, gdal_rt, tmp_path, monkeypatch) -> None:
    """缺表达式时 GUI 提前拒绝，不得把空任务交给 worker。"""
    src = _make_raster(gdal_rt, tmp_path / "empty_expr_src.tif", [[1, 2]])
    warnings: list[str] = []
    import spectra_workbench.ui.forms.param_form as param_form

    monkeypatch.setattr(
        param_form.QMessageBox,
        "warning",
        lambda *args, **kwargs: warnings.append(str(args[-1])),
    )
    form, emitted = _form_for(tmp_path, "gdal:calc")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(src))
        form.txt_calc.clear()
        form.txt_output.setText(str(tmp_path / "empty_expr.tif"))
        form._on_submit_clicked()
    finally:
        _close_form(qapp, form)

    assert not emitted, "缺表达式不得提交"
    assert any("calc" in message for message in warnings), f"应给出明确提示：{warnings}"


def test_switch_capability_does_not_leak_p404_params(qapp, gdal_rt, tmp_path) -> None:
    """从表达式切到统计、再切到转换，P4-04 控件值不得残留进新 TaskSpec。"""
    src = _make_raster(gdal_rt, tmp_path / "leak_src.tif", [[1, 2]])
    form, emitted = _form_for(tmp_path, "gdal:calc")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(src))
        form.txt_calc.setText("A*2")
        form.spin_hist_bins.setValue(8)

        _load_capability(form, "gdal:statistics")
        form.txt_input.setText(str(src))
        form.txt_output.setText(str(tmp_path / "after_calc.json"))
        form._on_submit_clicked()
        stats_params = emitted[-1].parameters

        _load_capability(form, "gdal:translate")
        form.txt_input.setText(str(src))
        form.txt_output.setText(str(tmp_path / "after_stats.tif"))
        form._on_submit_clicked()
        translate_params = emitted[-1].parameters
    finally:
        _close_form(qapp, form)

    assert "calc" not in stats_params, f"calc 泄漏进统计：{stats_params}"
    assert "calc" not in translate_params, f"calc 泄漏进转换：{translate_params}"
    assert "statisticsMode" not in translate_params, f"统计参数泄漏：{translate_params}"
    assert "histogramBins" not in translate_params, f"分箱参数泄漏：{translate_params}"
