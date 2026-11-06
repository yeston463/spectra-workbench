"""P4-05 首个完整用户流程验收：打开 → 选择工具 → 配置 → 执行 → 取消/重试
→ 加载 → 导出 → 保存项目 → 重开。

证据边界：
- 主流程与导出用**真实 MainWindow + 真实 GDAL worker + 真实栅格**，不 mock 任务结果；
- 取消/重试需要可控时长的在途任务，用 ``dummy`` provider 的慢任务（协议、UI 按钮、
  worker 子进程均为真实链路）；真实 GDAL 计算中取消由 test_scheduler_lifecycle.py 覆盖。
- 每层都断言真实副作用（落盘文件可重开 / 图层树 / 项目 JSON / attempt 变化），
  不用「函数被调用过」冒充可用性。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime
from spectra_workbench.domain.models import (
    EngineConstraint,
    OutputPlan,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.scheduler.client import QtWorkerClient
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


# --------------------------------------------------------------------- 辅助
def _make_raster(gdal_rt: dict[str, Any], path: Path, size: int = 8) -> Path:
    script = f"""
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
n = {size}
arr = np.arange(n * n, dtype=np.float32).reshape(n, n) % 200
ds = gdal.GetDriverByName("GTiff").Create({str(path)!r}, n, n, 1, gdal.GDT_Float32)
srs = osr.SpatialReference(); srs.ImportFromEPSG(4326)
ds.SetGeoTransform([100.0, 0.5, 0.0, 30.0, 0.0, -0.5])
ds.SetProjection(srs.ExportToWkt())
ds.GetRasterBand(1).WriteArray(arr)
ds.FlushCache()
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
        "print(json.dumps({'w': ds.RasterXSize, 'h': ds.RasterYSize,\n"
        "  'driver': ds.GetDriver().ShortName, 'crs': bool(ds.GetProjectionRef()),\n"
        "  'first': float(ds.GetRasterBand(1).ReadAsArray()[0][0])}))"
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


def _dummy_slow_spec(task_id: str) -> TaskSpec:
    """可控时长的在途任务：20 × 0.1s，每步都检查协作取消。"""
    return TaskSpec(
        task_id=task_id,
        attempt=1,
        capability_id="dummy:noop",
        descriptor_revision="1.0",
        engine=EngineConstraint(
            provider_id="dummy", runtime_id="dummy-test", expected_version="1.0.0"
        ),
        project_root=str(ROOT),
        workspace=tempfile.gettempdir(),
        parameters={"steps": 20, "delay": 0.1},
        outputs=[OutputPlan(key="output", kind="scalar", uri="memory://dummy")],
    )


