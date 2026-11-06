"""主窗口（P3-01 / P3-03 / P3-04 / P3-05）。

集成：菜单与工具栏、双画布（可联动）、图层/工具箱/信息/显示面板、任务与日志、
跨进程 worker 客户端（GUI 零原生地理依赖），以及项目保存与重开（含缺失源重定位）。
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import json
from collections.abc import Callable, Mapping
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
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
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
    PATH_ANCHOR_PROJECT_DIR,
    LayerRecord,
    MissingSource,
    ProjectSpec,
    ViewRecord,
    find_missing,
    has_ambiguous_legacy_relative_sources,
    load_project,
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
from spectra_workbench.ui.panels.metadata_panel import MetadataPanel
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
        base = getattr(self.parent(), "project_dir", Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "选择数据源", str(base))
        if path:
            edit.setText(path)

    def accept(self) -> None:  # noqa: D102
        self.mapping = {lid: Path(e.text()) for lid, e in self._edits.items() if e.text().strip()}
        super().accept()


class _LegacyConfirmDialog(QDialog):
    """旧版 1.0 项目路径基准确认对话框。"""

    def __init__(
        self,
        layers: list[LayerRecord],
        project_dir: Path,
        app_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("确认旧项目路径基准")
        self.resize(680, 380)
        self.mapping: dict[str, Path] = {}

        layout = QVBoxLayout(self)
        banner = QLabel(
            "⚠️ 检测到该项目为旧版格式（缺少路径基准标记），包含以下相对引用。\n"
            "为防止误读同名文件，请为每个图层明确数据源指向："
        )
        banner.setWordWrap(True)
        layout.addWidget(banner)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        box_layout = QVBoxLayout(container)

        self._combos: dict[str, QComboBox] = {}
        self._edits: dict[str, QLineEdit] = {}

        for ly in layers:
            grp = QGroupBox(f"图层: {ly.name} ({ly.source})")
            gly = QVBoxLayout(grp)

            p_cand = (project_dir / ly.source).resolve()
            a_cand = (app_root / ly.source).resolve()

            cmb = QComboBox()
            cmb.addItem(f"项目目录: {p_cand}" + (" [存在]" if p_cand.exists() else " [不存在]"), p_cand)
            cmb.addItem(f"程序根目录: {a_cand}" + (" [存在]" if a_cand.exists() else " [不存在]"), a_cand)
            cmb.addItem("自定义选择文件…", None)

            if p_cand.exists() and not a_cand.exists():
                cmb.setCurrentIndex(0)
            elif a_cand.exists() and not p_cand.exists():
                cmb.setCurrentIndex(1)
            else:
                cmb.setCurrentIndex(0)

            edit_custom = QLineEdit()
            edit_custom.setPlaceholderText("选择自定义文件路径")
            edit_custom.setVisible(False)

            def _on_idx(idx: int, c=cmb, e=edit_custom, p_dir=project_dir):
                if c.itemData(idx) is None:
                    e.setVisible(True)
                    fpath, _ = QFileDialog.getOpenFileName(self, "选择数据源", str(p_dir))
                    if fpath:
                        e.setText(fpath)
                else:
                    e.setVisible(False)

            cmb.currentIndexChanged.connect(_on_idx)
            gly.addWidget(cmb)
            gly.addWidget(edit_custom)
            box_layout.addWidget(grp)
            self._combos[ly.layer_id] = cmb
            self._edits[ly.layer_id] = edit_custom

        scroll.setWidget(container)
        layout.addWidget(scroll, stretch=1)

        btns = QHBoxLayout()
        btn_ok = QPushButton("确认迁移并打开")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self._on_accept)
        btn_cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        layout.addLayout(btns)

    def _on_accept(self) -> None:
        for lid, cmb in self._combos.items():
            val = cmb.currentData()
            if val is None:
                txt = self._edits[lid].text().strip()
                if not txt:
                    QMessageBox.warning(
                        self,
                        "请指定数据源路径",
                        f"图层 [{lid}] 选择了自定义文件，但未输入或选择有效路径。请先选择具体路径或改选已有候选。",
                    )
                    return
                self.mapping[lid] = Path(txt).resolve()
            else:
                self.mapping[lid] = Path(val).resolve()
        self.accept()


class MainWindow(QMainWindow):
    """Spectra Workbench 桌面主窗口。"""

    @property
    def project_dir(self) -> Path:
        if self.project_path is not None:
            return self.project_path.parent.resolve()
        return self.app_root

    def __init__(
        self,
        project_root: Path | str | None = None,
        parent: QWidget | None = None,
        cache_budget_bytes: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.app_root = Path(project_root or Path.cwd()).resolve()
        self.project_root = self.app_root
        self._project_records: dict[str, LayerRecord] = {}
        self._project_layer_order: list[str] = []
        self._project_rois: dict[str, list[ROI]] = {}
        self.is_pending_confirmation: bool = False
        self.legacy_status: str | None = None
        self._pending_legacy_spec: ProjectSpec | None = None
        self._pending_legacy_layers: list[LayerRecord] = []
        self._clients: dict[str, QtWorkerClient] = {}
        self._active_task_client: QtWorkerClient | None = None
        self._task_subscriptions: dict[str, Callable[[], None]] = {}
        # P4-05：保留最近一次提交的 TaskSpec，重试时按 next_attempt() 重发同一 task_id。
        self._last_task_spec: TaskSpec | None = None
        # 导出任务不自动加入图层树（导出不应改变当前工作流）。
        self._export_task_ids: set[str] = set()
        self._cache = TileCache(budget_bytes=cache_budget_bytes) if cache_budget_bytes else TileCache()
        self.project_path: Path | None = None
        self.missing_sources: list[MissingSource] = []
        self._pending_active_layer: str | None = None
        self._pending_view_v1: ViewRecord | None = None
        self._pending_view_v2: ViewRecord | None = None
        self._dirty = False
        self._hover_layer_id: str | None = None
        self._notifications: list[QMessageBox] = []
        self._open_seq = 0
        # 子数据集请求代际：同一容器内先后选中 A/B 时，
        # (项目代际, 图层 ID, 源文件) 三项完全相同，无法区分；
        # 只有单调递增的请求代际才能区分「上一轮 A」与「最后一轮 A」。
        self._subdataset_request_gen = 0
        self._subdataset_request_uri: str | None = None
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
        self.dock_left.setObjectName("dock_left")
        self.dock_left.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.tab_left = QTabWidget()
        self.toolbox_panel = ToolboxPanel()
        self.layer_panel = LayerPanel()
        self.tab_left.addTab(self.layer_panel, "图层管理")
        self.tab_left.addTab(self.toolbox_panel, "算法工具箱")
        self.dock_left.setWidget(self.tab_left)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_left)

        self.dock_right = QDockWidget("参数与查询 (Inspector)", self)
        self.dock_right.setObjectName("dock_right")
        self.dock_right.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.tab_right = QTabWidget()
        self.form_widget = CapabilityFormWidget()
        self.form_widget.set_project_root(self.project_root)
        self.info_panel = InfoPanel()
        self.display_panel = DisplayPanel()
        self.metadata_panel = MetadataPanel()
        self.tab_right.addTab(self.info_panel, "坐标与 ROI")
        self.tab_right.addTab(self.display_panel, "显示")
        self.tab_right.addTab(self.metadata_panel, "数据源元数据")
        self.tab_right.addTab(self.form_widget, "算法参数")
        self.dock_right.setWidget(self.tab_right)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_right)

        self.dock_bottom = QDockWidget("任务监控与日志", self)
        self.dock_bottom.setObjectName("dock_bottom")
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

        # P4-05 导出：视图截图（所见即所得）与带地理参考的数据集另存，两条独立通道。
        self.act_export_view = QAction("导出当前视图为图片… (&E)", self)
        self.act_export_view.triggered.connect(self.export_view_dialog)
        m_file.addAction(self.act_export_view)

        self.act_export_layer = QAction("导出当前图层为栅格… (&G)", self)
        self.act_export_layer.triggered.connect(self.export_layer_dialog)
        m_file.addAction(self.act_export_layer)
        m_file.addSeparator()

        act_exit = QAction("退出 (&X)", self)
        act_exit.triggered.connect(self.close)
        m_file.addAction(act_exit)

        m_task = mb.addMenu("任务 (&T)")
        self.act_cancel_task = QAction("取消当前任务 (&C)", self)
        self.act_cancel_task.triggered.connect(lambda: self._on_cancel_task())
        m_task.addAction(self.act_cancel_task)
        self.act_retry_task = QAction("重试上次任务 (&R)", self)
        self.act_retry_task.setShortcut(QKeySequence("Ctrl+R"))
        self.act_retry_task.triggered.connect(lambda: self._on_retry_task())
        m_task.addAction(self.act_retry_task)

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
        tb.setObjectName("main_toolbar")
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
        self.layer_panel.sig_missing_removed.connect(self._on_missing_layer_removed)
        self.layer_panel.sig_missing_relink_requested.connect(self._on_missing_relink_requested)
        self.layer_panel.sig_display_mode_changed.connect(self._on_display_mode_changed)
        self.layer_panel.sig_open_file_requested.connect(self.open_file_dialog)

        self.form_widget.sig_task_submitted.connect(self.submit_task)
        self.task_panel.sig_cancel_requested.connect(self._on_cancel_task)
        self.task_panel.sig_retry_requested.connect(self._on_retry_task)
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
        self.metadata_panel.sig_subdataset_selected.connect(self._on_subdataset_selected)

        # 双视图联动
        self.canvas.sig_view_changed.connect(self._on_peer_view_changed)
        self.canvas_b.sig_view_changed.connect(self._on_peer_view_changed)

    # ------------------------------------------------------------------ 图层
    def open_file_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开遥感栅格影像",
            str(self.project_dir),
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
            if layer.layer_id not in self._project_layer_order:
                self._project_layer_order.append(layer.layer_id)
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
            if client._closed:
                # 客户端已关闭（正在销毁窗口）：不再发起任何新探测
                return
            # 栅格探测失败：可能是矢量数据集，回退到矢量探测（P3-02）
            def _on_vector_ok(vec: dict[str, Any]) -> None:
                self._add_vector_layer(p, vec, record=record, rois=rois, order=seq)

            def _on_vector_err(vec_err: str) -> None:
                if client._closed:
                    return
                self._notify(
                    QMessageBox.Icon.Critical,
                    "数据打开失败",
                    f"worker 探测元数据失败（栅格与矢量均未成功）:\n{err}\n矢量探测：{vec_err}",
                )

            client.probe_vector_async(p, on_success=_on_vector_ok, on_error=_on_vector_err)

        client.probe_dataset_async(p, on_success=_on_probed, on_error=_on_probe_err)

    def _add_vector_layer(
        self,
        path: Path,
        vector_info: dict[str, Any],
        record: object | None = None,
        rois: list[ROI] | None = None,
        order: int | None = None,
    ) -> None:
        """把矢量数据集登记为图层（P3-02）。

        矢量**不做画布渲染**（渲染能力尚未实现），但图层本身可管理、可保存重开；
        元数据在「数据源元数据」面板中展示。
        """
        layer = LayerItem(
            name=(record.name if record else path.stem),  # type: ignore[attr-defined]
            file_path=path,
            metadata={
                "kind": "vector",
                "driver": vector_info.get("driver"),
                "layer_count": vector_info.get("layer_count", 0),
                "layers": [
                    {
                        "name": lyr.get("name"),
                        "geometry_type": lyr.get("geometry_type"),
                        "crs_epsg": lyr.get("crs_epsg"),
                        "feature_count": lyr.get("feature_count"),
                        "field_count": lyr.get("field_count"),
                    }
                    for lyr in (vector_info.get("layers") or [])
                ],
                "spatial_model": "vector",
                "renderable": False,
            },
        )
        if record is not None:
            layer.layer_id = record.layer_id  # type: ignore[attr-defined]
            layer.visible = record.visible  # type: ignore[attr-defined]
            layer.display = record.display.model_copy(deep=True)  # type: ignore[attr-defined]
            layer.provider_hint = record.provider_hint  # type: ignore[attr-defined]
            layer.rois = list(rois or [])
        if layer.layer_id not in self._project_layer_order:
            self._project_layer_order.append(layer.layer_id)
        self._client_for(layer)
        if order is not None:
            self._layer_seq[layer.layer_id] = order
        index = sum(1 for ly in self.layer_panel.layers if self._layer_seq.get(ly.layer_id, 0) < (order or 0))
        self.layer_panel.add_layer(layer, select=True, index=index)
        self._mark_dirty()
        # 矢量图层不进入影像画布：渲染能力未实现，明确说明而不提供假入口
        self.canvas.set_layer(None)
        self.canvas_b.set_layer(None)
        self.display_panel.clear()
        self.metadata_panel.show_vector(vector_info)
        self.status_bar.showMessage(
            f"矢量图层 {layer.name} | {layer.metadata.get('layer_count', 0)} 个子图层 | "
            "暂不支持矢量渲染（仅元数据读取）"
        )

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
        renderable = self.canvas.is_raster_renderable(layer)
        self.canvas.set_layer(layer, client)
        self._apply_pending_view(self.canvas, self._pending_view_v1)
        if self.canvas_b.isVisible():
            self.canvas_b.set_layer(layer, client)
            self._apply_pending_view(self.canvas_b, self._pending_view_v2)
        self.form_widget.set_input_path(layer.file_path)
        self.info_panel.set_rois(list(layer.rois))
        if renderable:
            self.display_panel.set_layer(layer.band_count, layer.display)
            self.status_bar.showMessage(
                f"图层 {layer.name} | {layer.width}x{layer.height} | {layer.band_count} 波段 | "
                f"{layer.crs_display} | 空间模型={layer.metadata.get('spatial_model', 'affine')}"
            )
        else:
            # 矢量图层：仅元数据读取。清除旧影像画面，禁用不适用的栅格操作，
            # 并保留明确说明（不提供假入口）。
            self.display_panel.clear()
            self.canvas.img_item.clear()
            self.canvas_b.img_item.clear()
            self.status_bar.showMessage(
                f"矢量图层 {layer.name} | "
                f"{layer.metadata.get('layer_count', 0)} 个子图层 | "
                "仅支持元数据读取（暂不支持矢量渲染）"
            )
        self._probe_source_metadata(layer)

    def _metadata_context(self, layer: LayerItem | None) -> tuple[str, str, str]:
        """构造元数据请求上下文：(项目代际, 图层 ID, 数据源引用)。

        回调必须校验上下文：图层切换/移除/打开项目/关窗都会让旧上下文失效，
        迟到的旧响应不得覆盖当前选择的元数据。
        """
        project_gen = str(getattr(self, "_open_seq", 0))
        layer_id = layer.layer_id if layer is not None else ""
        source = str(layer.file_path) if layer is not None else ""
        return project_gen, layer_id, source

    def _invalidate_subdataset_requests(self) -> None:
        """使所有在途的子数据集探测失效。

        在切换/清空数据源、图层移除、打开项目与关窗时调用：
        递增请求代际并清空当前请求 URI，此后到达的旧响应一律丢弃。
        """
        self._subdataset_request_gen += 1
        self._subdataset_request_uri = None

    def _metadata_context_valid(self, ctx: tuple[str, str, str]) -> bool:
        """判断某个请求上下文是否仍然有效。"""
        project_gen, layer_id, source = ctx
        if str(getattr(self, "_open_seq", 0)) != project_gen:
            return False
        current = self._selected_layer()
        if current is None:
            return False
        if current.layer_id != layer_id:
            return False
        return str(current.file_path) == source

    def _probe_source_metadata(self, layer: LayerItem) -> None:
        """探测并展示数据源元数据（P3-02）。

        矢量数据集走 probe_vector；其他容器数据集走 list_subdatasets。
        两条路径都通过 worker 完成，GUI 进程不导入任何原生地理库。
        回调带请求上下文校验：旧图层的迟到响应直接丢弃。
        """
        client = self._client_for(layer)
        # 切换数据源：此前在途的子数据集探测全部失效
        self._invalidate_subdataset_requests()
        self.metadata_panel.clear_all()
        path = str(layer.file_path)
        ctx = self._metadata_context(layer)

        def _on_vector(info: dict) -> None:
            if not self._metadata_context_valid(ctx):
                return
            self.metadata_panel.show_vector(info)

        def _on_vector_err(msg: str) -> None:
            if not self._metadata_context_valid(ctx):
                return
            self.metadata_panel.clear_vector(f"矢量元数据探测失败：{msg}")

        def _on_subs(info: dict) -> None:
            if not self._metadata_context_valid(ctx):
                return
            self.metadata_panel.show_subdatasets(info)

        def _on_subs_err(msg: str) -> None:
            if not self._metadata_context_valid(ctx):
                return
            self.metadata_panel.clear_subdatasets(f"子数据集枚举失败：{msg}")

        # 矢量：先按栅格探测会失败，用矢量探测单独走一条
        client.probe_vector_async(path, on_success=_on_vector, on_error=_on_vector_err)
        client.list_subdatasets_async(path, on_success=_on_subs, on_error=_on_subs_err)

    def _on_subdataset_selected(self, uri: str) -> None:
        """探测选中的子数据集。

        ``uri`` 是引擎返回的数据源引用（可能是 ``NETCDF:"file.nc":Band1``），
        原样交给 worker，**不做路径解析**。

        子数据集上下文来自**当前元数据选择**而不是画布当前图层：矢量图层本就不在
        画布中显示，其子数据集仍需可探测。
        """
        layer = self._selected_layer()
        if layer is None:
            self.metadata_panel.show_error("未选中图层")
            return
        client = self._client_for(layer)
        ctx = self._metadata_context(layer)

        # 捕获当次请求的 URI 与单调递增的请求代际。
        # 同一容器内先后选中 A 与 B 时，前三项上下文完全相同，
        # 只有代际能区分「上一轮 A」与「最后一轮 A」。
        self._subdataset_request_gen += 1
        request_gen = self._subdataset_request_gen
        self._subdataset_request_uri = uri

        def _still_current() -> bool:
            """该响应是否仍对应「当前选择」且是最新一轮请求。

            逐项校验：项目代际、图层 ID、数据源、当前选中 URI、最新请求代际。
            任一不符即丢弃——**被丢弃只 return，不得清空当前正确内容**。
            """
            if not self._metadata_context_valid(ctx):
                return False
            if request_gen != self._subdataset_request_gen:
                return False
            if self._subdataset_request_uri != uri:
                return False
            return self.metadata_panel.current_subdataset_uri() == uri

        def _on_ok(info: dict) -> None:
            if not _still_current():
                return
            self.metadata_panel.show_subdataset_detail(info)

        def _on_err(msg: str) -> None:
            if not _still_current():
                return
            self.metadata_panel.show_error(msg)

        client.probe_subdataset_async(uri, on_success=_on_ok, on_error=_on_err)

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
        # 图层移除：其子数据集探测结果不再有意义
        self._invalidate_subdataset_requests()
        self._layer_seq.pop(layer_id, None)
        rec = self._project_records.pop(layer_id, None)
        self._project_rois.pop(layer_id, None)
        if layer_id in self._project_layer_order:
            self._project_layer_order.remove(layer_id)
        self.missing_sources = [m for m in self.missing_sources if m.layer_id != layer_id]
        self.layer_panel.set_missing_layers(self.missing_sources)
        layer = None
        for cand in self.layer_panel.layers:
            if cand.layer_id == layer_id:
                layer = cand
                break
        if layer is not None:
            # 源变化即失效该源的全部缓存，避免删除后仍显示旧块
            self._cache.invalidate_source(str(layer.file_path))
        elif rec is not None:
            self._cache.invalidate_source(rec.source)
        if self.canvas.current_layer and self.canvas.current_layer.layer_id == layer_id:
            self.canvas.clear()
        if self.canvas_b.current_layer and self.canvas_b.current_layer.layer_id == layer_id:
            self.canvas_b.clear()
        self._mark_dirty()

    def _on_missing_layer_removed(self, layer_id: str) -> None:
        self._project_records.pop(layer_id, None)
        self._project_rois.pop(layer_id, None)
        if layer_id in self._project_layer_order:
            self._project_layer_order.remove(layer_id)
        self.missing_sources = [m for m in self.missing_sources if m.layer_id != layer_id]
        self.layer_panel.set_missing_layers(self.missing_sources)
        self._mark_dirty()
        self.status_bar.showMessage(f"已从项目中移除缺失图层记录: {layer_id}")

    def _on_missing_relink_requested(self, layer_id: str) -> None:
        target_missing = next((m for m in self.missing_sources if m.layer_id == layer_id), None)
        if target_missing is None:
            return
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            f"重新关联数据源 - {target_missing.name}",
            str(self.project_dir),
            "遥感栅格影像 (*.tif *.tiff *.dat *.img *.hdr *.vrt *.png *.jpg);;所有文件 (*.*)",
        )
        if not chosen:
            return
        self.relink_layer(layer_id, Path(chosen))

    def relink_layer(self, layer_id: str, new_path: Path | str) -> bool:
        p = Path(new_path).resolve()
        if not p.exists():
            self._notify(QMessageBox.Icon.Critical, "文件不存在", f"指定数据源不存在：{p}")
            return False
        rec = self._project_records.get(layer_id)
        stored, is_rel = store_source(self.project_dir, p)
        if rec is not None:
            rec = rec.model_copy(update={"source": stored, "source_is_relative": is_rel})
            self._project_records[layer_id] = rec
        self.missing_sources = [m for m in self.missing_sources if m.layer_id != layer_id]
        self.layer_panel.set_missing_layers(self.missing_sources)
        self.open_dataset(p, record=rec, rois=self._project_rois.get(layer_id, []))
        self._mark_dirty()
        self.status_bar.showMessage(f"图层已重新关联至：{p.name}")
        return True

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
    def _sync_roi_list(self) -> None:
        """把 ROI 列表与转换失败标记同步到信息面板。"""
        layer = self.canvas.current_layer
        if layer is None:
            return
        self.info_panel.set_rois(self.canvas.roi_list(), self.canvas.roi_transform_failed_ids())

    def _on_roi_changed(self) -> None:
        self._sync_roi_list()
        self._mark_dirty()

    def _on_roi_delete(self, roi_id: str) -> None:
        self.canvas.remove_roi(roi_id)
        self._sync_roi_list()

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
        self._project_records.clear()
        self._project_layer_order.clear()
        self._project_rois.clear()
        self.is_pending_confirmation = False
        self.legacy_status = None
        self._pending_legacy_spec = None
        self._pending_legacy_layers.clear()
        self.missing_sources = []
        self.layer_panel.set_missing_layers([])
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
        self._dirty = False
        self.status_bar.showMessage("已新建空项目")

    def build_project_spec(self, target_dir: Path | None = None) -> ProjectSpec:
        base_dir = (target_dir or self.project_dir).resolve()
        loaded_map = {layer.layer_id: layer for layer in self.layer_panel.layers}

        all_order: list[str] = []
        seen = set()
        for lid in self._project_layer_order:
            if lid not in seen:
                all_order.append(lid)
                seen.add(lid)
        for layer in self.layer_panel.layers:
            if layer.layer_id not in seen:
                all_order.append(layer.layer_id)
                seen.add(layer.layer_id)

        layers: list[LayerRecord] = []
        all_rois: list[ROI] = []

        for order_idx, lid in enumerate(all_order):
            if lid in loaded_map:
                layer = loaded_map[lid]
                stored, is_rel = store_source(base_dir, layer.file_path)
                rec = LayerRecord(
                    layer_id=layer.layer_id,
                    name=layer.name,
                    source=stored,
                    source_is_relative=is_rel,
                    visible=layer.visible,
                    order=order_idx,
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
                layers.append(rec)
                all_rois.extend(layer.rois)
            elif lid in self._project_records:
                old_rec = self._project_records[lid]
                orig_abs = resolve_source(self.project_dir, old_rec.source, is_relative=old_rec.source_is_relative)
                stored, is_rel = store_source(base_dir, orig_abs)
                rec = old_rec.model_copy(
                    update={
                        "source": stored,
                        "source_is_relative": is_rel,
                        "order": order_idx,
                    }
                )
                layers.append(rec)
                all_rois.extend(self._project_rois.get(lid, []))

        views = [self._view_record(self.canvas), self._view_record(self.canvas_b)]
        state = self.saveState()
        layout_b64 = base64.b64encode(state.data()).decode("ascii") if state is not None else None
        active = self.canvas.current_layer
        return ProjectSpec(
            path_anchor=PATH_ANCHOR_PROJECT_DIR,
            layers=layers,
            rois=all_rois,
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
        if self.is_pending_confirmation:
            self._notify(
                QMessageBox.Icon.Warning,
                "禁止保存",
                "当前项目包含待确认的旧版相对路径基准，请先完成迁移确认后再保存。",
            )
            return False
        target = self.project_path
        if save_as or target is None:
            suggested = (target or (self.project_dir / "未命名.spectra.json"))
            chosen, _ = QFileDialog.getSaveFileName(
                self, "保存项目", str(suggested), "Spectra 项目 (*.spectra.json);;JSON (*.json)"
            )
            if not chosen:
                return False
            target = Path(chosen).resolve()
        target_dir = target.parent.resolve()
        try:
            spec = self.build_project_spec(target_dir=target_dir)
            save_project(target, spec)
        except Exception as exc:
            self._notify(QMessageBox.Icon.Critical, "保存失败", f"项目无法保存：{exc}")
            return False
        self.project_path = target
        self._project_layer_order = [r.layer_id for r in spec.ordered_layers()]
        self._project_records = {r.layer_id: r for r in spec.layers}
        rois_by_layer: dict[str, list[ROI]] = {}
        for roi in spec.rois:
            rois_by_layer.setdefault(str(roi.attributes.get("layer_id", "")), []).append(roi)
        self._project_rois = rois_by_layer
        self.missing_sources = find_missing(target_dir, spec)
        self.layer_panel.set_missing_layers(self.missing_sources)
        self._dirty = False
        self.status_bar.showMessage(f"项目已保存：{target}")
        return True

    def open_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "打开项目", str(self.project_dir), "Spectra 项目 (*.spectra.json *.json)"
        )
        if not path:
            return
        res = self.open_project(path)
        if not res:
            QMessageBox.critical(self, "打开失败", "项目文件无法读取，详见状态栏。")
            return
        if (res == "pending_confirmation" or self.is_pending_confirmation) and not self.prompt_confirm_legacy_paths():
            return
        if self.missing_sources:
            self.prompt_relink_missing()

    def open_project(self, path: Path | str) -> bool | str:
        """程序化打开项目：不弹任何模态窗。

        若是旧版 1.0 且缺少路径基准标记的相对引用，返回 "pending_confirmation"；
        旧绝对引用直接按确定语义解析；
        正常打开返回 True；失败返回 False。
        """
        target = Path(path).resolve()
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

        self._project_records = {rec.layer_id: rec for rec in spec.layers}
        self._project_layer_order = [rec.layer_id for rec in spec.ordered_layers()]
        self._project_rois = {}
        for roi in spec.rois:
            self._project_rois.setdefault(str(roi.attributes.get("layer_id", "")), []).append(roi)

        project_dir = target.parent.resolve()

        if has_ambiguous_legacy_relative_sources(spec):
            self.is_pending_confirmation = True
            self.legacy_status = "pending_confirmation"
            self._pending_legacy_spec = spec
            self._pending_legacy_layers = [ly for ly in spec.layers if ly.source_is_relative]

            # 旧绝对引用按确定语义处理，不无故阻断已明确的绝对路径
            abs_loaded: list[str] = []
            base_seq = self._open_seq + 1
            for i, rec in enumerate(spec.ordered_layers()):
                if not rec.source_is_relative:
                    p = resolve_source(project_dir, rec.source, is_relative=False)
                    if p.exists():
                        self.open_dataset(
                            p,
                            record=rec,
                            rois=self._project_rois.get(rec.layer_id, []),
                            order=base_seq + i,
                        )
                        abs_loaded.append(rec.layer_id)
            self._open_seq = base_seq + len(spec.layers)
            # 打开项目后，此前发起的元数据探测全部失效（上下文代际已变）
            self._open_seq += 1
            self._invalidate_subdataset_requests()
            self.missing_sources = []
            self.layer_panel.set_missing_layers([])
            self._dirty = False
            self.status_bar.showMessage(
                f"项目待确认：{target.name} 缺少路径基准标记，包含 {len(self._pending_legacy_layers)} 个相对路径图层待确认"
            )
            return "pending_confirmation"

        self.is_pending_confirmation = False
        self.legacy_status = "ok"
        self._pending_legacy_spec = None
        self._pending_legacy_layers.clear()

        missing = find_missing(project_dir, spec)
        self.missing_sources = missing
        self.layer_panel.set_missing_layers(missing)

        loaded: list[str] = []
        base_seq = self._open_seq + 1
        for i, rec in enumerate(spec.ordered_layers()):
            p = resolve_source(project_dir, rec.source, is_relative=rec.source_is_relative)
            if not p.exists():
                continue
            self.open_dataset(
                p,
                record=rec,
                rois=self._project_rois.get(rec.layer_id, []),
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

    def prompt_confirm_legacy_paths(self) -> bool:
        """弹出模态对话框让用户确认旧项目相对路径基准。"""
        if not self.is_pending_confirmation or not self._pending_legacy_layers:
            return False
        dlg = _LegacyConfirmDialog(
            self._pending_legacy_layers,
            self.project_dir,
            self.app_root,
            self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.cancel_legacy_confirmation()
            return False
        return self.confirm_legacy_migration(dlg.mapping)

    def confirm_legacy_migration(self, mapping: Mapping[str, Path | str] | None = None) -> bool:
        """确认旧项目的路径关联与迁移，开始请求数据并允许后续保存。"""
        if not self.is_pending_confirmation or self._pending_legacy_spec is None:
            return False
        spec = self._pending_legacy_spec
        target_dir = self.project_dir
        mapping = mapping or {}

        base_seq = self._open_seq + 1
        loaded: list[str] = []
        missing: list[MissingSource] = []

        for i, rec in enumerate(spec.ordered_layers()):
            if rec.source_is_relative:
                if rec.layer_id in mapping:
                    p = Path(mapping[rec.layer_id]).resolve()
                else:
                    p = resolve_source(target_dir, rec.source, is_relative=True)
                stored, is_rel = store_source(target_dir, p)
                updated_rec = rec.model_copy(update={"source": stored, "source_is_relative": is_rel})
                self._project_records[rec.layer_id] = updated_rec
                if p.exists():
                    self.open_dataset(
                        p,
                        record=updated_rec,
                        rois=self._project_rois.get(rec.layer_id, []),
                        order=base_seq + i,
                    )
                    loaded.append(rec.layer_id)
                else:
                    missing.append(MissingSource(rec.layer_id, rec.name, stored, p))
            else:
                p = resolve_source(target_dir, rec.source, is_relative=False)
                if not p.exists() and not any(m.layer_id == rec.layer_id for m in missing):
                    missing.append(MissingSource(rec.layer_id, rec.name, rec.source, p))

        self._open_seq = base_seq + len(spec.layers)
        self.missing_sources = missing
        self.layer_panel.set_missing_layers(missing)
        self.is_pending_confirmation = False
        self.legacy_status = "confirmed"
        self._pending_legacy_spec = None
        self._pending_legacy_layers.clear()
        self._mark_dirty()
        self.status_bar.showMessage(
            f"已确认旧项目路径迁移：成功加载 {len(loaded)} 个图层，缺失 {len(missing)} 个图层"
        )
        return True

    def cancel_legacy_confirmation(self) -> None:
        """取消旧项目确认：清空当前打开的工作区状态，原项目文件不做任何写入或更改。"""
        self.new_project()
        self.legacy_status = "cancelled"
        self.status_bar.showMessage("已取消旧项目兼容迁移，原项目文件未作修改。")

    def prompt_relink_missing(self) -> bool:
        """交互式重新关联：模态对话框只由用户入口调用，程序化打开不会触发。"""
        if not self.missing_sources:
            return False
        dlg = _RelinkDialog(self.missing_sources, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        resolved = {lid: Path(p) for lid, p in dlg.mapping.items() if str(p).strip()}
        if not resolved:
            return False
        for lid, new_p in resolved.items():
            if new_p.exists():
                self.relink_layer(lid, new_p)
        if self.project_path is not None:
            save_project(self.project_path, self.build_project_spec())
            self._dirty = False
        return True

    def _mark_dirty(self) -> None:
        self._dirty = True

    # ------------------------------------------------------------------ 任务
    def submit_task(self, spec: TaskSpec) -> None:
        is_rasterio = "rasterio" in spec.capability_id or "rio" in spec.capability_id
        client = self.rasterio_client if is_rasterio else self.gdal_client
        self._active_task_client = client
        self._last_task_spec = spec
        task_id = spec.task_id

        old_cleanup = self._task_subscriptions.pop(task_id, None)
        if old_cleanup:
            old_cleanup()

        self.task_panel.on_task_started(spec)
        self.status_bar.showMessage(f"正在执行任务: {spec.capability_id} [{task_id}]…")

        cleaned = False

        def _cleanup() -> None:
            # 幂等：结果回调、重试前的 old_cleanup、关窗清理可能指向同一组闭包，
            # 重复 disconnect 会让 PySide6 抛 RuntimeWarning。
            nonlocal cleaned
            if cleaned:
                return
            cleaned = True
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
            is_export = task_id in self._export_task_ids
            self._export_task_ids.discard(task_id)
            if result.status.value == "succeeded":
                self.status_bar.showMessage(f"任务成功: {result.task_id}")
                # 输出变化 -> 失效相关缓存
                for out in result.outputs:
                    uri = out.get("uri", "")
                    if uri:
                        self._cache.invalidate_source(_uri_to_path(uri))
                if is_export:
                    # 导出只落盘，不改写当前图层树与视图。
                    target = result.outputs[0].get("uri", "") if result.outputs else ""
                    self.status_bar.showMessage(
                        f"导出完成：{_uri_to_path(target) if target else result.task_id}"
                    )
                else:
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

    def _on_cancel_task(self, task_id: str | None = None) -> None:
        tid = task_id or (self._last_task_spec.task_id if self._last_task_spec else None)
        if self._active_task_client is None or not tid:
            self.status_bar.showMessage("当前没有可取消的任务")
            return
        self._active_task_client.cancel_task(tid)

    def _on_retry_task(self, task_id: str | None = None) -> None:
        """用户主动重试：同一 task_id、attempt+1、原 TaskSpec 其余字段不变。

        不新建 task_id，也不改 overwrite / 输出路径等用户已确认的语义；
        底层 ``TaskSpec.next_attempt()`` 是唯一来源，避免 GUI 自己拼装重试契约。
        """
        spec = self._last_task_spec
        if spec is None:
            self.status_bar.showMessage("没有可重试的任务")
            return
        if task_id and task_id != spec.task_id:
            self.status_bar.showMessage(f"任务 {task_id} 不是最近一次提交的任务，拒绝重试")
            return
        if self._active_task_client is not None:
            self.status_bar.showMessage("任务仍在执行，请先取消或等待完成")
            return
        next_spec = spec.next_attempt()
        self.task_panel.append_log(
            "WARNING",
            f"重试任务 {spec.task_id}：attempt {spec.attempt} → {next_spec.attempt}（同一 task_id）",
            "scheduler",
        )
        self.status_bar.showMessage(
            f"重试任务 {spec.task_id}：第 {next_spec.attempt} 次尝试"
        )
        self.submit_task(next_spec)

    def load_result_to_layer(self, result: TaskResult) -> None:
        if not result.outputs:
            return
        first = result.outputs[0]
        uri = first.get("uri", "")
        if not uri:
            return
        # P4-04：JSON 统计报告等非栅格成果不进图层树（尝试打开只会报错），
        # 明确告知落盘位置，用户可在任务面板/资源管理器里查看。
        if first.get("kind") == "artifact" or first.get("driver") == "JSON":
            self.status_bar.showMessage(f"报告已生成（非栅格图层）：{_uri_to_path(uri)}")
            return
        self.open_dataset(_uri_to_path(uri))
        self.tab_left.setCurrentIndex(0)

    # ------------------------------------------------------- 导出（P4-05）
    def export_view_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出当前视图为图片",
            str(self.project_dir / "view_capture.png"),
            "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg)",
        )
        if path:
            self.export_view_image(path)

    def export_view_image(self, path: Path | str | None = None) -> Path | None:
        """导出当前视图的屏幕渲染结果，并写一份可复现的视图元数据 sidecar。

        图片本身**不含地理参考**（它是显示变换后的 RGBA 截图）；需要带坐标的
        数据请用 :meth:`export_layer_raster`。两条通道在元数据里明确区分。
        """
        if path is None:
            self.export_view_dialog()
            return None
        layer = self.canvas.current_layer or self._selected_layer()
        if layer is None:
            self._notify(QMessageBox.Icon.Warning, "无法导出视图", "当前没有已加载的图层。")
            return None
        target = Path(path).resolve()
        if target.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            self._notify(
                QMessageBox.Icon.Warning, "无法导出视图", "视图图片只支持 .png / .jpg / .jpeg。"
            )
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image = self.canvas.capture_view_image()
            if not image.save(str(target)):
                raise OSError(f"图片写入失败：{target}")
        except Exception as exc:
            self._notify(QMessageBox.Icon.Critical, "导出失败", f"视图无法导出：{exc}")
            return None

        metadata_snapshot = layer.metadata if isinstance(layer.metadata, dict) else {}
        meta = {
            "type": "spectra_view_capture",
            "schema_version": "1.0",
            "image": target.name,
            "exported_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "contains_georeference": False,
            "note": "屏幕渲染截图，不含地理参考；像元值与坐标请导出栅格数据集。",
            "image_size": [image.width(), image.height()],
            "supersampling": 2.0,
            "rendered": getattr(self.canvas.img_item, "image", None) is not None,
            "view": self.canvas.view_state(),
            "layer": {
                "name": layer.name,
                "source": str(layer.file_path),
                "width": metadata_snapshot.get("width"),
                "height": metadata_snapshot.get("height"),
                "band_count": getattr(layer, "band_count", None),
                "display": layer.display.to_dict(),
            },
            "layer_names": [ly.name for ly in self.layer_panel.layers],
        }
        sidecar = target.with_name(target.name + ".json")
        sidecar.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.status_bar.showMessage(f"视图已导出：{target}（元数据：{sidecar.name}）")
        return target

    def export_layer_dialog(self) -> None:
        path, selected = QFileDialog.getSaveFileName(
            self,
            "导出当前图层为栅格",
            str(self.project_dir / "export.tif"),
            "GeoTIFF (*.tif *.tiff);;ENVI (*.dat);;PNG (*.png);;JPEG (*.jpg *.jpeg)",
        )
        if not path:
            return
        preferred = "COG" if "云优化" in selected else None
        self.export_layer_raster(path, driver=_export_driver(Path(path), preferred))

    def export_layer_raster(
        self, path: Path | str, driver: str | None = None
    ) -> bool:
        """把当前图层另存为带地理参考的数据集，走与工具箱相同的真实 worker 通道。

        复用 ``gdal:translate`` 的参数契约与校验，不另造一套导出执行链路；
        成果只落盘、不自动加入图层树（提交前登记为 export 任务）。
        """
        layer = self._selected_layer() or self.canvas.current_layer
        if layer is None:
            self._notify(QMessageBox.Icon.Warning, "无法导出栅格", "当前没有选中的图层。")
            return False
        target = Path(path).resolve()
        source = Path(layer.file_path).resolve()
        # 目标若指向任一已加载图层（含本图层自身），带 overwrite 的提交会改写
        # 正在使用的数据源，必须明确拒绝，而不是等 GDAL 报错或静默覆盖。
        occupied = {ly.file_path for ly in self.layer_panel.layers}
        if target == source or target in occupied:
            used = "当前图层的数据源" if target == source else "其他已加载图层的数据源"
            self._notify(
                QMessageBox.Icon.Critical,
                "同源冲突",
                f"导出路径指向{used}，覆盖将破坏现有图层，操作已拒绝。",
            )
            return False
        fmt = driver or _export_driver(target)
        from spectra_workbench.registry.catalog import get_default_registry

        cap = get_default_registry().get("gdal:translate")
        if cap is None:
            self._notify(QMessageBox.Icon.Critical, "无法导出栅格", "注册表缺少 gdal:translate。")
            return False

        form = self.form_widget
        form.load_capability(cap)
        form.set_input_path(source)
        form.txt_output.setText(str(target))
        idx = form.cmb_format.findText(fmt, Qt.MatchFlag.MatchStartsWith)
        form.cmb_format.setCurrentIndex(idx if idx >= 0 else 0)
        form.chk_overwrite.setChecked(True)

        captured: list[TaskSpec] = []

        def _mark_export(spec: TaskSpec) -> None:
            captured.append(spec)
            self._export_task_ids.add(spec.task_id)

        form.sig_task_submitted.connect(_mark_export)
        try:
            form._on_submit_clicked()
        finally:
            with contextlib.suppress(RuntimeError, TypeError):
                form.sig_task_submitted.disconnect(_mark_export)
        if not captured:
            # 表单校验拒绝了导出（输入缺失 / 参数非法等），不要留下悬挂的 export 标记。
            self.status_bar.showMessage("导出未提交：表单校验未通过")
            return False
        self.status_bar.showMessage(f"正在导出栅格：{target}")
        return True

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
        # 关窗：在途的子数据集探测一律失效，迟到响应不得再改写界面
        self._invalidate_subdataset_requests()
        for canvas in (self.canvas, self.canvas_b):
            canvas.clear()
            canvas.close()
        for cleanup in list(self._task_subscriptions.values()):
            with contextlib.suppress(Exception):
                cleanup()
        self._task_subscriptions.clear()
        for client in self._clients.values():
            # 必须无条件回收请求线程：client.is_alive 只反映 worker 子进程状态，
            # worker 已退出时若跳过 stop()，仍在跑的守护请求线程会活到解释器退出，
            # 在 Windows 上随机触发 STATUS_STACK_BUFFER_OVERRUN（0xC0000409）异常退出码。
            with contextlib.suppress(Exception):
                client.stop(timeout=1.0)
        self._cache.clear()
        super().closeEvent(event)


def _uri_to_path(uri: str) -> str:
    if uri.startswith("file:"):
        return str(Path(url2pathname(urlparse(uri).path)).resolve())
    return str(Path(uri).resolve())


def _export_driver(path: Path, preferred: str | None = None) -> str:
    if preferred:
        return preferred
    return {
        ".dat": "ENVI",
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".tif": "GTiff",
        ".tiff": "GTiff",
    }.get(path.suffix.lower(), "GTiff")
