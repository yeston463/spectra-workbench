"""P4-02 GUI 接入验收：从表单真实提交到落盘核对。

第 30 轮结束时 P4-02 只是**后端可调用**，GUI 的 CONNECTED_CAPABILITIES 与表单
均未接入那四项能力 —— 按 AGENTS.md 要求，这不能表述为「桌面已可操作」。
本轮接入后，本文件从**真实表单**发出 TaskSpec，经真实 worker 执行，
再独立重开产出核对语义。

覆盖：
- 能力集合是**单一来源**（param_form 与 toolbox_panel 引用同一对象）；
- 多输入能力的显隐切换（单输入行 ↔ 多输入列表）；
- 输入顺序 = 重叠优先级依据（上移下移真实改变重叠区取值）；
- 多输入不足 2 个时 GUI 侧提前拒绝；
- P4-02 参数从控件进入 TaskSpec；
- NoData 留空=未设置、填 0=设置为 0 的区分；
- 能力切换后隐藏控件的值不泄漏。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.capability_wiring import CONNECTED_CAPABILITIES, is_multi_input
from spectra_workbench.domain.models import TaskStatus
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget

pytestmark = [pytest.mark.integration, pytest.mark.engine, pytest.mark.gui]

ROOT = find_project_root()


def _get_qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return _get_qapp()


@pytest.fixture(scope="module")
def gdal_rt() -> dict:
    return resolve_gdal_runtime(ROOT)


def _make_raster(gdal_rt: dict, path: Path, arr, gt, nodata=None) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
arr = np.array({arr.tolist()!r}, dtype=np.float32)
ds = gdal.GetDriverByName("GTiff").Create(r"{path}", arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform({list(gt)!r})
ds.SetProjection(srs.ExportToWkt())
b = ds.GetRasterBand(1)
b.WriteArray(arr)
{f"b.SetNoDataValue({nodata!r})" if nodata is not None else ""}
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


@pytest.fixture
def overlap_pair(gdal_rt: dict, tmp_path) -> tuple[Path, Path]:
    """A 覆盖 x0..3 值 10；B 覆盖 x2..5 值 20；重叠区 x2..3。"""
    import numpy as np

    a = _make_raster(
        gdal_rt, tmp_path / "a.tif", np.full((4, 4), 10.0), (100.0, 1.0, 0.0, 30.0, 0.0, -1.0)
    )
    b = _make_raster(
        gdal_rt, tmp_path / "b.tif", np.full((4, 4), 20.0), (102.0, 1.0, 0.0, 28.0, 0.0, -1.0)
    )
    return a, b


def _form_for(tmp_path: Path, capability_id: str):
    from spectra_workbench.registry.catalog import get_default_registry

    form = CapabilityFormWidget()
    form.set_project_root(tmp_path)
    cap = get_default_registry().get(capability_id)
    assert cap is not None, f"注册表缺少能力 {capability_id}"
    form.load_capability(cap)
    emitted: list = []
    form.sig_task_submitted.connect(emitted.append)
    return form, emitted


def _inspect(gdal_rt: dict, path: Path) -> dict:
    script = (
        "import json\n"
        "from osgeo import gdal\n"
        "gdal.UseExceptions()\n"
        f"ds = gdal.Open(r'{path}')\n"
        "assert ds is not None\n"
        "b = ds.GetRasterBand(1)\n"
        "m = b.GetMaskBand().ReadAsArray()\n"
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "                  'arr': b.ReadAsArray().tolist(), 'mask': m.tolist(),\n"
        "                  'nodata': b.GetNoDataValue(), 'bands': ds.RasterCount,\n"
        "                  'driver': ds.GetDriver().ShortName}))"
    )
    proc = subprocess.run(
        [str(gdal_rt["python"]), "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=gdal_rt["env"],
    )
    assert proc.returncode == 0, f"校验失败：{proc.stderr}"
    return json.loads(proc.stdout.strip())


def _execute(qapp: QApplication, spec, timeout: float = 60.0):
    client = QtWorkerClient(provider_id="gdal", worker_id="p402gui", project_root=str(ROOT))
    results, errors = [], []
    client.sig_result.connect(results.append)
    client.sig_error.connect(lambda c, m: errors.append((c, m)))
    try:
        client.start(timeout=25.0)
        client.submit_task_async(spec, timeout=timeout)
        deadline = time.monotonic() + timeout
        while not results and not errors and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert not errors, f"任务上报异常：{errors}"
        assert results, "任务未产生结果"
        return results[0]
    finally:
        client.stop(timeout=5.0)


# --------------------------------------------- 1. 能力集合单一来源
def test_capability_sets_are_single_source() -> None:
    """param_form 与 toolbox_panel 必须引用**同一份**能力集合（不得各存一份）。"""
    import spectra_workbench.ui.forms.param_form as pf
    import spectra_workbench.ui.panels.toolbox_panel as tp

    assert pf.CONNECTED_CAPABILITIES is tp.CONNECTED_CAPABILITIES, (
        "两处 UI 的 CONNECTED_CAPABILITIES 必须是同一个对象（单一来源）"
    )
    assert pf.CONNECTED_CAPABILITIES is CONNECTED_CAPABILITIES


def test_p402_capabilities_are_connected() -> None:
    """P4-02 四项能力必须出现在已接通集合中（否则 GUI 禁止提交）。"""
    for cid in ("gdal:buildvrt", "gdal:merge", "gdal:stack", "gdal:bandmanage"):
        assert cid in CONNECTED_CAPABILITIES, f"{cid} 未接入 GUI"


def test_multi_input_flags() -> None:
    """拼接/VRT/堆叠需要多输入；波段管理是单输入。"""
    for cid in ("gdal:buildvrt", "gdal:merge", "gdal:stack"):
        assert is_multi_input(cid), f"{cid} 应标记为多输入"
    assert not is_multi_input("gdal:bandmanage")


# --------------------------------------------- 2. 显隐切换
def test_multi_input_capability_shows_list(qapp, tmp_path, overlap_pair) -> None:
    """多输入能力：显示列表、隐藏单文件行。"""
    form, _ = _form_for(tmp_path, "gdal:merge")
    try:
        assert form.grp_inputs.isVisibleTo(form), "多输入区应可见"
        assert not form.lbl_input.isVisibleTo(form), "单输入行应隐藏"
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()


def test_single_input_capability_hides_list(qapp, tmp_path) -> None:
    """单输入能力：隐藏多输入区、显示单文件行。"""
    form, _ = _form_for(tmp_path, "gdal:translate")
    try:
        assert not form.grp_inputs.isVisibleTo(form), "单输入能力不应显示多输入区"
        assert form.lbl_input.isVisibleTo(form), "单文件行应可见"
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()


# --------------------------------------------- 3. 端到端：顺序即优先级
def test_gui_merge_respects_list_order(qapp, gdal_rt, tmp_path, overlap_pair) -> None:
    """从 GUI 提交拼接：列表顺序决定重叠区取值。

    默认 overlapPriority=last（后列的源胜）→ [A, B] 的重叠区应取 B 的值 20。
    """
    pa, pb = overlap_pair
    out = tmp_path / "gui_merge.tif"
    form, emitted = _form_for(tmp_path, "gdal:merge")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(pa))
        form.lst_inputs.addItem(str(pb))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form._on_submit_clicked()
        assert emitted, "表单应发出任务"
        spec = emitted[-1]
        assert "inputs" in spec.inputs, "多输入能力应使用 inputs['inputs'] 列表"
        assert len(spec.inputs["inputs"]) == 2
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    row = info["arr"][2]
    assert row[2] == 20.0, f"last 优先级下重叠区应取后列的 B（20），实际 {row[2]}"
    assert row[0] == 10.0, f"仅 A 覆盖区应为 10，实际 {row[0]}"


def test_gui_merge_first_priority_via_control(qapp, gdal_rt, tmp_path, overlap_pair) -> None:
    """在 GUI 里选「先列的源优先」：重叠区应取 A 的值 10。"""
    pa, pb = overlap_pair
    out = tmp_path / "gui_merge_first.tif"
    form, emitted = _form_for(tmp_path, "gdal:merge")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(pa))
        form.lst_inputs.addItem(str(pb))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        idx = form.cmb_overlap_priority.findData("first")
        assert idx >= 0
        form.cmb_overlap_priority.setCurrentIndex(idx)
        form._on_submit_clicked()
        spec = emitted[-1]
        assert spec.parameters.get("overlapPriority") == "first"
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["arr"][2][2] == 10.0, (
        f"first 优先级下重叠区应取先列的 A（10），实际 {info['arr'][2][2]}"
    )


def test_gui_move_input_changes_overlap(qapp, gdal_rt, tmp_path, overlap_pair) -> None:
    """列表上移/下移真实改变结果（验证 UI 顺序控制确实生效）。"""
    pa, pb = overlap_pair
    form, emitted = _form_for(tmp_path, "gdal:merge")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(pa))
        form.lst_inputs.addItem(str(pb))
        # 选中第二项并上移 → 顺序变为 [B, A] → last 优先级下 A 胜
        form.lst_inputs.setCurrentRow(1)
        form._move_selected_input(-1)
        paths = form.input_paths()
        assert paths[0] == pb and paths[1] == pa, f"上移后顺序应为 [B, A]，实际 {paths}"

        out = tmp_path / "gui_move.tif"
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form._on_submit_clicked()
        spec = emitted[-1]
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["arr"][2][2] == 10.0, (
        f"顺序反转为 [B,A] 后，last 优先级下重叠区应取 A（10），实际 {info['arr'][2][2]}"
    )


# --------------------------------------------- 4. GUI 侧提前拒绝
def test_gui_rejects_insufficient_inputs(qapp, tmp_path, overlap_pair) -> None:
    """多输入能力少于 2 个源时，GUI 侧应提前拒绝而不是提交后失败。"""
    pa, _ = overlap_pair
    form, emitted = _form_for(tmp_path, "gdal:merge")
    warnings: list[str] = []
    import spectra_workbench.ui.forms.param_form as pf

    original = pf.QMessageBox.warning
    try:
        pf.QMessageBox.warning = lambda *a, **k: warnings.append(str(a[-1]))  # type: ignore[assignment]
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(pa))
        form.txt_output.setText(str(tmp_path / "only_one.tif"))
        form._on_submit_clicked()
    finally:
        pf.QMessageBox.warning = original  # type: ignore[assignment]
        form.close()
        form.deleteLater()
        qapp.processEvents()

    assert not emitted, "输入不足时不得提交"
    assert warnings, "输入不足必须有明确提示"


# --------------------------------------------- 5. 参数进入 TaskSpec
def test_gui_nodata_blank_means_unset(qapp, tmp_path, overlap_pair) -> None:
    """NoData 控件留空 = 未设置（不产生参数）。"""
    pa, pb = overlap_pair
    form, emitted = _form_for(tmp_path, "gdal:merge")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(pa))
        form.lst_inputs.addItem(str(pb))
        form.txt_output.setText(str(tmp_path / "blank.tif"))
        form.txt_src_nodata.clear()
        form.txt_dst_nodata.clear()
        form._on_submit_clicked()
        spec = emitted[-1]
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()

    assert "srcNodata" not in spec.parameters, "留空不应产生 srcNodata"
    assert "dstNodata" not in spec.parameters, "留空不应产生 dstNodata"


def test_gui_nodata_zero_means_explicit_zero(qapp, gdal_rt, tmp_path, overlap_pair) -> None:
    """NoData 控件填 0 = 显式设置为 0（必须产生参数并真实生效）。"""
    import numpy as np

    # 用有缺口的源，验证 dstNodata=0 会重映射无效像元
    c = _make_raster(
        gdal_rt,
        tmp_path / "c.tif",
        np.full((2, 2), 5.0),
        (100.0, 1.0, 0.0, 30.0, 0.0, -1.0),
        nodata=-9999.0,
    )
    d = _make_raster(
        gdal_rt,
        tmp_path / "d.tif",
        np.full((2, 2), 7.0),
        (103.0, 1.0, 0.0, 30.0, 0.0, -1.0),
        nodata=-9999.0,
    )
    out = tmp_path / "zero.tif"
    form, emitted = _form_for(tmp_path, "gdal:merge")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(c))
        form.lst_inputs.addItem(str(d))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form.txt_src_nodata.setText("-9999")
        form.txt_vrt_nodata.setText("-9999")
        form.txt_dst_nodata.setText("0")
        form._on_submit_clicked()
        spec = emitted[-1]
        assert spec.parameters.get("dstNodata") == 0.0, (
            f"填 0 必须产生 dstNodata=0，实际 {spec.parameters.get('dstNodata')!r}"
        )
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["nodata"] == 0.0, f"输出 NoData 应为 0，实际 {info['nodata']}"
    assert info["arr"][0][2] == 0.0, f"缺口实际值应重映射为 0，实际 {info['arr'][0][2]}"
    assert info["mask"][0][2] == 0, f"缺口 mask 应标无效，实际 {info['mask'][0][2]}"


def test_gui_band_order_reaches_spec(qapp, gdal_rt, tmp_path) -> None:
    """波段顺序控件进入 TaskSpec 并真实改变波段顺序。"""

    multi = tmp_path / "multi.tif"
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
ds = gdal.GetDriverByName("GTiff").Create(r"{multi}", 4, 4, 3, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform([100.0, 1.0, 0.0, 30.0, 0.0, -1.0])
ds.SetProjection(srs.ExportToWkt())
for i in range(1, 4):
    ds.GetRasterBand(i).WriteArray(np.full((4, 4), float(i * 100)))
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

    out = tmp_path / "reordered.tif"
    form, emitted = _form_for(tmp_path, "gdal:bandmanage")
    try:
        # bandmanage 是单输入能力
        form.txt_input.setText(str(multi))
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        form.txt_band_list.setText("3,1,2")
        form._on_submit_clicked()
        spec = emitted[-1]
        assert spec.parameters.get("bandList") == [3, 1, 2]
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()

    result = _execute(qapp, spec)
    assert result.status is TaskStatus.succeeded, result.error
    info = _inspect(gdal_rt, out)
    assert info["bands"] == 3
    assert info["arr"][0][0] == 300.0, f"波段1 应为原波段3，实际 {info['arr'][0][0]}"


# --------------------------------------------- 6. 切能力不泄漏
def test_switch_capability_does_not_leak_p402_params(qapp, tmp_path, overlap_pair) -> None:
    """从 gdal:merge 切到 gdal:translate 后，P4-02 控件值不得进入新 TaskSpec。"""
    pa, pb = overlap_pair
    form, emitted = _form_for(tmp_path, "gdal:merge")
    try:
        form.lst_inputs.clear()
        form.lst_inputs.addItem(str(pa))
        form.lst_inputs.addItem(str(pb))
        form.txt_src_nodata.setText("-9999")
        form.txt_band_list.setText("1,2")

        from spectra_workbench.registry.catalog import get_default_registry

        form.load_capability(get_default_registry().get("gdal:translate"))
        form.txt_input.setText(str(pa))
        form.txt_output.setText(str(tmp_path / "switched.tif"))
        form._on_submit_clicked()
        params = emitted[-1].parameters
    finally:
        form.close()
        form.deleteLater()
        qapp.processEvents()

    assert params.get("srcNodata") is None, f"srcNodata 泄漏：{params}"
    assert params.get("bandList") is None, f"bandList 泄漏：{params}"
    assert params.get("overlapPriority") is None, f"overlapPriority 泄漏：{params}"