# ------------------------------------------------------- 主流程（八环串联）
def test_full_flow_open_configure_execute_load_export_save_reopen(
    qapp, gdal_rt, tmp_path
) -> None:
    src = _make_raster(gdal_rt, tmp_path / "flow_src.tif", size=8)
    out = tmp_path / "flow_processed.tif"
    view_png = tmp_path / "flow_view.png"
    export_tif = tmp_path / "flow_export.tif"
    project = tmp_path / "flow.spectra.json"

    win = MainWindow(project_root=tmp_path)
    results: list[Any] = []
    errors: list[tuple[str, str]] = []
    win.gdal_client.sig_result.connect(results.append)

    def _record_task_error(code: str, msg: str) -> None:
        # client.sig_error 是**全客户端广播**：P3-02 元数据面板对栅格图层也会
        # 发起矢量/子数据集探测，失败时广播 probe_vector_failed 等查询类错误。
        # 这些与任务执行无关，已在 work/_probe_p405_errors.py 实测确认来源。
        if code.startswith(("probe_", "read_window_", "subdataset", "list_subdatasets")):
            return
        errors.append((code, msg))

    win.gdal_client.sig_error.connect(_record_task_error)
    win2: MainWindow | None = None
    try:
        win.resize(1100, 760)
        win.show()
        qapp.processEvents()

        # 1) 打开：真实 worker 探测，图层树出现且画布绑定
        win.open_dataset(src)
        _pump(qapp, lambda: len(win.layer_panel.layers) == 1, label="源栅格探测")
        assert win.layer_panel.layers[0].file_path == src.resolve()
        _pump(
            qapp,
            lambda: getattr(win.canvas.img_item, "image", None) is not None,
            timeout=20.0,
            label="画布渲染出影像",
        )

        # 2) 选择工具：走工具箱树选中 -> 信号 -> 表单，且自动填入当前图层
        win.toolbox_panel.select_capability("gdal:translate")
        qapp.processEvents()
        assert win.form_widget._current_cap is not None
        assert win.form_widget._current_cap.capability_id == "gdal:translate"
        assert Path(win.form_widget.txt_input.text()) == src.resolve()

        # 3) 配置：输出路径、格式、覆盖策略
        form = win.form_widget
        form.txt_output.setText(str(out))
        form.chk_overwrite.setChecked(True)
        idx = form.cmb_format.findText("GTiff", Qt.MatchFlag.MatchStartsWith)
        form.cmb_format.setCurrentIndex(max(idx, 0))

        # 4) 执行：表单 -> TaskSpec -> 真实 GDAL worker -> 成果落盘
        form._on_submit_clicked()
        _pump(qapp, lambda: len(results) >= 1 or bool(errors), label="转换任务完成")
        assert not errors, f"任务上报异常：{errors}"
        assert results[0].status is TaskStatus.succeeded, results[0].error
        assert _inspect(gdal_rt, out)["w"] == 8, "成果必须能被 GDAL 独立重开"

        # 5) 加载：成功后自动加入图层树（异步探测）
        _pump(qapp, lambda: len(win.layer_panel.layers) == 2, label="成果自动加载")
        loaded_paths = {ly.file_path for ly in win.layer_panel.layers}
        assert out.resolve() in loaded_paths

        # 6) 导出 A：当前视图截图 + 可复现元数据
        captured = win.export_view_image(view_png)
        assert captured == view_png.resolve()
        assert view_png.is_file() and view_png.stat().st_size > 0
        image = QImage(str(view_png))
        assert not image.isNull() and image.width() > 0 and image.height() > 0
        sidecar = view_png.with_name(view_png.name + ".json")
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        assert meta["type"] == "spectra_view_capture"
        assert meta["contains_georeference"] is False
        assert meta["rendered"] is True, "导出必须来自已渲染的画布"
        assert Path(meta["layer"]["source"]) in loaded_paths
        assert len(meta["layer_names"]) == 2

        # 7) 导出 B：带地理参考的栅格，走同一套参数契约与 worker
        layer_count_before = len(win.layer_panel.layers)
        assert win.export_layer_raster(export_tif) is True
        _pump(qapp, lambda: len(results) >= 2 or bool(errors), label="栅格导出任务完成")
        assert not errors, f"导出任务上报异常：{errors}"
        assert results[1].status is TaskStatus.succeeded, results[1].error
        exported = _inspect(gdal_rt, export_tif)
        assert exported["driver"] == "GTiff" and exported["w"] == 8
        assert exported["crs"] is True, "数据导出必须保留 CRS"
        assert exported["first"] == pytest.approx(0.0)
        assert len(win.layer_panel.layers) == layer_count_before, "导出不得污染图层树"

        # 目标指向任一已加载图层（含自身）都要拒绝，且不留悬挂的 export 标记
        assert win.export_layer_raster(src) is False
        assert "同源冲突" in win.status_bar.currentMessage()
        assert win._export_task_ids == set()

        # 8) 保存项目：写真实 .spectra.json，记录两个图层
        win.project_path = project
        assert win.save_project_action() is True
        saved = json.loads(project.read_text(encoding="utf-8"))
        assert project.is_file()
        assert len(saved["layers"]) == 2
        assert saved["path_anchor"] == "project_dir"

        # 9) 重开：新窗口按项目文件恢复图层与视图
        win2 = MainWindow(project_root=tmp_path)
        win2_results: list[Any] = []
        win2.gdal_client.sig_result.connect(win2_results.append)
        try:
            assert win2.open_project(project) is True
            _pump(qapp, lambda: len(win2.layer_panel.layers) == 2, label="重开项目恢复图层")
            reopened = {ly.layer_id for ly in win2.layer_panel.layers}
            assert reopened == {ly.layer_id for ly in win.layer_panel.layers}
            assert win2.missing_sources == [], "项目内相对路径不得产生缺失源"
            assert {ly.file_path for ly in win2.layer_panel.layers} == loaded_paths
            assert win2.project_path is not None
            assert win2.project_path.name == project.name

            # 重开不只是「看见图层」：工具链必须能继续产出真实成果
            reopen_export = tmp_path / "reopen_export.tif"
            assert win2.export_layer_raster(reopen_export) is True
            _pump(qapp, lambda: len(win2_results) >= 1, label="重开后再次导出")
            assert win2_results[0].status is TaskStatus.succeeded, win2_results[0].error
            assert _inspect(gdal_rt, reopen_export)["w"] == 8
        finally:
            _close_window(qapp, win2)
            win2 = None
    finally:
        if win2 is not None:
            _close_window(qapp, win2)
        _close_window(qapp, win)
        qapp.processEvents()

    assert not win.gdal_client.is_alive, "关窗后 GDAL worker 必须停止"


