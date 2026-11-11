"""Spectra Workbench 高清矢量图标库 (ENVI / QGIS 风格).

使用 QPainter 纯矢量绘制，支持高 DPI 视网膜抗锯齿，色彩对标经典 QGIS / ENVI 专业 GIS 界面：
- 项目与数据：蓝色/琥珀色文件与工程
- 地图导航：经典手型、放大镜、全图适配
- 显示与拉伸：线性 2% 折线、极值拉伸、正态高斯分布、RGB/灰度/伪彩
- ROI 标定：多边形、矩形、像元十字探针
- 遥感专题：植被指数、DEM 地形山峰、重投影网格、主成分 PCA、傅里叶波形
"""

from __future__ import annotations

import math
from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

_ICON_CACHE: dict[tuple[str, int], QIcon] = {}


def _create_pixmap(size: int) -> tuple[QPixmap, QPainter]:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    return pix, p


# ---------------------------------------------------------------------------
# 1. 项目与数据图标 (File & Project)
# ---------------------------------------------------------------------------


def _draw_new_project(p: QPainter, s: float) -> None:
    # 白色折角纸张 + 绿色加号
    path = QPainterPath()
    path.moveTo(s * 0.22, s * 0.15)
    path.lineTo(s * 0.58, s * 0.15)
    path.lineTo(s * 0.78, s * 0.35)
    path.lineTo(s * 0.78, s * 0.85)
    path.lineTo(s * 0.22, s * 0.85)
    path.closeSubpath()
    p.setPen(QPen(QColor("#2C3E50"), 1.5))
    p.setBrush(QBrush(QColor("#ECF0F1")))
    p.drawPath(path)
    # 折角
    fold = QPainterPath()
    fold.moveTo(s * 0.58, s * 0.15)
    fold.lineTo(s * 0.58, s * 0.35)
    fold.lineTo(s * 0.78, s * 0.35)
    p.setBrush(QBrush(QColor("#BDC3C7")))
    p.drawPath(fold)
    # 绿色加号
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#27AE60")))
    p.drawEllipse(QPointF(s * 0.72, s * 0.72), s * 0.22, s * 0.22)
    p.setPen(QPen(QColor("#FFFFFF"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.72, s * 0.60), QPointF(s * 0.72, s * 0.84))
    p.drawLine(QPointF(s * 0.60, s * 0.72), QPointF(s * 0.84, s * 0.72))


def _draw_open_project(p: QPainter, s: float) -> None:
    # 经典 QGIS 文件夹打开 + 蓝色工程文件
    # 后夹
    p.setPen(QPen(QColor("#D35400"), 1.2))
    p.setBrush(QBrush(QColor("#F39C12")))
    back = QPainterPath()
    back.moveTo(s * 0.12, s * 0.25)
    back.lineTo(s * 0.40, s * 0.25)
    back.lineTo(s * 0.50, s * 0.35)
    back.lineTo(s * 0.88, s * 0.35)
    back.lineTo(s * 0.88, s * 0.78)
    back.lineTo(s * 0.12, s * 0.78)
    back.closeSubpath()
    p.drawPath(back)
    # 内插工程纸张
    p.setPen(QPen(QColor("#2980B9"), 1.0))
    p.setBrush(QBrush(QColor("#E8F8F5")))
    p.drawRect(QRectF(s * 0.25, s * 0.28, s * 0.50, s * 0.45))
    # 前盖
    front = QPainterPath()
    front.moveTo(s * 0.10, s * 0.82)
    front.lineTo(s * 0.24, s * 0.46)
    front.lineTo(s * 0.90, s * 0.46)
    front.lineTo(s * 0.78, s * 0.82)
    front.closeSubpath()
    p.setPen(QPen(QColor("#B9770E"), 1.2))
    p.setBrush(QBrush(QColor("#F5B041")))
    p.drawPath(front)


def _draw_save_project(p: QPainter, s: float) -> None:
    # 经典软盘保存图标
    p.setPen(QPen(QColor("#1A5276"), 1.2))
    p.setBrush(QBrush(QColor("#2980B9")))
    p.drawRoundedRect(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64), 3, 3)
    # 标签白框
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#FDFEFE")))
    p.drawRect(QRectF(s * 0.28, s * 0.45, s * 0.44, s * 0.35))
    # 金属滑块
    p.setBrush(QBrush(QColor("#BDC3C7")))
    p.drawRect(QRectF(s * 0.32, s * 0.18, s * 0.36, s * 0.22))
    p.setBrush(QBrush(QColor("#2980B9")))
    p.drawRect(QRectF(s * 0.52, s * 0.20, s * 0.10, s * 0.16))


