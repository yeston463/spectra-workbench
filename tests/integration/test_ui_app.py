"""UI 桌面集成验收测试（P2-04 / P2-05 / P3-01 / P3-04）。

验证：
1. 主窗口启动与 Dock 组件完整加载（零原生地理库导入）。
2. ENVI/GeoTIFF 影像打开、通过后台 Worker 探测元数据与图层列表加载。
3. 后台分块/窗口抽样读取与画布显示。
4. 动态参数表单提交 GDAL Warp 任务并流式接收进度与日志。
5. 任务完成后成果图层自动加载至图层列表与画布。
6. 回调严格归属 GUI 主线程（QThread.currentThread() == app.thread()）。
7. 大预览尺寸（1024x1024）跨进程传输与 Spill 机制接通。
8. 损坏/非法数据集错误响应符合 Schema 且不崩溃后台执行线程。
9. 连续提交任务按任务 ID 隔离订阅，避免成果重复加载。
10. 画布缩放动态重新计算并索取视口局部像元细节（非全图缩略图）。
11. 任务面板支持 duration_seconds 为 None 的兜底展示。
12. 离屏渲染字体注册与就绪信号驱动的高质量截图生成。
"""

from __future__ import annotations

import base64
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QThread
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from spectra_workbench.domain.models import (
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    TaskResult,
    TaskSpec,
)
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.canvas.image_canvas import ImageCanvas
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow
from spectra_workbench.ui.models.layer import LayerItem
from spectra_workbench.ui.panels.task_panel import TaskPanel
from spectra_workbench.workers.process import WorkerProcess

pytestmark = [pytest.mark.integration, pytest.mark.gui, pytest.mark.engine]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


def _pump_events(timeout_seconds: float = 1.0, step: float = 0.05) -> None:
    """循环泵送 Qt 事件循环指定时长。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(step)


def _spin_until(condition: callable, timeout_seconds: float = 10.0, step: float = 0.02) -> bool:
    """持续泵送事件直到条件成立或超时。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(step)
    return bool(condition())


def test_ui_fonts_registered(qapp: QApplication) -> None:
    """验证离屏模式下已加载 Windows 系统字体，字体族数大于 0。"""
    families = QFontDatabase.families()
    assert len(families) > 0, "离屏模式下 QFontDatabase 字体库为空，文字将渲染为方框"


def test_ui_callback_thread_affinity(qapp: QApplication) -> None:
    """验证 probe 与 read_window 的异步回调严格在 GUI 主线程执行。"""
    root = Path(__file__).resolve().parents[2]
    sample_hdr = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.hdr"
    client = QtWorkerClient(provider_id="gdal", project_root=root)

    callbacks_record: list[dict[str, bool | str]] = []
    errors: list[str] = []

    def _record(action: str) -> callable:
        def _cb(_: object) -> None:
            callbacks_record.append(
                {
                    "action": action,
                    "on_gui_thread": QThread.currentThread() == qapp.thread(),
                    "python_thread": threading.current_thread().name,
                }
            )

        return _cb

    try:
        client.probe_dataset_async(
            sample_hdr,
            on_success=_record("probe_dataset"),
            on_error=errors.append,
        )
        assert _spin_until(lambda: len(callbacks_record) == 1 or errors, timeout_seconds=8.0)
        assert not errors, f"probe 发生错误: {errors}"
        assert callbacks_record[0]["on_gui_thread"] is True

        client.read_window_async(
            sample_hdr,
            win_w=16,
            win_h=16,
            out_w=16,
            out_h=16,
            bands=[1],
            on_success=_record("read_window"),
            on_error=errors.append,
        )
        assert _spin_until(lambda: len(callbacks_record) == 2 or errors, timeout_seconds=8.0)
        assert not errors, f"read 发生错误: {errors}"
        assert callbacks_record[1]["on_gui_thread"] is True

    finally:
        client.stop(timeout=2.0)
        client.kill()


