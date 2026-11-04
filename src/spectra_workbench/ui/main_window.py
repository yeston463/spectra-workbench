"""主窗口（P3-01 / P3-03 / P3-04 / P3-05）。

集成：菜单与工具栏、双画布（可联动）、图层/工具箱/信息/显示面板、任务与日志、
跨进程 worker 客户端（GUI 零原生地理依赖），以及项目保存与重开（含缺失源重定位）。
"""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from spectra_workbench.domain.models import TaskResult, TaskSpec
from spectra_workbench.domain.roi import ROI
from spectra_workbench.project.model import (
    LayerRecord,
    MissingSource,
    ProjectSpec,
    ViewRecord,
    find_missing,
    load_project,
    relink,
    resolve_source,
    save_project,
    store_source,
)
from spectra_workbench.rendering.cache import TileCache
from spectra_workbench.rendering.display import DisplaySettings, stats_source_label
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.canvas.image_canvas import ImageCanvas
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget
from spectra_workbench.ui.models.layer import LayerItem
from spectra_workbench.ui.panels.display_panel import DisplayPanel
from spectra_workbench.ui.panels.info_panel import InfoPanel
from spectra_workbench.ui.panels.layer_panel import LayerPanel
from spectra_workbench.ui.panels.task_panel import TaskPanel
from spectra_workbench.ui.panels.toolbox_panel import ToolboxPanel

# 视图 CRS 预设（WKT 由 worker 索取，界面不导入 osr）
_VIEW_CRS_PRESETS: list[tuple[str, int | None]] = [
    ("像元坐标（无投影视图）", None),
    ("数据集自身 CRS", 0),
    ("EPSG:4326 (WGS84 经纬度)", 4326),
    ("EPSG:3857 (Web 墨卡托)", 3857),
    ("EPSG:32650 (UTM 50N)", 32650),
]