def _draw_open_raster(p: QPainter, s: float) -> None:
    # 遥感栅格像元网格 + 绿色加号
    p.setPen(QPen(QColor("#2C3E50"), 1.0))
    colors = ["#3498DB", "#2ECC71", "#E67E22", "#9B59B6"]
    idx = 0
    for r in range(2):
        for c in range(2):
            p.setBrush(QBrush(QColor(colors[idx])))
            p.drawRect(QRectF(s * (0.16 + c * 0.28), s * (0.16 + r * 0.28), s * 0.26, s * 0.26))
            idx += 1
    # 绿色加号徽标
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#27AE60")))
    p.drawEllipse(QPointF(s * 0.74, s * 0.74), s * 0.20, s * 0.20)
    p.setPen(QPen(QColor("#FFFFFF"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.74, s * 0.62), QPointF(s * 0.74, s * 0.86))
    p.drawLine(QPointF(s * 0.62, s * 0.74), QPointF(s * 0.86, s * 0.74))


def _draw_open_remote(p: QPainter, s: float) -> None:
    # 地球 / 经纬云朵网络源
    p.setPen(QPen(QColor("#2980B9"), 1.5))
    p.setBrush(QBrush(QColor("#EBF5FB")))
    p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.35, s * 0.35)
    # 经纬线
    p.drawLine(QPointF(s * 0.15, s * 0.5), QPointF(s * 0.85, s * 0.5))
    p.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.18, s * 0.35)
    # 云彩徽标
    cloud = QPainterPath()
    cloud.addEllipse(QPointF(s * 0.65, s * 0.65), s * 0.15, s * 0.15)
    cloud.addEllipse(QPointF(s * 0.80, s * 0.68), s * 0.12, s * 0.12)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#3498DB")))
    p.drawPath(cloud)


# ---------------------------------------------------------------------------
# 2. 地图浏览与导航图标 (Map Navigation)
# ---------------------------------------------------------------------------


