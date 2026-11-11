"""可搜索工具箱面板（P2-04 / P2-05）。

特性：
1. 载入官方基准全量能力（443 项），支持模糊关键词实时搜索。
2. 引擎筛选（全部 / GDAL / Rasterio）。
3. 重点高亮并优先呈现平台已接通的核心算法（GDAL 转换、Warp、Rasterio 转换）。
4. 其它能力完整保留并展示状态标签（[ready], [blocked], [missing_dependency]）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from spectra_workbench.domain.capability_wiring import CONNECTED_CAPABILITIES
from spectra_workbench.domain.models import CapabilityDescriptor
from spectra_workbench.registry.catalog import get_default_registry


class ToolboxPanel(QWidget):
    """可搜索工具箱控件。"""

    sig_capability_selected = Signal(object)  # CapabilityDescriptor

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._registry = get_default_registry()
        self._all_caps: list[CapabilityDescriptor] = self._registry.filter()
        self._init_ui()
        self._populate_tree()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # 搜索与过滤栏
        search_row = QHBoxLayout()
        search_row.setSpacing(4)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("🔍 搜索算法、CLI 或驱动 (如 warp, tif)...")
        self.txt_search.setClearButtonEnabled(True)
        self.txt_search.textChanged.connect(self._on_filter_changed)
        search_row.addWidget(self.txt_search, stretch=1)

        self.cmb_provider = QComboBox()
        self.cmb_provider.addItems(["全部引擎", "GDAL", "Rasterio", "扩展引擎"])
        self.cmb_provider.currentTextChanged.connect(self._on_filter_changed)
        search_row.addWidget(self.cmb_provider)

        layout.addLayout(search_row)

        # 树形展示控件
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["算法工具名称 / 标识符", "状态"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.setIndentation(14)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection_changed)

        layout.addWidget(self.tree, stretch=1)

        # 底部统计摘要
        self.lbl_stats = QLabel()
        self.lbl_stats.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(self.lbl_stats)

    def _populate_tree(self) -> None:
        self.tree.clear()

        query = self.txt_search.text().strip().lower()
        prov_filter = self.cmb_provider.currentText()
        if prov_filter == "GDAL":
            target_prov = "gdal"
        elif prov_filter == "Rasterio":
            target_prov = "rasterio"
        elif prov_filter == "扩展引擎":
            target_prov = "extension"
        else:
            target_prov = None

        # 1. 过滤候选条目
        filtered: list[CapabilityDescriptor] = []
        for c in self._all_caps:
            if target_prov and c.engine.provider_id != target_prov:
                continue
            if query:
                text_corpus = f"{c.capability_id} {c.name} {c.description} {c.kind.value}".lower()
                if query not in text_corpus:
                    continue
            filtered.append(c)

        # 2. 分类构建树节点
        node_connected = QTreeWidgetItem(["⭐ 平台已接通核心算法 (Ready to Run)"])
        node_connected.setExpanded(True)

        node_gdal_algo = QTreeWidgetItem(["📦 GDAL 算法与经典 CLI (GDAL)"])
        node_rio_algo = QTreeWidgetItem(["🔧 Rasterio 算法与 rio CLI (Rasterio)"])
        node_drivers = QTreeWidgetItem(["💾 栅格与矢量驱动 (Drivers)"])
        node_extensions = QTreeWidgetItem(["🔌 可选引擎扩展登记 (未实现/待接入)"])

        total_shown = len(filtered)
        connected_count = 0

        for c in filtered:
            cid = c.capability_id
            is_conn = cid in CONNECTED_CAPABILITIES

            state_str = f"[{c.state.availability.value}]"
            if is_conn:
                state_str = "🟢 已接通"
                connected_count += 1
            elif c.engine.provider_id == "extension":
                state_str = "🔴 扩展未实现"
            elif c.state.availability.value == "blocked":
                state_str = "🟡 仅CLI/未桥接"
            elif c.state.availability.value == "missing_dependency":
                state_str = "🔴 依赖缺失"

            item = QTreeWidgetItem([f"{c.name} ({c.capability_id})", state_str])
            item.setData(0, Qt.ItemDataRole.UserRole, c)
            item.setToolTip(
                0,
                f"【{c.name}】\n标识符: {c.capability_id}\n引擎: {c.engine.provider_id.upper()}\n状态: {state_str}\n\n{c.description or '暂无详细描述'}",
            )
            item.setToolTip(1, f"引擎: {c.engine.provider_id.upper()}\n状态: {state_str}")

            if is_conn:
                node_connected.addChild(item)
            elif c.engine.provider_id == "extension":
                node_extensions.addChild(item)
            elif c.kind.value == "driver":
                node_drivers.addChild(item)
            elif c.engine.provider_id == "gdal":
                node_gdal_algo.addChild(item)
            else:
                node_rio_algo.addChild(item)

        if node_connected.childCount() > 0:
            self.tree.addTopLevelItem(node_connected)
        if node_gdal_algo.childCount() > 0:
            self.tree.addTopLevelItem(node_gdal_algo)
        if node_rio_algo.childCount() > 0:
            self.tree.addTopLevelItem(node_rio_algo)
        if node_drivers.childCount() > 0:
            self.tree.addTopLevelItem(node_drivers)
        if node_extensions.childCount() > 0:
            self.tree.addTopLevelItem(node_extensions)

        # 展开包含搜索结果的节点
        if query:
            self.tree.expandAll()

        self.lbl_stats.setText(
            f"已展示: {total_shown} 项 / 基准总计: {len(self._all_caps)} 项 | 平台已接通: {connected_count} 项"
        )

    def _on_filter_changed(self) -> None:
        self._populate_tree()

    def _on_tree_selection_changed(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        cap = items[0].data(0, Qt.ItemDataRole.UserRole)
        if isinstance(cap, CapabilityDescriptor):
            self.sig_capability_selected.emit(cap)

    def select_capability(self, capability_id: str) -> None:
        """根据 capability_id 自动定位并选中树节点。"""

        def _search_node(node: QTreeWidgetItem | None) -> bool:
            if node is None:
                return False
            cap = node.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(cap, CapabilityDescriptor) and cap.capability_id == capability_id:
                self.tree.setCurrentItem(node)
                return True
            return any(_search_node(node.child(i)) for i in range(node.childCount()))

        for i in range(self.tree.topLevelItemCount()):
            if _search_node(self.tree.topLevelItem(i)):
                break
