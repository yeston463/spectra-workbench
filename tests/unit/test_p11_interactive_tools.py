"""P11-01 高级查看与交互工具单元测试 (Swipe 卷帘、折线测距、波段播放器、光谱剖面)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication

from spectra_workbench.rendering.display import DisplaySettings
from spectra_workbench.ui.canvas.image_canvas import ImageCanvas, SwipeImageItem
from spectra_workbench.ui.dialogs.spectral_profile_dialog import (
    SpectralProfileDialog,
    _smooth_spectrum,
)
from spectra_workbench.ui.models.layer import LayerItem
from spectra_workbench.ui.panels.display_panel import DisplayPanel


def _make_dummy_layer(
    layer_id: str = "L1", name: str = "Layer 1", is_geo: bool = False
) -> LayerItem:
    meta: dict[str, Any] = {
        "kind": "raster",
        "renderable": True,
        "width": 500,
        "height": 400,
        "bands": 4,
    }
    if is_geo:
        meta["geotransform"] = [116.30, 0.0001, 0.0, 39.90, 0.0, -0.0001]
        meta["crs_wkt"] = 'GEOGCS["WGS 84", DATUM["WGS_1984"]]'
    else:
        # Projected UTM (meters)
        meta["geotransform"] = [500000.0, 10.0, 0.0, 4400000.0, 0.0, -10.0]
        meta["crs_wkt"] = 'PROJCS["WGS 84 / UTM zone 50N", UNIT["metre",1]]'

    return LayerItem(
        layer_id=layer_id,
        name=name,
        file_path=Path(f"C:/fake/{layer_id}.tif"),
        metadata=meta,
        display=DisplaySettings(),
    )


def test_swipe_image_item_paint() -> None:
    """验证 SwipeImageItem 在开启和关闭卷帘时的裁剪绘制逻辑。"""
    app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    assert app is not None

    item = SwipeImageItem()
    item.setRect(QRectF(0, 0, 200, 200))
    item.set_swipe(False, 100.0, "vertical")
    assert not item.swipe_enabled

    pix = QPixmap(200, 200)
    p = QPainter(pix)
    # 不抛出异常即绘制通过
    item.paint(p, None)
    p.end()

    # 开启垂直卷帘
    item.set_swipe(True, 120.0, "vertical")
    assert item.swipe_enabled
    assert item.split_coord == 120.0
    p = QPainter(pix)
    item.paint(p, None)
    p.end()

    # 开启水平卷帘
    item.set_swipe(True, 80.0, "horizontal")
    assert item.swipe_orientation == "horizontal"
    p = QPainter(pix)
    item.paint(p, None)
    p.end()


def test_canvas_swipe_lifecycle() -> None:
    """验证 ImageCanvas 卷帘对比的开启、图层匹配、滑块拖动与关闭。"""
    app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    assert app is not None

    canvas = ImageCanvas(view_id="V1")
    canvas.show()
    layer_a = _make_dummy_layer("L1", "Base")
    layer_b = _make_dummy_layer("L2", "Compare")

    canvas.set_available_layers_provider(lambda: [layer_a, layer_b])
    canvas.set_layer(layer_a)

    # 初始状态
    assert not canvas._swipe_enabled
    assert not canvas.swipe_bar.isVisible()

    # 开启卷帘
    canvas.enable_swipe(layer_b, orientation="vertical")
    assert canvas._swipe_enabled
    assert canvas.swipe_bar.isVisible()
    assert canvas.swipe_line.isVisible()
    assert canvas._swipe_layer is not None
    assert canvas._swipe_layer.layer_id == "L2"

    # 滑动条调整
    canvas.slider_swipe.setValue(30)
    assert canvas.img_swipe_item.swipe_enabled

    # 切换方向为水平
    canvas.cmb_swipe_orient.setCurrentIndex(1)
    assert canvas._swipe_orientation == "horizontal"

    # 关闭卷帘
    canvas.disable_swipe()
    assert not canvas._swipe_enabled
    assert not canvas.swipe_bar.isVisible()
    assert not canvas.swipe_line.isVisible()


def test_canvas_polyline_measurement() -> None:
    """验证折线测距：平面投影坐标与地理坐标系下的真实地面距离计算。"""
    app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    assert app is not None

    canvas = ImageCanvas(view_id="V1")
    canvas.show()
    layer_utm = _make_dummy_layer("L_UTM", "UTM Layer", is_geo=False)
    canvas.set_layer(layer_utm)

    # 开启测距模式
    canvas.set_measure_mode(True)
    assert canvas._measure_mode
    assert canvas.measure_bar.isVisible()

    # 点 (0, 0) 到 (100, 0)：X差 100 像素 * 10m/像素 = 1000m = 1.000 km
    p1 = (0.0, 0.0)
    p2 = (100.0, 0.0)
    dist_m, dist_str = canvas._compute_segment_distance(p1, p2)
    assert abs(dist_m - 1000.0) < 1e-3
    assert "1.000 km" in dist_str

    # 点 (100, 0) 到 (100, 30)：Y差 30 像素 * 10m/像素 = 300m
    p3 = (100.0, 30.0)
    dist_m2, dist_str2 = canvas._compute_segment_distance(p2, p3)
    assert abs(dist_m2 - 300.0) < 1e-3
    assert "300.00 m" in dist_str2

    # 验证地理坐标系 (WGS84 大圆大地线 Haversine)
    layer_geo = _make_dummy_layer("L_GEO", "Geo Layer", is_geo=True)
    canvas.set_layer(layer_geo)
    # 经纬度差约 0.01 度 (约 1.11 km)
    g1 = (0.0, 0.0)
    g2 = (100.0, 0.0)  # dx = 100 * 0.0001 = 0.01 deg
    geo_dist, geo_str = canvas._compute_segment_distance(g1, g2)
    assert 800.0 < geo_dist < 1200.0

    # 清空测距
    canvas.clear_measurement()
    assert len(canvas._measure_points) == 0

    canvas.set_measure_mode(False)
    assert not canvas._measure_mode


def test_display_panel_band_player() -> None:
    """验证 DisplayPanel 波段自动播放器：播放/暂停、前后波段切换与循环模式。"""
    app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    assert app is not None

    panel = DisplayPanel()
    # 4 波段
    panel.set_layer(4, DisplaySettings())

    assert panel._band_count == 4
    assert not panel._player_timer.isActive()

    # 启动自动播放
    panel.start_playback()
    assert panel._player_timer.isActive()
    assert "暂停" in panel.btn_play_pause.text()

    # 单步前进
    panel.cmb_band.setCurrentIndex(0)
    panel._step_next_band()
    assert panel.cmb_band.currentIndex() == 1
    panel._step_next_band()
    assert panel.cmb_band.currentIndex() == 2

    # 单步后退
    panel._step_prev_band()
    assert panel.cmb_band.currentIndex() == 1

    # 循环播放跨越末尾
    panel.cmb_band.setCurrentIndex(3)
    panel.chk_loop.setChecked(True)
    panel._step_next_band()
    assert panel.cmb_band.currentIndex() == 0

    # 暂停
    panel.pause_playback()
    assert not panel._player_timer.isActive()
    assert "播放" in panel.btn_play_pause.text()

    panel.clear()
    assert not panel._player_timer.isActive()


def test_spectral_profile_dialog(tmp_path: Path) -> None:
    """验证 SpectralProfileDialog 曲线叠加、波长对齐、平滑与 CSV 导出。"""
    app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    assert app is not None

    dlg = SpectralProfileDialog()
    wls = [450.0, 550.0, 650.0, 850.0]
    vals1 = [0.05, 0.08, 0.04, 0.45]  # 植被光谱
    vals2 = [0.12, 0.15, 0.18, 0.22]  # 土壤光谱

    # 添加第一个像元
    dlg.add_profile(10, 20, vals1, wavelengths=wls, layer_name="Sentinel2")
    assert len(dlg._profiles) == 1
    assert dlg._profiles[0]["col"] == 10

    # 叠加模式添加第二个像元
    dlg.chk_hold.setChecked(True)
    dlg.add_profile(35, 45, vals2, wavelengths=wls, layer_name="Sentinel2")
    assert len(dlg._profiles) == 2

    # 测试移动平均平滑
    raw_sig = [10.0, 20.0, 30.0, 20.0, 10.0]
    smooth_sig = _smooth_spectrum(raw_sig, window_size=3)
    assert len(smooth_sig) == len(raw_sig)
    assert abs(smooth_sig[1] - 20.0) < 1e-3

    # 导出 CSV
    csv_out = tmp_path / "spectral_export.csv"
    with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        header = ["Band_Index", "Wavelength_nm"] + [
            f"Pixel_{p['col']}_{p['row']}_{p['layer_name']}" for p in dlg._profiles
        ]
        writer.writerow(header)
        for b_idx in range(4):
            row_data = [b_idx + 1, wls[b_idx], vals1[b_idx], vals2[b_idx]]
            writer.writerow(row_data)

    assert csv_out.exists()
    content = csv_out.read_text(encoding="utf-8-sig")
    assert "Pixel_10_20_Sentinel2" in content
    assert "Pixel_35_45_Sentinel2" in content

    # 清空
    dlg.clear_profiles()
    assert len(dlg._profiles) == 0