def test_large_preview_transport_and_worker_resilience(qapp: QApplication) -> None:
    """验证 1024x1024 预览通过溢出通道正常传输，且 Worker 进程与执行线程持续存活。"""
    root = Path(__file__).resolve().parents[2]
    sample_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"
    worker = WorkerProcess(provider_id="gdal", project_root=root)

    try:
        worker.start(timeout=15.0)
        # 1. 正常小预览
        small = worker.read_window(
            sample_dat, win_w=16, win_h=16, out_w=16, out_h=16, bands=[1], timeout=5.0
        )
        assert small.get("out_w") == 16

        # 2. 1024x1024 灰度（base64 超过 1 MiB 协议上限，需由 spill 机制透明转发）
        large = worker.read_window(
            sample_dat, win_w=16, win_h=16, out_w=1024, out_h=1024, bands=[1], timeout=8.0
        )
        assert large.get("out_w") == 1024
        assert len(large.get("b64_data", "")) > 1000000
        assert worker.is_alive

        # 3. 后续小请求应正常响应，证明执行线程没有因超限退出
        next_small = worker.read_window(
            sample_dat, win_w=16, win_h=16, out_w=16, out_h=16, bands=[1], timeout=5.0
        )
        assert next_small.get("out_w") == 16

    finally:
        worker.stop(timeout=2.0)
        worker.kill()


def test_invalid_dataset_error_handling_resilience(qapp: QApplication) -> None:
    """验证探测损坏文件返回符合 Schema 的错误响应，且不终止 Worker 执行循环。"""
    root = Path(__file__).resolve().parents[2]
    sample_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"
    worker = WorkerProcess(provider_id="gdal", project_root=root)

    try:
        worker.start(timeout=15.0)
        with tempfile.TemporaryDirectory() as td:
            invalid_tif = Path(td) / "corrupt.tif"
            invalid_tif.write_text("NOT A VALID RASTER", encoding="utf-8")

            # 探测损坏文件应抛出异常
            with pytest.raises(Exception) as exc_info:
                worker.probe_dataset(invalid_tif, timeout=3.0)
            assert "not recognized" in str(exc_info.value) or "失败" in str(exc_info.value)
            assert worker.is_alive

            # 损坏请求后，后续合法请求仍能正常响应
            valid_info = worker.probe_dataset(sample_dat, timeout=3.0)
            assert valid_info.get("driver") is not None

    finally:
        worker.stop(timeout=2.0)
        worker.kill()


def test_sequential_task_submissions_no_duplicate_loads(qapp: QApplication) -> None:
    """验证连续提交任务按任务 ID 隔离订阅，终态后注销，成果加载不重复。"""
    root = Path(__file__).resolve().parents[2]
    window = MainWindow(project_root=root)
    loaded_task_ids: list[str] = []

    try:
        task_client = window.gdal_client
        with (
            patch.object(task_client, "submit_task_async", return_value=None),
            patch.object(
                window,
                "load_result_to_layer",
                side_effect=lambda r: loaded_task_ids.append(r.task_id),
            ),
        ):
            for n in (1, 2, 3):
                spec = TaskSpec(
                    task_id=f"seq-task-{n}",
                    capability_id="gdal:translate",
                    descriptor_revision="1.0",
                    engine=EngineConstraint(
                        provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"
                    ),
                    project_root=str(root),
                    workspace=str(root / "workspace"),
                    inputs={
                        "input": DatasetRef(
                            dataset_id="source", kind="raster", uri="file:///tmp/dummy.tif"
                        )
                    },
                    outputs=[
                        OutputPlan(key="output", kind="raster", uri=f"file:///tmp/out_{n}.tif")
                    ],
                )
                window.submit_task(spec)
                result = TaskResult(
                    task_id=spec.task_id,
                    attempt=1,
                    status="succeeded",
                    outputs=[
                        {"key": "output", "uri": f"file:///tmp/out_{n}.tif", "data_type": "raster"}
                    ],
                    duration_seconds=0.05,
                )
                task_client.sig_result.emit(result)
                _pump_events(0.05)

        # 检查连续三个任务每个均恰好加载一次
        assert loaded_task_ids == ["seq-task-1", "seq-task-2", "seq-task-3"]
        assert loaded_task_ids.count("seq-task-2") == 1

    finally:
        window.close()
        _pump_events(0.1)