class _RelinkDialog(QDialog):
    """缺失数据源重定位：逐项显示原路径，允许浏览选择新位置。"""

    def __init__(self, missing: list[Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("重新关联缺失数据源")
        self.mapping: dict[str, Path] = {}
        self._edits: dict[str, QLineEdit] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("以下图层的数据源不存在，请指定新位置（留空表示跳过该图层）："))
        for item in missing:
            row = QHBoxLayout()
            row.addWidget(QLabel(item.name + "（原：" + item.stored + "）"))
            edit = QLineEdit()
            edit.setPlaceholderText(str(item.tried))
            row.addWidget(edit, stretch=1)
            btn = QPushButton("浏览…")
            btn.clicked.connect(lambda _=False, e=edit, lid=item.layer_id: self._pick(e, lid))
            row.addWidget(btn)
            layout.addLayout(row)
            self._edits[item.layer_id] = edit
        buttons = QHBoxLayout()
        ok = QPushButton("确定")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def _pick(self, edit: QLineEdit, layer_id: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择数据源", str(Path.home()))
        if path:
            edit.setText(path)

    def accept(self) -> None:  # noqa: D102
        self.mapping = {lid: Path(e.text()) for lid, e in self._edits.items() if e.text().strip()}
        super().accept()


class MainWindow(QMainWindow):
    """Spectra Workbench 桌面主窗口。"""

    def __init__(self, project_root: Path | str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self._clients: dict[str, QtWorkerClient] = {}
        self._active_task_client: QtWorkerClient | None = None
        self._task_subscriptions: dict[str, Callable[[], None]] = {}
        self._cache = TileCache()
        self.project_path: Path | None = None
        self.missing_sources: list[MissingSource] = []
        self._pending_active_layer: str | None = None
        self._pending_view_v1: ViewRecord | None = None
        self._pending_view_v2: ViewRecord | None = None
        self._dirty = False
        self._hover_layer_id: str | None = None
        self._notifications: list[QMessageBox] = []
        self._open_seq = 0
        self._layer_seq: dict[str, int] = {}

        app = QApplication.instance()
        if isinstance(app, QApplication):
            ensure_application_fonts(app)
        self._init_ui()
        self._wire_signals()

    # ------------------------------------------------------------------ 客户端
    def _get_client(self, provider_id: str) -> QtWorkerClient:
        if provider_id not in self._clients:
            client = QtWorkerClient(provider_id=provider_id, project_root=self.project_root, parent=self)
            self._clients[provider_id] = client
        return self._clients[provider_id]

    @property
    def gdal_client(self) -> QtWorkerClient:
        return self._get_client("gdal")

    @property
    def rasterio_client(self) -> QtWorkerClient:
        return self._get_client("rasterio")

    def _client_for(self, layer: LayerItem) -> QtWorkerClient:
        hint = (layer.provider_hint or "").lower()
        return self.rasterio_client if "rasterio" in hint else self.gdal_client

    def _notify(self, icon: QMessageBox.Icon, title: str, message: str) -> None:
        """状态栏始终记录消息；窗口已显示时再补一个对话框。

        对话框必须是非模态的：worker 回调里阻塞事件循环会让离屏/自动化运行永远挂起。
        """
        self.status_bar.showMessage(f"{title}：{message}")
        if not self.isVisible():
            return
        box = QMessageBox(icon, title, message, QMessageBox.StandardButton.Ok, self)
        box.setModal(False)
        self._notifications.append(box)
        box.finished.connect(lambda *_: self._forget(box))
        box.show()

    def _forget(self, box: QMessageBox) -> None:
        with contextlib.suppress(ValueError):
            self._notifications.remove(box)

    # -------------------------------------------------------------------- UI
    def _init_ui(self) -> None:
        self.setWindowTitle("Spectra Workbench · 遥感与光谱计算工作台 (P3)")
        self.resize(1600, 900)

        self.canvas = ImageCanvas(view_id="V1", cache=self._cache)
        self.canvas_b = ImageCanvas(view_id="V2", cache=self._cache)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.canvas)
        self.splitter.addWidget(self.canvas_b)
        self.canvas_b.setVisible(False)
        self.setCentralWidget(self.splitter)

        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪 | 界面进程不导入 GDAL/Rasterio，地理运算在 worker 内完成")

        self.setDockOptions(
            QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AllowNestedDocks
        )

        self.dock_left = QDockWidget("数据与工具 (Explorer)", self)
        self.dock_left.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.tab_left = QTabWidget()
        self.toolbox_panel = ToolboxPanel()
        self.layer_panel = LayerPanel()
        self.tab_left.addTab(self.layer_panel, "图层管理")
        self.tab_left.addTab(self.toolbox_panel, "算法工具箱")
        self.dock_left.setWidget(self.tab_left)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_left)

        self.dock_right = QDockWidget("参数与查询 (Inspector)", self)
        self.dock_right.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.tab_right = QTabWidget()
        self.form_widget = CapabilityFormWidget()
        self.form_widget.set_project_root(self.project_root)
        self.info_panel = InfoPanel()
        self.display_panel = DisplayPanel()
        self.tab_right.addTab(self.info_panel, "坐标与 ROI")
        self.tab_right.addTab(self.display_panel, "显示")
        self.tab_right.addTab(self.form_widget, "算法参数")
        self.dock_right.setWidget(self.tab_right)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_right)

        self.dock_bottom = QDockWidget("任务监控与日志", self)
        self.dock_bottom.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea
        )
        self.task_panel = TaskPanel()
        self.dock_bottom.setWidget(self.task_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_bottom)

        self._create_menus_and_toolbars()

    def _create_menus_and_toolbars(self) -> None:
        mb = self.menuBar()

        m_file = mb.addMenu("文件 (&F)")
        self.act_open_img = QAction("打开影像 (&O)…", self)
        self.act_open_img.setShortcut(QKeySequence.StandardKey.Open)
        self.act_open_img.triggered.connect(self.open_file_dialog)
        m_file.addAction(self.act_open_img)

        self.act_sample = QAction("加载合成样例 (&S)", self)
        self.act_sample.triggered.connect(self.load_default_sample)
        m_file.addAction(self.act_sample)
        m_file.addSeparator()

        self.act_proj_new = QAction("新建项目 (&N)", self)
        self.act_proj_new.triggered.connect(self.new_project)
        m_file.addAction(self.act_proj_new)

        self.act_proj_open = QAction("打开项目… (&P)", self)
        self.act_proj_open.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.act_proj_open.triggered.connect(self.open_project_dialog)
        m_file.addAction(self.act_proj_open)

        self.act_proj_save = QAction("保存项目 (&S)", self)
        self.act_proj_save.setShortcut(QKeySequence.StandardKey.Save)
        self.act_proj_save.triggered.connect(self.save_project_action)
        m_file.addAction(self.act_proj_save)

        self.act_proj_saveas = QAction("项目另存为… (&A)", self)
        self.act_proj_saveas.triggered.connect(lambda: self.save_project_action(save_as=True))
        m_file.addAction(self.act_proj_saveas)

        self.act_relink = QAction("重新关联缺失数据源… (&R)", self)
        self.act_relink.triggered.connect(self.prompt_relink_missing)
        m_file.addAction(self.act_relink)
        m_file.addSeparator()

        act_exit = QAction("退出 (&X)", self)
        act_exit.triggered.connect(self.close)
        m_file.addAction(act_exit)

        m_view = mb.addMenu("视图 (&V)")
        act_fit = QAction("适应全图", self)
        act_fit.triggered.connect(self.canvas.fit_extent)
        m_view.addAction(act_fit)
        act_in = QAction("放大", self)
        act_in.triggered.connect(self.canvas.zoom_in)
        m_view.addAction(act_in)
        act_out = QAction("缩小", self)
        act_out.triggered.connect(self.canvas.zoom_out)
        m_view.addAction(act_out)

        self.act_dual = QAction("双视图 (&D)", self)
        self.act_dual.setCheckable(True)
        self.act_dual.toggled.connect(self.set_dual_view)
        m_view.addAction(self.act_dual)

        self.act_sync = QAction("双视图联动 (&L)", self)
        self.act_sync.setCheckable(True)
        self.act_sync.setChecked(True)
        self.act_sync.toggled.connect(self._on_sync_toggled)
        m_view.addAction(self.act_sync)

        crs_row = QWidget()
        lay = QHBoxLayout(crs_row)
        lay.setContentsMargins(12, 0, 12, 0)
        lay.addWidget(QLabel("视图 CRS："))
        self.cmb_view_crs = QComboBox()
        for label, epsg in _VIEW_CRS_PRESETS:
            self.cmb_view_crs.addItem(label, epsg)
        self.cmb_view_crs.currentIndexChanged.connect(self._on_view_crs_changed)
        lay.addWidget(self.cmb_view_crs)
        m_view.setEnabled(True)
        holder = QWidget(self)
        holder_layout = QVBoxLayout(holder)
        holder_layout.addWidget(crs_row)

        m_help = mb.addMenu("帮助 (&H)")
        act_about = QAction("关于 (&A)", self)
        act_about.triggered.connect(self._show_about)
        m_help.addAction(act_about)

        tb = QToolBar("主快捷栏", self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        tb.addAction(self.act_open_img)
        tb.addAction(self.act_sample)
        tb.addSeparator()
        tb.addAction(self.act_proj_save)
        tb.addAction(act_fit)
        tb.addAction(act_in)
        tb.addAction(act_out)
        tb.addSeparator()
        tb.addAction(self.act_dual)
        tb.addWidget(holder)

    def _wire_signals(self) -> None:
        self.toolbox_panel.sig_capability_selected.connect(self._on_capability_selected)

        self.layer_panel.sig_layer_selected.connect(self._on_layer_selected)
        self.layer_panel.sig_layer_visibility_changed.connect(self._on_layer_visibility_changed)
        self.layer_panel.sig_layer_removed.connect(self._on_layer_removed)
        self.layer_panel.sig_display_mode_changed.connect(self._on_display_mode_changed)
        self.layer_panel.sig_open_file_requested.connect(self.open_file_dialog)

        self.form_widget.sig_task_submitted.connect(self.submit_task)
        self.task_panel.sig_cancel_requested.connect(self._on_cancel_task)
        self.task_panel.sig_load_result_requested.connect(self.load_result_to_layer)

        # 悬停查询（P3-05）
        self.canvas.sig_pixel_hover.connect(self._on_hover_pixel)
        self.canvas_b.sig_pixel_hover.connect(self._on_hover_pixel)

        # 显示面板（P3-04）
        self.display_panel.sig_display_changed.connect(self._on_display_changed)

        # 信息面板：坐标反算、统计、ROI 管理
        self.info_panel.sig_map_query_requested.connect(self._on_map_query)
        self.info_panel.sig_stats_requested.connect(self._on_stats_requested)
        self.info_panel.sig_roi_selected.connect(self.canvas.select_roi)
        self.info_panel.sig_roi_delete_requested.connect(self._on_roi_delete)
        self.info_panel.sig_roi_clear_requested.connect(self._on_roi_clear)
        self.canvas.sig_roi_changed.connect(self._on_roi_changed)

        # 双视图联动
        self.canvas.sig_view_changed.connect(self._on_peer_view_changed)
        self.canvas_b.sig_view_changed.connect(self._on_peer_view_changed)

    # ------------------------------------------------------------------ 图层
    def open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开遥感栅格影像",
            str(self.project_root),
            "遥感栅格影像 (*.tif *.tiff *.dat *.img *.hdr *.vrt *.png *.jpg);;所有文件 (*.*)",
        )
        if path:
            self.open_dataset(path)

    def open_dataset(
        self,
        path: Path | str,
        record: LayerRecord | None = None,
        rois: list[ROI] | None = None,
        order: int | None = None,
    ) -> None:
        p = Path(str(path)).resolve()
        if not p.exists():
            self._notify(QMessageBox.Icon.Critical, "文件未找到", f"指定影像文件不存在:\n{p}")
            return
        self.status_bar.showMessage(f"正在探测元数据: {p.name}…")
        client = self.gdal_client
        self._open_seq += 1
        seq = self._open_seq if order is None else order

        def _on_probed(meta: dict[str, Any]) -> None:
            layer = LayerItem(name=(record.name if record else p.stem), file_path=p, metadata=meta)
            if record is not None:
                layer.layer_id = record.layer_id
                layer.visible = record.visible
                layer.display = record.display.model_copy(deep=True)
                layer.provider_hint = record.provider_hint
                layer.rois = list(rois or [])
            client = self._client_for(layer)
            self._layer_seq[layer.layer_id] = seq
            # 顺序按“发起打开的先后”确定：探测响应谁先回来不影响图层顺序
            index = sum(1 for ly in self.layer_panel.layers if self._layer_seq.get(ly.layer_id, 0) < seq)
            self.layer_panel.add_layer(
                layer,
                select=(record is None or layer.layer_id == self._pending_active_layer),
                index=index,
            )
            self._mark_dirty()
            if record is None:
                self.canvas.set_layer(layer, client)
                self.display_panel.set_layer(layer.band_count, layer.display)

        def _on_probe_err(err: str) -> None:
            self._notify(QMessageBox.Icon.Critical, "影像打开失败", f"worker 探测元数据失败:\n{err}")

        client.probe_dataset_async(p, on_success=_on_probed, on_error=_on_probe_err)

    def load_default_sample(self) -> None:
        for name in ("a01_multiband_4b_8x8.hdr", "a01_ramp_16x16.hdr"):
            cand = self.project_root / "tests" / "fixtures" / "data" / name
            if cand.exists():
                self.open_dataset(cand)
                return
        self._notify(QMessageBox.Icon.Information, "提示", "未找到预置样例数据，请通过 [打开影像] 选择文件。")

    def _selected_layer(self) -> LayerItem | None:
        return getattr(self.layer_panel, "_selected_layer", None)

    def _on_layer_selected(self, layer: LayerItem | None) -> None:
        if layer is None:
            self.canvas.set_layer(None)
            self.canvas_b.set_layer(None)
            self.info_panel.clear_all()
            self.display_panel.clear()
            self.status_bar.showMessage("未选中图层")
            return
        client = self._client_for(layer)
        self.canvas.set_layer(layer, client)
        self._apply_pending_view(self.canvas, self._pending_view_v1)
        if self.canvas_b.isVisible():
            self.canvas_b.set_layer(layer, client)
            self._apply_pending_view(self.canvas_b, self._pending_view_v2)
        self.form_widget.set_input_path(layer.file_path)
        self.display_panel.set_layer(layer.band_count, layer.display)
        self.info_panel.set_rois(list(layer.rois))
        self.status_bar.showMessage(
            f"图层 {layer.name} | {layer.width}x{layer.height} | {layer.band_count} 波段 | "
            f"{layer.crs_display} | 空间模型={layer.metadata.get('spatial_model', 'affine')}"
        )

    def _apply_pending_view(self, canvas: ImageCanvas, view: ViewRecord | None) -> None:
        """图层是异步探测回来的，保存的视图必须等它真正被选中后才能套用。"""
        if view is None:
            return
        canvas.apply_view_state(
            {
                "view_id": view.view_id,
                "center_col": view.center_col,
                "center_row": view.center_row,
                "scale": view.scale,
            }
        )
        if canvas is self.canvas:
            self._pending_view_v1 = None
        else:
            self._pending_view_v2 = None

    def _on_layer_visibility_changed(self, layer: LayerItem, visible: bool) -> None:
        if self.canvas.current_layer and self.canvas.current_layer.layer_id == layer.layer_id:
            self.canvas.set_layer(layer if visible else None, self._client_for(layer))
        self._mark_dirty()

    def _on_layer_removed(self, layer_id: str) -> None:
        self._layer_seq.pop(layer_id, None)
        layer = None
        for cand in self.layer_panel.layers:
            if cand.layer_id == layer_id:
                layer = cand
                break
        if layer is not None:
            # 源变化即失效该源的全部缓存，避免删除后仍显示旧块
            self._cache.invalidate_source(str(layer.file_path))
        if self.canvas.current_layer and self.canvas.current_layer.layer_id == layer_id:
            self.canvas.clear()
        if self.canvas_b.current_layer and self.canvas_b.current_layer.layer_id == layer_id:
            self.canvas_b.clear()
        self._mark_dirty()

    def _on_display_mode_changed(self, layer: LayerItem) -> None:
        self.canvas.set_display(layer.display)
        self.display_panel.set_layer(layer.band_count, layer.display)
        self._mark_dirty()

    def _on_display_changed(self, settings: DisplaySettings) -> None:
        layer = self.canvas.current_layer
        if layer is None:
            return
        layer.display = settings
        self.canvas.set_display(settings)
        if self.canvas_b.current_layer and self.canvas_b.current_layer.layer_id == layer.layer_id:
            self.canvas_b.set_display(settings)
        self._mark_dirty()

    # ------------------------------------------------------------------ 查询
    def _on_hover_pixel(self, col: int, row: int, layer_id: str) -> None:
        layer = self.canvas.current_layer
        if layer is None or layer.layer_id != layer_id:
            layer = None
            for cand in self.layer_panel.layers:
                if cand.layer_id == layer_id:
                    layer = cand
                    break
        if layer is None:
            return
        self._hover_layer_id = layer_id

        def _ok(data: dict[str, Any]) -> None:
            if self._hover_layer_id != layer_id:
                return
            self.info_panel.show_pixel_query(data)

        def _err(msg: str) -> None:
            if self._hover_layer_id != layer_id:
                return
            self.info_panel.show_query_error(msg)

        self._client_for(layer).query_pixel_async(
            layer.file_path, row=row, col=col, view_crs_wkt=self.canvas.view_crs_wkt,
            on_success=_ok, on_error=_err,
        )

    def _on_map_query(self, x: float, y: float) -> None:
        layer = self.canvas.current_layer
        if layer is None:
            self.info_panel.show_map_error("没有选中图层")
            return
        convention = self.info_panel.cmb_conv.currentData() or "corner"

        def _ok(data: dict[str, Any]) -> None:
            self.info_panel.show_map_to_pixel(data)

        def _err(msg: str) -> None:
            self.info_panel.show_map_error(msg)

        self._client_for(layer).map_to_pixel_async(
            layer.file_path, x, y,
            view_crs_wkt=self.canvas.view_crs_wkt or layer.crs_wkt,
            convention=str(convention), on_success=_ok, on_error=_err,
        )

    def _on_stats_requested(self, mode: str) -> None:
        layer = self.canvas.current_layer
        if layer is None:
            self.info_panel.show_stats_error("没有选中图层")
            return
        bands = None
        try:
            bands = layer.display.selected_bands(layer.band_count)
        except ValueError:
            bands = None

        def _ok(data: dict[str, Any]) -> None:
            self.info_panel.show_stats(data)
            self.canvas.set_stats_source_label(stats_source_label(data))
            self.canvas.refresh()

        def _err(msg: str) -> None:
            self.info_panel.show_stats_error(msg)

        self._client_for(layer).compute_stats_async(
            layer.file_path, mode=mode, bands=bands, on_success=_ok, on_error=_err
        )

    # ------------------------------------------------------------------- ROI
    def _on_roi_changed(self) -> None:
        layer = self.canvas.current_layer
        if layer is not None:
            self.info_panel.set_rois(self.canvas.roi_list())
        self._mark_dirty()

    def _on_roi_delete(self, roi_id: str) -> None:
        self.canvas.remove_roi(roi_id)
        layer = self.canvas.current_layer
        if layer is not None:
            self.info_panel.set_rois(self.canvas.roi_list())

    def _on_roi_clear(self) -> None:
        self.canvas.load_rois([])
        layer = self.canvas.current_layer
        if layer is not None:
            layer.rois = []
        self.info_panel.set_rois([])
        self._mark_dirty()

    # ---------------------------------------------------------------- 双视图
    def set_dual_view(self, enabled: bool) -> None:
        self.canvas_b.setVisible(bool(enabled))
        if enabled:
            layer = self.canvas.current_layer
            if layer is not None:
                self.canvas_b.set_layer(layer, self._client_for(layer))
                self.canvas_b.apply_view_state(self.canvas.view_state())
        else:
            self.canvas_b.clear()

    def _on_sync_toggled(self, on: bool) -> None:
        self._sync_enabled = bool(on)

    def _on_peer_view_changed(self, state: dict[str, Any]) -> None:
        if not getattr(self, "_sync_enabled", True):
            return
        if not self.canvas_b.isVisible():
            return
        sender_id = state.get("view_id")
        target = self.canvas_b if sender_id == "V1" else self.canvas
        target.apply_view_state(state)

    def _on_view_crs_changed(self) -> None:
        epsg = self.cmb_view_crs.currentData()
        layer = self.canvas.current_layer
        if epsg is None:
            for canvas in (self.canvas, self.canvas_b):
                canvas.set_view_crs(None)
            self.status_bar.showMessage("视图 CRS：像元坐标")
            return
        if epsg == 0:
            if layer is None or not layer.crs_wkt:
                self._notify(
                    QMessageBox.Icon.Information,
                    "需要数据集 CRS",
                    "当前图层没有 CRS，无法用其自身作为视图 CRS。",
                )
                self.cmb_view_crs.setCurrentIndex(0)
                return
            for canvas in (self.canvas, self.canvas_b):
                canvas.set_view_crs(layer.crs_wkt)
            self.status_bar.showMessage(f"视图 CRS：数据集 CRS ({layer.crs_display})")
            return

        def _ok(data: dict[str, Any]) -> None:
            wkt = data.get("wkt")
            for canvas in (self.canvas, self.canvas_b):
                canvas.set_view_crs(wkt)
            self.status_bar.showMessage(f"视图 CRS：EPSG:{epsg}（跨 CRS 图层将重投影到该网格）")

        def _err(msg: str) -> None:
            self._notify(QMessageBox.Icon.Warning, "CRS 不可用", f"worker 无法构建 EPSG:{epsg}：{msg}")
            self.cmb_view_crs.setCurrentIndex(0)

        self.gdal_client.crs_wkt_async(int(epsg), on_success=_ok, on_error=_err)

    # ------------------------------------------------------------------ 项目
    def new_project(self) -> None:
        self.project_path = None
        for layer in list(self.layer_panel.layers):
            self.layer_panel.remove_layer(layer.layer_id)
        self.canvas.clear()
        self.canvas_b.clear()
        self.info_panel.clear_all()
        self.display_panel.clear()
        self._cache.clear()
        self._pending_active_layer = None
        self._pending_view_v1 = self._pending_view_v2 = None
        self._layer_seq.clear()
        self.missing_sources = []
        self._dirty = False
        self.status_bar.showMessage("已新建空项目")

    def build_project_spec(self) -> ProjectSpec:
        layers = []
        for order, layer in enumerate(self.layer_panel.layers):
            stored, is_rel = store_source(self.project_root, layer.file_path)
            layers.append(
                LayerRecord(
                    layer_id=layer.layer_id,
                    name=layer.name,
                    source=stored,
                    source_is_relative=is_rel,
                    visible=layer.visible,
                    order=order,
                    display=layer.display.model_copy(deep=True),
                    provider_hint=layer.provider_hint,
                    metadata_snapshot={
                        "width": layer.width,
                        "height": layer.height,
                        "count": layer.band_count,
                        "crs_epsg": layer.crs_epsg,
                        "spatial_model": layer.metadata.get("spatial_model"),
                    },
                )
            )
        rois: list[ROI] = []
        for layer in self.layer_panel.layers:
            rois.extend(layer.rois)
        views = [self._view_record(self.canvas), self._view_record(self.canvas_b)]
        state = self.saveState()
        layout_b64 = base64.b64encode(state.data()).decode("ascii") if state is not None else None
        active = self.canvas.current_layer
        return ProjectSpec(
            layers=layers,
            rois=rois,
            views=views,
            active_layer_id=active.layer_id if active else None,
            active_view_id="V1",
            layout_b64=layout_b64,
        )

    @staticmethod
    def _view_record(canvas: ImageCanvas) -> ViewRecord:
        st = canvas.view_state()
        return ViewRecord(
            view_id=str(st.get("view_id", canvas.view_id)),
            center_col=float(st.get("center_col", 0.0)),
            center_row=float(st.get("center_row", 0.0)),
            scale=float(st.get("scale", 1.0) or 1.0),
            view_crs_wkt=st.get("view_crs_wkt"),
            linked_views=["V2"] if canvas.view_id == "V1" else ["V1"],
        )

    def save_project_action(self, save_as: bool = False) -> bool:
        target = self.project_path
        if save_as or target is None:
            suggested = (target or (self.project_root / "未命名.spectra.json"))
            chosen, _ = QFileDialog.getSaveFileName(
                self, "保存项目", str(suggested), "Spectra 项目 (*.spectra.json);;JSON (*.json)"
            )
            if not chosen:
                return False
            target = Path(chosen)
        try:
            save_project(target, self.build_project_spec())
        except Exception as exc:
            self._notify(QMessageBox.Icon.Critical, "保存失败", f"项目无法保存：{exc}")
            return False
        self.project_path = target
        self._dirty = False
        self.status_bar.showMessage(f"项目已保存：{target}")
        return True

    def open_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开项目", str(self.project_root), "Spectra 项目 (*.spectra.json *.json)"
        )
        if not path:
            return
        if not self.open_project(path):
            QMessageBox.critical(self, "打开失败", "项目文件无法读取，详见状态栏。")
            return
        if self.missing_sources:
            self.prompt_relink_missing()

    def open_project(self, path: Path | str) -> bool:
        """程序化打开项目：不弹任何模态窗，缺失源记录在 missing_sources。"""
        target = Path(path)
        try:
            spec = load_project(target)
        except Exception as exc:
            self.status_bar.showMessage(f"项目无法读取：{exc}")
            return False

        for layer in list(self.layer_panel.layers):
            self.layer_panel.remove_layer(layer.layer_id)
        self.canvas.clear()
        self._cache.clear()
        self.project_path = target
        self._pending_active_layer = spec.active_layer_id
        self._pending_view_v1 = self._pending_view_v2 = None

        missing = find_missing(self.project_root, spec)
        self.missing_sources = missing

        rois_by_layer: dict[str, list[ROI]] = {}
        for roi in spec.rois:
            rois_by_layer.setdefault(str(roi.attributes.get("layer_id", "")), []).append(roi)

        loaded: list[str] = []
        base_seq = self._open_seq + 1
        for i, rec in enumerate(spec.ordered_layers()):
            path = resolve_source(self.project_root, rec.source, is_relative=rec.source_is_relative)
            if not path.exists():
                continue
            self.open_dataset(
                path,
                record=rec,
                rois=rois_by_layer.get(rec.layer_id, []),
                order=base_seq + i,
            )
            loaded.append(rec.layer_id)
        self._open_seq = base_seq + len(spec.layers)
        if self._pending_active_layer is None and loaded:
            self._pending_active_layer = loaded[0]

        # 视图与布局恢复（视图在图层探测回来后由 _on_layer_selected 套用）
        for view in spec.views:
            if view.view_id == "V1":
                self._pending_view_v1 = view
            elif view.view_id == "V2":
                self._pending_view_v2 = view
        if spec.layout_b64:
            with contextlib.suppress(Exception):
                self.restoreState(base64.b64decode(spec.layout_b64))
        self._dirty = False
        self.status_bar.showMessage(
            f"项目已打开：{target.name} | 图层 {len(spec.layers)}（成功关联 {len(loaded)}, "
            f"待重新关联 {len(self.missing_sources)}）"
        )
        return True

    def prompt_relink_missing(self) -> bool:
        """交互式重新关联：模态对话框只由用户入口调用，程序化打开不会触发。"""
        if self.project_path is None or not self.missing_sources:
            return False
        dlg = _RelinkDialog(self.missing_sources, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        resolved = {lid: p for lid, p in dlg.mapping.items() if str(p)}
        if not resolved:
            return False
        spec = relink(self.project_root, load_project(self.project_path), resolved)
        save_project(self.project_path, spec)
        return self.open_project(self.project_path)

    def _mark_dirty(self) -> None:
        self._dirty = True

    # ------------------------------------------------------------------ 任务
    def submit_task(self, spec: TaskSpec) -> None:
        is_rasterio = "rasterio" in spec.capability_id or "rio" in spec.capability_id
        client = self.rasterio_client if is_rasterio else self.gdal_client
        self._active_task_client = client
        task_id = spec.task_id

        old_cleanup = self._task_subscriptions.pop(task_id, None)
        if old_cleanup:
            old_cleanup()

        self.task_panel.on_task_started(spec)
        self.status_bar.showMessage(f"正在执行任务: {spec.capability_id} [{task_id}]…")

        def _cleanup() -> None:
            for sig, slot in (
                (client.sig_progress, _on_prog),
                (client.sig_log, _on_log),
                (client.sig_result, _on_res),
                (client.sig_error, _on_err),
            ):
                with contextlib.suppress(RuntimeError, TypeError):
                    sig.disconnect(slot)
            self._task_subscriptions.pop(task_id, None)

        def _on_prog(ratio: float, phase: str, msg: str) -> None:
            self.task_panel.on_progress(ratio, phase, msg)

        def _on_log(level: str, msg: str, stream: str) -> None:
            self.task_panel.on_log(level, msg, stream)

        def _on_res(result: TaskResult) -> None:
            if result.task_id != task_id:
                return
            _cleanup()
            self.task_panel.on_task_completed(result)
            self._active_task_client = None
            if result.status.value == "succeeded":
                self.status_bar.showMessage(f"任务成功: {result.task_id}")
                # 输出变化 -> 失效相关缓存
                for out in result.outputs:
                    uri = out.get("uri", "")
                    if uri:
                        self._cache.invalidate_source(_uri_to_path(uri))
                self.load_result_to_layer(result)
            elif result.status.value == "cancelled":
                self.status_bar.showMessage(f"任务已取消: {result.task_id}")
            else:
                self.status_bar.showMessage(f"任务失败: {result.task_id}")

        def _on_err(code: str, msg: str) -> None:
            _cleanup()
            self.task_panel.on_task_error(code, msg)
            self._active_task_client = None
            self.status_bar.showMessage(f"错误: {code} {msg}")

        self._task_subscriptions[task_id] = _cleanup
        client.sig_progress.connect(_on_prog)
        client.sig_log.connect(_on_log)
        client.sig_result.connect(_on_res)
        client.sig_error.connect(_on_err)
        client.submit_task_async(spec)

    def _on_cancel_task(self, task_id: str) -> None:
        if self._active_task_client is not None:
            self._active_task_client.cancel_task(task_id)

    def load_result_to_layer(self, result: TaskResult) -> None:
        if not result.outputs:
            return
        uri = result.outputs[0].get("uri", "")
        if not uri:
            return
        self.open_dataset(_uri_to_path(uri))
        self.tab_left.setCurrentIndex(0)

    # ------------------------------------------------------------------ 其它
    def _on_capability_selected(self, cap: Any) -> None:
        self.form_widget.load_capability(cap)
        layer = self._selected_layer()
        if layer:
            self.form_widget.set_input_path(layer.file_path)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 Spectra Workbench",
            "<h3>Spectra Workbench 遥感与光谱计算工作台</h3>"
            "<p>界面进程零原生地理依赖；坐标运算、窗口读取、统计与重投影全部在 worker 子进程完成。</p>"
            "<p>P3：双视图联动、缓存预算与概览、原值/物理值/显示值分离、坐标查询与 ROI、项目保存重开。</p>",
        )

    def closeEvent(self, event: Any) -> None:
        for canvas in (self.canvas, self.canvas_b):
            canvas.clear()
            canvas.close()
        for cleanup in list(self._task_subscriptions.values()):
            with contextlib.suppress(Exception):
                cleanup()
        self._task_subscriptions.clear()
        for client in self._clients.values():
            if client.is_alive:
                client.stop(timeout=1.0)
        super().closeEvent(event)


def _uri_to_path(uri: str) -> str:
    if uri.startswith("file:"):
        return str(Path(url2pathname(urlparse(uri).path)).resolve())
    return str(Path(uri).resolve())
