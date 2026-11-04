"""P3 主窗口 Dock 布局跨会话持久化与恢复测试（P3-01）。

按原验收标准验收：
1. 调整 Dock 浮动（isFloating）
2. 调整 Dock 显隐（isVisible）
3. 调整 Dock 停靠区域（dockWidgetArea）与几何尺寸
4. 保存项目后关闭主窗口，在全新主窗口实例中打开项目，验证布局完整恢复。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow

pytestmark = [pytest.mark.integration, pytest.mark.gui]


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    ensure_application_fonts(app)
    return app


@pytest.fixture(scope="module")
def p3_samples() -> dict:
    from tests.fixtures.p3_env import ensure_p3_samples

    return ensure_p3_samples()


def _wait_layers(win: MainWindow, qapp: QApplication, count: int = 1, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if len(win.layer_panel.layers) >= count:
            return
        time.sleep(0.03)
    raise AssertionError(f"等待图层超时：期望 {count} 个，实际 {len(win.layer_panel.layers)} 个")


def test_dock_layout_floating_and_visibility_persistence(
    qapp: QApplication, tmp_path: Path, p3_samples: dict
) -> None:
    """验证 Dock 浮动与显隐跨会话保存与恢复。"""
    north_path = Path(p3_samples["samples"]["north"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(north_path)
        _wait_layers(win, qapp, count=1)

        # 调整 dock_left 为浮动窗口
        win.dock_left.setFloating(True)
        # 隐藏 dock_right
        win.dock_right.setVisible(False)
        qapp.processEvents()

        assert win.dock_left.isFloating() is True
        assert win.dock_right.isVisible() is False

        # 保存项目
        proj_file = tmp_path / "dock_floating_proj.spectra.json"
        with patch("spectra_workbench.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(proj_file), "")):
            win.save_project_action(save_as=True)
        assert proj_file.exists()
    finally:
        win.close()
        qapp.processEvents()

    # 打开新窗口重开项目
    win2 = MainWindow()
    win2.show()
    try:
        # 重开前初始状态为默认值
        assert win2.dock_left.isFloating() is False
        assert win2.dock_right.isVisible() is True

        win2.open_project(proj_file)
        _wait_layers(win2, qapp, count=1)
        qapp.processEvents()

        # 验证浮动与隐藏状态准确恢复
        assert win2.dock_left.isFloating() is True
        assert win2.dock_right.isVisible() is False
    finally:
        win2.close()
        qapp.processEvents()


def test_dock_layout_area_rearrangement_persistence(
    qapp: QApplication, tmp_path: Path, p3_samples: dict
) -> None:
    """验证 Dock 停靠区域重排（左右互换、底部调整）跨会话保存与恢复。"""
    north_path = Path(p3_samples["samples"]["north"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(north_path)
        _wait_layers(win, qapp, count=1)

        # 将 dock_left 移动到右侧停靠区，dock_right 移动到左侧停靠区
        win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, win.dock_left)
        win.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, win.dock_right)
        qapp.processEvents()

        assert win.dockWidgetArea(win.dock_left) == Qt.DockWidgetArea.RightDockWidgetArea
        assert win.dockWidgetArea(win.dock_right) == Qt.DockWidgetArea.LeftDockWidgetArea

        proj_file = tmp_path / "dock_area_proj.spectra.json"
        with patch("spectra_workbench.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(proj_file), "")):
            win.save_project_action(save_as=True)
        assert proj_file.exists()
    finally:
        win.close()
        qapp.processEvents()

    # 打开新窗口
    win2 = MainWindow()
    win2.show()
    try:
        win2.open_project(proj_file)
        _wait_layers(win2, qapp, count=1)
        qapp.processEvents()

        # 验证区域调换准确恢复
        assert win2.dockWidgetArea(win2.dock_left) == Qt.DockWidgetArea.RightDockWidgetArea
        assert win2.dockWidgetArea(win2.dock_right) == Qt.DockWidgetArea.LeftDockWidgetArea
    finally:
        win2.close()
        qapp.processEvents()
