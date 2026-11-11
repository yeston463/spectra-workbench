"""像元光谱剖面分析对话框 (P11-01).

用于高光谱 / 多光谱影像像元光谱剖面曲线交互式绘制、波长对齐、多曲线叠加对比、曲线平滑与 CSV 导出。
"""

from __future__ import annotations

import contextlib
import csv
from typing import Any

import pyqtgraph as pg
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# 8 色经典对比调色板（确保在深色/浅色背景下均具备鲜明对比度）
_SPECTRUM_PALETTE = [
    "#00B4D8",  # 湖蓝 (Cyan Blue)
    "#2ECC71",  # 翠绿 (Emerald Green)
    "#E74C3C",  # 猩红 (Crimson Red)
    "#F39C12",  # 琥珀黄 (Amber Orange)
    "#9B59B6",  # 紫罗兰 (Amethyst Purple)
    "#1ABC9C",  # 绿松石 (Turquoise)
    "#E67E22",  # 橙色 (Orange)
    "#E84393",  # 玫瑰粉 (Rose Pink)
]


def _smooth_spectrum(values: list[float], window_size: int = 3) -> list[float]:
    """简单加权移动平均平滑（无外部科学库依赖）。"""
    if len(values) < window_size:
        return list(values)
    smoothed = []
    half = window_size // 2
    for i in range(len(values)):
        start = max(0, i - half)
        end = min(len(values), i + half + 1)
        sub = values[start:end]
        smoothed.append(sum(sub) / len(sub))
    return smoothed


class SpectralProfileDialog(QDialog):
    """像元连续反射光谱剖面曲线交互对话框。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("像元连续光谱剖面分析 (Spectral Profile)")
        self.resize(760, 480)

        self._profiles: list[dict[str, Any]] = []
        self._color_idx: int = 0

        self._init_ui()

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ---------------- 顶部操作栏
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self.lbl_info = QLabel("提示：在影像画布上点击像元即可提取光谱曲线")
        self.lbl_info.setStyleSheet("color: #555; font-size: 12px;")
        bar.addWidget(self.lbl_info)
        bar.addStretch()

        self.chk_hold = QCheckBox("叠加对比 (Hold On)")
        self.chk_hold.setChecked(True)
        self.chk_hold.setToolTip("开启后点击新像元将追加曲线，不清除已有像元光谱")
        bar.addWidget(self.chk_hold)

        self.chk_smooth = QCheckBox("曲线平滑")
        self.chk_smooth.setChecked(False)
        self.chk_smooth.setToolTip("使用移动平均对离散波段值进行平滑滤波")
        self.chk_smooth.toggled.connect(self._replot_all)
        bar.addWidget(self.chk_smooth)

        self.btn_clear = QPushButton("清除所有曲线")
        self.btn_clear.clicked.connect(self.clear_profiles)
        bar.addWidget(self.btn_clear)

        self.btn_export = QPushButton("导出 CSV…")
        self.btn_export.clicked.connect(self.export_csv)
        bar.addWidget(self.btn_export)

        root.addLayout(bar)

        # ---------------- 绘图区 (pyqtgraph)
        pg.setConfigOptions(antialias=True)
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground("#1e1e1e")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.plot_widget.setLabel("left", "像元数值 (反射率 / 计数值)")
        self.plot_widget.setLabel("bottom", "波段序号 / 波长 (nm)")
        self.legend = self.plot_widget.addLegend(offset=(10, 10))

        root.addWidget(self.plot_widget, stretch=1)

    # ------------------------------------------------------------------ 核心逻辑
    def add_profile(
        self,
        col: int,
        row: int,
        values: list[float],
        wavelengths: list[float] | None = None,
        layer_name: str = "Layer",
    ) -> None:
        """接收新像元的多波段光谱并绘制。"""
        if not self.chk_hold.isChecked():
            self.clear_profiles()

        if not wavelengths or len(wavelengths) != len(values):
            x = [float(i + 1) for i in range(len(values))]
            has_wl = False
        else:
            x = [float(w) for w in wavelengths]
            has_wl = True

        color = _SPECTRUM_PALETTE[self._color_idx % len(_SPECTRUM_PALETTE)]
        self._color_idx += 1

        label = f"({col}, {row}) - {layer_name}"
        data_record = {
            "col": col,
            "row": row,
            "layer_name": layer_name,
            "x": x,
            "values": values,
            "has_wl": has_wl,
            "color": color,
            "label": label,
        }
        self._profiles.append(data_record)

        self._plot_record(data_record)

        # 更新 X 轴标题
        if has_wl:
            self.plot_widget.setLabel("bottom", "波长 (Wavelength / nm)")
        else:
            self.plot_widget.setLabel("bottom", "波段序号 (Band Index)")

        self.lbl_info.setText(
            f"已提取像元 ({col}, {row})，共 {len(values)} 个波段，当前已叠加 {len(self._profiles)} 条曲线"
        )
        self.show()
        self.raise_()
        self.activateWindow()

    def _plot_record(self, record: dict[str, Any]) -> None:
        x = record["x"]
        y = record["values"]
        if self.chk_smooth.isChecked():
            y = _smooth_spectrum(y)

        pen = pg.mkPen(color=record["color"], width=2.0)
        # 折线图 + 散点端点
        curve = self.plot_widget.plot(
            x,
            y,
            pen=pen,
            symbol="o",
            symbolSize=6,
            symbolBrush=pg.mkBrush(record["color"]),
            symbolPen=pg.mkPen(color="#ffffff", width=1.0),
            name=record["label"],
        )
        record["curve_item"] = curve

    def _replot_all(self) -> None:
        """平滑开关切换时重新绘制所有曲线。"""
        self.plot_widget.clear()
        if self.legend is not None:
            with contextlib.suppress(Exception):
                self.legend.clear()
        for record in self._profiles:
            self._plot_record(record)

    def clear_profiles(self) -> None:
        """清空当前所有光谱曲线。"""
        self.plot_widget.clear()
        if self.legend is not None:
            with contextlib.suppress(Exception):
                self.legend.clear()
        self._profiles.clear()
        self._color_idx = 0
        self.lbl_info.setText("已清空所有曲线，点击影像像元重新提取")

    def export_csv(self) -> None:
        """将当前加载的所有像元光谱曲线导出为 CSV 表格。"""
        if not self._profiles:
            QMessageBox.information(self, "提示", "当前没有可导出的光谱曲线。")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "导出像元光谱曲线为 CSV", "spectral_profile.csv", "CSV 文件 (*.csv)"
        )
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                header = ["Band_Index", "Wavelength_nm"] + [
                    f"Pixel_{p['col']}_{p['row']}_{p['layer_name']}" for p in self._profiles
                ]
                writer.writerow(header)

                max_bands = max(len(p["values"]) for p in self._profiles)
                for b_idx in range(max_bands):
                    row_data: list[Any] = [b_idx + 1]
                    # 波长列
                    first_p = self._profiles[0]
                    wl = (
                        first_p["x"][b_idx]
                        if (first_p["has_wl"] and b_idx < len(first_p["x"]))
                        else ""
                    )
                    row_data.append(wl)
                    # 各像元反射率
                    for p in self._profiles:
                        val = p["values"][b_idx] if b_idx < len(p["values"]) else ""
                        row_data.append(val)
                    writer.writerow(row_data)

            QMessageBox.information(self, "导出成功", f"光谱曲线已成功导出至：\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", f"写入 CSV 文件失败：{exc}")