# ------------------------------------------------- 取消 / 重试（真实 UI 按钮）
def test_full_flow_cancel_then_retry_same_task(qapp, tmp_path) -> None:
    win = MainWindow(project_root=tmp_path)
    client = QtWorkerClient(
        provider_id="dummy",
        worker_id=f"p405-dummy-{uuid.uuid4().hex[:8]}",
        project_root=str(ROOT),
    )
    # submit_task 按 capability_id 选 client；把 GDAL 槽位换成 dummy worker，
    # 其余链路（面板按钮、信号、协议、子进程、取消令牌）全部保持真实。
    win._clients["gdal"] = client
    results: list[Any] = []
    progress: list[Any] = []
    errors: list[tuple[str, str]] = []
    client.sig_result.connect(results.append)
    client.sig_progress.connect(lambda *args: progress.append(args))
    client.sig_error.connect(lambda code, msg: errors.append((code, msg)))
    # dummy 成果是 memory:// 标量而非栅格，本用例只验取消/重试；
    # 「加载成果图层」环由主流程用例用真实栅格覆盖。
    win.load_result_to_layer = lambda result: None  # type: ignore[method-assign]
    try:
        spec = _dummy_slow_spec(f"p405-flow-{uuid.uuid4().hex[:6]}")
        # 执行
        win.submit_task(spec)
        assert win._last_task_spec is spec
        _pump(qapp, lambda: bool(progress) or bool(results), label="任务开始产生进度")
        assert win.task_panel.btn_cancel.isEnabled()

        # 取消：真实点击面板按钮 -> 主窗口 -> QtWorkerClient -> Kind.cancel -> worker
        win.task_panel.btn_cancel.click()
        _pump(qapp, lambda: len(results) >= 1, label="取消结果回传")
        cancelled = results[0]
        assert cancelled.status is TaskStatus.cancelled, cancelled.error
        assert cancelled.task_id == spec.task_id
        assert cancelled.attempt == 1
        assert cancelled.error["code"] == "task_cancelled"
        assert win._active_task_client is None
        assert win.task_panel.btn_retry.isEnabled(), "取消后必须允许重试"

        # 重试：同一 task_id、attempt+1、原参数不变
        win.task_panel.btn_retry.click()
        assert win._last_task_spec.task_id == spec.task_id
        assert win._last_task_spec.attempt == 2
        assert win._last_task_spec.parameters == spec.parameters
        assert "第 2 次尝试" in win.task_panel.lbl_task_info.text()
        _pump(qapp, lambda: len(results) >= 2 or bool(errors), label="重试任务完成")
        assert not errors, f"重试任务上报异常：{errors}"
        retried = results[1]
        assert retried.status is TaskStatus.succeeded, retried.error
        assert retried.task_id == spec.task_id, "重试不得新建 task_id"
        assert retried.attempt == 2
        assert not win.task_panel.btn_retry.isEnabled(), "成功后不应继续提示重试"
    finally:
        _close_window(qapp, win)
        qapp.processEvents()

    assert not client.is_alive, "关窗后 dummy worker 必须停止"


# ------------------------------------------------------------ 边界与可发现性
def test_full_flow_actions_exist_and_refuse_invalid_exports(qapp, tmp_path) -> None:
    win = MainWindow(project_root=tmp_path)
    try:
        assert "导出" in win.act_export_view.text()
        assert "导出" in win.act_export_layer.text()
        assert "取消" in win.act_cancel_task.text()
        assert "重试" in win.act_retry_task.text()
        assert not win.task_panel.btn_retry.isEnabled()
        assert not win.task_panel.btn_cancel.isEnabled()

        # 菜单动作不是摆设：触发后必须到达对应的对话框入口
        triggered: list[str] = []
        win.export_view_dialog = lambda: triggered.append("view")  # type: ignore[method-assign]
        win.export_layer_dialog = lambda: triggered.append("layer")  # type: ignore[method-assign]
        win.act_export_view.trigger()
        win.act_export_layer.trigger()
        assert triggered == ["view", "layer"]

        win.act_retry_task.trigger()
        assert "没有可重试的任务" in win.status_bar.currentMessage()
        win.act_cancel_task.trigger()
        assert "没有可取消的任务" in win.status_bar.currentMessage()

        # 无图层时两条导出通道都必须明确拒绝，不得写出空文件
        empty_png = tmp_path / "empty.png"
        empty_tif = tmp_path / "empty.tif"
        assert win.export_view_image(empty_png) is None
        assert not empty_png.exists()
        assert win.export_layer_raster(empty_tif) is False
        assert not empty_tif.exists()
        assert win._export_task_ids == set()
    finally:
        _close_window(qapp, win)
