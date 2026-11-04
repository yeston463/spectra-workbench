"""坐标查询、统计与 ROI 管理面板（P3-05 / P3-04）。

面板自身不做任何地理数学：行列、地图坐标、物理值、统计一律由 worker 返回。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

_STATUS_TEXT = {
    "valid": "有效",
    "nodata": "NoData",
    "masked": "被掩膜",
    "alpha_zero": "alpha=0",
    "outside": "超出图幅",
}


class InfoPanel(QWidget):
    """像元/坐标查询 + 统计 + ROI 列表。"""

    sig_map_query_requested = Signal(float, float)
    sig_stats_requested = Signal(str)  # sampled | full
    sig_roi_selected = Signal(str)
    sig_roi_delete_requested = Signal(str)
    sig_roi_clear_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rois: dict[str, Any] = {}
        self._init_ui()

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        # ---------------- 像元查询
        box_pixel = QGroupBox("像元查询（worker 计算）")
        form = QFormLayout(box_pixel)
        self.lbl_pos = QLabel("—")
        self.lbl_map = QLabel("—")
        self.lbl_crs = QLabel("—")
        form.addRow("行列 (列, 行):", self.lbl_pos)
        form.addRow("地图坐标:", self.lbl_map)
        form.addRow("CRS:", self.lbl_crs)
        root.addWidget(box_pixel)

        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["波段", "原值", "物理值", "状态"])
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setColumnWidth(0, 60)
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        root.addWidget(self.tbl, stretch=1)

        # ---------------- 地图坐标反算
        box_map = QGroupBox("地图坐标 → 行列")
        row = QHBoxLayout(box_map)
        self.spin_x = QDoubleSpinBox()
        self.spin_x.setRange(-1e12, 1e12)
        self.spin_x.setDecimals(6)
        self.spin_y = QDoubleSpinBox()
        self.spin_y.setRange(-1e12, 1e12)
        self.spin_y.setDecimals(6)
        self.cmb_conv = QComboBox()
        self.cmb_conv.addItem("像元角点 (corner)", "corner")
        self.cmb_conv.addItem("像元中心 (center)", "center")
        self.btn_map = QPushButton("反算")
        self.btn_map.clicked.connect(self._emit_map_query)
        row.addWidget(QLabel("X:"))
        row.addWidget(self.spin_x)
        row.addWidget(QLabel("Y:"))
        row.addWidget(self.spin_y)
        row.addWidget(self.cmb_conv)
        row.addWidget(self.btn_map)
        root.addWidget(box_map)

        self.lbl_map_result = QLabel("未查询")
        self.lbl_map_result.setWordWrap(True)
        root.addWidget(self.lbl_map_result)

        # ---------------- 统计
        box_stats = QGroupBox("统计（抽样与全量必须区分）")
        stats_layout = QHBoxLayout(box_stats)
        self.btn_sampled = QPushButton("抽样统计")
        self.btn_sampled.clicked.connect(lambda: self.sig_stats_requested.emit("sampled"))
        self.btn_full = QPushButton("全量统计")
        self.btn_full.clicked.connect(lambda: self.sig_stats_requested.emit("full"))
        stats_layout.addWidget(self.btn_sampled)
        stats_layout.addWidget(self.btn_full)
        root.addWidget(box_stats)
        self.lbl_stats = QLabel("未统计")
        self.lbl_stats.setWordWrap(True)
        root.addWidget(self.lbl_stats)

        # ---------------- ROI 列表
        box_roi = QGroupBox("ROI（点击选中，删除作用于选中项）")
        roi_layout = QVBoxLayout(box_roi)
        self.lst_rois = QListWidget()
        self.lst_rois.itemSelectionChanged.connect(self._on_roi_selected)
        roi_layout.addWidget(self.lst_rois)
        btn_row = QHBoxLayout()
        self.btn_del_roi = QPushButton("删除选中")
        self.btn_del_roi.clicked.connect(self._emit_roi_delete)
        self.btn_clear_roi = QPushButton("清空全部")
        self.btn_clear_roi.clicked.connect(self.sig_roi_clear_requested)
        btn_row.addWidget(self.btn_del_roi)
        btn_row.addWidget(self.btn_clear_roi)
        roi_layout.addLayout(btn_row)
        root.addWidget(box_roi)

    # ------------------------------------------------------------------ 回调
    def _emit_map_query(self) -> None:
        self.sig_map_query_requested.emit(float(self.spin_x.value()), float(self.spin_y.value()))

    # ------------------------------------------------------------------ 填充
    def show_pixel_query(self, data: dict[str, Any]) -> None:
        row = int(data.get("row", -1))
        col = int(data.get("col", -1))
        self.lbl_pos.setText(f"列 {col}, 行 {row}" + ("" if data.get("inside") else "  (超出图幅)"))
        center = data.get("map_center") or []
        corner = data.get("map_corner") or []
        if len(center) == 2 and len(corner) == 2:
            self.lbl_map.setText(
                f"中心 ({center[0]:.3f}, {center[1]:.3f})\n角点 ({corner[0]:.3f}, {corner[1]:.3f})"
            )
        elif data.get("has_geotransform"):
            self.lbl_map.setText("无")
        else:
            self.lbl_map.setText("数据集无地理参考（仅像元坐标）")
        epsg = data.get("crs_epsg")
        self.lbl_crs.setText(f"EPSG:{epsg}" if epsg else ("自定义 WKT" if data.get("crs_wkt") else "无 CRS"))

        bands = data.get("bands", [])
        self.tbl.setRowCount(len(bands))
        for i, b in enumerate(bands):
            raw = b.get("raw")
            phys = b.get("physical")
            self.tbl.setItem(i, 0, QTableWidgetItem(str(b.get("band"))))
            self.tbl.setItem(i, 1, QTableWidgetItem("—" if raw is None else f"{raw:g}"))
            self.tbl.setItem(i, 2, QTableWidgetItem("—" if phys is None else f"{phys:g}"))
            status = str(b.get("status", "valid"))
            text = _STATUS_TEXT.get(status, status)
            if status == "valid" and b.get("is_zero"):
                text = "有效 (值恰为 0)"
            item = QTableWidgetItem(text)
            if status != "valid":
                item.setForeground(Qt.GlobalColor.gray)
            self.tbl.setItem(i, 3, item)

    def show_query_error(self, message: str) -> None:
        self.lbl_pos.setText(f"查询失败：{message}")
        self.tbl.setRowCount(0)

    def show_map_to_pixel(self, data: dict[str, Any]) -> None:
        conv = data.get("requested_convention", "corner")
        self.lbl_map_result.setText(
            f"X={data.get('map_x'):.4f}, Y={data.get('map_y'):.4f} → "
            f"列 {data.get('col')}, 行 {data.get('row')}（{conv} 约定）\n"
            f"角点约定：列 {data.get('col_corner')}, 行 {data.get('row_corner')}；"
            f"中心约定：列 {data.get('col_center')}, 行 {data.get('row_center')}\n"
            f"浮点位置：({data.get('col_float_corner'):.4f}, {data.get('row_float_corner'):.4f}) "
            f"{'在图幅内' if data.get('inside') else '超出图幅'}"
        )

    def show_map_error(self, message: str) -> None:
        self.lbl_map_result.setText(f"反算失败：{message}")

    def show_stats(self, data: dict[str, Any]) -> None:
        from spectra_workbench.rendering.display import stats_source_label

        lines = [f"来源：{stats_source_label(data)}"]
        for b in data.get("bands", []):
            lines.append(
                f"波段 {b.get('band')}: min={_fmt(b.get('min'))} max={_fmt(b.get('max'))} "
                f"mean={_fmt(b.get('mean'))} std={_fmt(b.get('std'))} "
                f"p2={_fmt(b.get('p2'))} p98={_fmt(b.get('p98'))} "
                f"有效 {b.get('valid_count')}/{b.get('total_count')}"
            )
        self.lbl_stats.setText("\n".join(lines))

    def show_stats_error(self, message: str) -> None:
        self.lbl_stats.setText(f"统计失败：{message}")

    # ------------------------------------------------------------------- ROI
    def set_rois(self, rois: list[Any]) -> None:
        self._rois = {r.roi_id: r for r in rois}
        self.lst_rois.blockSignals(True)
        self.lst_rois.clear()
        for r in rois:
            item = QListWidgetItem(f"{r.name}  [{r.shape.value}]")
            item.setData(Qt.ItemDataRole.UserRole, r.roi_id)
            self.lst_rois.addItem(item)
        self.lst_rois.blockSignals(False)

    def _on_roi_selected(self) -> None:
        items = self.lst_rois.selectedItems()
        if not items:
            return
        roi_id = str(items[0].data(Qt.ItemDataRole.UserRole))
        self.sig_roi_selected.emit(roi_id)

    def _emit_roi_delete(self) -> None:
        items = self.lst_rois.selectedItems()
        if not items:
            return
        self.sig_roi_delete_requested.emit(str(items[0].data(Qt.ItemDataRole.UserRole)))

    def clear_all(self) -> None:
        self.lbl_pos.setText("—")
        self.lbl_map.setText("—")
        self.lbl_crs.setText("—")
        self.tbl.setRowCount(0)
        self.lbl_map_result.setText("未查询")
        self.lbl_stats.setText("未统计")
        self.lst_rois.clear()
        self._rois = {}


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.6g}"
    except (TypeError, ValueError):
        return str(v)
