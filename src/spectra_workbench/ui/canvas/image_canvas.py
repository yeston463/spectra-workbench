"""基于 pyqtgraph 的高性能影像画布组件（P3-01 / P3-03 / P3-04）。

特性：
1. 交互操作：鼠标滚轮平移与无级缩放、左键拖拽平移。
2. 灰度与 RGB 彩色合成显示。
3. 后台分块/视口窗口动态抽样读取（严格禁止整幅读入大影像，局部放大读取 1:1 原始细节）。
4. 请求代际与图层 ID 校验，丢弃过期响应并防竞态覆盖。
5. 严格遵守 GUI 进程隔离要求，零原生地理库导入。
"""

from __future__ import annotations

import base64
import contextlib
import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from spectra_workbench.scheduler.client import QtWorkerClient
    from spectra_workbench.ui.models.layer import LayerItem


class ImageCanvas(QWidget):
    """栅格影像显示画布。"""

    sig_view_changed = Signal(float, str)  # zoom, mode_info

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_layer: LayerItem | None = None
        self._last_client: QtWorkerClient | None = None
        self._is_loading = False
        self._pending_refresh = False
        self._active_request_id = 0
        self._active_layer_id: str | None = None

        self._throttle_timer = QTimer(self)
        self._throttle_timer.setSingleShot(True)
        self._throttle_timer.setInterval(150)
        self._throttle_timer.timeout.connect(self._on_throttled_refresh)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # 顶部微型状态与快捷操作栏
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(6, 4, 6, 4)

        self.lbl_status = QLabel("就绪 (未加载图层)")
        self.lbl_status.setStyleSheet("color: #666; font-size: 12px;")
        toolbar.addWidget(self.lbl_status)

        toolbar.addStretch()

        self.btn_fit = QPushButton("适应全图")
        self.btn_fit.setToolTip("自适应将图层缩放至画布中心")
        self.btn_fit.clicked.connect(self.fit_extent)
        toolbar.addWidget(self.btn_fit)

        self.btn_zoom_in = QPushButton("+ 放大")
        self.btn_zoom_in.clicked.connect(self.zoom_in)
        toolbar.addWidget(self.btn_zoom_in)

        self.btn_zoom_out = QPushButton("- 缩小")
        self.btn_zoom_out.clicked.connect(self.zoom_out)
        toolbar.addWidget(self.btn_zoom_out)

        self.btn_refresh = QPushButton("刷新视图")
        self.btn_refresh.clicked.connect(self.refresh)
        toolbar.addWidget(self.btn_refresh)

        layout.addLayout(toolbar)

        # pyqtgraph 画布窗口
        pg.setConfigOptions(imageAxisOrder="row-major", antialias=True)
        self.gl_widget = pg.GraphicsLayoutWidget()
        self.plot_item = self.gl_widget.addPlot()
        self.plot_item.invertY(True)  # 遥感影像 Y 轴由上至下
        self.plot_item.setAspectLocked(True)
        self.plot_item.showGrid(x=True, y=True, alpha=0.2)

        self.img_item = pg.ImageItem()
        self.plot_item.addItem(self.img_item)

        # 连接视口变化信号（节流调度）
        self.plot_item.getViewBox().sigRangeChanged.connect(self._on_view_range_changed)

        layout.addWidget(self.gl_widget, stretch=1)

    @property
    def current_layer(self) -> LayerItem | None:
        return self._current_layer

    def _on_view_range_changed(self, *args: Any) -> None:
        if self._current_layer is not None and not self._is_loading:
            self._throttle_timer.start()

    def _on_throttled_refresh(self) -> None:
        self.refresh()

    def set_layer(self, layer: LayerItem | None, client: QtWorkerClient | None = None) -> None:
        """设置当前主显示图层并加载分块预览。"""
        self._throttle_timer.stop()
        self._current_layer = layer
        if client is not None:
            self._last_client = client

        if layer is None:
            self._active_layer_id = None
            self.clear()
            return

        self._active_layer_id = layer.layer_id

        if not layer.visible:
            self.img_item.clear()
            self.lbl_status.setText(f"图层 [{layer.name}] 已设为隐藏")
            return

        # 临时断开 rangeChanged 避免 fit_extent 重复触发
        vb = self.plot_item.getViewBox()
        with contextlib.suppress(RuntimeError, TypeError):
            vb.sigRangeChanged.disconnect(self._on_view_range_changed)

        self.fit_extent()
        self.refresh()

        with contextlib.suppress(RuntimeError, TypeError):
            vb.sigRangeChanged.connect(self._on_view_range_changed)

    def clear(self) -> None:
        """清空画布。"""
        self._throttle_timer.stop()
        self._current_layer = None
        self._active_layer_id = None
        self._pending_refresh = False
        self._is_loading = False
        self._active_request_id += 1
        self.img_item.clear()
        self.lbl_status.setText("就绪 (画布已清空)")

    def closeEvent(self, event: Any) -> None:
        self.clear()
        super().closeEvent(event)

    def fit_extent(self) -> None:
        """自适应全图范围。"""
        if self._current_layer is None:
            return
        w = self._current_layer.width
        h = self._current_layer.height
        if w > 0 and h > 0:
            self.plot_item.setRange(xRange=(0, w), yRange=(0, h), padding=0.0)

    def zoom_in(self) -> None:
        """放大视图。"""
        self._throttle_timer.stop()
        self.plot_item.getViewBox().scaleBy((0.75, 0.75))
        self.refresh()

    def zoom_out(self) -> None:
        """缩小视图。"""
        self._throttle_timer.stop()
        self.plot_item.getViewBox().scaleBy((1.33, 1.33))
        self.refresh()

    def _compute_viewport_window(
        self,
        layer: LayerItem,
        bands: list[int] | None = None,
    ) -> tuple[int, int, int, int, int, int]:
        """依据 ViewBox 当前可见范围与图层尺寸计算像元窗口与采样分辨率。

        返回: (x_off, y_off, win_w, win_h, out_w, out_h)
        """
        vr = self.plot_item.getViewBox().viewRange()
        vx_min, vx_max = min(vr[0][0], vr[0][1]), max(vr[0][0], vr[0][1])
        vy_min, vy_max = min(vr[1][0], vr[1][1]), max(vr[1][0], vr[1][1])

        # 若未初始化有效范围，默认使用全图
        if vx_max <= vx_min or vy_max <= vy_min or vx_max <= 0 or vy_max <= 0:
            x_off, y_off = 0, 0
            win_w, win_h = layer.width, layer.height
        else:
            x0 = max(0, min(layer.width, int(math.floor(vx_min))))
            x1 = max(0, min(layer.width, int(math.ceil(vx_max))))
            y0 = max(0, min(layer.height, int(math.floor(vy_min))))
            y1 = max(0, min(layer.height, int(math.ceil(vy_max))))

            if x1 <= x0 or y1 <= y0:
                # 视口与栅格无交集，退化为全图
                x_off, y_off = 0, 0
                win_w, win_h = layer.width, layer.height
            else:
                x_off = x0
                y_off = y0
                win_w = max(1, x1 - x0)
                win_h = max(1, y1 - y0)

        # 抽样分辨率预算：灰度上限 1024，RGB 采样上限 512
        is_rgb_mode = bands is not None and len(bands) >= 3
        max_budget = 512 if is_rgb_mode else 1024

        if win_w <= max_budget and win_h <= max_budget:
            # 局部放大时提供 1:1 原始细节无损采样
            out_w = win_w
            out_h = win_h
        else:
            # 大范围视口按长边比例缩减，保证内存恒定与传输轻量
            scale = min(max_budget / win_w, max_budget / win_h)
            out_w = max(1, int(round(win_w * scale)))
            out_h = max(1, int(round(win_h * scale)))

        return x_off, y_off, win_w, win_h, out_w, out_h

    def refresh(self) -> None:
        """根据当前图层与视口范围，向后台发起分块/窗口抽样读取。"""
        if self._current_layer is None or self._last_client is None:
            return

        layer = self._current_layer
        client = self._last_client

        if self._is_loading:
            self._pending_refresh = True
            self._active_request_id += 1
            return

        self._is_loading = True
        self._active_request_id += 1
        req_id = self._active_request_id
        req_layer_id = layer.layer_id

        bands = layer.get_selected_bands()
        x_off, y_off, win_w, win_h, target_w, target_h = self._compute_viewport_window(layer, bands)

        self.lbl_status.setText(
            f"正在后台分块读取 [{layer.name}] (窗口 {win_w}x{win_h} 采样 {target_w}x{target_h})..."
        )

        def _on_success(data: dict[str, Any]) -> None:
            self._is_loading = False

            # 代际与图层一致性校验：丢弃过期响应
            if req_id != self._active_request_id:
                if self._pending_refresh:
                    self._pending_refresh = False
                    self.refresh()
                return

            if self._current_layer is None or self._current_layer.layer_id != req_layer_id:
                if self._pending_refresh:
                    self._pending_refresh = False
                    self.refresh()
                return

            if self._pending_refresh:
                self._pending_refresh = False
                self.refresh()

            b64_str = data.get("b64_data", "")
            out_w = data.get("out_w", target_w)
            out_h = data.get("out_h", target_h)
            is_rgb = bool(data.get("is_rgb", False))
            resp_x_off = data.get("x_off", x_off)
            resp_y_off = data.get("y_off", y_off)
            resp_win_w = data.get("win_w", win_w)
            resp_win_h = data.get("win_h", win_h)

            if not b64_str:
                self.lbl_status.setText("读取失败：空数据返回")
                return

            raw_bytes = base64.b64decode(b64_str)
            try:
                if is_rgb:
                    arr = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((out_h, out_w, 3))
                else:
                    arr = np.frombuffer(raw_bytes, dtype=np.uint8).reshape((out_h, out_w))

                # 更新 pyqtgraph ImageItem 并映射到栅格物理坐标
                self.img_item.setImage(arr, autoLevels=False)
                self.img_item.setRect(QRectF(float(resp_x_off), float(resp_y_off), float(resp_win_w), float(resp_win_h)))
            except Exception as e:
                self.lbl_status.setText(f"渲染数据异常: {e}")
                return

            mode_str = "RGB 合成" if is_rgb else f"灰度 (波段 {layer.gray_band})"
            info = (
                f"图层: {layer.name} | 模式: {mode_str} | "
                f"视口窗口: [{resp_x_off}, {resp_y_off}, {resp_win_w}x{resp_win_h}] | 采样: {out_w}x{out_h}"
            )
            self.lbl_status.setText(info)
            self.sig_view_changed.emit(1.0, info)

        def _on_error(err: str) -> None:
            self._is_loading = False
            self._pending_refresh = False
            if req_id != self._active_request_id:
                return

            if self._current_layer is None or self._current_layer.layer_id != req_layer_id:
                return

            self.lbl_status.setText(f"分块读取异常: {err}")

        # 发起异步调用向 Worker 索取窗口数据
        client.read_window_async(
            path=layer.file_path,
            x_off=x_off,
            y_off=y_off,
            win_w=win_w,
            win_h=win_h,
            out_w=target_w,
            out_h=target_h,
            bands=bands,
            on_success=_on_success,
            on_error=_on_error,
        )
