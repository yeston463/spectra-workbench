"""影像画布（P3-03 / P3-04 / P3-05）。

1. **只读可见范围**：窗口按 512 像元对齐，平移时复用已缓存块。
2. **原值进、显示值出**：worker 返回原始 dtype 与有效性位图；拉伸/伪彩/透明度在 GUI 侧完成，
   不回写源数据。无效像元（NoData / mask=0 / alpha=0）走 alpha 通道，合法 0 值照常显示。
3. **缓存有字节预算**：TileCache 按字节 LRU；键含源、波段、窗口、输出尺寸与概览层。
4. **代际防过期**：视图/图层/波段/显示参数变化递增代际，迟到响应丢弃，最终显示对应最新视图。
5. **跨 CRS**：设置视图 CRS 后请求重投影到统一视图网格的窗口，不在 GUI 拼像素位置。
6. 坐标与投影数学全部在 worker 内完成，本模块零原生地理依赖。
"""

from __future__ import annotations

import contextlib
import json
import math
import time
from typing import TYPE_CHECKING, Any

import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from spectra_workbench.domain.roi import ROI, ROIShape
from spectra_workbench.rendering.cache import TileCache, TileKey, ViewGeneration
from spectra_workbench.rendering.display import (
    DisplayMode,
    DisplaySettings,
    compute_stretch_bounds,
    decode_window,
    to_rgba,
)

if TYPE_CHECKING:
    from spectra_workbench.scheduler.client import QtWorkerClient
    from spectra_workbench.ui.models.layer import LayerItem

TILE_ALIGN = 512
GRAY_BUDGET = 1024
RGB_BUDGET = 512