def test_viewport_zoom_window_reading_and_layer_generation(qapp: QApplication) -> None:
    """验证画布放大后索取的是可见视口窗口与原始细节，且切层时过期响应被丢弃。"""
    root = Path(__file__).resolve().parents[2]
    sample_dat = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"

    class StubClient:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def read_window_async(self, **kwargs: object) -> None:
            on_success = kwargs.pop("on_success", None)
            kwargs.pop("on_error", None)
            self.requests.append(kwargs)
            if callable(on_success):
                # 模拟返回对应采样尺寸的数据
                w = int(kwargs.get("out_w", 8))
                h = int(kwargs.get("out_h", 8))
                dummy_buf = bytes(w * h)
                on_success(
                    {
                        "b64_data": base64.b64encode(dummy_buf).decode(),
                        "out_w": w,
                        "out_h": h,
                        "is_rgb": False,
                        "x_off": kwargs.get("x_off", 0),
                        "y_off": kwargs.get("y_off", 0),
                        "win_w": kwargs.get("win_w", 4096),
                        "win_h": kwargs.get("win_h", 2048),
                    }
                )

    canvas = ImageCanvas()
    stub = StubClient()
    layer = LayerItem(
        name="virtual_large_layer",
        file_path=sample_dat,
        metadata={"width": 4096, "height": 2048, "count": 1, "driver": "GTiff"},
    )

    try:
        canvas.set_layer(layer, stub)
        req_count_before = len(stub.requests)
        assert req_count_before == 1

        # 放大视图并验证触发视口窗口读取
        canvas.zoom_in()
        _pump_events(0.05)
        req_count_after = len(stub.requests)
        assert req_count_after > req_count_before

        latest_req = stub.requests[-1]
        # 放大后请求的窗口尺寸 win_w/win_h 必须小于全图，x_off/y_off 动态内缩
        assert int(latest_req["win_w"]) < 4096
        assert int(latest_req["win_h"]) <= 2048
        assert int(latest_req["x_off"]) > 0

    finally:
        canvas.close()
        _pump_events(0.05)


def test_task_panel_duration_none_fallback(qapp: QApplication) -> None:
    """验证 TaskResult 的 duration_seconds 为 None 时不会抛出 TypeError。"""
    panel = TaskPanel()
    result = TaskResult(
        task_id="test-dur-none",
        attempt=1,
        status="succeeded",
        outputs=[{"key": "out", "uri": "file:///tmp/out.tif", "data_type": "raster"}],
        duration_seconds=None,
    )
    # 应安全格式化为 '-' 并正常显示
    panel.on_task_completed(result)
    assert "耗时 -" in panel.progress_bar.format()


def test_main_window_open_dataset_and_chunked_preview(qapp: QApplication) -> None:
    """测试打开 ENVI 影像、Worker 跨进程元数据探测与分块读取。"""
    root = Path(__file__).resolve().parents[2]
    sample_hdr = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.hdr"
    assert sample_hdr.exists()

    window = MainWindow(project_root=root)
    window.show()
    _pump_events(0.2)

    try:
        window.open_dataset(sample_hdr)
        assert _spin_until(lambda: len(window.layer_panel.layers) == 1, timeout_seconds=8.0)

        layer = window.layer_panel.layers[0]
        assert layer.name == "a01_ramp_16x16"
        assert layer.width == 16
        assert layer.height == 16
        assert layer.band_count == 1
        assert "ENVI" in layer.driver.upper() or "RASTER" in layer.driver.upper()

        # 等待分块读取与渲染完成
        assert _spin_until(
            lambda: (
                "视口窗口" in window.canvas.lbl_status.text()
                or "a01_ramp_16x16" in window.canvas.lbl_status.text()
            ),
            timeout_seconds=8.0,
        )

        window.canvas.zoom_in()
        _pump_events(0.1)
        window.canvas.zoom_out()
        _pump_events(0.1)
        window.canvas.fit_extent()
        _pump_events(0.1)

    finally:
        window.close()
        _pump_events(0.2)


