"""显示参数面板（P3-04）。

伪彩、拉伸模式、百分位、透明度集中在此；任何变化都只改显示，不改源像元。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from spectra_workbench.rendering.display import (
    DisplayMode,
    DisplaySettings,
    StretchMode,
    available_colormaps,
)


class DisplayPanel(QWidget):
    """当前图层的显示参数编辑器。"""

    sig_display_changed = Signal(object)  # DisplaySettings

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = DisplaySettings()
        self._band_count = 0
        self._init_ui()

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        box = QGroupBox("显示参数（不影响原始像元值）")
        form = QFormLayout(box)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("单波段灰度", DisplayMode.gray)
        self.cmb_mode.addItem("三波段彩色合成", DisplayMode.rgb)
        self.cmb_mode.addItem("单波段伪彩", DisplayMode.pseudo)
        self.cmb_mode.currentIndexChanged.connect(self._on_mode)
        form.addRow("显示模式", self.cmb_mode)

        self.cmb_band = QComboBox()
        self.cmb_band.currentIndexChanged.connect(self._on_band)
        form.addRow("波段 (1-based)", self.cmb_band)

        self.cmb_r = QComboBox()
        self.cmb_g = QComboBox()
        self.cmb_b = QComboBox()
        for cmb, name in ((self.cmb_r, "R"), (self.cmb_g, "G"), (self.cmb_b, "B")):
            cmb.currentIndexChanged.connect(self._on_rgb)
            form.addRow(f"{name} 波段", cmb)

        self.cmb_stretch = QComboBox()
        for label, value in (
            ("不拉伸（原值区间）", StretchMode.none),
            ("最小-最大线性", StretchMode.min_max),
            ("百分位截断", StretchMode.percentile),
            ("标准差 (mean±k·std)", StretchMode.stddev),
        ):
            self.cmb_stretch.addItem(label, value)
        self.cmb_stretch.currentIndexChanged.connect(self._on_stretch)
        form.addRow("拉伸", self.cmb_stretch)

        self.spin_lo = QDoubleSpinBox()
        self.spin_lo.setRange(0.0, 50.0)
        self.spin_lo.setValue(2.0)
        self.spin_hi = QDoubleSpinBox()
        self.spin_hi.setRange(50.0, 100.0)
        self.spin_hi.setValue(98.0)
        for spin in (self.spin_lo, self.spin_hi):
            spin.valueChanged.connect(self._on_percentiles)
        form.addRow("百分位 下/上", self.spin_lo)
        form.addRow("　", self.spin_hi)

        self.cmb_cmap = QComboBox()
        for name in available_colormaps():
            self.cmb_cmap.addItem(name, name)
        self.cmb_cmap.currentIndexChanged.connect(self._on_cmap)
        form.addRow("伪彩", self.cmb_cmap)

        self.spin_opacity = QDoubleSpinBox()
        self.spin_opacity.setRange(0.0, 1.0)
        self.spin_opacity.setSingleStep(0.05)
        self.spin_opacity.setValue(1.0)
        self.spin_opacity.valueChanged.connect(self._on_opacity)
        form.addRow("透明度", self.spin_opacity)

        root.addWidget(box)
        self.lbl_note = QLabel("说明：拉伸区间来自当前视图窗口的抽样统计，状态栏会标注来源。")
        self.lbl_note.setWordWrap(True)
        root.addWidget(self.lbl_note)
        root.addStretch()

    # ------------------------------------------------------------------ 绑定
    def set_layer(self, band_count: int, settings: DisplaySettings | None) -> None:
        self._band_count = max(0, int(band_count))
        self._populate_bands(self._band_count)
        if settings is not None:
            self._settings = settings.model_copy(deep=True)
        self._sync_widgets_from_settings()

    def clear(self) -> None:
        self._band_count = 0
        self._populate_bands(0)
        self._settings = DisplaySettings()
        self._sync_widgets_from_settings()

    def current_settings(self) -> DisplaySettings:
        return self._settings.model_copy(deep=True)

    def _populate_bands(self, count: int) -> None:
        for cmb in (self.cmb_band, self.cmb_r, self.cmb_g, self.cmb_b):
            cmb.blockSignals(True)
            cmb.clear()
            for i in range(1, max(count, 1) + 1):
                cmb.addItem(str(i), i)
            cmb.blockSignals(False)

    def _sync_widgets_from_settings(self) -> None:
        s = self._settings
        for cmb in (self.cmb_mode, self.cmb_band, self.cmb_r, self.cmb_g, self.cmb_b,
                    self.cmb_stretch, self.cmb_cmap, self.spin_lo, self.spin_hi, self.spin_opacity):
            cmb.blockSignals(True)
        self.cmb_mode.setCurrentIndex(
            [DisplayMode.gray, DisplayMode.rgb, DisplayMode.pseudo].index(s.mode)
        )
        self._set_combo(self.cmb_band, s.gray_band)
        self._set_combo(self.cmb_r, s.rgb_bands[0])
        self._set_combo(self.cmb_g, s.rgb_bands[1])
        self._set_combo(self.cmb_b, s.rgb_bands[2])
        idx = [StretchMode.none, StretchMode.min_max, StretchMode.percentile, StretchMode.stddev].index(s.stretch)
        self.cmb_stretch.setCurrentIndex(idx)
        self._set_combo(self.cmb_cmap, s.pseudocolor)
        self.spin_lo.setValue(float(s.percentiles[0]))
        self.spin_hi.setValue(float(s.percentiles[1]))
        self.spin_opacity.setValue(float(s.opacity))
        for cmb in (self.cmb_mode, self.cmb_band, self.cmb_r, self.cmb_g, self.cmb_b,
                    self.cmb_stretch, self.cmb_cmap, self.spin_lo, self.spin_hi, self.spin_opacity):
            cmb.blockSignals(False)

    @staticmethod
    def _set_combo(combo: QComboBox, value) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return

    def _emit(self) -> None:
        self.sig_display_changed.emit(self.current_settings())

    # ------------------------------------------------------------------ 回调
    def _on_mode(self) -> None:
        self._settings.mode = self.cmb_mode.currentData()
        self._emit()

    def _on_band(self) -> None:
        data = self.cmb_band.currentData()
        if data is not None:
            self._settings.gray_band = int(data)
        self._emit()

    def _on_rgb(self) -> None:
        vals = [c.currentData() for c in (self.cmb_r, self.cmb_g, self.cmb_b)]
        if all(v is not None for v in vals):
            self._settings.rgb_bands = (int(vals[0]), int(vals[1]), int(vals[2]))
        self._emit()

    def _on_stretch(self) -> None:
        self._settings.stretch = self.cmb_stretch.currentData()
        self._emit()

    def _on_percentiles(self) -> None:
        lo, hi = float(self.spin_lo.value()), float(self.spin_hi.value())
        if hi <= lo:
            hi = min(100.0, lo + 1.0)
            self.spin_hi.blockSignals(True)
            self.spin_hi.setValue(hi)
            self.spin_hi.blockSignals(False)
        self._settings.percentiles = (lo, hi)
        self._emit()

    def _on_cmap(self) -> None:
        self._settings.pseudocolor = str(self.cmb_cmap.currentData())
        self._emit()

    def _on_opacity(self) -> None:
        self._settings.opacity = float(self.spin_opacity.value())
        self._emit()
