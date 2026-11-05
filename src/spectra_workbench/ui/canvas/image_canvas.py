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
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
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


class ImageCanvas(QWidget):
    """栅格影像显示画布。"""

    sig_view_changed = Signal(object)  # dict：view_id / 中心 / 比例
    sig_pixel_hover = Signal(int, int, str)  # col, row, layer_id
    sig_roi_changed = Signal()
    sig_status = Signal(str)

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

        self.btn_rect = QPushButton("画矩形ROI")
        self.btn_rect.setCheckable(True)
        self.btn_rect.toggled.connect(lambda on: self.set_roi_draw(ROIShape.rectangle, on))
        bar.addWidget(self.btn_rect)

        self.btn_poly = QPushButton("画多边形ROI")
        self.btn_poly.setCheckable(True)
        self.btn_poly.toggled.connect(lambda on: self.set_roi_draw(ROIShape.polygon, on))
        bar.addWidget(self.btn_poly)

        self.btn_refresh = QPushButton("刷新视图")
        self.btn_refresh.clicked.connect(self.refresh)
        bar.addWidget(self.btn_refresh)
        layout.addLayout(bar)

        pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)
        self.gl_widget = pg.GraphicsLayoutWidget()
        self.plot_item = self.gl_widget.addPlot()
        self.plot_item.invertY(True)
        self.plot_item.setAspectLocked(True)
        self.plot_item.showGrid(x=True, y=True, alpha=0.2)

        self.img_item = pg.ImageItem()
        self.plot_item.addItem(self.img_item)
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
    def set_layer(self, layer: LayerItem | None, client: QtWorkerClient | None = None) -> None:
        if client is not None:
            self._client = client
        same = bool(
            layer is not None
            and self._layer is not None
            and layer.layer_id == self._layer.layer_id
        )
        self._layer = layer
        self._gen.bump(layer.layer_id if layer else None)
        self._bounds_gen = -1
        self._bounds = None
        if layer is None:
            self.clear()
            return
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
                        pts = [(float(p[0]), float(p[1])) for p in res.get("points", [])]
                        roi_results[r_id] = pts if pts else None
                        _try_finish()
                    return _ok_roi

                def _make_err(r_id: str):
                    def _err_roi(msg: str) -> None:
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

    def set_display(self, settings: DisplaySettings) -> None:
        """显示参数变化：递增代际丢弃旧结果，但不清缓存（缓存的是原值，不需重读盘）。"""
        if self._layer is not None:
            self._layer.display = settings
        self._gen.bump(self._layer.layer_id if self._layer else None)
        self._bounds_gen = -1
        self._bounds = None
        self.refresh()

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
        settings: DisplaySettings = layer.display
        try:
            bands = settings.selected_bands(layer.band_count)
        except ValueError as exc:
            self._status(f"波段配置无效：{exc}")
            return

        generation = self._gen.generation

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

    def select_roi(self, roi_id: str | None) -> None:
        self._selected_roi_id = roi_id
        for rid, state in self._roi_states.items():
            state["item"].setPen(pg.mkPen((255, 220, 0) if rid == roi_id else (0, 200, 255), width=3))

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

    def _roi_geometry(self, state: dict[str, Any]) -> ROI:
        item = state["item"]
        base: ROI = state["roi"]
        if state.get("transform_error") or state.get("display_points") is None:
            return base
        if base.shape is ROIShape.rectangle:
            if isinstance(item, pg.RectROI):
                c0, r0, c1, r1 = self._rect_bounds(item)
                view_points = [(c0, r0), (c1, r1)]
            else:
                # 变换视图下的矩形轮廓（多边形）受权威矩形约束，不可任意形变为任意四边形
                return base
        else:
            view_points = [
                (float(pt.x()), float(pt.y()))
                for pt in (item.mapToParent(p) for p in item.getState()["points"])
            ]
        view_space = "map" if self.view_crs_wkt else "pixel"
        view_crs = self.view_crs_wkt

        if base.coordinate_space == view_space and (view_space == "pixel" or base.crs_wkt == view_crs):
            return base.model_copy(update={"points": view_points})

        if self._client is not None and self._layer is not None:
            try:
                res = self._client.transform_coordinates(
                    path=self._layer.file_path,
                    points=view_points,
                    from_space=view_space,
                    to_space=base.coordinate_space,
                    src_crs_wkt=view_crs,
                    dst_crs_wkt=base.crs_wkt,
                    convention=base.pixel_convention,
                )
                auth_pts = [(float(p[0]), float(p[1])) for p in res.get("points", [])]
                if auth_pts and len(auth_pts) == len(view_points):
                    return base.model_copy(update={"points": auth_pts})
            except Exception as exc:
                self._status(f"ROI 反算权威坐标失败：{exc}")
                return base
        return base

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
            "gen": self._gen.generation,
            "transform_error": transform_error,
        }
        self._roi_states[roi.roi_id] = state
        if self._layer is not None and not any(r.roi_id == roi.roi_id for r in self._layer.rois):
            self._layer.rois.append(roi)
        self.plot_item.addItem(item)
        if not transform_error:
            item.sigRegionChanged.connect(lambda *a, s=state: self._on_roi_moved(s))
            item.sigClicked.connect(lambda *a, rid=roi.roi_id: self.select_roi(rid))

    def _on_roi_moved(self, state: dict[str, Any]) -> None:
        """手柄拖动 / 平移后把几何写回模型，供保存与统计使用。"""
        if state.get("transform_error") or state.get("display_points") is None:
            return
        state["roi"] = self._roi_geometry(state)
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
        if self._draw_shape is not ROIShape.polygon:
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