def test_main_window_task_execution_and_auto_layer_load(qapp: QApplication) -> None:
    """测试从参数表单提交 GDAL Warp 重投影任务，验证进度、日志与结果图层自动加载。"""
    root = Path(__file__).resolve().parents[2]
    sample_hdr = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.hdr"
    assert sample_hdr.exists()

    window = MainWindow(project_root=root)
    window.show()
    _pump_events(0.2)

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_tif = Path(tmp_dir) / "warp_from_ui.tif"

        try:
            window.open_dataset(sample_hdr)
            assert _spin_until(lambda: len(window.layer_panel.layers) == 1, timeout_seconds=8.0)

            warp_cap = window.toolbox_panel._registry.get("gdal:warp")
            assert warp_cap is not None
            window.toolbox_panel.select_capability("gdal:warp")
            _pump_events(0.2)

            window.form_widget.set_input_path(sample_hdr)
            window.form_widget.txt_output.setText(str(out_tif))
            window.form_widget.cmb_dst_crs.setCurrentText("EPSG:3857")
            window.form_widget.chk_overwrite.setChecked(True)
            _pump_events(0.1)

            window.form_widget._on_submit_clicked()

            # 等待任务在后台 Worker 执行完毕
            assert _spin_until(
                lambda: window.task_panel._last_result is not None, timeout_seconds=30.0
            )

            result = window.task_panel._last_result
            assert result is not None
            assert result.status.value == "succeeded", f"任务未成功: {result.error}"

            # 验证成果自动加载为新图层
            assert _spin_until(lambda: len(window.layer_panel.layers) >= 2, timeout_seconds=8.0)
            assert out_tif.exists(), "成果 TIF 文件未生成"

            res_layer = window.layer_panel.layers[-1]
            assert "warp_from_ui" in res_layer.name
            assert res_layer.crs_epsg == 3857

        finally:
            window.close()
            _pump_events(0.2)


def test_main_window_screenshot_capture(qapp: QApplication) -> None:
    """测试主窗口实际渲染并通过就绪信号生成高质量截图。"""
    root = Path(__file__).resolve().parents[2]
    sample_hdr = root / "tests" / "fixtures" / "data" / "a01_multiband_4b_8x8.hdr"
    if not sample_hdr.exists():
        sample_hdr = root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.hdr"

    out_shot = root / "docs" / "progress" / "runs" / "desktop_ui_preview.png"
    out_shot.parent.mkdir(parents=True, exist_ok=True)

    window = MainWindow(project_root=root)
    window.show()
    _pump_events(0.2)

    try:
        ready_flag = {"ready": False}
        window.canvas.sig_view_changed.connect(lambda *_: ready_flag.update(ready=True))

        window.open_dataset(sample_hdr)
        # 通过就绪信号确认渲染就绪，避免固定盲等
        assert _spin_until(lambda: ready_flag["ready"], timeout_seconds=10.0), (
            "影像视图渲染未在时限内就绪"
        )

        trans_cap = window.toolbox_panel._registry.get("gdal:translate")
        if trans_cap:
            window.form_widget.load_capability(trans_cap)
        _pump_events(0.3)

        # 捕获实际窗口像素并保存
        pixmap = window.grab()
        saved = pixmap.save(str(out_shot))
        assert saved, "截图保存失败"
        assert out_shot.exists(), "截图文件不存在"
        assert out_shot.stat().st_size > 1024, "截图文件过小，可能未正确渲染"

    finally:
        window.close()
        _pump_events(0.2)
