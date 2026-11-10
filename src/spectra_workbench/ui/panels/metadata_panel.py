"""P3-02 元数据面板：展示矢量图层结构与可选子数据集。

设计约束（按验收标准）：
- 矢量渲染尚未实现，因此面板**只做元数据展示**，不提供任何「渲染矢量」的假入口；
  明确说明当前仅支持元数据读取。
- 子数据集以引擎返回的数据源引用（可能是 ``NETCDF:"file.nc":Band1`` 这类协议 URI）
  呈现，选中后由 worker 探测；面板不把 URI 当普通路径解析。
- 坏文件或缺失子数据集必须给出明确错误，而不是静默留空。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class MetadataPanel(QWidget):
    """矢量元数据与子数据集面板（P3-02）。"""

    sig_subdataset_selected = Signal(str)  # 引擎返回的子数据集 URI

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._subdatasets: list[dict[str, Any]] = []
        self._init_ui()

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        # ---------------- 矢量元数据
        box_vec = QGroupBox("矢量元数据（仅元数据读取，暂不支持矢量渲染）")
        vec_layout = QVBoxLayout(box_vec)
        self.lbl_vector_summary = QLabel("未加载矢量数据")
        self.lbl_vector_summary.setWordWrap(True)
        vec_layout.addWidget(self.lbl_vector_summary)

        self.lst_vector_layers = QListWidget()
        self.lst_vector_layers.currentRowChanged.connect(self._on_vector_layer_changed)
        vec_layout.addWidget(self.lst_vector_layers)

        self.tbl_fields = QTableWidget(0, 4)
        self.tbl_fields.setHorizontalHeaderLabels(["字段名", "类型", "可空", "类型码"])
        self.tbl_fields.horizontalHeader().setStretchLastSection(True)
        self.tbl_fields.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        vec_layout.addWidget(self.tbl_fields)

        self.lbl_vector_detail = QLabel("—")
        self.lbl_vector_detail.setWordWrap(True)
        vec_layout.addWidget(self.lbl_vector_detail)
        root.addWidget(box_vec)

        # ---------------- 子数据集
        box_sub = QGroupBox("子数据集（容器数据集内的可选数据源）")
        sub_layout = QVBoxLayout(box_sub)
        self.lbl_subdataset_summary = QLabel("未加载子数据集")
        self.lbl_subdataset_summary.setWordWrap(True)
        sub_layout.addWidget(self.lbl_subdataset_summary)

        row = QVBoxLayout()
        self.cmb_subdataset = QComboBox()
        self.cmb_subdataset.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_subdataset.setMinimumContentsLength(18)
        self.cmb_subdataset.currentIndexChanged.connect(self._on_subdataset_combo_changed)
        row.addWidget(self.cmb_subdataset)
        self.btn_probe_subdataset = QPushButton("探测选中子数据集")
        self.btn_probe_subdataset.clicked.connect(self._emit_probe_subdataset)
        row.addWidget(self.btn_probe_subdataset)
        sub_layout.addLayout(row)

        self.lbl_subdataset_detail = QLabel("—")
        self.lbl_subdataset_detail.setWordWrap(True)
        sub_layout.addWidget(self.lbl_subdataset_detail)
        root.addWidget(box_sub)

    # ------------------------------------------------------------ 矢量元数据
    def show_vector(self, info: dict[str, Any]) -> None:
        """展示矢量数据集元数据。

        ``info`` 为 worker 返回的 probe_vector 结果。
        """
        layers = info.get("layers") or []
        self.lbl_vector_summary.setText(
            f"图层数：{info.get('layer_count', len(layers))}　驱动：{info.get('driver') or '未知'}"
        )
        self.lst_vector_layers.blockSignals(True)
        self.lst_vector_layers.clear()
        for lyr in layers:
            item = QListWidgetItem(str(lyr.get("name", "?")))
            item.setData(Qt.ItemDataRole.UserRole, lyr)
            self.lst_vector_layers.addItem(item)
        self.lst_vector_layers.blockSignals(False)
        self.tbl_fields.setRowCount(0)
        self.lbl_vector_detail.setText("—")
        if layers:
            self.lst_vector_layers.setCurrentRow(0)
        else:
            self.lbl_vector_summary.setText("矢量数据集不含图层")

    def clear_vector(self, message: str = "未加载矢量数据") -> None:
        self.lbl_vector_summary.setText(message)
        self.lst_vector_layers.clear()
        self.tbl_fields.setRowCount(0)
        self.lbl_vector_detail.setText("—")

    def _on_vector_layer_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.lst_vector_layers.item(row)
        if item is None:
            return
        lyr = item.data(Qt.ItemDataRole.UserRole) or {}

        fields = lyr.get("fields") or []
        self.tbl_fields.setRowCount(len(fields))
        for i, f in enumerate(fields):
            nullable = f.get("nullable")
            nullable_text = "是" if nullable else ("否" if nullable is False else "未知")
            for col, value in enumerate(
                (
                    str(f.get("name", "")),
                    str(f.get("type", "")),
                    nullable_text,
                    str(f.get("type_code", "")),
                )
            ):
                self.tbl_fields.setItem(i, col, QTableWidgetItem(value))

        extent = lyr.get("extent")
        extent_text = (
            ", ".join(f"{v:g}" for v in extent) if isinstance(extent, list) else "不可用"
        )
        fc = lyr.get("feature_count")
        if fc is None:
            fc_text = "未知（驱动未提供或需昂贵扫描）"
        else:
            fc_text = str(fc)
            if lyr.get("feature_count_expensive"):
                fc_text += "（该计数可能需要昂贵扫描）"
        crs = lyr.get("crs_epsg")
        crs_text = f"EPSG:{crs}" if crs else "无 CRS"
        self.lbl_vector_detail.setText(
            f"图层：{lyr.get('name')}　几何类型：{lyr.get('geometry_type') or '未知'}\n"
            f"CRS：{crs_text}　要素数：{fc_text}\n"
            f"范围 (minx, maxx, miny, maxy)：{extent_text}"
        )

    # -------------------------------------------------------------- 子数据集
    def show_subdatasets(self, info: dict[str, Any]) -> None:
        """展示容器数据集的子数据集列表。"""
        subs = info.get("subdatasets") or []
        self._subdatasets = list(subs)
        self.cmb_subdataset.blockSignals(True)
        self.cmb_subdataset.clear()
        for s in subs:
            desc = str(s.get("description") or s.get("uri") or "")
            self.cmb_subdataset.addItem(desc, str(s.get("uri", "")))
        self.cmb_subdataset.blockSignals(False)
        count = info.get("subdataset_count", len(subs))
        if count:
            self.lbl_subdataset_summary.setText(f"子数据集数：{count}")
            if subs:
                self.cmb_subdataset.setCurrentIndex(0)
                self._emit_probe_subdataset()
        else:
            self.lbl_subdataset_summary.setText("该数据集没有子数据集")
            self.lbl_subdataset_detail.setText("—")

    def clear_subdatasets(self, message: str = "未加载子数据集") -> None:
        self._subdatasets = []
        self.cmb_subdataset.blockSignals(True)
        self.cmb_subdataset.clear()
        self.cmb_subdataset.blockSignals(False)
        self.lbl_subdataset_summary.setText(message)
        self.lbl_subdataset_detail.setText("—")

    def current_subdataset_uri(self) -> str | None:
        """返回当前在 UI 上选中的子数据集 URI（没有选中时返回 None）。

        这是「用户当前选择」的权威来源：回调必须与它比对，
        而不是只与「发起请求时的 URI」比对——后者无法区分
        同一容器内先后选中的 A 与 B。
        """
        index = self.cmb_subdataset.currentIndex()
        if index < 0:
            return None
        data = self.cmb_subdataset.currentData()
        return str(data) if data else None

    def _on_subdataset_combo_changed(self, index: int) -> None:
        if index >= 0 and index < len(self._subdatasets):
            self._emit_probe_subdataset()

    def _emit_probe_subdataset(self) -> None:
        index = self.cmb_subdataset.currentIndex()
        if index < 0 or index >= len(self._subdatasets):
            return
        uri = str(self._subdatasets[index].get("uri", ""))
        if uri:
            self.sig_subdataset_selected.emit(uri)

    def show_subdataset_detail(self, info: dict[str, Any]) -> None:
        """展示单个子数据集的探测结果。"""
        bands = info.get("bands") or []
        band_text = "、".join(
            f"B{b.get('band')}:{b.get('dtype')}"
            + (f"/NoData={b.get('nodata'):g}" if b.get("nodata") is not None else "")
            for b in bands
        )
        crs = info.get("crs_epsg")
        crs_text = f"EPSG:{crs}" if crs else "无 CRS"
        gt = info.get("geotransform")
        gt_text = ", ".join(f"{v:g}" for v in gt) if isinstance(gt, list) else "不可用"
        self.lbl_subdataset_detail.setText(
            f"尺寸：{info.get('width')}×{info.get('height')}　波段：{info.get('count')}　"
            f"驱动：{info.get('driver')}\n"
            f"CRS：{crs_text}　NoData：{info.get('nodata')}\n"
            f"GeoTransform：{gt_text}\n"
            f"波段类型：{band_text or '—'}"
        )

    def show_error(self, message: str) -> None:
        """显示明确的错误信息（不静默留空）。"""
        self.lbl_subdataset_detail.setText(f"错误：{message}")

    def clear_all(self) -> None:
        self.clear_vector()
        self.clear_subdatasets()