class SwipeImageItem(pg.ImageItem):
    """带卷帘区域裁剪的图像项（用于 Swipe 模式下的顶层对比图层展示，P11-01）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.swipe_enabled: bool = False
        self.split_coord: float = 0.0
        self.swipe_orientation: str = "vertical"  # "vertical"（左右卷帘）或 "horizontal"（上下卷帘）

    def set_swipe(self, enabled: bool, split_coord: float, orientation: str = "vertical") -> None:
        self.swipe_enabled = enabled
        self.split_coord = split_coord
        self.swipe_orientation = orientation
        self.update()

    def paint(self, p: QPainter, opt: Any, widget: Any = None) -> None:
        if not self.swipe_enabled:
            super().paint(p, opt, widget)
            return
        p.save()
        br = self.boundingRect()
        if self.swipe_orientation == "vertical":
            clip_w = max(0.0, min(br.width(), self.split_coord - br.x()))
            clip_rect = QRectF(br.x(), br.y(), clip_w, br.height())
        else:
            clip_h = max(0.0, min(br.height(), self.split_coord - br.y()))
            clip_rect = QRectF(br.x(), br.y(), br.width(), clip_h)
        p.setClipRect(clip_rect, Qt.ClipOperation.IntersectClip)
        super().paint(p, opt, widget)
        p.restore()


class ImageCanvas(QWidget):
    """栅格影像显示画布。"""

    sig_view_changed = Signal(object)  # dict：view_id / 中心 / 比例
    sig_pixel_hover = Signal(int, int, str)  # col, row, layer_id
    sig_pixel_clicked = Signal(int, int, str)  # col, row, layer_id (P11-01 光谱剖面点击)
    sig_roi_changed = Signal()
    sig_clip_roi_requested = Signal(object, object)
    sig_status = Signal(str)
    sig_measure_status = Signal(str)  # P11-01 测距状态

    def __init__(
        self,
        view_id: str = "V1",
        cache: TileCache | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.view_id = view_id
        self._layer: LayerItem | None = None
        self._client: QtWorkerClient | None = None
        self._cache = cache or TileCache()
        self._gen = ViewGeneration()
        self._bounds: list[tuple[float, float]] | None = None
        self._bounds_gen: int = -1
        self._stats_label = "窗口抽样(当前视图)"

        self.view_crs_wkt: str | None = None
        self._view_bounds: tuple[float, float, float, float] | None = None
        self._sync_guard = False

        self._roi_states: dict[str, dict[str, Any]] = {}
        self._selected_roi_id: str | None = None
        self._draw_shape: ROIShape | None = None
        self._poly_pts: list[tuple[float, float]] = []
        self._poly_last_ts: float | None = None
        self._poly_last_scene_pos: Any = None
        self._rubber: Any = None

        self._throttle = QTimer(self)
        self._throttle.setSingleShot(True)
        self._throttle.setInterval(120)
        self._throttle.timeout.connect(self.refresh)

        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(60)
        self._hover_timer.timeout.connect(self._emit_hover)
        self._hover_pos: tuple[float, float] | None = None

        # P11-01 Swipe 卷帘状态
        self._swipe_enabled: bool = False
        self._swipe_layer: LayerItem | None = None
        self._swipe_orientation: str = "vertical"
        self._available_layers_provider: Any = None

        # P11-01 折线测距状态
        self._measure_mode: bool = False
        self._measure_points: list[tuple[float, float]] = []
        self._measure_line: pg.PlotDataItem | None = None
        self._measure_rubber: pg.PlotDataItem | None = None
        self._measure_labels: list[pg.TextItem] = []

        self._init_ui()

    # ------------------------------------------------------------------ 界面
    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 4)
        self.lbl_status = QLabel("就绪 (未加载图层)")
        self.lbl_status.setStyleSheet("color: #666; font-size: 12px;")
        bar.addWidget(self.lbl_status)
        bar.addStretch()

        self.btn_fit = QPushButton("适应全图")
        self.btn_fit.clicked.connect(self.fit_extent)
        bar.addWidget(self.btn_fit)

        self.btn_swipe = QPushButton("卷帘对比")
        self.btn_swipe.setCheckable(True)
        self.btn_swipe.setToolTip("开启两图层卷帘对比 (Swipe)")
        self.btn_swipe.toggled.connect(self._on_swipe_toggled)
        bar.addWidget(self.btn_swipe)

        self.btn_measure = QPushButton("折线测距")
        self.btn_measure.setCheckable(True)
        self.btn_measure.setToolTip("开启地图折线多段测距 (Measure Distance)")
        self.btn_measure.toggled.connect(self.set_measure_mode)
        bar.addWidget(self.btn_measure)

        self.btn_rect = QPushButton("画矩形ROI")
        self.btn_rect.setCheckable(True)
        self.btn_rect.toggled.connect(lambda on: self.set_roi_draw(ROIShape.rectangle, on))
        bar.addWidget(self.btn_rect)

        self.btn_poly = QPushButton("画多边形ROI")
        self.btn_poly.setCheckable(True)
        self.btn_poly.toggled.connect(lambda on: self.set_roi_draw(ROIShape.polygon, on))
        bar.addWidget(self.btn_poly)

        self.btn_clip_roi = QPushButton("✂️ 按ROI裁剪")
        self.btn_clip_roi.setToolTip("使用当前绘制或选中的 ROI 裁剪活动栅格图层（多边形外部赋 NoData）")
        self.btn_clip_roi.clicked.connect(self._on_clip_roi_clicked)
        bar.addWidget(self.btn_clip_roi)

        self.btn_refresh = QPushButton("刷新视图")
        self.btn_refresh.clicked.connect(self.refresh)
        bar.addWidget(self.btn_refresh)
        layout.addLayout(bar)

        # ---------------- P11-01 卷帘控制子工具栏
        self.swipe_bar = QWidget()
        bar_swipe_layout = QHBoxLayout(self.swipe_bar)
        bar_swipe_layout.setContentsMargins(6, 2, 6, 2)
        bar_swipe_layout.setSpacing(6)
        bar_swipe_layout.addWidget(QLabel("对比图层:"))
        self.cmb_swipe_layer = QComboBox()
        self.cmb_swipe_layer.currentIndexChanged.connect(self._on_swipe_layer_selected)
        bar_swipe_layout.addWidget(self.cmb_swipe_layer)
        bar_swipe_layout.addWidget(QLabel("方向:"))
        self.cmb_swipe_orient = QComboBox()
        self.cmb_swipe_orient.addItem("左右卷帘 (垂直线)", "vertical")
        self.cmb_swipe_orient.addItem("上下卷帘 (水平线)", "horizontal")
        self.cmb_swipe_orient.currentIndexChanged.connect(self._on_swipe_orient_changed)
        bar_swipe_layout.addWidget(self.cmb_swipe_orient)
        bar_swipe_layout.addWidget(QLabel("位置:"))
        self.slider_swipe = QSlider(Qt.Orientation.Horizontal)
        self.slider_swipe.setRange(0, 100)
        self.slider_swipe.setValue(50)
        self.slider_swipe.valueChanged.connect(self._on_swipe_slider_changed)
        bar_swipe_layout.addWidget(self.slider_swipe, stretch=1)
        self.btn_exit_swipe = QPushButton("退出卷帘")
        self.btn_exit_swipe.clicked.connect(lambda: self.btn_swipe.setChecked(False))
        bar_swipe_layout.addWidget(self.btn_exit_swipe)
        self.swipe_bar.setVisible(False)
        layout.addWidget(self.swipe_bar)

        # ---------------- P11-01 测距控制子工具栏
        self.measure_bar = QWidget()
        bar_meas_layout = QHBoxLayout(self.measure_bar)
        bar_meas_layout.setContentsMargins(6, 2, 6, 2)
        bar_meas_layout.setSpacing(6)
        self.lbl_measure_status = QLabel("测距模式：鼠标左键点击拾取起点，连续点击添加顶点，右键/双击完成")
        self.lbl_measure_status.setStyleSheet("color: #d35400; font-weight: bold;")
        bar_meas_layout.addWidget(self.lbl_measure_status, stretch=1)
        self.btn_clear_measure = QPushButton("清除测距")
        self.btn_clear_measure.clicked.connect(self.clear_measurement)
        bar_meas_layout.addWidget(self.btn_clear_measure)
        self.btn_exit_measure = QPushButton("退出测距")
        self.btn_exit_measure.clicked.connect(lambda: self.btn_measure.setChecked(False))
        bar_meas_layout.addWidget(self.btn_exit_measure)
        self.measure_bar.setVisible(False)
        layout.addWidget(self.measure_bar)

        pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
        self.gl_widget = pg.GraphicsLayoutWidget()
        self.plot_item = self.gl_widget.addPlot()
        self.plot_item.invertY(True)
        self.plot_item.setAspectLocked(True)
        self.plot_item.showGrid(x=True, y=True, alpha=0.2)

        self.img_item = pg.ImageItem()
        self.plot_item.addItem(self.img_item)

        # P11-01 卷帘顶层图层与分割线
        self.img_swipe_item = SwipeImageItem()
        self.img_swipe_item.setZValue(2)
        self.img_swipe_item.setVisible(False)
        self.plot_item.addItem(self.img_swipe_item)

        self.swipe_line = pg.InfiniteLine(
            angle=90,
            movable=True,
            pen=pg.mkPen(color="#00e5ff", width=2, style=Qt.PenStyle.DashLine),
            label="◀ 卷帘对比 ▶",
            labelOpts={"position": 0.85, "color": "#00e5ff", "fill": "#1a1a1a"},
        )
        self.swipe_line.setZValue(15)
        self.swipe_line.setVisible(False)
        self.plot_item.addItem(self.swipe_line)
        self.swipe_line.sigPositionChanged.connect(self._on_swipe_line_changed)

        # P11-01 测距折线与橡皮筋
        self._measure_line = pg.PlotDataItem(
            pen=pg.mkPen(color="#ff9900", width=2.0),
            symbol="o",
            symbolBrush="#ffcc00",
            symbolSize=8,
        )
        self._measure_line.setZValue(25)
        self.plot_item.addItem(self._measure_line)

        self._measure_rubber = pg.PlotDataItem(
            pen=pg.mkPen(color="#ff9900", width=1.5, style=Qt.PenStyle.DashLine),
        )
        self._measure_rubber.setZValue(24)
        self.plot_item.addItem(self._measure_rubber)

        # ROI 直接挂在 plot_item 上：pyqtgraph 的 ItemGroup 只能 addItem，无法逐个摘除
        self.roi_z = 10
        self.rubber_z = 20

        vb = self.plot_item.getViewBox()
        vb.sigRangeChanged.connect(self._on_range_changed)
        vb.scene().sigMouseMoved.connect(self._on_scene_mouse_moved)
        vb.scene().sigMouseClicked.connect(self._on_scene_mouse_clicked)
        layout.addWidget(self.gl_widget, stretch=1)

    # ------------------------------------------------------------------ 属性
    @property
    def current_layer(self) -> LayerItem | None:
        return self._layer

    @property
    def cache(self) -> TileCache:
        return self._cache

    @property
    def generation(self) -> int:
        return self._gen.generation

    @property
    def stretch_bounds(self) -> list[tuple[float, float]] | None:
        return self._bounds

    @property
    def stats_source_label(self) -> str:
        return self._stats_label

    def set_cache(self, cache: TileCache) -> None:
        """双视图共享同一缓存，避免同一窗口重复读取。"""
        self._cache = cache

    # ------------------------------------------------------------------ 图层
    @staticmethod
    def is_raster_renderable(layer: LayerItem | None) -> bool:
        """该图层是否可按栅格渲染。

        矢量图层（kind == "vector" 或 metadata 明确 renderable=False）当前只支持
        元数据读取，不得进入栅格读取流程，也不得保留上一幅影像的画面。
        """
        if layer is None:
            return False
        meta = getattr(layer, "metadata", None) or {}
        kind = str(meta.get("kind") or "raster")
        if kind == "vector":
            return False
        # 显式声明不可渲染的图层（矢量等）一律不走栅格流程
        renderable = meta.get("renderable")
        return renderable is not False

    def set_layer(self, layer: LayerItem | None, client: QtWorkerClient | None = None) -> None:
        if client is not None:
            self._client = client
        same = bool(
            layer is not None
            and self._layer is not None
            and layer.layer_id == self._layer.layer_id
        )
        self._gen.bump(layer.layer_id if layer else None)
        self._bounds_gen = -1
        self._bounds = None
        if layer is None:
            self._layer = None
            self.clear()
            return
        if not self.is_raster_renderable(layer):
            # 矢量图层：只支持元数据读取。
            # 关键：不得把矢量登记为渲染图层（current_layer 必须保持为空），
            # 否则下游会以为有栅格可读，进而派发栅格窗口请求；
            # 同时清除旧影像画面，避免残留上一幅影像。
            self._layer = None
            self.img_item.clear()
            self._bounds = None
            self.load_rois([])
            self._status("矢量图层仅支持元数据读取，暂不渲染")
            return
        self._layer = layer
        if not same:
            self.load_rois(list(getattr(layer, "rois", []) or []))
        if not layer.visible:
            self.img_item.clear()
            self._status("图层已设为隐藏")
            return
        if not same:
            vb = self.plot_item.getViewBox()
            with contextlib.suppress(RuntimeError, TypeError):
                vb.sigRangeChanged.disconnect(self._on_range_changed)
            self.fit_extent()
            with contextlib.suppress(RuntimeError, TypeError):
                vb.sigRangeChanged.connect(self._on_range_changed)
        self.refresh()

    def clear(self) -> None:
        self._throttle.stop()
        self._layer = None
        self._gen.bump(None)
        self._bounds = None
        self._bounds_gen = -1
        self.img_item.clear()
        self.disable_swipe()
        self.clear_measurement()
        self._clear_rois()
        self._status("就绪 (画布已清空)")

    def closeEvent(self, event: Any) -> None:
        self.clear()
        super().closeEvent(event)

    # ------------------------------------------------------------------ 视图
    def fit_extent(self) -> None:
        layer = self._layer
        if layer is None:
            return
        if self.view_crs_wkt and self._view_bounds:
            x0, y0, x1, y1 = self._view_bounds
            self.plot_item.setRange(xRange=(x0, x1), yRange=(y1, y0), padding=0.0)
        else:
            self.plot_item.setRange(
                xRange=(0, max(1, layer.width)), yRange=(max(1, layer.height), 0), padding=0.0
            )

    def zoom_in(self) -> None:
        """显式缩放：立即按新视口重算窗口，不等节流定时器。"""
        self._throttle.stop()
        self.plot_item.getViewBox().scaleBy((0.75, 0.75))
        self.refresh()

    def zoom_out(self) -> None:
        self._throttle.stop()
        self.plot_item.getViewBox().scaleBy((1.33, 1.33))
        self.refresh()

    def zoom_to_100(self) -> None:
        """缩放至 1:1 实际像元分辨率（1 屏幕像素 = 1 影像像元）。"""
        layer = self._layer
        if layer is None:
            return
        self._throttle.stop()
        vb = self.plot_item.getViewBox()
        vr = vb.viewRange()
        cx = (vr[0][0] + vr[0][1]) / 2.0
        cy = (vr[1][0] + vr[1][1]) / 2.0
        sg = vb.screenGeometry()
        w = float(sg.width()) if sg.isValid() and sg.width() > 0 else float(self.width())
        h = float(sg.height()) if sg.isValid() and sg.height() > 0 else float(self.height())
        half_w = max(5.0, w / 2.0)
        half_h = max(5.0, h / 2.0)
        vb.setRange(xRange=(cx - half_w, cx + half_w), yRange=(cy + half_h, cy - half_h), padding=0.0)
        self.refresh()

    def _on_range_changed(self, *args: Any) -> None:
        if self._sync_guard:
            return
        if self._layer is not None:
            self._throttle.start()
            self.sig_view_changed.emit(self.view_state())

    def view_state(self) -> dict[str, Any]:
        vr = self.plot_item.getViewBox().viewRange()
        span_x = abs(vr[0][1] - vr[0][0])
        base = float(self._layer.width) if self._layer is not None else 0.0
        return {
            "view_id": self.view_id,
            "center_col": (vr[0][0] + vr[0][1]) / 2.0,
            "center_row": (vr[1][0] + vr[1][1]) / 2.0,
            "scale": (span_x / base) if base else 1.0,
            "view_crs_wkt": self.view_crs_wkt,
        }

    def capture_view_image(self, scale_factor: float | None = None) -> QImage:
        """把当前绘图区的真实渲染结果（影像 + 网格 + ROI）导出为 QImage。

        用 ``QGraphicsScene.render`` 而不是 ``QWidget.grab``：后者依赖原生窗口句柄与
        可见状态，在离屏或未显示时可能得到空白图；场景渲染不依赖这些，且可超采样。
        产物是**屏幕渲染截图**，不含地理参考；需要带坐标的数据请走栅格导出通道。

        第 37 轮：渲染倍率不再固定 2.0，而是取 ``max(2.0, devicePixelRatioF())``
        （显式传 ``scale_factor`` 时同样不低于 dpr）。高 DPI 屏幕上若仍按 2.0
        出图，2x/3x 缩放下的截图会相对屏幕变糊；同时给 QImage 写入 dpr 元数据，
        让查看器按正确的逻辑尺寸显示。
        """
        scene = self.plot_item.scene()
        if scene is None:
            raise RuntimeError("画布场景尚未初始化，无法导出视图")
        rect = QRectF(self.plot_item.getViewBox().sceneBoundingRect())
        if rect.width() < 2 or rect.height() < 2:
            size = self.gl_widget.size()
            rect = QRectF(0, 0, max(1, size.width()), max(1, size.height()))
        dpr = max(1.0, float(self.devicePixelRatioF()))
        base = 2.0 if scale_factor is None else max(1.0, float(scale_factor))
        factor = max(base, dpr)
        width = max(1, int(round(rect.width() * factor)))
        height = max(1, int(round(rect.height() * factor)))
        image = QImage(width, height, QImage.Format.Format_ARGB32)
        image.setDevicePixelRatio(factor)
        image.fill(QColor("#ffffff"))
        painter = QPainter(image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            scene.render(painter, QRectF(0, 0, width, height), rect)
        finally:
            painter.end()
        return image

    def apply_view_state(self, state: dict[str, Any]) -> None:
        """联动：套用另一视图的中心与比例；_sync_guard 防止信号回环。"""
        try:
            cx = float(state["center_col"])
            cy = float(state["center_row"])
            ratio = max(float(state.get("scale", 1.0)), 1e-9)
        except (KeyError, TypeError, ValueError):
            return
        base = float(self._layer.width) if self._layer is not None else 100.0
        half = (base * ratio) / 2.0
        self._sync_guard = True
        try:
            self.plot_item.getViewBox().setRange(
                xRange=(cx - half, cx + half), yRange=(cy + half, cy - half), padding=0.0
            )
        finally:
            self._sync_guard = False
        self.refresh()

    @staticmethod
    def _roi_points_for_transform(roi: ROI) -> list[tuple[float, float]]:
        """将 ROI 权威几何展开为供坐标转换的顶点序列。

        矩形权威模型在自身空间仅存对角两点，但在旋转仿射、倾斜或投影形变下，
        其在其它坐标空间不再是轴对齐矩形。必须将权威对角两点展开为全部 4 个角点：
        (min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y)
        经 Worker 转换后渲染实际几何轮廓，以严格保持真实覆盖面积与角点位置，
        绝不能仅转换两个对角点后再用包围盒重建轴对齐矩形。
        """
        if roi.shape is ROIShape.rectangle and len(roi.points) == 2:
            (x0, y0), (x1, y1) = roi.points
            min_x, max_x = min(x0, x1), max(x0, x1)
            min_y, max_y = min(y0, y1), max(y0, y1)
            return [
                (min_x, min_y),
                (max_x, min_y),
                (max_x, max_y),
                (min_x, max_y),
            ]
        return [(float(c), float(r)) for (c, r) in roi.points]

    def set_view_crs(self, wkt: str | None, on_ready: Any = None) -> None:
        """切换显示 CRS。范围与 ROI 坐标换算交给 worker，带代际保护。"""
        layer = self._layer
        self.view_crs_wkt = wkt
        self._view_bounds = None
        self._gen.bump(layer.layer_id if layer else None)
        self._bounds_gen = -1
        current_gen = self._gen.generation
        current_layer_id = layer.layer_id if layer else None

        if not wkt or layer is None or self._client is None:
            self._sync_rois_to_view(current_gen, current_layer_id, "pixel", None)
            self.fit_extent()
            self.refresh()
            if on_ready:
                on_ready()
            return

        target_view_crs = wkt
        bounds_data: list[float] | None = None
        roi_results: dict[str, list[tuple[float, float]] | None] = {}
        pending_roi_ids = set(self._roi_states.keys())
        bounds_ready = False

        def _try_finish() -> None:
            if not bounds_ready or len(roi_results) < len(pending_roi_ids):
                return
            if self._gen.generation != current_gen:
                return
            if self._layer is None or self._layer.layer_id != current_layer_id:
                return
            if bounds_data and len(bounds_data) == 4:
                self._view_bounds = (
                    float(bounds_data[0]),
                    float(bounds_data[1]),
                    float(bounds_data[2]),
                    float(bounds_data[3]),
                )
            for rid, pts in roi_results.items():
                st = self._roi_states.get(rid)
                if st is None:
                    continue
                old_item = st["item"]
                if pts is not None and len(pts) > 0:
                    with contextlib.suppress(Exception):
                        self.plot_item.removeItem(old_item)
                    new_item = self._create_roi_item(st["roi"], pts, selected=(rid == self._selected_roi_id))
                    new_item.setVisible(True)
                    st["item"] = new_item
                    st["display_points"] = pts
                    st["display_space"] = "map"
                    st["display_crs"] = target_view_crs
                    st["gen"] = current_gen
                    st["transform_error"] = False
                    self.plot_item.addItem(new_item)
                    new_item.sigRegionChanged.connect(lambda *a, s=st: self._on_roi_moved(s))
                    new_item.sigClicked.connect(lambda *a, r_id=rid: self.select_roi(r_id))
                else:
                    # 转换失败：绝不使用原像元坐标冒充地图坐标！
                    # 隐藏失败的图形项，清空 display_points，保留权威模型不变
                    old_item.setVisible(False)
                    st["display_points"] = None
                    st["gen"] = current_gen
                    st["transform_error"] = True

            self.fit_extent()
            self.refresh()
            # 成功与失败都要通知 UI：失败的 ROI 必须在列表中标出转换失败状态
            self.sig_roi_changed.emit()
            if on_ready:
                on_ready()

        def _on_bounds_ok(data: dict[str, Any]) -> None:
            nonlocal bounds_ready, bounds_data
            if self._gen.generation != current_gen:
                return
            b = data.get("bounds")
            if b and len(b) == 4:
                bounds_data = [float(b[0]), float(b[1]), float(b[2]), float(b[3])]
            bounds_ready = True
            _try_finish()

        def _on_bounds_err(msg: str) -> None:
            nonlocal bounds_ready
            if self._gen.generation != current_gen:
                return
            self._status(f"视图 CRS 范围换算失败：{msg}")
            bounds_ready = True
            _try_finish()

        self._client.transform_bounds_async(layer.file_path, target_view_crs, on_success=_on_bounds_ok, on_error=_on_bounds_err)

        if not pending_roi_ids:
            return

        for rid in list(pending_roi_ids):
            state = self._roi_states[rid]
            roi: ROI = state["roi"]
            if roi.coordinate_space == "map" and roi.crs_wkt == target_view_crs:
                roi_results[rid] = [(float(c), float(r)) for (c, r) in roi.points]
                _try_finish()
            else:
                def _make_ok(r_id: str):
                    def _ok_roi(res: dict[str, Any]) -> None:
                        # 过期响应（代际已变）必须直接丢弃，不得写入结果字典，
                        # 否则脏数据会被同代际的 _try_finish 读到并污染图形与权威几何
                        if self._gen.generation != current_gen:
                            return
                        pts = [(float(p[0]), float(p[1])) for p in res.get("points", [])]
                        roi_results[r_id] = pts if pts else None
                        _try_finish()
                    return _ok_roi

                def _make_err(r_id: str):
                    def _err_roi(msg: str) -> None:
                        if self._gen.generation != current_gen:
                            return
                        self._status(f"ROI [{r_id}] 转换到视图 CRS 失败：{msg}")
                        roi_results[r_id] = None
                        _try_finish()
                    return _err_roi

                self._client.transform_coordinates_async(
                    path=layer.file_path,
                    points=self._roi_points_for_transform(roi),
                    from_space=roi.coordinate_space,
                    to_space="map",
                    src_crs_wkt=roi.crs_wkt,
                    dst_crs_wkt=target_view_crs,
                    convention=roi.pixel_convention,
                    on_success=_make_ok(rid),
                    on_error=_make_err(rid),
                )

    def _sync_rois_to_view(
        self, current_gen: int, current_layer_id: str | None, target_space: str, target_crs: str | None
    ) -> None:
        layer = self._layer
        for rid, state in list(self._roi_states.items()):
            roi: ROI = state["roi"]
            pts: list[tuple[float, float]] | None = None
            if roi.coordinate_space == target_space and (target_space == "pixel" or roi.crs_wkt == target_crs):
                pts = [(float(c), float(r)) for (c, r) in roi.points]
            elif self._client is not None and layer is not None:
                try:
                    res = self._client.transform_coordinates(
                        path=layer.file_path,
                        points=self._roi_points_for_transform(roi),
                        from_space=roi.coordinate_space,
                        to_space=target_space,
                        src_crs_wkt=roi.crs_wkt,
                        dst_crs_wkt=target_crs,
                        convention=roi.pixel_convention,
                    )
                    pts = [(float(p[0]), float(p[1])) for p in res.get("points", [])]
                    if not pts:
                        pts = None
                except Exception as exc:
                    self._status(f"ROI 转换失败：{exc}")
                    pts = None
            else:
                pts = None

            old_item = state["item"]
            if pts is not None and len(pts) > 0:
                with contextlib.suppress(Exception):
                    self.plot_item.removeItem(old_item)
                new_item = self._create_roi_item(roi, pts, selected=(rid == self._selected_roi_id))
                new_item.setVisible(True)
                state["item"] = new_item
                state["display_points"] = pts
                state["display_space"] = target_space
                state["display_crs"] = target_crs
                state["gen"] = current_gen
                state["transform_error"] = False
                self.plot_item.addItem(new_item)
                new_item.sigRegionChanged.connect(lambda *a, s=state: self._on_roi_moved(s))
                new_item.sigClicked.connect(lambda *a, r_id=rid: self.select_roi(r_id))
            else:
                old_item.setVisible(False)
                state["display_points"] = None
                state["gen"] = current_gen
                state["transform_error"] = True

        # 通知 UI 刷新 ROI 列表与转换失败标记
        self.sig_roi_changed.emit()

    def set_display(self, settings: DisplaySettings) -> None:
        """显示参数变化：递增代际丢弃旧结果，但不清缓存（缓存的是原值，不需重读盘）。"""
        if self._layer is not None:
            self._layer.display = settings
        self._gen.bump(self._layer.layer_id if self._layer else None)
        self._bounds_gen = -1
        self._bounds = None
        self.refresh()

    # ------------------------------------------------------------------ P11-01 Swipe 卷帘
    def set_available_layers_provider(self, provider: Any) -> None:
        """设置图层提供者回调，用于填充卷帘可选图层下拉框。"""
        self._available_layers_provider = provider

    def _on_swipe_toggled(self, on: bool) -> None:
        if on:
            self.enable_swipe()
        else:
            self.disable_swipe()

    def enable_swipe(
        self,
        swipe_layer: LayerItem | None = None,
        orientation: str = "vertical",
    ) -> None:
        """开启两图层卷帘对比模式。"""
        self._swipe_enabled = True
        self._swipe_orientation = orientation
        self.swipe_bar.setVisible(True)
        self.btn_swipe.blockSignals(True)
        self.btn_swipe.setChecked(True)
        self.btn_swipe.blockSignals(False)

        # 填充对比图层候选列表
        self.cmb_swipe_layer.blockSignals(True)
        self.cmb_swipe_layer.clear()
        layers: list[LayerItem] = []
        if callable(self._available_layers_provider):
            layers = self._available_layers_provider() or []
        for ly in layers:
            if self._layer is not None and ly.layer_id == self._layer.layer_id:
                continue  # 跳过当前底图
            if self.is_raster_renderable(ly):
                self.cmb_swipe_layer.addItem(ly.name, ly)

        if swipe_layer is not None:
            for idx in range(self.cmb_swipe_layer.count()):
                if self.cmb_swipe_layer.itemData(idx).layer_id == swipe_layer.layer_id:
                    self.cmb_swipe_layer.setCurrentIndex(idx)
                    break
            self._swipe_layer = swipe_layer
        elif self.cmb_swipe_layer.count() > 0:
            self._swipe_layer = self.cmb_swipe_layer.itemData(0)
        else:
            self._swipe_layer = None
        self.cmb_swipe_layer.blockSignals(False)

        # 初始化分割线
        vr = self.plot_item.getViewBox().viewRange()
        if orientation == "vertical":
            self.swipe_line.setAngle(90)
            split_pos = (vr[0][0] + vr[0][1]) * 0.5
        else:
            self.swipe_line.setAngle(0)
            split_pos = (vr[1][0] + vr[1][1]) * 0.5

        self.swipe_line.setValue(split_pos)
        self.swipe_line.setVisible(True)
        self.slider_swipe.setValue(50)

        self._refresh_swipe_layer()
        self._status("已开启卷帘对比：可拖拽蓝色分割线或滑动条实时对比两图层")

    def disable_swipe(self) -> None:
        """关闭卷帘对比模式。"""
        self._swipe_enabled = False
        self._swipe_layer = None
        self.img_swipe_item.setVisible(False)
        self.img_swipe_item.swipe_enabled = False
        self.swipe_line.setVisible(False)
        self.swipe_bar.setVisible(False)
        self.btn_swipe.blockSignals(True)
        self.btn_swipe.setChecked(False)
        self.btn_swipe.blockSignals(False)
        self._status("已退出卷帘对比模式")

    def _on_swipe_layer_selected(self, idx: int) -> None:
        if not self._swipe_enabled:
            return
        ly = self.cmb_swipe_layer.itemData(idx)
        self._swipe_layer = ly
        self._refresh_swipe_layer()

    def _on_swipe_orient_changed(self, idx: int) -> None:
        orient = self.cmb_swipe_orient.itemData(idx) or "vertical"
        self._swipe_orientation = orient
        vr = self.plot_item.getViewBox().viewRange()
        if orient == "vertical":
            self.swipe_line.setAngle(90)
            pos = (vr[0][0] + vr[0][1]) * (self.slider_swipe.value() / 100.0)
        else:
            self.swipe_line.setAngle(0)
            pos = (vr[1][0] + vr[1][1]) * (self.slider_swipe.value() / 100.0)
        self.swipe_line.setValue(pos)
        self.img_swipe_item.set_swipe(True, pos, orient)

    def _on_swipe_slider_changed(self, val: int) -> None:
        if not self._swipe_enabled:
            return
        vr = self.plot_item.getViewBox().viewRange()
        frac = max(0.0, min(1.0, val / 100.0))
        if self._swipe_orientation == "vertical":
            pos = vr[0][0] + (vr[0][1] - vr[0][0]) * frac
        else:
            pos = vr[1][0] + (vr[1][1] - vr[1][0]) * frac
        self.swipe_line.blockSignals(True)
        self.swipe_line.setValue(pos)
        self.swipe_line.blockSignals(False)
        self.img_swipe_item.set_swipe(True, pos, self._swipe_orientation)

    def _on_swipe_line_changed(self) -> None:
        if not self._swipe_enabled:
            return
        pos = self.swipe_line.value()
        self.img_swipe_item.set_swipe(True, pos, self._swipe_orientation)
        vr = self.plot_item.getViewBox().viewRange()
        if self._swipe_orientation == "vertical":
            span = max(1e-6, vr[0][1] - vr[0][0])
            frac = (pos - vr[0][0]) / span
        else:
            span = max(1e-6, vr[1][1] - vr[1][0])
            frac = (pos - vr[1][0]) / span
        pct = int(round(max(0.0, min(1.0, frac)) * 100))
        self.slider_swipe.blockSignals(True)
        self.slider_swipe.setValue(pct)
        self.slider_swipe.blockSignals(False)

    def _refresh_swipe_layer(self) -> None:
        layer, client = self._swipe_layer, self._client
        if layer is None or client is None or not layer.visible or not self._swipe_enabled:
            self.img_swipe_item.setVisible(False)
            return
        if not self.is_raster_renderable(layer):
            self.img_swipe_item.setVisible(False)
            return
        settings = layer.display
        try:
            bands = settings.selected_bands(layer.band_count)
        except Exception:
            return

        x_off, y_off, win_w, win_h = self._visible_window(layer)
        budget = RGB_BUDGET if settings.mode is DisplayMode.rgb else GRAY_BUDGET
        out_w, out_h = self._out_size(win_w, win_h, budget)
        key = TileKey(
            source=f"{layer.file_path}|swipe",
            bands=tuple(bands),
            x_off=x_off,
            y_off=y_off,
            win_w=win_w,
            win_h=win_h,
            out_w=out_w,
            out_h=out_h,
            overview=-1,
        )
        hit = self._cache.get(key)
        if hit is not None:
            self._render_swipe(_payload_from_cache(hit), layer, settings)
            return

        def _on_ok(payload: dict[str, Any], k=key, ly=layer, st=settings) -> None:
            self._cache.put(k, _cache_bytes(payload))
            self._render_swipe(payload, ly, st)

        client.read_window_async(
            path=layer.file_path,
            x_off=x_off,
            y_off=y_off,
            win_w=win_w,
            win_h=win_h,
            out_w=out_w,
            out_h=out_h,
            bands=bands,
            raw=True,
            on_success=_on_ok,
            on_error=lambda msg: None,
        )

    def _render_swipe(
        self,
        payload: dict[str, Any],
        layer: LayerItem,
        settings: DisplaySettings,
    ) -> None:
        if not self._swipe_enabled or self._swipe_layer is None:
            return
        try:
            win = decode_window(payload)
            bounds = compute_stretch_bounds(win, settings)
            rgba, _ = to_rgba(win, settings, bounds_override=bounds)
        except Exception:
            return

        self.img_swipe_item.setImage(rgba, autoLevels=False)
        self.img_swipe_item.setRect(QRectF(float(win.x_off), float(win.y_off), float(win.win_w), float(win.win_h)))
        self.img_swipe_item.set_swipe(True, self.swipe_line.value(), self._swipe_orientation)
        self.img_swipe_item.setVisible(True)

    # ------------------------------------------------------------------ P11-01 折线测距
    def set_measure_mode(self, enabled: bool) -> None:
        """切换折线测距模式开关。"""
        self._measure_mode = enabled
        self.measure_bar.setVisible(enabled)
        self.btn_measure.blockSignals(True)
        self.btn_measure.setChecked(enabled)
        self.btn_measure.blockSignals(False)

        if enabled:
            # 退出 ROI 绘制模式避免事件冲突
            self.set_roi_draw(ROIShape.rectangle, False)
            self.set_roi_draw(ROIShape.polygon, False)
            self.btn_rect.setChecked(False)
            self.btn_poly.setChecked(False)
            self.lbl_measure_status.setText("测距模式：左键点击拾取起点，连续点击添加顶点，右键/双击完成")
            self._status("折线测距已开启：鼠标左键点击开始测量")
        else:
            if self._measure_rubber is not None:
                self._measure_rubber.setData([], [])
            self._status("已退出测距模式")

    def clear_measurement(self) -> None:
        """清空当前测距折线与端点标记。"""
        self._measure_points.clear()
        if self._measure_line is not None:
            self._measure_line.setData([], [])
        if self._measure_rubber is not None:
            self._measure_rubber.setData([], [])
        for lbl in self._measure_labels:
            with contextlib.suppress(Exception):
                self.plot_item.removeItem(lbl)
        self._measure_labels.clear()
        self.lbl_measure_status.setText("测距数据已清空，点击影像左键开始新测量")
        self.sig_measure_status.emit("")

    def _compute_segment_distance(
        self, p1: tuple[float, float], p2: tuple[float, float]
    ) -> tuple[float, str]:
        """计算两点间真实距离（支持 CRS 投影/地理坐标换算，无投影回退像素距离）。"""
        layer = self._layer
        if layer is None:
            d_px = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
            return d_px, f"{d_px:.1f} px"

        meta = getattr(layer, "metadata", None) or {}
        gt = meta.get("geotransform")
        crs_wkt = str(meta.get("crs_wkt") or "")

        if gt and len(gt) >= 6:
            x1 = gt[0] + p1[0] * gt[1] + p1[1] * gt[2]
            y1 = gt[3] + p1[0] * gt[4] + p1[1] * gt[5]
            x2 = gt[0] + p2[0] * gt[1] + p2[1] * gt[2]
            y2 = gt[3] + p2[0] * gt[4] + p2[1] * gt[5]

            is_geo = any(k in crs_wkt.lower() for k in ("degree", "4326", "geogcs", "geographic"))
            if is_geo:
                # 经纬度：大圆大地线公式 (Haversine)
                lat1, lon1 = y1, x1
                lat2, lon2 = y2, x2
                r = 6371000.0  # 地球平均半径 (米)
                phi1, phi2 = math.radians(lat1), math.radians(lat2)
                dphi = math.radians(lat2 - lat1)
                dlambda = math.radians(lon2 - lon1)
                a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
                dist_m = 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
            else:
                # 投影平面直角坐标系 (米制)
                dist_m = math.hypot(x2 - x1, y2 - y1)

            if dist_m >= 1000.0:
                return dist_m, f"{dist_m / 1000.0:.3f} km"
            return dist_m, f"{dist_m:.2f} m"

        # 默认无仿射参数：纯像素距离
        dist_px = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        return dist_px, f"{dist_px:.1f} px"

    def _handle_measure_click(self, event: Any) -> None:
        vb = self.plot_item.getViewBox()
        if not vb.sceneBoundingRect().contains(event.scenePos()):
            return
        pos = vb.mapSceneToView(event.scenePos())
        btn = event.button() if hasattr(event, "button") and callable(event.button) else None
        is_right = (btn == Qt.MouseButton.RightButton)
        is_double = False
        if hasattr(event, "double") and callable(event.double):
            is_double = bool(event.double())

        if is_right or is_double:
            self._finish_measurement()
            return

        # 左键添加顶点
        self._measure_points.append((float(pos.x()), float(pos.y())))
        self._update_measurement_display()

    def _handle_measure_hover(self, view_pos: Any) -> None:
        if not self._measure_points or self._measure_rubber is None:
            return
        last_pt = self._measure_points[-1]
        curr_pt = (float(view_pos.x()), float(view_pos.y()))
        self._measure_rubber.setData([last_pt[0], curr_pt[0]], [last_pt[1], curr_pt[1]])

        seg_dist, seg_str = self._compute_segment_distance(last_pt, curr_pt)
        total_dist = sum(
            self._compute_segment_distance(self._measure_points[i], self._measure_points[i + 1])[0]
            for i in range(len(self._measure_points) - 1)
        ) + seg_dist

        total_str = f"{total_dist / 1000.0:.3f} km" if total_dist >= 1000.0 else f"{total_dist:.2f} m"
        status_txt = f"当前段长: {seg_str} | 累计总长: {total_str} (单击添加顶点，右键/双击完成)"
        self.lbl_measure_status.setText(status_txt)
        self.sig_measure_status.emit(status_txt)

    def _finish_measurement(self) -> None:
        if len(self._measure_points) < 2:
            self.clear_measurement()
            return
        if self._measure_rubber is not None:
            self._measure_rubber.setData([], [])
        total_dist = sum(
            self._compute_segment_distance(self._measure_points[i], self._measure_points[i + 1])[0]
            for i in range(len(self._measure_points) - 1)
        )
        total_str = f"{total_dist / 1000.0:.3f} km" if total_dist >= 1000.0 else f"{total_dist:.2f} m"
        finish_txt = f"测距完成：共 {len(self._measure_points)} 个顶点，累计总距离 {total_str}"
        self.lbl_measure_status.setText(finish_txt)
        self.sig_measure_status.emit(finish_txt)
        self._status(finish_txt)

    def _update_measurement_display(self) -> None:
        if not self._measure_points or self._measure_line is None:
            return
        xs = [p[0] for p in self._measure_points]
        ys = [p[1] for p in self._measure_points]
        self._measure_line.setData(xs, ys)

        if len(self._measure_points) >= 2:
            p_prev = self._measure_points[-2]
            p_curr = self._measure_points[-1]
            _, seg_str = self._compute_segment_distance(p_prev, p_curr)
            # 在中点添加文本标注
            mid_x = (p_prev[0] + p_curr[0]) * 0.5
            mid_y = (p_prev[1] + p_curr[1]) * 0.5
            lbl = pg.TextItem(seg_str, color="#ffcc00", fill="#2c3e50")
            lbl.setPos(mid_x, mid_y)
            lbl.setZValue(26)
            self.plot_item.addItem(lbl)
            self._measure_labels.append(lbl)

        count = len(self._measure_points)
        status_txt = f"已标定 {count} 个端点 (移动鼠标观察当前段长，单击继续，右键完成)"
        self.lbl_measure_status.setText(status_txt)

    # ------------------------------------------------------------------ 取数
    def _visible_window(self, layer: LayerItem) -> tuple[int, int, int, int]:
        vr = self.plot_item.getViewBox().viewRange()
        x0 = int(math.floor(min(vr[0][0], vr[0][1])))
        x1 = int(math.ceil(max(vr[0][0], vr[0][1])))
        y0 = int(math.floor(min(vr[1][0], vr[1][1])))
        y1 = int(math.ceil(max(vr[1][0], vr[1][1])))
        x0 = max(0, min(x0, layer.width - 1))
        y0 = max(0, min(y0, layer.height - 1))
        x1 = max(x0 + 1, min(x1, layer.width))
        y1 = max(y0 + 1, min(y1, layer.height))
        ax0 = (x0 // TILE_ALIGN) * TILE_ALIGN
        ay0 = (y0 // TILE_ALIGN) * TILE_ALIGN
        ax1 = min(layer.width, int(math.ceil(x1 / TILE_ALIGN) * TILE_ALIGN))
        ay1 = min(layer.height, int(math.ceil(y1 / TILE_ALIGN) * TILE_ALIGN))
        return (ax0, ay0, max(1, ax1 - ax0), max(1, ay1 - ay0))

    @staticmethod
    def _out_size(win_w: int, win_h: int, budget: int) -> tuple[int, int]:
        if win_w <= budget and win_h <= budget:
            return (win_w, win_h)
        ratio = min(budget / win_w, budget / win_h)
        return (max(1, int(round(win_w * ratio))), max(1, int(round(win_h * ratio))))

    def refresh(self) -> None:
        layer, client = self._layer, self._client
        if layer is None or client is None or not layer.visible:
            return
        if not self.is_raster_renderable(layer):
            # 矢量图层不派发任何栅格窗口请求
            return
        settings: DisplaySettings = layer.display
        try:
            bands = settings.selected_bands(layer.band_count)
        except ValueError as exc:
            self._status(f"波段配置无效：{exc}")
            return

        generation = self._gen.generation

        if self._swipe_enabled and self._swipe_layer is not None:
            self._refresh_swipe_layer()

        if self.view_crs_wkt:
            self._refresh_reprojected(layer, client, settings, bands, generation)
            return

        x_off, y_off, win_w, win_h = self._visible_window(layer)
        budget = RGB_BUDGET if settings.mode is DisplayMode.rgb else GRAY_BUDGET
        out_w, out_h = self._out_size(win_w, win_h, budget)
        key = TileKey(
            source=str(layer.file_path),
            bands=tuple(bands),
            x_off=x_off,
            y_off=y_off,
            win_w=win_w,
            win_h=win_h,
            out_w=out_w,
            out_h=out_h,
            overview=-1,
        )
        hit = self._cache.get(key)
        if hit is not None:
            self._render(_payload_from_cache(hit), key, generation, layer, settings, from_cache=True)
            return

        self._status(f"读取窗口 [{x_off},{y_off} {win_w}x{win_h}] -> {out_w}x{out_h}")

        def _on_ok(payload: dict[str, Any], k=key, g=generation, ly=layer, st=settings) -> None:
            self._after_fetch(payload, k, g, ly, st)

        client.read_window_async(
            path=layer.file_path,
            x_off=x_off,
            y_off=y_off,
            win_w=win_w,
            win_h=win_h,
            out_w=out_w,
            out_h=out_h,
            bands=bands,
            raw=True,
            on_success=_on_ok,
            on_error=lambda msg: self._status(f"窗口读取失败：{msg}"),
        )

    def _refresh_reprojected(
        self,
        layer: LayerItem,
        client: QtWorkerClient,
        settings: DisplaySettings,
        bands: list[int],
        generation: int,
    ) -> None:
        vr = self.plot_item.getViewBox().viewRange()
        x_min, x_max = sorted((vr[0][0], vr[0][1]))
        y_min, y_max = sorted((vr[1][0], vr[1][1]))
        if x_max <= x_min or y_max <= y_min:
            return
        win_w = int(math.ceil(x_max - x_min))
        win_h = int(math.ceil(y_max - y_min))
        out_w, out_h = self._out_size(win_w, win_h, GRAY_BUDGET)
        key = TileKey(
            source=f"{layer.file_path}|view={self.view_crs_wkt}",
            bands=tuple(bands),
            x_off=int(math.floor(x_min)),
            y_off=int(math.floor(y_min)),
            win_w=win_w,
            win_h=win_h,
            out_w=out_w,
            out_h=out_h,
            overview=-2,
        )
        hit = self._cache.get(key)
        if hit is not None:
            self._render(_payload_from_cache(hit), key, generation, layer, settings, from_cache=True)
            return
        def _on_ok(payload: dict[str, Any], k=key, g=generation, ly=layer, st=settings) -> None:
            self._after_fetch(payload, k, g, ly, st)

        client.read_view_window_async(
            layer.file_path,
            view_crs_wkt=self.view_crs_wkt or "",
            x_min=x_min,
            y_min=y_min,
            x_max=x_max,
            y_max=y_max,
            out_w=out_w,
            out_h=out_h,
            bands=bands,
            on_success=_on_ok,
            on_error=lambda msg: self._status(f"重投影窗口读取失败：{msg}"),
        )

    def _after_fetch(
        self,
        payload: dict[str, Any],
        key: TileKey,
        generation: int,
        layer: LayerItem,
        settings: DisplaySettings,
    ) -> None:
        if not self._gen.is_current(generation, layer.layer_id):
            return  # 过期响应：不渲染也不入缓存，避免污染
        self._cache.put(key, _cache_bytes(payload))
        self._render(payload, key, generation, layer, settings, from_cache=False)

    def _render(
        self,
        payload: dict[str, Any],
        key: TileKey,
        generation: int,
        layer: LayerItem,
        settings: DisplaySettings,
        *,
        from_cache: bool,
    ) -> None:
        if not self._gen.is_current(generation, layer.layer_id):
            return
        try:
            win = decode_window(payload)
        except ValueError as exc:
            self._status(f"窗口解码失败：{exc}")
            return

        if self._bounds_gen != generation or not self._bounds:
            self._bounds = compute_stretch_bounds(win, settings)
            self._bounds_gen = generation
        try:
            rgba, bounds = to_rgba(win, settings, bounds_override=self._bounds)
        except Exception as exc:
            self._status(f"显示变换失败：{exc}")
            return

        self.img_item.setImage(rgba, autoLevels=False)
        if payload.get("reprojected"):
            grid = payload.get("grid") or {}
            x0, y0 = float(grid.get("x_min", 0.0)), float(grid.get("y_min", 0.0))
            x1, y1 = float(grid.get("x_max", 1.0)), float(grid.get("y_max", 1.0))
            self.img_item.setRect(QRectF(x0, y1, max(x1 - x0, 1e-9), max(y1 - y0, 1e-9)))
        else:
            self.img_item.setRect(QRectF(float(win.x_off), float(win.y_off), float(win.win_w), float(win.win_h)))

        ov = payload.get("overview_used", -1)
        ratio = payload.get("overview_ratio")
        ov_text = "未用(1:1)" if ov is None or ov < 0 else f"{ratio or 1:g}x 抽取(层 {ov})"
        stats = self._stats_label
        if payload.get("reprojected"):
            g = payload.get("grid") or {}
            area = (
                f"重投影网格 [{float(g.get('x_min', 0)):.0f},{float(g.get('y_min', 0)):.0f}] ~ "
                f"[{float(g.get('x_max', 0)):.0f},{float(g.get('y_max', 0)):.0f}]"
            )
        else:
            area = f"窗口 [{win.x_off:g},{win.y_off:g}] {win.win_w:g}x{win.win_h:g}->{win.out_w}x{win.out_h}"
        self._status(
            f"{layer.name} | {settings.mode.value} | {area} | "
            f"概览 {ov_text} | "
            f"拉伸 {bounds[0][0]:.5g}~{bounds[0][1]:.5g} [{stats}] | "
            f"缓存 {self._cache.stats.bytes // 1024}KiB/{self._cache.budget_bytes // 1024}KiB "
            f"命中{self._cache.stats.hits} 逐出{self._cache.stats.evictions}{' 复用' if from_cache else ''}"
        )

    def set_stats_source_label(self, label: str) -> None:
        """由统计面板写入：抽样还是全量，必须显示在状态里。"""
        self._stats_label = label

    def _status(self, text: str) -> None:
        self.lbl_status.setText(text)
        self.sig_status.emit(text)

    # ------------------------------------------------------------------ 悬停
    def _on_mouse_moved(self, pos: Any) -> None:
        if self._layer is None:
            return
        vb = self.plot_item.getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            return
        p = vb.mapSceneToView(pos)
        self._hover_pos = (float(p.x()), float(p.y()))
        self._hover_timer.start()

        # P11-01 测距橡皮筋与实时距离提示
        if self._measure_mode and self._measure_points:
            self._handle_measure_hover(p)

    def _emit_hover(self) -> None:
        layer = self._layer
        if layer is None or self._hover_pos is None:
            return
        x, y = self._hover_pos
        if self.view_crs_wkt:
            # 视图 CRS 下画布坐标是地图坐标，行列换算交给 worker（见 info panel）
            self.sig_pixel_hover.emit(int(math.floor(x)), int(math.floor(y)), layer.layer_id)
            return
        col = int(math.floor(x))
        row = int(math.floor(y))
        if 0 <= col < layer.width and 0 <= row < layer.height:
            self.sig_pixel_hover.emit(col, row, layer.layer_id)

    # ------------------------------------------------------------------- ROI
    def set_roi_draw(self, shape: ROIShape, activate: bool) -> None:
        """工具栏开关：矩形立即放置一个可编辑 ROI；多边形进入逐点绘制模式。"""
        if not activate:
            self._draw_shape = None
            self._poly_pts = []
            self._poly_last_ts = None
            self._poly_last_scene_pos = None
            self._drop_rubber()
            return
        if shape is ROIShape.rectangle:
            self.begin_rect_roi()
        else:
            self.begin_polygon_roi()

    def load_rois(self, rois: list[ROI]) -> None:
        self._clear_rois()
        for roi in rois:
            self._add_roi(roi)

    def roi_list(self) -> list[ROI]:
        return [self._roi_geometry(state) for state in self._roi_states.values()]

    def roi_transform_failed_ids(self) -> set[str]:
        """返回坐标转换失败、当前视图下不可显示/编辑的 ROI id 集合。

        这些 ROI 的权威几何完好保存在模型里，只是当前视图坐标系转换失败。
        UI 必须把它们标出来，提供明确可恢复状态，而不是让用户以为 ROI 丢失了。
        """
        return {
            rid
            for rid, state in self._roi_states.items()
            if state.get("transform_error") or state.get("display_points") is None
        }

    def select_roi(self, roi_id: str | None) -> None:
        self._selected_roi_id = roi_id
        for rid, state in self._roi_states.items():
            state["item"].setPen(pg.mkPen((255, 220, 0) if rid == roi_id else (0, 200, 255), width=3))

    def selected_roi(self) -> ROI | None:
        """返回当前选中的 ROI 模型对象；若未显式选中但仅有 1 个 ROI 时返回该 ROI。"""
        if self._selected_roi_id and self._selected_roi_id in self._roi_states:
            return self._roi_geometry(self._roi_states[self._selected_roi_id])
        rois = self.roi_list()
        if len(rois) == 1:
            return rois[0]
        return None

    def _on_clip_roi_clicked(self) -> None:
        roi = self.selected_roi()
        if roi is None:
            if not self._roi_states:
                self._status("未找到可用于裁剪的 ROI：请先使用工具栏绘制矩形或多边形 ROI")
            else:
                self._status("当前存在多个 ROI，请先在 ROI 列表或画布中点击选中一个用于裁剪的 ROI")
            return
        if self._layer is None:
            self._status("无法裁剪：当前未加载或未选中任何栅格图层")
            return
        self.sig_clip_roi_requested.emit(self._layer, roi)

    def delete_selected_roi(self) -> str | None:
        return self.remove_roi(self._selected_roi_id)

    def remove_roi(self, roi_id: str | None) -> str | None:
        if not roi_id:
            return None
        state = self._roi_states.pop(roi_id, None)
        if state is None:
            return None
        self.plot_item.removeItem(state["item"])
        if self._selected_roi_id == roi_id:
            self._selected_roi_id = None
        if self._layer is not None:
            self._layer.rois = [r for r in self._layer.rois if r.roi_id != roi_id]
        self.sig_roi_changed.emit()
        return roi_id

    def _roi_view_points(self, state: dict[str, Any]) -> list[tuple[float, float]]:
        """取当前显示图形在视图坐标系下的点位。"""
        item = state["item"]
        base: ROI = state["roi"]
        if base.shape is ROIShape.rectangle and isinstance(item, pg.RectROI):
            c0, r0, c1, r1 = self._rect_bounds(item)
            return [(c0, r0), (c1, r1)]
        return [
            (float(pt.x()), float(pt.y()))
            for pt in (item.mapToParent(p) for p in item.getState()["points"])
        ]

    def _roi_geometry(self, state: dict[str, Any]) -> ROI:
        """读取当前显示图形对应的权威几何（只读，无副作用）。

        编辑路径请用 ``_roi_geometry_for_edit``，它额外报告编辑是否被拒绝。
        """
        roi, _ok = self._roi_geometry_for_edit(state)
        return roi

    def _roi_geometry_for_edit(self, state: dict[str, Any]) -> tuple[ROI, bool]:
        """把当前显示图形反算回权威几何，返回 (权威几何, 编辑是否被拒绝)。

        反算失败时返回原权威模型（权威几何保持不变），并把 rejected 置为 True，
        供调用方恢复显示图形——绝不允许权威几何与显示图形悄悄分叉。
        """
        base: ROI = state["roi"]
        if state.get("transform_error") or state.get("display_points") is None:
            return base, True
        view_points = self._roi_view_points(state)
        view_space = "map" if self.view_crs_wkt else "pixel"
        view_crs = self.view_crs_wkt

        # 显示点属于哪个坐标空间由状态记录，不可用当前视图空间臆测：
        # 视图切换后异步转换尚未完成时，显示点仍停留在旧空间，
        # 此时若按新视图空间反算，会把不同坐标空间的数据直接混用，产出垃圾几何。
        disp_space = state.get("display_space")
        disp_crs = state.get("display_crs")
        if disp_space is None:
            # 旧状态没有记录空间信息：保守起见不反算，保持权威几何不变
            return base, True
        if disp_space != view_space or (disp_space == "map" and disp_crs != view_crs):
            # 显示点与当前视图不在同一坐标空间：拒绝反算，权威几何原样保留
            return base, True

        if base.coordinate_space == disp_space and (disp_space == "pixel" or base.crs_wkt == disp_crs):
            return base.model_copy(update={"points": view_points}), False

        if base.shape is ROIShape.rectangle and not isinstance(state["item"], pg.RectROI):
            # 变换视图下的矩形轮廓受权威矩形约束，不可任意形变为任意四边形：
            # 权威几何原样保留，编辑被拒绝，轮廓恢复为已知良好形状。
            return base, True

        if self._client is not None and self._layer is not None:
            try:
                res = self._client.transform_coordinates(
                    path=self._layer.file_path,
                    points=view_points,
                    from_space=disp_space,
                    to_space=base.coordinate_space,
                    src_crs_wkt=disp_crs,
                    dst_crs_wkt=base.crs_wkt,
                    convention=base.pixel_convention,
                )
                auth_pts = [(float(p[0]), float(p[1])) for p in res.get("points", [])]
                if auth_pts and len(auth_pts) == len(view_points):
                    return base.model_copy(update={"points": auth_pts}), False
                self._status("ROI 反算权威坐标失败：返回点数不匹配，已保留原几何")
            except Exception as exc:
                self._status(f"ROI 反算权威坐标失败：{exc}；已保留原几何")
            return base, True
        return base, True

    def _restore_roi_display(self, state: dict[str, Any]) -> None:
        """编辑被拒绝或反算失败后，把显示图形恢复到最近一次已知良好的轮廓。

        权威几何保持不变，显示图形必须回到与之一致的位置，
        否则会出现「图形画在 A 处、模型记着 B 处」的分叉，并污染保存结果。
        这里只用本地保存的轮廓快照恢复，不再调用 worker——刚刚失败的通道不可再依赖。
        """
        roi: ROI = state["roi"]
        display_points = state.get("display_points")
        if not display_points:
            # 没有可用轮廓快照：明确隐藏并标记不可编辑，而不是显示错误图形
            with contextlib.suppress(Exception):
                self.plot_item.removeItem(state["item"])
            item = (
                pg.RectROI(pos=(0, 0), size=(0, 0))
                if roi.shape is ROIShape.rectangle
                else pg.PolyLineROI([(0, 0), (0, 0), (0, 0)], closed=True)
            )
            item.setVisible(False)
            state["item"] = item
            state["transform_error"] = True
            self.plot_item.addItem(item)
            return

        with contextlib.suppress(Exception):
            self.plot_item.removeItem(state["item"])
        new_item = self._create_roi_item(
            roi, list(display_points), selected=(roi.roi_id == self._selected_roi_id)
        )
        new_item.setVisible(True)
        state["item"] = new_item
        state["transform_error"] = False
        # 空间标记保持不变：恢复的正是同一空间下的原轮廓
        self.plot_item.addItem(new_item)
        new_item.sigRegionChanged.connect(lambda *a, s=state: self._on_roi_moved(s))
        new_item.sigClicked.connect(lambda *a, r_id=roi.roi_id: self.select_roi(roi.roi_id))

    def _create_roi_item(
        self, roi: ROI, display_points: list[tuple[float, float]], selected: bool = False
    ) -> pg.GraphicsObject:
        pen = pg.mkPen((255, 220, 0) if selected else (0, 200, 255), width=3)
        if roi.shape is ROIShape.rectangle:
            if len(display_points) == 2:
                (c0, r0), (c1, r1) = display_points
                item = pg.RectROI(
                    pos=(min(c0, c1), min(r0, r1)),
                    size=(max(abs(c1 - c0), 1e-6), max(abs(r1 - r0), 1e-6)),
                    pen=pen,
                    resizable=True,
                    movable=True,
                )
                item.addScaleHandle([1, 1], [0, 0])
                item.addScaleHandle([0, 1], [1, 0])
            else:
                # 变换视图下的矩形轮廓：按真实转换后的 4 角点渲染闭合多边形轮廓。
                # 禁用顶点手柄编辑与平移，防止将权威矩形扭曲为任意四边形。
                item = pg.PolyLineROI(
                    [(float(c), float(r)) for (c, r) in display_points],
                    closed=True,
                    pen=pen,
                    movable=False,
                )
                for h in item.getHandles():
                    h.setVisible(False)
                    h.setEnabled(False)
                item.translatable = False
        else:
            item = pg.PolyLineROI(
                [(float(c), float(r)) for (c, r) in display_points], closed=True, pen=pen, movable=True
            )
        item.setZValue(self.roi_z)
        return item

    def _add_roi(self, roi: ROI, display_points: list[tuple[float, float]] | None = None) -> None:
        view_space = "map" if self.view_crs_wkt else "pixel"
        view_crs = self.view_crs_wkt

        if display_points is None:
            if roi.coordinate_space == view_space and (view_space == "pixel" or roi.crs_wkt == view_crs):
                display_points = [(float(c), float(r)) for (c, r) in roi.points]
            elif self._client is not None and self._layer is not None:
                try:
                    res = self._client.transform_coordinates(
                        path=self._layer.file_path,
                        points=self._roi_points_for_transform(roi),
                        from_space=roi.coordinate_space,
                        to_space=view_space,
                        src_crs_wkt=roi.crs_wkt,
                        dst_crs_wkt=view_crs,
                        convention=roi.pixel_convention,
                    )
                    display_points = [(float(p[0]), float(p[1])) for p in res.get("points", [])]
                    if not display_points:
                        display_points = None
                except Exception as exc:
                    self._status(f"ROI 坐标换算失败：{exc}")
                    display_points = None
            else:
                display_points = None

        if self._layer is not None:
            # ROI 必须记住所属图层，否则项目重开后无法归回正确图层
            attrs = dict(roi.attributes or {})
            if attrs.get("layer_id") != self._layer.layer_id:
                attrs["layer_id"] = self._layer.layer_id
                roi = roi.model_copy(update={"attributes": attrs})

        if display_points is not None and len(display_points) > 0:
            item = self._create_roi_item(roi, display_points, selected=(roi.roi_id == self._selected_roi_id))
            item.setVisible(True)
            transform_error = False
        else:
            # 转换失败：创建不可见占位项，保留权威模型不变
            if roi.shape is ROIShape.rectangle:
                item = pg.RectROI(pos=(0, 0), size=(0, 0))
            else:
                item = pg.PolyLineROI([(0, 0), (0, 0), (0, 0)], closed=True)
            item.setVisible(False)
            display_points = None
            transform_error = True

        state = {
            "roi": roi,
            "item": item,
            "display_points": display_points,
            # 显示点所属坐标空间与 CRS：反算时必须按它判定，不能用当前视图臆测
            "display_space": view_space if display_points else None,
            "display_crs": view_crs if display_points else None,
            "gen": self._gen.generation,
            "transform_error": transform_error,
            "inverse_error": False,
        }
        self._roi_states[roi.roi_id] = state
        if self._layer is not None and not any(r.roi_id == roi.roi_id for r in self._layer.rois):
            self._layer.rois.append(roi)
        self.plot_item.addItem(item)
        if not transform_error:
            item.sigRegionChanged.connect(lambda *a, s=state: self._on_roi_moved(s))
            item.sigClicked.connect(lambda *a, rid=roi.roi_id: self.select_roi(rid))
        # 转换失败时也通知 UI：列表中标出失败状态，用户才知道 ROI 还在但当前不可见
        self.sig_roi_changed.emit()

    def _on_roi_moved(self, state: dict[str, Any]) -> None:
        """手柄拖动 / 平移后把几何写回模型，供保存与统计使用。

        反算失败时权威几何保持不变，并把显示图形恢复到与之一致的轮廓，
        杜绝「图形画在 A 处、模型记着 B 处」的分叉污染保存结果。
        """
        if state.get("transform_error") or state.get("display_points") is None:
            return
        previous_display = state.get("display_points")
        roi, rejected = self._roi_geometry_for_edit(state)
        if rejected:
            # 编辑被拒绝：显示图形回到上一次已知良好的轮廓，权威几何保持不变
            state["roi"] = roi
            state["display_points"] = previous_display
            self._restore_roi_display(state)
            if self._layer is not None:
                self._layer.rois = self.roi_list()
            self.sig_roi_changed.emit()
            return
        state["roi"] = roi
        # 编辑生效：把当前显示轮廓登记为新的「已知良好」快照，供后续失败时回滚
        state["display_points"] = self._roi_view_points(state)
        # 编辑不改变显示点所属坐标空间，此处仅刷新快照，空间标记保持不变
        if self._layer is not None:
            self._layer.rois = self.roi_list()
        self.sig_roi_changed.emit()

    def _clear_rois(self) -> None:
        for state in self._roi_states.values():
            self.plot_item.removeItem(state["item"])
        self._roi_states.clear()
        self._selected_roi_id = None
        self._poly_pts = []
        self._poly_last_ts = None
        self._poly_last_scene_pos = None
        self._drop_rubber()

    def _redraw_rubber(self) -> None:
        """多边形绘制中的实时预览：尚未闭合，因此还不是 ROI。"""
        self._drop_rubber()
        if len(self._poly_pts) < 2:
            return
        xs = [p[0] for p in self._poly_pts]
        ys = [p[1] for p in self._poly_pts]
        self._rubber = pg.PlotDataItem(
            x=xs,
            y=ys,
            pen=pg.mkPen((255, 140, 0), width=2),
            symbol="o",
            symbolSize=8,
            symbolBrush=(255, 140, 0),
        )
        self._rubber.setZValue(self.rubber_z)
        self.plot_item.addItem(self._rubber)

    def _drop_rubber(self) -> None:
        if self._rubber is not None:
            self.plot_item.removeItem(self._rubber)
            self._rubber = None

    @staticmethod
    def _rect_bounds(item: pg.RectROI) -> tuple[float, float, float, float]:
        """取矩形 ROI 在视图坐标下的对角两点。

        用 parentBounds 而不是 pos/size：ROI 的内部变换在 Y 轴反向的视图里会随缩放顺序漂移，
        而视图坐标下的包围盒正是模型能表示的（且 worker 需要的）轴对齐矩形。
        """
        b = item.parentBounds()
        return (float(b.left()), float(b.top()), float(b.right()), float(b.bottom()))

    # ------------------------------------------------------- ROI 鼠标交互
    # 事件由 pyqtgraph 场景发出（QWidget 的 mousePress 拿不到 scenePos，
    # 且画布鼠标事件已被内部视图消费），因此这里连接 scene 信号。
    def _on_scene_mouse_clicked(self, event: Any) -> None:
        # P11-01 测距点击优先接管
        if self._measure_mode:
            self._handle_measure_click(event)
            return

        if self._draw_shape is not ROIShape.polygon:
            # P11-01 普通点击像元发射光谱剖面查看信号
            if self._layer is not None and self.is_raster_renderable(self._layer):
                vb = self.plot_item.getViewBox()
                if vb.sceneBoundingRect().contains(event.scenePos()):
                    pos = vb.mapSceneToView(event.scenePos())
                    col = int(math.floor(pos.x()))
                    row = int(math.floor(pos.y()))
                    if 0 <= col < self._layer.width and 0 <= row < self._layer.height:
                        self.sig_pixel_clicked.emit(col, row, self._layer.layer_id)
            return
        vb = self.plot_item.getViewBox()
        if not vb.sceneBoundingRect().contains(event.scenePos()):
            return
        pos = vb.mapSceneToView(event.scenePos())

        now = time.monotonic()
        btn = event.button() if hasattr(event, "button") and callable(event.button) else None
        is_right = (btn == Qt.MouseButton.RightButton)

        # Qt / pyqtgraph 传递的显式双击标记
        is_double = False
        if hasattr(event, "double") and callable(event.double):
            is_double = bool(event.double())
        elif hasattr(event, "isDoubleClick") and callable(event.isDoubleClick):
            is_double = bool(event.isDoubleClick())
        elif hasattr(event, "doubleClicked") and callable(event.doubleClicked):
            is_double = bool(event.doubleClicked())

        # 当点数 >= 3 时，点击在首个顶点附近（< 15px）完成闭合
        near_first = False
        if len(self._poly_pts) >= 3:
            first_x, first_y = self._poly_pts[0]
            p0 = vb.mapViewToScene(pg.Point(first_x, first_y))
            if (p0 - event.scenePos()).manhattanLength() < 15:
                near_first = True

        # 同位置连击（双击补偿）：只有在同一个屏幕位置附近（< 8px 像素抖动内）且时间间隔 < 0.40s 且点数 >= 3 时，
        # 且仅当为主键（左键）连击时，才作为双击闭合判定。
        # 两个位置不同的点击（>= 8px）无论点击多快（例如 < 50ms），都是独立的有效顶点，绝不能误判为双击！
        is_same_pos_double = False
        if (
            self._poly_last_scene_pos is not None
            and (event.scenePos() - self._poly_last_scene_pos).manhattanLength() < 8
            and self._poly_last_ts is not None
            and (now - self._poly_last_ts < 0.40)
            and len(self._poly_pts) >= 3
            and (btn == Qt.MouseButton.LeftButton or btn is None)
        ):
            is_same_pos_double = True

        if is_double or is_right or near_first or is_same_pos_double:
            self.finish_polygon()
            return

        # 仅响应左键（或未指定按钮的模拟点击）添加顶点，忽略其它杂项键
        if btn is not None and btn != Qt.MouseButton.LeftButton:
            return

        self._poly_pts.append((float(pos.x()), float(pos.y())))
        self._poly_last_scene_pos = event.scenePos()
        self._poly_last_ts = now
        self._redraw_rubber()
        if len(self._poly_pts) == 1:
            self._status("多边形 ROI：继续单击加点，双击、右键或点击起点闭合")

    def _on_scene_mouse_moved(self, pos: Any) -> None:
        # 悬停坐标查询复用同一个场景信号
        self._on_mouse_moved(pos)

    def begin_rect_roi(self, name: str | None = None) -> str:
        """在当前视图中心放一个可拖拽矩形，用户拖手柄确定范围后落入图层。"""
        layer = self._layer
        if layer is None:
            raise RuntimeError("没有选中图层，无法创建 ROI")
        vr = self.plot_item.getViewBox().viewRange()
        cx0, cx1 = sorted(vr[0])
        cy0, cy1 = sorted(vr[1])
        if not self.view_crs_wkt:
            # 适应全图留有 padding，初始矩形必须落在真实图幅内，否则 ROI 起点会是负行列
            cx0, cy0 = max(cx0, 0.0), max(cy0, 0.0)
            cx1, cy1 = min(cx1, float(layer.width)), min(cy1, float(layer.height))
        w = max(1.0, (cx1 - cx0) * 0.25)
        h = max(1.0, (cy1 - cy0) * 0.25)
        roi = ROI(
            name=name or f"ROI-{len(self._roi_states) + 1}",
            shape=ROIShape.rectangle,
            points=[(cx0, cy0), (cx0 + w, cy0 + h)],
            coordinate_space="map" if self.view_crs_wkt else "pixel",
            crs_wkt=self.view_crs_wkt,
        )
        self._add_roi(roi)
        state = self._roi_states[roi.roi_id]
        item = state["item"]
        if isinstance(item, pg.RectROI) and len(item.getHandles()) == 0:
            item.addScaleHandle([1, 1], [0, 0])
            item.addScaleHandle([0, 1], [1, 0])
        # 只挂缩放手柄，不挂旋转手柄：ROI 模型与 worker 的 rectangle_window 只表示轴对齐矩形，
        # 允许旋转会静默丢失几何（画出来能转，存下来却对不上）。
        self.select_roi(roi.roi_id)
        self._draw_shape = None
        self.btn_rect.setChecked(False)
        self.sig_roi_changed.emit()
        self._status("已放置矩形 ROI：拖动手柄调整范围，拖动内部平移")
        return roi.roi_id

    def begin_polygon_roi(self) -> None:
        self._draw_shape = ROIShape.polygon
        self._poly_pts = []
        self._poly_last_ts = None
        self._poly_last_scene_pos = None
        self._status("多边形 ROI：在画布上逐点单击，双击结束")

    def add_polygon_vertex(self, col: float, row: float) -> None:
        """供测试与脚本驱动：显式加一个顶点。"""
        if self._draw_shape is not ROIShape.polygon:
            self._draw_shape = ROIShape.polygon
            self._poly_pts = []
            self._poly_last_ts = None
            self._poly_last_scene_pos = None
        self._poly_pts.append((float(col), float(row)))
        self._redraw_rubber()

    def finish_polygon(self) -> str | None:
        pts = list(self._poly_pts)
        self._poly_pts = []
        self._poly_last_ts = None
        self._poly_last_scene_pos = None
        self._draw_shape = None
        self.btn_poly.setChecked(False)
        self._drop_rubber()
        if len(pts) < 3:
            self._status(f"多边形至少需要 3 个顶点，收到 {len(pts)} 个，已放弃")
            return None
        roi = ROI(
            name=f"ROI-{len(self._roi_states) + 1}",
            shape=ROIShape.polygon,
            points=pts,
            coordinate_space="map" if self.view_crs_wkt else "pixel",
            crs_wkt=self.view_crs_wkt,
        )
        self._add_roi(roi)
        self.select_roi(roi.roi_id)
        if self._layer is not None:
            self._layer.rois = self.roi_list()
        self.sig_roi_changed.emit()
        self._status(f"已创建多边形 ROI：{len(pts)} 个顶点，面积 {roi.area_pixels():.1f} 像元")
        return roi.roi_id

    def mousePressEvent(self, event: Any) -> None:  # pragma: no cover - 保留默认行为
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:  # pragma: no cover
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:  # pragma: no cover
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:  # pragma: no cover
        super().mouseMoveEvent(event)


def _cache_bytes(payload: dict) -> bytes:
    """缓存里存原始窗口的可序列化形式（base64 文本），字节数即真实占用。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _payload_from_cache(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))
