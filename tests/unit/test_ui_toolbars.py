"""多专业工具栏系统单元测试（ENVI / QGIS 风格）。

验证：
1. 28 个矢量图标生成完整性、非空性与缓存可用性。
2. 6 大专业工具栏初始化（项目管理、地图导航、ENVI 拉伸、ROI 标定、遥感算法、空间参考）。
3. 主工具栏向后兼容别名 `self.main_toolbar`。
4. 1:1 像元真实分辨率缩放 `zoom_to_100`。
5. 工具栏拉伸模式与显示模式快捷切换。
6. 遥感核心算法一键直通与表单激活。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from spectra_workbench.rendering.display import DisplayMode, StretchMode
from spectra_workbench.ui.icons import _ICON_CACHE, get_icon
from spectra_workbench.ui.main_window import MainWindow
from spectra_workbench.ui.models.layer import LayerItem

pytestmark = [pytest.mark.unit, pytest.mark.gui]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_vector_icons_generation(qapp: QApplication) -> None:
    """验证所有 28 种 ENVI / QGIS 矢量图标均能正确绘制为有效 QIcon。"""
    icon_names = [
        "new_project",
        "open_project",
        "save_project",
        "open_raster",
        "open_remote",
        "pan",
        "zoom_in",
        "zoom_out",
        "zoom_full",
        "zoom_100",
        "refresh",
        "stretch_linear",
        "stretch_minmax",
        "stretch_stddev",
        "display_rgb",
        "display_pseudo",
        "dual_view",
        "sync_link",
        "roi_polygon",
        "roi_rect",
        "roi_probe",
        "roi_delete",
        "tool_ndvi",
        "tool_dem",
        "tool_warp",
        "tool_stats",
        "tool_pca",
        "tool_fft",
    ]

    for name in icon_names:
        icon = get_icon(name, size=24)
        assert isinstance(icon, QIcon), f"图标 {name} 未返回 QIcon 实例"
        assert not icon.isNull(), f"图标 {name} 渲染为空图标"

    # 测试缓存命中
    assert ("new_project", 24) in _ICON_CACHE
    icon_cached = get_icon("new_project", size=24)
    assert not icon_cached.isNull()

    # 测试未知图标安全回退（返回空 QIcon，不抛异常）
    fallback_icon = get_icon("non_existent_icon_name")
    assert isinstance(fallback_icon, QIcon)
    assert fallback_icon.isNull()


def test_main_window_multi_toolbars(qapp: QApplication, tmp_path: Path) -> None:
    """验证主窗口包含 6 大 ENVI/QGIS 专业工具栏，并满足命名与向后兼容契约。"""
    win = MainWindow(project_root=tmp_path)
    try:
        # 1. 验证 6 大工具栏存在
        assert hasattr(win, "tb_project")
        assert hasattr(win, "tb_nav")
        assert hasattr(win, "tb_stretch")
        assert hasattr(win, "tb_roi")
        assert hasattr(win, "tb_analysis")
        assert hasattr(win, "tb_crs")

        # 2. 验证 objectName 规范（供布局持久化序列化）
        assert win.tb_project.objectName() == "toolbar_project"
        assert win.tb_nav.objectName() == "toolbar_nav"
        assert win.tb_stretch.objectName() == "toolbar_stretch"
        assert win.tb_roi.objectName() == "toolbar_roi"
        assert win.tb_analysis.objectName() == "toolbar_analysis"
        assert win.tb_crs.objectName() == "toolbar_crs"

        # 3. 验证主工具栏向后兼容别名
        assert win.main_toolbar is win.tb_project

        # 4. 验证工具栏上的 Action 数量合理
        assert len(win.tb_project.actions()) >= 7
        assert len(win.tb_nav.actions()) >= 6
        assert len(win.tb_stretch.actions()) >= 7
        assert len(win.tb_roi.actions()) >= 4
        assert len(win.tb_analysis.actions()) >= 6
    finally:
        win.close()


def test_canvas_zoom_to_100(qapp: QApplication, tmp_path: Path) -> None:
    """验证 1:1 像元真实比例缩放动作执行安全无异常。"""
    win = MainWindow(project_root=tmp_path)
    try:
        # 执行 1:1 缩放
        win.canvas.zoom_to_100()
        # 触发刷新
        win._on_refresh_views()
    finally:
        win.close()


def test_toolbar_stretch_and_display_modes(qapp: QApplication, tmp_path: Path) -> None:
    """验证从工具栏切换拉伸模式与显示模式。"""
    win = MainWindow(project_root=tmp_path)
    try:
        # 无图层时调用不会抛异常，状态栏友好提示
        win.apply_stretch_mode(StretchMode.min_max)
        assert "请先选择或打开图层" in win.status_bar.currentMessage()

        win.apply_display_mode(DisplayMode.pseudo)
        assert "请先选择或打开图层" in win.status_bar.currentMessage()

        # 注入一个单波段测试图层
        sample_layer = LayerItem(
            name="Test Raster",
            file_path=tmp_path / "test.tif",
            metadata={"band_count": 1, "width": 256, "height": 256, "crs": "EPSG:4326"},
            layer_id="test_lyr",
            provider_hint="gdal",
        )
        win.canvas.set_layer(sample_layer)

        # 单波段图层尝试切 RGB 应拦截并提示
        win.apply_display_mode(DisplayMode.rgb)
        assert "无法进行 RGB 合成" in win.status_bar.currentMessage()

        # 切伪彩色成功
        win.apply_display_mode(DisplayMode.pseudo)
        assert win.canvas.current_layer is not None
        assert win.canvas.current_layer.display.mode == DisplayMode.pseudo

        # 切换拉伸模式
        win.apply_stretch_mode(StretchMode.percentile)
        assert win.canvas.current_layer.display.stretch == StretchMode.percentile
        assert win.canvas.current_layer.display.percentiles == (2.0, 98.0)

        win.apply_stretch_mode(StretchMode.stddev)
        assert win.canvas.current_layer.display.stretch == StretchMode.stddev
        assert win.canvas.current_layer.display.stddev_k == 2.0
    finally:
        win.close()


def test_quick_launch_capability(qapp: QApplication, tmp_path: Path) -> None:
    """验证从工具栏一键快捷启动遥感专用算法。"""
    win = MainWindow(project_root=tmp_path)
    try:
        # 快捷启动 NDVI
        win.quick_launch_capability("gdal:sindex")
        assert win.tab_right.currentWidget() is win.form_widget
        assert win.form_widget._current_cap is not None
        assert win.form_widget._current_cap.capability_id == "gdal:sindex"
        assert "已激活算法工具" in win.status_bar.currentMessage()

        # 快捷启动 DEM
        win.quick_launch_capability("gdal:dem")
        assert win.form_widget._current_cap is not None
        assert win.form_widget._current_cap.capability_id == "gdal:dem"

        # 快捷启动 空间重投影
        win.quick_launch_capability("gdal:warp")
        assert win.form_widget._current_cap is not None
        assert win.form_widget._current_cap.capability_id == "gdal:warp"
    finally:
        win.close()
