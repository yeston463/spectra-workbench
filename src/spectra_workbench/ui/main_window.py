"""主窗口组件（P3-01 / P3-04）。

集成：
1. 菜单栏与工具栏。
2. 影像画布（基于 pyqtgraph，支持缩放、平移、灰度/RGB 渲染与分块抽样）。
3. 停靠窗口：图层面板、可搜索工具箱、动态参数表单、任务进度与日志面板。
4. 跨进程 WorkerClient 调度：无原生地理库导入，通过 JSONL 驱动后台 GDAL/Rasterio 工作进程。
5. 任务成果自动加载与错误提示。
"""

from __future__ import annotations

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
    QDockWidget,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QWidget,
)

from spectra_workbench.domain.models import TaskResult, TaskSpec
from spectra_workbench.scheduler.client import QtWorkerClient
from spectra_workbench.ui.canvas.image_canvas import ImageCanvas
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget
from spectra_workbench.ui.models.layer import LayerItem
from spectra_workbench.ui.panels.layer_panel import LayerPanel
from spectra_workbench.ui.panels.task_panel import TaskPanel
from spectra_workbench.ui.panels.toolbox_panel import ToolboxPanel


class MainWindow(QMainWindow):
    """Spectra Workbench 桌面主窗口。"""

    def __init__(
        self,
        project_root: Path | str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.project_root = Path(project_root or Path.cwd()).resolve()

        # 跨进程 Worker 客户端字典（无原生地理库导入）
        self._clients: dict[str, QtWorkerClient] = {}
        self._active_task_client: QtWorkerClient | None = None
        self._task_subscriptions: dict[str, Callable[[], None]] = {}

        _app = QApplication.instance()
        if isinstance(_app, QApplication):
            ensure_application_fonts(_app)
        self._init_ui()
        self._wire_signals()

    def _get_client(self, provider_id: str) -> QtWorkerClient:
        """获取或按需懒加载指定引擎的 QtWorkerClient。"""
        if provider_id not in self._clients:
            client = QtWorkerClient(
                provider_id=provider_id,
                project_root=self.project_root,
                parent=self,
            )
            self._clients[provider_id] = client
        return self._clients[provider_id]

    @property
    def gdal_client(self) -> QtWorkerClient:
        return self._get_client("gdal")

    @property
    def rasterio_client(self) -> QtWorkerClient:
        return self._get_client("rasterio")

    def _init_ui(self) -> None:
        self.setWindowTitle("Spectra Workbench · 遥感与光谱计算工作台 (P3)")
        self.resize(1366, 850)

        # 1. 中心画布
        self.canvas = ImageCanvas(self)
        self.setCentralWidget(self.canvas)

        # 2. 状态栏
        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪 | 界面进程严格保持无 GDAL/Rasterio 原生依赖导入")

        # 3. 停靠窗口配置
        self.setDockOptions(
            QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AllowNestedDocks
        )

        # 左侧 Dock：工具箱与图层面板分页
        self.dock_left = QDockWidget("数据与工具 (Explorer)", self)
        self.dock_left.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)

        self.tab_left = QTabWidget()
        self.toolbox_panel = ToolboxPanel()
        self.layer_panel = LayerPanel()
        self.tab_left.addTab(self.layer_panel, "📑 图层管理 (Layers)")
        self.tab_left.addTab(self.toolbox_panel, "🧰 算法工具箱 (Toolbox)")
        self.dock_left.setWidget(self.tab_left)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.dock_left)

        # 右侧 Dock：动态参数表单
        self.dock_right = QDockWidget("参数配置 (Parameters)", self)
        self.dock_right.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self.form_widget = CapabilityFormWidget()
        self.form_widget.set_project_root(self.project_root)
        self.dock_right.setWidget(self.form_widget)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock_right)

        # 底部 Dock：任务监控与日志
        self.dock_bottom = QDockWidget("任务监控与日志 (Tasks & Logs)", self)
        self.dock_bottom.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea | Qt.DockWidgetArea.TopDockWidgetArea)
        self.task_panel = TaskPanel()
        self.dock_bottom.setWidget(self.task_panel)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.dock_bottom)

        # 4. 菜单栏与工具栏
        self._create_menus_and_toolbars()

    def _create_menus_and_toolbars(self) -> None:
        # 菜单
        menu_bar = self.menuBar()

        # [文件]
        menu_file = menu_bar.addMenu("文件 (&F)")

        act_open = QAction("打开影像 (&O)...", self)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self.open_file_dialog)
        menu_file.addAction(act_open)

        act_sample = QAction("加载 A01 合成多光谱样例 (&S)", self)
        act_sample.triggered.connect(self.load_default_sample)
        menu_file.addAction(act_sample)

        menu_file.addSeparator()

        act_exit = QAction("退出 (&X)", self)
        act_exit.setShortcut(QKeySequence("Alt+F4"))
        act_exit.triggered.connect(self.close)
        menu_file.addAction(act_exit)

        # [视图]
        menu_view = menu_bar.addMenu("视图 (&V)")

        act_fit = QAction("自适应全图 (&F)", self)
        act_fit.triggered.connect(self.canvas.fit_extent)
        menu_view.addAction(act_fit)

        act_zoom_in = QAction("放大 (&I)", self)
        act_zoom_in.triggered.connect(self.canvas.zoom_in)
        menu_view.addAction(act_zoom_in)

        act_zoom_out = QAction("缩小 (&O)", self)
        act_zoom_out.triggered.connect(self.canvas.zoom_out)
        menu_view.addAction(act_zoom_out)

        act_refresh = QAction("刷新画布 (&R)", self)
        act_refresh.setShortcut(QKeySequence.StandardKey.Refresh)
        act_refresh.triggered.connect(self.canvas.refresh)
        menu_view.addAction(act_refresh)

        # [帮助]
        menu_help = menu_bar.addMenu("帮助 (&H)")
        act_about = QAction("关于工作台 (&A)", self)
        act_about.triggered.connect(self._show_about)
        menu_help.addAction(act_about)

        # 工具栏
        toolbar = QToolBar("主快捷栏", self)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        toolbar.addAction(act_open)
        toolbar.addAction(act_sample)
        toolbar.addSeparator()
        toolbar.addAction(act_fit)
        toolbar.addAction(act_zoom_in)
        toolbar.addAction(act_zoom_out)
        toolbar.addAction(act_refresh)

    def _wire_signals(self) -> None:
        # 工具箱选中能力 -> 参数面板动态呈现
        self.toolbox_panel.sig_capability_selected.connect(self._on_capability_selected)

        # 图层面板交互
        self.layer_panel.sig_layer_selected.connect(self._on_layer_selected)
        self.layer_panel.sig_layer_visibility_changed.connect(self._on_layer_visibility_changed)
        self.layer_panel.sig_layer_removed.connect(self._on_layer_removed)
        self.layer_panel.sig_display_mode_changed.connect(self._on_display_mode_changed)
        self.layer_panel.sig_open_file_requested.connect(self.open_file_dialog)

        # 参数面板提交任务
        self.form_widget.sig_task_submitted.connect(self.submit_task)

        # 任务面板取消任务与加载成果
        self.task_panel.sig_cancel_requested.connect(self._on_cancel_task)
        self.task_panel.sig_load_result_requested.connect(self.load_result_to_layer)

    def _on_capability_selected(self, cap: Any) -> None:
        self.form_widget.load_capability(cap)
        # 若当前图层列表有选中图层，自动回填输入文件路径
        selected_layer = self.layer_panel._selected_layer
        if selected_layer:
            self.form_widget.set_input_path(selected_layer.file_path)

    def _on_layer_selected(self, layer: LayerItem | None) -> None:
        if layer is not None:
            client = self.rasterio_client if "rasterio" in layer.driver.lower() else self.gdal_client
            self.canvas.set_layer(layer, client)
            self.form_widget.set_input_path(layer.file_path)
            self.status_bar.showMessage(f"当前选中图层: {layer.name} | 尺寸: {layer.width}x{layer.height} | {layer.crs_display}")
        else:
            self.canvas.set_layer(None)
            self.status_bar.showMessage("未选中图层")

    def _on_layer_visibility_changed(self, layer: LayerItem, visible: bool) -> None:
        if self.canvas.current_layer and self.canvas.current_layer.layer_id == layer.layer_id:
            client = self.rasterio_client if "rasterio" in layer.driver.lower() else self.gdal_client
            self.canvas.set_layer(layer if visible else None, client)

    def _on_layer_removed(self, layer_id: str) -> None:
        if self.canvas.current_layer and self.canvas.current_layer.layer_id == layer_id:
            self.canvas.clear()

    def _on_display_mode_changed(self, layer: LayerItem) -> None:
        if self.canvas.current_layer and self.canvas.current_layer.layer_id == layer.layer_id:
            self.canvas.refresh()

    def open_file_dialog(self) -> None:
        """弹出文件选择对话框打开影像。"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开遥感栅格影像",
            str(self.project_root),
            "遥感栅格影像 (*.tif *.tiff *.dat *.img *.hdr *.png *.jpg);;所有文件 (*.*)",
        )
        if path:
            self.open_dataset(path)

    def open_dataset(self, path: Path | str) -> None:
        """通过后台 Worker 探测并加载栅格影像（零原生地理库导入）。"""
        p = Path(path).resolve()
        if not p.exists():
            QMessageBox.critical(self, "文件未找到", f"指定影像文件不存在:\n{p}")
            return

        self.status_bar.showMessage(f"正在通过后台 Worker 探测元数据: {p.name}...")

        # 默认优先使用 GDAL 运行时探测，若为特定格式亦可使用 Rasterio
        client = self.gdal_client

        def _on_probed(meta: dict[str, Any]) -> None:
            name = p.stem
            layer = LayerItem(name=name, file_path=p, metadata=meta)
            self.layer_panel.add_layer(layer, select=True)
            self.canvas.set_layer(layer, client)
            self.status_bar.showMessage(f"成功加载影像图层: {name} ({layer.width}x{layer.height}, {layer.band_count} 波段)")

        def _on_probe_err(err: str) -> None:
            QMessageBox.critical(self, "影像打开失败", f"后台 Worker 探测影像元数据失败:\n{err}")
            self.status_bar.showMessage(f"探测失败: {err}")

        client.probe_dataset_async(
            p,
            on_success=_on_probed,
            on_error=_on_probe_err,
        )

    def load_default_sample(self) -> None:
        """加载 A01 合成多光谱影像样例。"""
        sample_hdr = self.project_root / "tests" / "fixtures" / "data" / "a01_multiband_4b_8x8.hdr"
        if not sample_hdr.exists():
            # 尝试 ramp 样例
            sample_hdr = self.project_root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.hdr"
        if sample_hdr.exists():
            self.open_dataset(sample_hdr)
        else:
            QMessageBox.information(self, "提示", "未找到预置样例数据，请通过 [打开影像] 选择任意 GeoTIFF 或 ENVI 文件。")

    def submit_task(self, spec: TaskSpec) -> None:
        """异步向后台 Worker 提交计算任务。"""
        # 确定 Provider
        is_rasterio = "rasterio" in spec.capability_id or "rio" in spec.capability_id
        client = self.rasterio_client if is_rasterio else self.gdal_client
        self._active_task_client = client

        task_id = spec.task_id

        # 若已有同 ID 的活跃订阅，先进行清理
        old_cleanup = self._task_subscriptions.pop(task_id, None)
        if old_cleanup:
            old_cleanup()

        # 启动任务面板监听
        self.task_panel.on_task_started(spec)
        self.status_bar.showMessage(f"正在执行任务: {spec.capability_id} [{task_id}]...")

        def _cleanup() -> None:
            with contextlib.suppress(RuntimeError, TypeError):
                client.sig_progress.disconnect(_on_prog)
            with contextlib.suppress(RuntimeError, TypeError):
                client.sig_log.disconnect(_on_log)
            with contextlib.suppress(RuntimeError, TypeError):
                client.sig_result.disconnect(_on_res)
            with contextlib.suppress(RuntimeError, TypeError):
                client.sig_error.disconnect(_on_err)
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
                self.status_bar.showMessage(f"✅ 任务成功: {result.task_id}")
                # 自动提示并加载成果图层
                self.load_result_to_layer(result)
            elif result.status.value == "cancelled":
                self.status_bar.showMessage(f"⚠️ 任务已取消: {result.task_id}")
            else:
                self.status_bar.showMessage(f"❌ 任务失败: {result.task_id}")

        def _on_err(code: str, msg: str) -> None:
            _cleanup()
            self.task_panel.on_task_error(code, msg)
            self._active_task_client = None
            self.status_bar.showMessage(f"❌ 错误: {code}")

        self._task_subscriptions[task_id] = _cleanup

        # 单次按任务绑定的信号连接
        client.sig_progress.connect(_on_prog)
        client.sig_log.connect(_on_log)
        client.sig_result.connect(_on_res)
        client.sig_error.connect(_on_err)

        client.submit_task_async(spec)

    def _on_cancel_task(self, task_id: str) -> None:
        if self._active_task_client is not None:
            self._active_task_client.cancel_task(task_id)

    def load_result_to_layer(self, result: TaskResult) -> None:
        """将任务成功输出的成果文件载入图层管理与画布。"""
        if not result.outputs:
            return
        out_info = result.outputs[0]
        uri = out_info.get("uri", "")
        if not uri:
            return

        if uri.startswith("file:"):
            parsed = urlparse(uri)
            target_path = Path(url2pathname(parsed.path)).resolve()
        else:
            target_path = Path(uri).resolve()

        if target_path.exists():
            self.open_dataset(target_path)
            # 自动切换到图层面板选项卡
            self.tab_left.setCurrentIndex(0)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 Spectra Workbench",
            "<h3>Spectra Workbench 遥感与光谱计算工作台</h3>"
            "<p><b>版本</b>: 0.1.0-alpha (Milestone P3)</p>"
            "<p><b>架构约束</b>: GUI 进程零原生地理依赖导入 (osgeo / rasterio absent)，"
            "所有计算与分块窗口读取均由独立 Worker 子进程驱动。</p>"
            "<p><b>已接通算法</b>: GDAL 格式转换、GDAL Warp 重投影、Rasterio 格式转换。</p>"
            "<p><b>全量基准</b>: 443 项条目覆盖与动态状态审计。</p>",
        )

    def closeEvent(self, event: Any) -> None:
        """关闭窗口时妥善清理并退出后台 Worker 进程。"""
        self.canvas.clear()
        self.canvas.close()
        for cleanup in list(self._task_subscriptions.values()):
            with contextlib.suppress(Exception):
                cleanup()
        self._task_subscriptions.clear()
        for client in self._clients.values():
            if client.is_alive:
                client.stop(timeout=1.0)
        super().closeEvent(event)
