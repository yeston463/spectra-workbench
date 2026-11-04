"""图层管理面板（P3-01 / P3-04）。

功能：
1. 影像图层树/列表管理与可见性控制。
2. 选中图层元数据详情展示（驱动、尺寸、波段数、数据类型、CRS、地理范围）。
3. 灰度模式与 RGB 伪彩/真彩合成波段配置及实时切换。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from spectra_workbench.ui.models.layer import LayerItem


class LayerPanel(QWidget):
    """图层列表与显示属性面板。"""

    sig_layer_selected = Signal(object)  # LayerItem | None
    sig_layer_visibility_changed = Signal(object, bool)  # LayerItem, visible
    sig_layer_removed = Signal(str)  # layer_id
    sig_display_mode_changed = Signal(object)  # LayerItem
    sig_open_file_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layers: list[LayerItem] = []
        self._selected_layer: LayerItem | None = None

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # 1. 顶部操作栏
        top_bar = QHBoxLayout()
        self.btn_open = QPushButton("📂 打开影像...")
        self.btn_open.clicked.connect(self.sig_open_file_requested.emit)
        top_bar.addWidget(self.btn_open)

        self.btn_remove = QPushButton("🗑️ 移除")
        self.btn_remove.setEnabled(False)
        self.btn_remove.clicked.connect(self._on_remove_clicked)
        top_bar.addWidget(self.btn_remove)

        layout.addLayout(top_bar)

        # 2. 图层列表
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.list_widget, stretch=1)

        # 3. 显示模式设置箱
        self.grp_display = QGroupBox("显示与波段合成 (Display)")
        self.grp_display.setEnabled(False)
        disp_layout = QVBoxLayout(self.grp_display)
        disp_layout.setContentsMargins(6, 6, 6, 6)
        disp_layout.setSpacing(6)

        mode_row = QHBoxLayout()
        self.rad_gray = QRadioButton("单波段灰度")
        self.rad_rgb = QRadioButton("RGB 彩色合成")
        self.rad_gray.setChecked(True)

        self.btn_grp_mode = QButtonGroup(self)
        self.btn_grp_mode.addButton(self.rad_gray, 0)
        self.btn_grp_mode.addButton(self.rad_rgb, 1)
        self.btn_grp_mode.idToggled.connect(self._on_mode_toggled)

        mode_row.addWidget(self.rad_gray)
        mode_row.addWidget(self.rad_rgb)
        disp_layout.addLayout(mode_row)

        # 灰度选择
        self.widget_gray = QWidget()
        gray_layout = QHBoxLayout(self.widget_gray)
        gray_layout.setContentsMargins(0, 0, 0, 0)
        gray_layout.addWidget(QLabel("选择波段:"))
        self.cmb_gray_band = QComboBox()
        gray_layout.addWidget(self.cmb_gray_band, stretch=1)
        disp_layout.addWidget(self.widget_gray)

        # RGB 选择
        self.widget_rgb = QWidget()
        rgb_layout = QFormLayout(self.widget_rgb)
        rgb_layout.setContentsMargins(0, 0, 0, 0)
        self.cmb_r_band = QComboBox()
        self.cmb_g_band = QComboBox()
        self.cmb_b_band = QComboBox()
        rgb_layout.addRow("红通道 (R):", self.cmb_r_band)
        rgb_layout.addRow("绿通道 (G):", self.cmb_g_band)
        rgb_layout.addRow("蓝通道 (B):", self.cmb_b_band)
        self.widget_rgb.setVisible(False)
        disp_layout.addWidget(self.widget_rgb)

        self.btn_apply_display = QPushButton("应用显示设置 (Apply)")
        self.btn_apply_display.clicked.connect(self._on_apply_display_clicked)
        disp_layout.addWidget(self.btn_apply_display)

        layout.addWidget(self.grp_display)

        # 4. 底部元数据卡片
        self.grp_meta = QGroupBox("图层元数据 (Metadata)")
        meta_layout = QFormLayout(self.grp_meta)
        meta_layout.setContentsMargins(6, 6, 6, 6)
        meta_layout.setSpacing(4)

        self.lbl_meta_dims = QLabel("-")
        self.lbl_meta_driver = QLabel("-")
        self.lbl_meta_bands = QLabel("-")
        self.lbl_meta_dtype = QLabel("-")
        self.lbl_meta_crs = QLabel("-")
        self.lbl_meta_path = QLabel("-")
        self.lbl_meta_path.setWordWrap(True)

        meta_layout.addRow("尺寸 (WxH):", self.lbl_meta_dims)
        meta_layout.addRow("驱动格式:", self.lbl_meta_driver)
        meta_layout.addRow("波段数量:", self.lbl_meta_bands)
        meta_layout.addRow("数据类型:", self.lbl_meta_dtype)
        meta_layout.addRow("坐标系统:", self.lbl_meta_crs)
        meta_layout.addRow("物理路径:", self.lbl_meta_path)

        layout.addWidget(self.grp_meta)

    @property
    def layers(self) -> list[LayerItem]:
        return list(self._layers)

    def add_layer(self, layer: LayerItem, select: bool = True) -> None:
        """添加新图层至列表并可选选中。"""
        self._layers.append(layer)
        item = QListWidgetItem(f"[{layer.driver}] {layer.name}")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked)
        item.setData(Qt.ItemDataRole.UserRole, layer.layer_id)
        self.list_widget.addItem(item)

        if select:
            self.list_widget.setCurrentItem(item)

    def select_layer(self, layer_id: str) -> None:
        for idx in range(self.list_widget.count()):
            item = self.list_widget.item(idx)
            if item.data(Qt.ItemDataRole.UserRole) == layer_id:
                self.list_widget.setCurrentItem(item)
                break

    def remove_layer(self, layer_id: str) -> None:
        for idx in range(len(self._layers)):
            if self._layers[idx].layer_id == layer_id:
                self._layers.pop(idx)
                break
        for idx in range(self.list_widget.count()):
            item = self.list_widget.item(idx)
            if item.data(Qt.ItemDataRole.UserRole) == layer_id:
                self.list_widget.takeItem(idx)
                break
        if self._selected_layer and self._selected_layer.layer_id == layer_id:
            self._selected_layer = None
            self._update_details()
            self.sig_layer_selected.emit(None)

    def _on_selection_changed(self) -> None:
        items = self.list_widget.selectedItems()
        if not items:
            self._selected_layer = None
            self.btn_remove.setEnabled(False)
            self.grp_display.setEnabled(False)
            self._update_details()
            self.sig_layer_selected.emit(None)
            return

        layer_id = items[0].data(Qt.ItemDataRole.UserRole)
        match = next((ly for ly in self._layers if ly.layer_id == layer_id), None)
        self._selected_layer = match
        self.btn_remove.setEnabled(match is not None)
        self.grp_display.setEnabled(match is not None)
        self._populate_display_controls()
        self._update_details()
        self.sig_layer_selected.emit(match)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        layer_id = item.data(Qt.ItemDataRole.UserRole)
        match = next((ly for ly in self._layers if ly.layer_id == layer_id), None)
        if match is not None:
            new_vis = item.checkState() == Qt.CheckState.Checked
            if match.visible != new_vis:
                match.visible = new_vis
                self.sig_layer_visibility_changed.emit(match, new_vis)

    def _on_remove_clicked(self) -> None:
        if self._selected_layer:
            lid = self._selected_layer.layer_id
            self.remove_layer(lid)
            self.sig_layer_removed.emit(lid)

    def _on_mode_toggled(self, btn_id: int, checked: bool) -> None:
        if not checked:
            return
        is_rgb = (btn_id == 1)
        self.widget_gray.setVisible(not is_rgb)
        self.widget_rgb.setVisible(is_rgb)

    def _populate_display_controls(self) -> None:
        if not self._selected_layer:
            return
        ly = self._selected_layer
        count = ly.band_count

        for cmb in (self.cmb_gray_band, self.cmb_r_band, self.cmb_g_band, self.cmb_b_band):
            cmb.blockSignals(True)
            cmb.clear()
            for b in range(1, count + 1):
                dt = ly.dtypes[b - 1] if b - 1 < len(ly.dtypes) else ""
                cmb.addItem(f"波段 {b} ({dt})", b)
            cmb.blockSignals(False)

        # 设置当前选中
        self.rad_rgb.setEnabled(count >= 3)
        if ly.display_mode == "rgb" and count >= 3:
            self.rad_rgb.setChecked(True)
            self._set_combo_band(self.cmb_r_band, ly.rgb_bands[0])
            self._set_combo_band(self.cmb_g_band, ly.rgb_bands[1])
            self._set_combo_band(self.cmb_b_band, ly.rgb_bands[2])
            self.widget_gray.setVisible(False)
            self.widget_rgb.setVisible(True)
        else:
            self.rad_gray.setChecked(True)
            self._set_combo_band(self.cmb_gray_band, ly.gray_band)
            self.widget_gray.setVisible(True)
            self.widget_rgb.setVisible(False)

    def _set_combo_band(self, combo: QComboBox, band_idx: int) -> None:
        idx = combo.findData(band_idx)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _on_apply_display_clicked(self) -> None:
        if not self._selected_layer:
            return
        ly = self._selected_layer
        if self.rad_rgb.isChecked():
            ly.display_mode = "rgb"
            r = int(self.cmb_r_band.currentData() or 1)
            g = int(self.cmb_g_band.currentData() or 1)
            b = int(self.cmb_b_band.currentData() or 1)
            ly.rgb_bands = (r, g, b)
        else:
            ly.display_mode = "gray"
            ly.gray_band = int(self.cmb_gray_band.currentData() or 1)

        self.sig_display_mode_changed.emit(ly)

    def _update_details(self) -> None:
        ly = self._selected_layer
        if ly is None:
            self.lbl_meta_dims.setText("-")
            self.lbl_meta_driver.setText("-")
            self.lbl_meta_bands.setText("-")
            self.lbl_meta_dtype.setText("-")
            self.lbl_meta_crs.setText("-")
            self.lbl_meta_path.setText("-")
            return

        self.lbl_meta_dims.setText(f"{ly.width} × {ly.height} 像元")
        self.lbl_meta_driver.setText(f"{ly.driver} ({ly.metadata.get('driver_long_name', '')})")
        self.lbl_meta_bands.setText(f"{ly.band_count} 个波段")
        self.lbl_meta_dtype.setText(", ".join(set(ly.dtypes)) if ly.dtypes else "-")
        self.lbl_meta_crs.setText(ly.crs_display)
        self.lbl_meta_path.setText(str(ly.file_path))
