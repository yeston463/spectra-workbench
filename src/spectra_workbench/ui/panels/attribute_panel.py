"""P5-03：矢量图层分页属性表面板。

设计要点（对应验收「大图层无一次性全读」）：
- 面板每次只向 worker 索取一页（默认 100 条，worker 硬上限 1000），
  从不请求全表，也不在 GUI 进程缓存全表；
- 总要素数用 worker 的**廉价计数**（force=0）；驱动给不出廉价计数时
  如实显示「昂贵未统计」，翻页以「最后一页不满」为结束条件，绝不取全表；
- 请求代际：图层切换/翻页会让旧请求失效，迟到的旧页响应一律丢弃；
- 空几何与多部件在「几何」列如实标注（空 / 类型 ×部件数），不做美化。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

__all__ = ["AttributePanel", "VectorPageModel"]

#: Qt 惯例的空父索引；模块级单例避免在默认参数里做函数调用。
_EMPTY_INDEX = QModelIndex()


class VectorPageModel(QAbstractTableModel):
    """当前页要素的表格模型：FID | 几何 | 字段…。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fields: list[dict[str, Any]] = []
        self._features: list[dict[str, Any]] = []
        self._start = 0

    # -------------------------------------------------- Qt 模型接口
    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._features)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = _EMPTY_INDEX) -> int:  # noqa: N802
        return 0 if parent.isValid() else 2 + len(self._fields)

    def headerData(
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Vertical:
            return None
        if section == 0:
            return "FID"
        if section == 1:
            return "几何"
        field = self._fields[section - 2]
        return str(field.get("name", f"field{section - 2}"))

    def data(
        self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole or not index.isValid():
            return None
        if index.row() >= len(self._features):
            return None
        feature = self._features[index.row()]
        if index.column() == 0:
            return str(feature.get("fid", ""))
        if index.column() == 1:
            return self._geometry_text(feature)
        name = str(self._fields[index.column() - 2].get("name", ""))
        value = (feature.get("fields") or {}).get(name)
        return "—" if value is None else str(value)

    # -------------------------------------------------- 页面装配
    @staticmethod
    def _geometry_text(feature: dict[str, Any]) -> str:
        if feature.get("empty_geometry"):
            geom_type = feature.get("geometry_type")
            return f"{geom_type}（空）" if geom_type else "空几何"
        geom_type = feature.get("geometry_type")
        if not geom_type:
            return "—"
        part_count = int(feature.get("part_count") or 0)
        if part_count > 1:
            return f"{geom_type} ×{part_count}"
        return geom_type

    def apply_page(self, page: dict[str, Any]) -> None:
        """用一页 worker 响应整体替换模型内容（代际校验由面板负责）。"""
        self.beginResetModel()
        self._fields = list(page.get("fields") or [])
        self._features = list(page.get("features") or [])
        self._start = int(page.get("start", 0))
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._fields = []
        self._features = []
        self._start = 0
        self.endResetModel()

    @property
    def start_index(self) -> int:
        return self._start


class AttributePanel(QWidget):
    """分页属性表：一页 100 条，prev/next 翻页，绝不一次性取全表。"""

    PAGE_SIZE = 100

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._client: Any = None
        self._path: Path | None = None
        self._layer_index = 0
        self._start = 0
        self._load_seq = 0
        self._total: int | None = None
        self._total_expensive = False
        self._last_page_full = False

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        self.lbl_title = QLabel("未加载矢量图层")
        header.addWidget(self.lbl_title)
        header.addStretch(1)
        self.lbl_count = QLabel("")
        header.addWidget(self.lbl_count)
        root.addLayout(header)

        layer_row = QHBoxLayout()
        self.lbl_layer = QLabel("子图层:")
        self.cmb_layer = QComboBox()
        self.cmb_layer.currentIndexChanged.connect(self._on_layer_changed)
        layer_row.addWidget(self.lbl_layer)
        layer_row.addWidget(self.cmb_layer, stretch=1)
        root.addLayout(layer_row)

        self.table = QTableView()
        self._model = VectorPageModel(self)
        self.table.setModel(self._model)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, stretch=1)

        pager = QHBoxLayout()
        self.btn_prev = QPushButton("上一页")
        self.btn_next = QPushButton("下一页")
        self.btn_prev.clicked.connect(lambda: self._go(-1))
        self.btn_next.clicked.connect(lambda: self._go(1))
        self.lbl_page = QLabel("—")
        pager.addWidget(self.btn_prev)
        pager.addWidget(self.lbl_page)
        pager.addWidget(self.btn_next)
        pager.addStretch(1)
        root.addLayout(pager)

        self._update_pager()

    # ------------------------------------------------------------ 对外接口
    def set_client(self, client: Any) -> None:
        """注入 QtWorkerClient（MainWindow 持有的真实 worker 客户端）。"""
        self._client = client

    def load_layer(self, path: Path | str, layer_index: int = 0) -> None:
        """加载矢量数据集的第 layer_index 层并回到第一页。"""
        self._path = Path(path)
        self._layer_index = int(layer_index)
        self._start = 0
        self._total = None
        self.lbl_title.setText(f"属性表：{self._path.name}")
        self._fetch()
        # 多子图层时补充下拉；页响应回来后同样会刷新字段
        if self._client is not None:
            self._client.probe_vector_async(
                self._path,
                on_success=self._on_probed,
                on_error=lambda _err: None,
            )

    def clear(self) -> None:
        self._load_seq += 1
        self._path = None
        self._total = None
        self.lbl_title.setText("未加载矢量图层")
        self.lbl_count.setText("")
        self.cmb_layer.blockSignals(True)
        self.cmb_layer.clear()
        self.cmb_layer.blockSignals(False)
        self._model.clear()
        self._update_pager()

    # ------------------------------------------------------------ 内部
    def _on_probed(self, info: dict[str, Any]) -> None:
        """probe_vector 回调：填充子图层下拉。"""
        if self._path is None or Path(info.get("path", "")) != self._path:
            return
        layers = info.get("layers") or []
        self.cmb_layer.blockSignals(True)
        self.cmb_layer.clear()
        for idx, lyr in enumerate(layers):
            name = str(lyr.get("name", f"layer_{idx}"))
            count = lyr.get("feature_count")
            label = f"{name}（{count} 要素）" if count is not None else name
            self.cmb_layer.addItem(label, idx)
        self.cmb_layer.blockSignals(False)
        self.lbl_layer.setVisible(self.cmb_layer.count() > 1)
        self.cmb_layer.setVisible(self.cmb_layer.count() > 1)
        if self.cmb_layer.count() > 1:
            self.cmb_layer.setCurrentIndex(min(self._layer_index, self.cmb_layer.count() - 1))

    def _on_layer_changed(self, index: int) -> None:
        if index < 0 or self._path is None:
            return
        layer_index = self.cmb_layer.itemData(index)
        if layer_index is None or int(layer_index) == self._layer_index:
            return
        self.load_layer(self._path, layer_index=int(layer_index))

    def _fetch(self) -> None:
        if self._client is None or self._path is None:
            return
        self._load_seq += 1
        seq = self._load_seq
        self.lbl_page.setText("加载中…")
        self._client.read_vector_page_async(
            self._path,
            layer_index=self._layer_index,
            start=self._start,
            count=self.PAGE_SIZE,
            on_success=lambda page: self._on_page(page, seq),
            on_error=lambda err: self._on_page_error(err, seq),
        )

    def _on_page(self, page: dict[str, Any], seq: int) -> None:
        # 代际校验：翻页/切层/清空后的旧响应一律丢弃
        if seq != self._load_seq or self._path is None:
            return
        self._total = page.get("feature_count")
        self._total_expensive = bool(page.get("feature_count_expensive"))
        returned = int(page.get("returned_count") or 0)
        self._last_page_full = returned >= self.PAGE_SIZE
        if self._total is None:
            # 廉价计数不可得：以页累计推算下界，绝不发起全表统计
            self._total = self._start + returned
        self._model.apply_page(page)
        count_text = (
            str(self._total)
            if not self._total_expensive or not self._last_page_full
            else f"≥ {self._total}"
        )
        self.lbl_count.setText(f"要素数：{count_text}")
        self._update_pager()

    def _on_page_error(self, message: str, seq: int) -> None:
        if seq != self._load_seq:
            return
        self.lbl_page.setText(f"读取失败：{message}")
        self._last_page_full = False
        self._update_pager()

    def _go(self, delta: int) -> None:
        self._start = max(0, self._start + delta * self.PAGE_SIZE)
        self._fetch()

    def _update_pager(self) -> None:
        page_no = self._start // self.PAGE_SIZE + 1
        if self._total is not None and not self._total_expensive:
            total_pages = max(1, -(-int(self._total) // self.PAGE_SIZE))
            self.lbl_page.setText(f"第 {page_no} / {total_pages} 页")
            has_prev = self._start > 0
            has_next = self._start + self.PAGE_SIZE < int(self._total)
        else:
            self.lbl_page.setText(f"第 {page_no} 页")
            has_prev = self._start > 0
            # 廉价计数不可得时以「最后一页不满」为结束条件
            has_next = self._last_page_full
        self.btn_prev.setEnabled(has_prev)
        self.btn_next.setEnabled(has_next)