def _draw_pan(p: QPainter, s: float) -> None:
    # 经典 QGIS 平移手型 (Pan)
    p.setPen(
        QPen(
            QColor("#2C3E50"),
            1.5,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    p.setBrush(QBrush(QColor("#F9E79F")))
    hand = QPainterPath()
    hand.moveTo(s * 0.35, s * 0.80)
    hand.lineTo(s * 0.30, s * 0.60)
    hand.lineTo(s * 0.30, s * 0.40)  # 大拇指
    hand.quadTo(s * 0.35, s * 0.35, s * 0.40, s * 0.40)
    hand.lineTo(s * 0.40, s * 0.25)  # 食指
    hand.quadTo(s * 0.45, s * 0.20, s * 0.50, s * 0.25)
    hand.lineTo(s * 0.50, s * 0.22)  # 中指
    hand.quadTo(s * 0.55, s * 0.18, s * 0.60, s * 0.22)
    hand.lineTo(s * 0.60, s * 0.28)  # 无名指
    hand.quadTo(s * 0.65, s * 0.25, s * 0.70, s * 0.28)
    hand.lineTo(s * 0.70, s * 0.45)  # 小指
    hand.lineTo(s * 0.75, s * 0.65)
    hand.lineTo(s * 0.68, s * 0.80)
    hand.closeSubpath()
    p.drawPath(hand)


def _draw_zoom_in(p: QPainter, s: float) -> None:
    # 放大镜 +
    p.setPen(QPen(QColor("#2980B9"), 2.0))
    p.setBrush(QBrush(QColor("#EAF2F8")))
    p.drawEllipse(QPointF(s * 0.42, s * 0.42), s * 0.28, s * 0.28)
    # 手柄
    p.setPen(QPen(QColor("#34495E"), 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.63, s * 0.63), QPointF(s * 0.85, s * 0.85))
    # 蓝色加号
    p.setPen(QPen(QColor("#E74C3C"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.42, s * 0.28), QPointF(s * 0.42, s * 0.56))
    p.drawLine(QPointF(s * 0.28, s * 0.42), QPointF(s * 0.56, s * 0.42))


def _draw_zoom_out(p: QPainter, s: float) -> None:
    # 放大镜 -
    p.setPen(QPen(QColor("#2980B9"), 2.0))
    p.setBrush(QBrush(QColor("#EAF2F8")))
    p.drawEllipse(QPointF(s * 0.42, s * 0.42), s * 0.28, s * 0.28)
    # 手柄
    p.setPen(QPen(QColor("#34495E"), 3.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.63, s * 0.63), QPointF(s * 0.85, s * 0.85))
    # 蓝色减号
    p.setPen(QPen(QColor("#2980B9"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.28, s * 0.42), QPointF(s * 0.56, s * 0.42))


def _draw_zoom_full(p: QPainter, s: float) -> None:
    # 适应全图 (四角向外展开箭头)
    p.setPen(
        QPen(
            QColor("#16A085"),
            2.0,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.MiterJoin,
        )
    )
    # 左上
    p.drawLine(QPointF(s * 0.22, s * 0.38), QPointF(s * 0.22, s * 0.22))
    p.drawLine(QPointF(s * 0.22, s * 0.22), QPointF(s * 0.38, s * 0.22))
    # 右上
    p.drawLine(QPointF(s * 0.62, s * 0.22), QPointF(s * 0.78, s * 0.22))
    p.drawLine(QPointF(s * 0.78, s * 0.22), QPointF(s * 0.78, s * 0.38))
    # 左下
    p.drawLine(QPointF(s * 0.22, s * 0.62), QPointF(s * 0.22, s * 0.78))
    p.drawLine(QPointF(s * 0.22, s * 0.78), QPointF(s * 0.38, s * 0.78))
    # 右下
    p.drawLine(QPointF(s * 0.62, s * 0.78), QPointF(s * 0.78, s * 0.78))
    p.drawLine(QPointF(s * 0.78, s * 0.78), QPointF(s * 0.78, s * 0.62))
    # 中心方框
    p.setBrush(QBrush(QColor("#1ABC9C")))
    p.drawRect(QRectF(s * 0.38, s * 0.38, s * 0.24, s * 0.24))


def _draw_zoom_100(p: QPainter, s: float) -> None:
    # 1:1 实际像元大小
    p.setPen(QPen(QColor("#2C3E50"), 1.5))
    p.setBrush(QBrush(QColor("#F2F4F4")))
    p.drawRoundedRect(QRectF(s * 0.15, s * 0.25, s * 0.70, s * 0.50), 3, 3)
    p.setFont(QFont("Arial", int(s * 0.26), QFont.Weight.Bold))
    p.setPen(QColor("#2980B9"))
    p.drawText(QRectF(s * 0.15, s * 0.25, s * 0.70, s * 0.50), Qt.AlignmentFlag.AlignCenter, "1:1")


def _draw_refresh(p: QPainter, s: float) -> None:
    # 环形刷新箭头
    p.setPen(QPen(QColor("#27AE60"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawArc(QRectF(s * 0.20, s * 0.20, s * 0.60, s * 0.60), 45 * 16, 270 * 16)
    # 箭头头部
    arrow = QPainterPath()
    arrow.moveTo(s * 0.75, s * 0.40)
    arrow.lineTo(s * 0.85, s * 0.22)
    arrow.lineTo(s * 0.65, s * 0.22)
    arrow.closeSubpath()
    p.setBrush(QBrush(QColor("#27AE60")))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPath(arrow)


# ---------------------------------------------------------------------------
# 3. 显示与拉伸增强图标 (Stretch & Display - ENVI 经典)
# ---------------------------------------------------------------------------


def _draw_stretch_linear(p: QPainter, s: float) -> None:
    # ENVI 经典 2%~98% 线性拉伸折线 (直方图拉伸)
    p.setPen(QPen(QColor("#7F8C8D"), 1.0))
    p.drawLine(QPointF(s * 0.15, s * 0.85), QPointF(s * 0.85, s * 0.85))  # X轴
    p.drawLine(QPointF(s * 0.15, s * 0.85), QPointF(s * 0.15, s * 0.15))  # Y轴
    # 线性折线：前平、中斜、后平
    p.setPen(
        QPen(
            QColor("#E67E22"),
            2.5,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    path = QPainterPath()
    path.moveTo(s * 0.15, s * 0.85)
    path.lineTo(s * 0.30, s * 0.85)  # 2% 下限截断
    path.lineTo(s * 0.70, s * 0.20)  # 98% 上限拉伸
    path.lineTo(s * 0.85, s * 0.20)
    p.drawPath(path)


def _draw_stretch_minmax(p: QPainter, s: float) -> None:
    # 极值拉伸 (双向箭头展开)
    p.setPen(QPen(QColor("#8E44AD"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.20, s * 0.50), QPointF(s * 0.80, s * 0.50))
    # 左箭头
    p.drawLine(QPointF(s * 0.35, s * 0.35), QPointF(s * 0.20, s * 0.50))
    p.drawLine(QPointF(s * 0.35, s * 0.65), QPointF(s * 0.20, s * 0.50))
    # 右箭头
    p.drawLine(QPointF(s * 0.65, s * 0.35), QPointF(s * 0.80, s * 0.50))
    p.drawLine(QPointF(s * 0.65, s * 0.65), QPointF(s * 0.80, s * 0.50))


def _draw_stretch_stddev(p: QPainter, s: float) -> None:
    # 正态高斯分布曲线 (2σ 标准差拉伸)
    p.setPen(
        QPen(
            QColor("#2980B9"),
            2.2,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    path = QPainterPath()
    path.moveTo(s * 0.15, s * 0.85)
    path.quadTo(s * 0.35, s * 0.80, s * 0.50, s * 0.20)  # 峰值
    path.quadTo(s * 0.65, s * 0.80, s * 0.85, s * 0.85)
    p.drawPath(path)
    # 坐标基线
    p.setPen(QPen(QColor("#BDC3C7"), 1.0))
    p.drawLine(QPointF(s * 0.10, s * 0.85), QPointF(s * 0.90, s * 0.85))


def _draw_display_rgb(p: QPainter, s: float) -> None:
    # RGB 三色方块
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#E74C3C")))  # Red
    p.drawRoundedRect(QRectF(s * 0.25, s * 0.15, s * 0.35, s * 0.35), 2, 2)
    p.setBrush(QBrush(QColor("#2ECC71")))  # Green
    p.drawRoundedRect(QRectF(s * 0.48, s * 0.35, s * 0.35, s * 0.35), 2, 2)
    p.setBrush(QBrush(QColor("#3498DB")))  # Blue
    p.drawRoundedRect(QRectF(s * 0.18, s * 0.48, s * 0.35, s * 0.35), 2, 2)


def _draw_display_pseudo(p: QPainter, s: float) -> None:
    # 伪彩色谱条 (Viridis 紫-青-黄渐变条)
    p.setPen(QPen(QColor("#2C3E50"), 1.0))
    colors = ["#440154", "#3B528B", "#21908C", "#5DC863", "#FDE725"]
    w = (s * 0.70) / len(colors)
    for i, col in enumerate(colors):
        p.setBrush(QBrush(QColor(col)))
        p.drawRect(QRectF(s * 0.15 + i * w, s * 0.30, w, s * 0.40))


def _draw_dual_view(p: QPainter, s: float) -> None:
    # 双视口并排对比 (Split Screen)
    p.setPen(QPen(QColor("#34495E"), 1.5))
    p.setBrush(QBrush(QColor("#EBF5FB")))
    p.drawRect(QRectF(s * 0.12, s * 0.20, s * 0.35, s * 0.60))
    p.setBrush(QBrush(QColor("#FEF9E7")))
    p.drawRect(QRectF(s * 0.53, s * 0.20, s * 0.35, s * 0.60))


def _draw_sync_link(p: QPainter, s: float) -> None:
    # 视口联动同步锁链 🔗
    p.setPen(QPen(QColor("#2980B9"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    # 环 1
    p.drawRoundedRect(QRectF(s * 0.20, s * 0.35, s * 0.35, s * 0.25), 4, 4)
    # 环 2
    p.drawRoundedRect(QRectF(s * 0.45, s * 0.40, s * 0.35, s * 0.25), 4, 4)


# ---------------------------------------------------------------------------
# 4. ROI 数字化与交互标定 (ROI & Vector Tools - ENVI 核心)
# ---------------------------------------------------------------------------


def _draw_roi_polygon(p: QPainter, s: float) -> None:
    # 经典多边形 ROI 绘制
    p.setPen(QPen(QColor("#E74C3C"), 1.8, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(QBrush(QColor(231, 76, 60, 60)))
    poly = QPainterPath()
    poly.moveTo(s * 0.20, s * 0.35)
    poly.lineTo(s * 0.50, s * 0.18)
    poly.lineTo(s * 0.82, s * 0.40)
    poly.lineTo(s * 0.68, s * 0.80)
    poly.lineTo(s * 0.32, s * 0.75)
    poly.closeSubpath()
    p.drawPath(poly)
    # 顶点手柄
    p.setPen(QPen(QColor("#C0392B"), 1.0))
    p.setBrush(QBrush(QColor("#FFFFFF")))
    for pt in [(0.20, 0.35), (0.50, 0.18), (0.82, 0.40), (0.68, 0.80), (0.32, 0.75)]:
        p.drawRect(QRectF(s * pt[0] - 2, s * pt[1] - 2, 4, 4))


def _draw_roi_rect(p: QPainter, s: float) -> None:
    # 矩形 ROI
    p.setPen(QPen(QColor("#E67E22"), 1.8, Qt.PenStyle.DashLine))
    p.setBrush(QBrush(QColor(230, 126, 34, 60)))
    p.drawRect(QRectF(s * 0.20, s * 0.25, s * 0.60, s * 0.50))
    # 4 角手柄
    p.setPen(QPen(QColor("#D35400"), 1.0))
    p.setBrush(QBrush(QColor("#FFFFFF")))
    for pt in [(0.20, 0.25), (0.80, 0.25), (0.80, 0.75), (0.20, 0.75)]:
        p.drawRect(QRectF(s * pt[0] - 2, s * pt[1] - 2, 4, 4))


def _draw_roi_probe(p: QPainter, s: float) -> None:
    # 像元探针 / 准星靶心 (Cursor Location Value / Identify)
    p.setPen(QPen(QColor("#27AE60"), 1.8))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(s * 0.50, s * 0.50), s * 0.30, s * 0.30)
    p.drawEllipse(QPointF(s * 0.50, s * 0.50), s * 0.12, s * 0.12)
    # 十字十字线
    p.drawLine(QPointF(s * 0.50, s * 0.12), QPointF(s * 0.50, s * 0.88))
    p.drawLine(QPointF(s * 0.12, s * 0.50), QPointF(s * 0.88, s * 0.50))


def _draw_roi_delete(p: QPainter, s: float) -> None:
    # 红色垃圾桶 / 删除选中 ROI
    p.setPen(QPen(QColor("#C0392B"), 1.5))
    p.setBrush(QBrush(QColor("#E74C3C")))
    # 桶盖
    p.drawRect(QRectF(s * 0.25, s * 0.25, s * 0.50, s * 0.08))
    p.drawRect(QRectF(s * 0.42, s * 0.18, s * 0.16, s * 0.07))
    # 桶身
    body = QPainterPath()
    body.moveTo(s * 0.28, s * 0.33)
    body.lineTo(s * 0.72, s * 0.33)
    body.lineTo(s * 0.66, s * 0.82)
    body.lineTo(s * 0.34, s * 0.82)
    body.closeSubpath()
    p.drawPath(body)


# ---------------------------------------------------------------------------
# 5. 遥感专题与核心算法快捷图标 (Remote Sensing Analysis)
# ---------------------------------------------------------------------------


def _draw_tool_ndvi(p: QPainter, s: float) -> None:
    # 植被指数 (嫩绿色叶片)
    p.setPen(QPen(QColor("#1E8449"), 1.5))
    p.setBrush(QBrush(QColor("#2ECC71")))
    leaf = QPainterPath()
    leaf.moveTo(s * 0.20, s * 0.80)
    leaf.quadTo(s * 0.20, s * 0.30, s * 0.80, s * 0.20)
    leaf.quadTo(s * 0.70, s * 0.80, s * 0.20, s * 0.80)
    p.drawPath(leaf)
    # 叶脉
    p.setPen(QPen(QColor("#FFFFFF"), 1.2))
    p.drawLine(QPointF(s * 0.20, s * 0.80), QPointF(s * 0.65, s * 0.35))


def _draw_tool_dem(p: QPainter, s: float) -> None:
    # DEM 地形与山体阴影 (双峰山峦)
    p.setPen(QPen(QColor("#5D4037"), 1.2))
    p.setBrush(QBrush(QColor("#8D6E63")))
    mountain = QPainterPath()
    mountain.moveTo(s * 0.12, s * 0.82)
    mountain.lineTo(s * 0.42, s * 0.28)  # 主峰
    mountain.lineTo(s * 0.62, s * 0.55)
    mountain.lineTo(s * 0.78, s * 0.38)  # 次峰
    mountain.lineTo(s * 0.90, s * 0.82)
    mountain.closeSubpath()
    p.drawPath(mountain)
    # 山顶积雪
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#FFFFFF")))
    snow = QPainterPath()
    snow.moveTo(s * 0.42, s * 0.28)
    snow.lineTo(s * 0.48, s * 0.40)
    snow.lineTo(s * 0.36, s * 0.40)
    snow.closeSubpath()
    p.drawPath(snow)


def _draw_tool_warp(p: QPainter, s: float) -> None:
    # 空间重投影网格
    p.setPen(QPen(QColor("#2980B9"), 1.5, Qt.PenStyle.SolidLine))
    p.setBrush(QBrush(QColor("#EAF2F8")))
    grid = QPainterPath()
    grid.moveTo(s * 0.25, s * 0.25)
    grid.quadTo(s * 0.50, s * 0.35, s * 0.78, s * 0.22)
    grid.quadTo(s * 0.70, s * 0.50, s * 0.85, s * 0.75)
    grid.quadTo(s * 0.50, s * 0.65, s * 0.20, s * 0.80)
    grid.quadTo(s * 0.30, s * 0.50, s * 0.25, s * 0.25)
    p.drawPath(grid)


def _draw_tool_stats(p: QPainter, s: float) -> None:
    # 统计直方图
    p.setPen(Qt.PenStyle.NoPen)
    bars = [
        (0.18, 0.40, "#3498DB"),
        (0.36, 0.65, "#2ECC71"),
        (0.54, 0.80, "#E67E22"),
        (0.72, 0.50, "#9B59B6"),
    ]
    for x, h, col in bars:
        p.setBrush(QBrush(QColor(col)))
        p.drawRect(QRectF(s * x, s * (0.85 - h * 0.70), s * 0.14, s * h * 0.70))
    p.setPen(QPen(QColor("#7F8C8D"), 1.2))
    p.drawLine(QPointF(s * 0.12, s * 0.85), QPointF(s * 0.90, s * 0.85))


def _draw_tool_pca(p: QPainter, s: float) -> None:
    # PCA 主成分旋转轴
    p.setPen(QPen(QColor("#34495E"), 1.0, Qt.PenStyle.DashLine))
    p.drawLine(QPointF(s * 0.20, s * 0.80), QPointF(s * 0.80, s * 0.80))
    p.drawLine(QPointF(s * 0.20, s * 0.80), QPointF(s * 0.20, s * 0.20))
    # 主成分旋转轴 (PC1)
    p.setPen(QPen(QColor("#E74C3C"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(QPointF(s * 0.25, s * 0.75), QPointF(s * 0.80, s * 0.25))
    # 散点
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#3498DB")))
    for pt in [(0.35, 0.60), (0.45, 0.50), (0.55, 0.52), (0.60, 0.38), (0.70, 0.32)]:
        p.drawEllipse(QPointF(s * pt[0], s * pt[1]), 2, 2)


def _draw_tool_fft(p: QPainter, s: float) -> None:
    # 傅里叶正弦波形
    p.setPen(QPen(QColor("#8E44AD"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    path = QPainterPath()
    path.moveTo(s * 0.15, s * 0.50)
    for i in range(25):
        t = i / 24.0
        x = s * (0.15 + t * 0.70)
        y = s * (0.50 - 0.28 * math.sin(t * 2.0 * math.pi * 1.5))
        path.lineTo(x, y)
    p.drawPath(path)


def _draw_tool_swipe(p: QPainter, s: float) -> None:
    # 卷帘对比：左右分屏矩形 + 垂直分割线 + 左右箭头
    p.setPen(Qt.PenStyle.NoPen)
    # 左半屏（深蓝）
    p.setBrush(QBrush(QColor("#2980B9")))
    p.drawRect(QRectF(s * 0.12, s * 0.18, s * 0.38, s * 0.64))
    # 右半屏（亮绿）
    p.setBrush(QBrush(QColor("#27AE60")))
    p.drawRect(QRectF(s * 0.50, s * 0.18, s * 0.38, s * 0.64))
    # 边框
    p.setPen(QPen(QColor("#2C3E50"), 1.6))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(s * 0.12, s * 0.18, s * 0.76, s * 0.64))
    # 中间分割虚线
    p.setPen(QPen(QColor("#F1C40F"), 2.0, Qt.PenStyle.DashLine))
    p.drawLine(QPointF(s * 0.50, s * 0.14), QPointF(s * 0.50, s * 0.86))
    # 左右对比小箭头
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#F1C40F")))
    # 左箭头
    p1 = QPainterPath()
    p1.moveTo(s * 0.40, s * 0.50)
    p1.lineTo(s * 0.46, s * 0.42)
    p1.lineTo(s * 0.46, s * 0.58)
    p1.closeSubpath()
    p.drawPath(p1)
    # 右箭头
    p2 = QPainterPath()
    p2.moveTo(s * 0.60, s * 0.50)
    p2.lineTo(s * 0.54, s * 0.42)
    p2.lineTo(s * 0.54, s * 0.58)
    p2.closeSubpath()
    p.drawPath(p2)


def _draw_tool_measure(p: QPainter, s: float) -> None:
    # 折线测距：刻度尺 + 斜向测距线与端点圆圈
    p.setPen(QPen(QColor("#D35400"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    path = QPainterPath()
    path.moveTo(s * 0.18, s * 0.78)
    path.lineTo(s * 0.48, s * 0.32)
    path.lineTo(s * 0.82, s * 0.55)
    p.drawPath(path)
    # 端点圆圈
    p.setPen(QPen(QColor("#BA4A00"), 1.5))
    p.setBrush(QBrush(QColor("#F39C12")))
    p.drawEllipse(QPointF(s * 0.18, s * 0.78), s * 0.07, s * 0.07)
    p.drawEllipse(QPointF(s * 0.48, s * 0.32), s * 0.07, s * 0.07)
    p.drawEllipse(QPointF(s * 0.82, s * 0.55), s * 0.07, s * 0.07)


def _draw_tool_profile(p: QPainter, s: float) -> None:
    # 像元光谱剖面：L型坐标轴 + 典型反射光谱连续曲线（含红边与吸收谷）
    p.setPen(QPen(QColor("#7F8C8D"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
    p.drawLine(QPointF(s * 0.15, s * 0.15), QPointF(s * 0.15, s * 0.85))
    p.drawLine(QPointF(s * 0.15, s * 0.85), QPointF(s * 0.88, s * 0.85))
    # 光谱曲线 (典型植被曲线：绿峰 + 红吸收谷 + 红边陡升 + 近红外高原)
    path = QPainterPath()
    path.moveTo(s * 0.18, s * 0.76)
    path.lineTo(s * 0.28, s * 0.65)  # 绿峰
    path.lineTo(s * 0.38, s * 0.78)  # 红光吸收谷
    path.lineTo(s * 0.52, s * 0.30)  # 红边陡升
    path.lineTo(s * 0.68, s * 0.28)  # NIR 高原
    path.lineTo(s * 0.75, s * 0.55)  # 水吸收谷
    path.lineTo(s * 0.85, s * 0.45)
    p.setPen(QPen(QColor("#00B4D8"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawPath(path)
    # 吸收谷亮点
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#E74C3C")))
    p.drawEllipse(QPointF(s * 0.38, s * 0.78), s * 0.06, s * 0.06)


def _draw_tool_play(p: QPainter, s: float) -> None:
    # 播放按钮：圆形底盘 + 向右播放实心三角
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#27AE60")))
    p.drawEllipse(QPointF(s * 0.50, s * 0.50), s * 0.38, s * 0.38)
    p.setBrush(QBrush(QColor("#FFFFFF")))
    path = QPainterPath()
    path.moveTo(s * 0.40, s * 0.32)
    path.lineTo(s * 0.68, s * 0.50)
    path.lineTo(s * 0.40, s * 0.68)
    path.closeSubpath()
    p.drawPath(path)


def _draw_tool_clip(p: QPainter, s: float) -> None:
    # 裁剪图标：多边形虚线框 + 剪刀
    poly = QPainterPath()
    poly.moveTo(s * 0.15, s * 0.35)
    poly.lineTo(s * 0.55, s * 0.15)
    poly.lineTo(s * 0.85, s * 0.45)
    poly.lineTo(s * 0.65, s * 0.85)
    poly.lineTo(s * 0.25, s * 0.80)
    poly.closeSubpath()

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(0, 180, 216, 40)))
    p.drawPath(poly)

    p.setPen(QPen(QColor("#00B4D8"), 1.8, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(poly)

    # 剪刀把柄
    p.setPen(QPen(QColor("#E67E22"), 2.0))
    p.drawEllipse(QPointF(s * 0.28, s * 0.30), s * 0.10, s * 0.10)
    p.drawEllipse(QPointF(s * 0.24, s * 0.52), s * 0.10, s * 0.10)

    # 剪刀刀刃
    blade = QPainterPath()
    blade.moveTo(s * 0.36, s * 0.33)
    blade.lineTo(s * 0.72, s * 0.62)
    blade.moveTo(s * 0.32, s * 0.50)
    blade.lineTo(s * 0.70, s * 0.26)
    p.setPen(QPen(QColor("#ECEFF1"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawPath(blade)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor("#D35400")))
    p.drawEllipse(QPointF(s * 0.48, s * 0.42), s * 0.045, s * 0.045)


# 图标分发注册表
_DRAWERS: dict[str, Callable[[QPainter, float], None]] = {
    # 文件与工程
    "new_project": _draw_new_project,
    "open_project": _draw_open_project,
    "save_project": _draw_save_project,
    "open_raster": _draw_open_raster,
    "open_remote": _draw_open_remote,
    # 导航
    "pan": _draw_pan,
    "zoom_in": _draw_zoom_in,
    "zoom_out": _draw_zoom_out,
    "zoom_full": _draw_zoom_full,
    "zoom_100": _draw_zoom_100,
    "refresh": _draw_refresh,
    # 拉伸与显示
    "stretch_linear": _draw_stretch_linear,
    "stretch_minmax": _draw_stretch_minmax,
    "stretch_stddev": _draw_stretch_stddev,
    "display_rgb": _draw_display_rgb,
    "display_pseudo": _draw_display_pseudo,
    "dual_view": _draw_dual_view,
    "sync_link": _draw_sync_link,
    # ROI
    "roi_polygon": _draw_roi_polygon,
    "roi_rect": _draw_roi_rect,
    "roi_probe": _draw_roi_probe,
    "roi_delete": _draw_roi_delete,
    # 算法
    "tool_ndvi": _draw_tool_ndvi,
    "tool_dem": _draw_tool_dem,
    "tool_warp": _draw_tool_warp,
    "tool_stats": _draw_tool_stats,
    "tool_pca": _draw_tool_pca,
    "tool_fft": _draw_tool_fft,
    "tool_swipe": _draw_tool_swipe,
    "tool_measure": _draw_tool_measure,
    "tool_profile": _draw_tool_profile,
    "tool_play": _draw_tool_play,
    "tool_clip": _draw_tool_clip,
    "roi_clip": _draw_tool_clip,
}


def get_icon(name: str, size: int = 24) -> QIcon:
    """获取指定名称的高清矢量图标，带有尺寸缓存。"""
    key = (name, size)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]

    drawer = _DRAWERS.get(name)
    if drawer is None:
        return QIcon()

    pix, p = _create_pixmap(size)
    try:
        drawer(p, float(size))
    finally:
        p.end()

    icon = QIcon(pix)
    # 为高分屏添加 2x 尺寸
    pix2x, p2x = _create_pixmap(size * 2)
    try:
        drawer(p2x, float(size * 2))
    finally:
        p2x.end()
    icon.addPixmap(pix2x)

    _ICON_CACHE[key] = icon
    return icon
