"""P3 交互式多边形 ROI 实际鼠标交互验收测试。

严格遵守 AGENTS.md 约束：
1. 使用真实 Qt 鼠标事件（QTest.mouseClick / mousePress / mouseMove / mouseRelease），
   不以 add_polygon_vertex 内部打桩代替交互。
2. 覆盖：
   - 真实鼠标单击顶点、右键/起点闭合多边形 ROI
   - 多边形 ROI 选择与手柄真实拖拽移动
   - 多边形 ROI 删除与图层/面板同步
   - 保存项目并重开恢复多边形 ROI
   - 多图层切换下 ROI 所属图层隔离与恢复
   - 旋转 Affine 栅格与不同 View CRS（pixel vs map）下的坐标空间与点位持久化
3. GUI 进程零原生地理依赖（无 import osgeo/rasterio）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from spectra_workbench.domain.roi import ROIShape
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


def test_interactive_polygon_roi_lifecycle_with_real_mouse_events(
    qapp: QApplication, tmp_path: Path, p3_samples: dict
) -> None:
    """真实鼠标事件完成多边形 ROI：绘制、闭合、选择、手柄拖动、保存重开与删除。"""
    north_path = Path(p3_samples["samples"]["north"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(north_path)
        _wait_layers(win, qapp, count=1)
        layer = win.layer_panel.layers[0]

        # 1. 激活多边形绘制模式并验证
        win.canvas.set_roi_draw(ROIShape.polygon, True)
        assert win.canvas._draw_shape == ROIShape.polygon

        vp = win.canvas.gl_widget.viewport()

        # 2. 真实鼠标点击 3 个点
        p1 = QPoint(150, 150)
        p2 = QPoint(280, 150)
        p3 = QPoint(220, 260)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p2)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p3)
        assert len(win.canvas._poly_pts) == 3

        # 3. 真实右键点击闭合多边形
        QTest.mouseClick(vp, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, p3)
        qapp.processEvents()

        rois = win.canvas.roi_list()
        assert len(rois) == 1
        roi = rois[0]
        assert roi.shape == ROIShape.polygon
        assert len(roi.points) == 3
        assert roi.area_pixels() > 0
        assert roi.attributes.get("layer_id") == layer.layer_id
        assert len(layer.rois) == 1

        # 4. 选择 ROI
        win.canvas.select_roi(roi.roi_id)
        assert win.canvas._selected_roi_id == roi.roi_id

        # 5. 真实鼠标拖动手柄顶点
        orig_p0 = roi.points[0]
        state = win.canvas._roi_states[roi.roi_id]
        item = state["item"]
        handles = item.getHandles()
        assert len(handles) == 3
        h0_view_pos = win.canvas.gl_widget.mapFromScene(handles[0].scenePos())

        # 拖动手柄 0
        drag_target = h0_view_pos + QPoint(30, 20)
        QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, h0_view_pos)
        QTest.mouseMove(vp, drag_target)
        QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, drag_target)
        qapp.processEvents()

        roi_dragged = win.canvas.roi_list()[0]
        assert roi_dragged.points[0] != orig_p0, "拖动手柄后顶点坐标必须被真实更新"

        # 6. 保存项目
        proj_file = tmp_path / "interactive_roi_proj.spectra.json"
        with patch("spectra_workbench.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(proj_file), "")):
            win.save_project_action(save_as=True)
        assert proj_file.exists()
    finally:
        win.close()
        qapp.processEvents()

    # 7. 重开项目，验证多边形 ROI 完整恢复
    win2 = MainWindow()
    win2.show()
    try:
        win2.open_project(proj_file)
        _wait_layers(win2, qapp, count=1)
        layer2 = win2.layer_panel.layers[0]

        rois_restored = win2.canvas.roi_list()
        assert len(rois_restored) == 1
        r_res = rois_restored[0]
        assert r_res.shape == ROIShape.polygon
        assert len(r_res.points) == 3
        assert r_res.attributes.get("layer_id") == layer2.layer_id

        # 验证点位与拖拽后的点位一致
        for (x_exp, y_exp), (x_act, y_act) in zip(roi_dragged.points, r_res.points, strict=True):
            assert x_act == pytest.approx(x_exp, abs=1e-3)
            assert y_act == pytest.approx(y_exp, abs=1e-3)

        # 8. 删除 ROI
        win2.canvas.select_roi(r_res.roi_id)
        deleted_id = win2.canvas.delete_selected_roi()
        assert deleted_id == r_res.roi_id
        assert len(win2.canvas.roi_list()) == 0
        assert len(layer2.rois) == 0
    finally:
        win2.close()
        qapp.processEvents()


def test_interactive_polygon_roi_layer_switching_and_isolation(
    qapp: QApplication, tmp_path: Path, p3_samples: dict
) -> None:
    """多图层切换下，多边形 ROI 保持正确的图层归属与显示隔离。"""
    north_path = Path(p3_samples["samples"]["north"]["path"]).resolve()
    spike_path = Path(p3_samples["samples"]["spike"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(north_path)
        win.open_dataset(spike_path)
        _wait_layers(win, qapp, count=2)

        layer_north = win.layer_panel.layers[0]
        layer_spike = win.layer_panel.layers[1]

        # 1. 选中第一个图层，绘制 ROI-A
        win.layer_panel.sig_layer_selected.emit(layer_north)
        qapp.processEvents()
        win.canvas.set_roi_draw(ROIShape.polygon, True)
        vp = win.canvas.gl_widget.viewport()

        p1 = QPoint(100, 100)
        p2 = QPoint(200, 100)
        p3 = QPoint(150, 180)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p2)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p3)
        # 点击起点附近闭合
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(102, 101))
        qapp.processEvents()

        assert len(win.canvas.roi_list()) == 1
        roi_a = win.canvas.roi_list()[0]
        assert roi_a.attributes.get("layer_id") == layer_north.layer_id

        # 2. 切换到第二个图层，画布 ROI 列表必须自动隔离（为空）
        win.layer_panel.sig_layer_selected.emit(layer_spike)
        qapp.processEvents()
        assert len(win.canvas.roi_list()) == 0

        # 3. 在第二个图层绘制 ROI-B
        win.canvas.set_roi_draw(ROIShape.polygon, True)
        pb1 = QPoint(250, 250)
        pb2 = QPoint(350, 250)
        pb3 = QPoint(300, 350)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pb1)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pb2)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pb3)
        QTest.mouseClick(vp, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pb3)
        qapp.processEvents()

        assert len(win.canvas.roi_list()) == 1
        roi_b = win.canvas.roi_list()[0]
        assert roi_b.attributes.get("layer_id") == layer_spike.layer_id

        # 4. 切回第一个图层，应只显示 ROI-A
        win.layer_panel.sig_layer_selected.emit(layer_north)
        qapp.processEvents()
        rois_layer1 = win.canvas.roi_list()
        assert len(rois_layer1) == 1
        assert rois_layer1[0].roi_id == roi_a.roi_id

        # 5. 切回第二个图层，应只显示 ROI-B
        win.layer_panel.sig_layer_selected.emit(layer_spike)
        qapp.processEvents()
        rois_layer2 = win.canvas.roi_list()
        assert len(rois_layer2) == 1
        assert rois_layer2[0].roi_id == roi_b.roi_id

        # 6. 保存并重开，验证两个图层各自的 ROI 归属完全保全
        proj_file = tmp_path / "multi_layer_roi.spectra.json"
        with patch("spectra_workbench.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(proj_file), "")):
            win.save_project_action(save_as=True)
    finally:
        win.close()
        qapp.processEvents()

    win2 = MainWindow()
    win2.show()
    try:
        win2.open_project(proj_file)
        _wait_layers(win2, qapp, count=2)

        l1 = next(lyr for lyr in win2.layer_panel.layers if lyr.name == layer_north.name)
        l2 = next(lyr for lyr in win2.layer_panel.layers if lyr.name == layer_spike.name)

        assert len(l1.rois) == 1
        assert l1.rois[0].roi_id == roi_a.roi_id
        assert len(l2.rois) == 1
        assert l2.rois[0].roi_id == roi_b.roi_id
    finally:
        win2.close()
        qapp.processEvents()


def test_interactive_polygon_roi_rotated_affine_and_view_crs(
    qapp: QApplication, tmp_path: Path, p3_samples: dict
) -> None:
    """旋转 Affine 栅格与 View CRS 切换下的多边形 ROI 交互与持久化。"""
    rot_path = Path(p3_samples["samples"]["rotated"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rot_path)
        _wait_layers(win, qapp, count=1)
        layer = win.layer_panel.layers[0]
        assert layer.metadata.get("spatial_model") == "affine"

        # 1. 在像素坐标系下绘制多边形 ROI
        win.canvas.set_roi_draw(ROIShape.polygon, True)
        vp = win.canvas.gl_widget.viewport()

        p1 = QPoint(120, 120)
        p2 = QPoint(220, 120)
        p3 = QPoint(170, 200)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p2)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p3)
        QTest.mouseClick(vp, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, p3)
        qapp.processEvents()

        rois = win.canvas.roi_list()
        assert len(rois) == 1
        roi_pix = rois[0]
        assert roi_pix.coordinate_space == "pixel"
        assert roi_pix.area_pixels() > 0

        # 2. 切换视图 CRS 到 EPSG:4326 (WGS84 经纬度)
        win.cmb_view_crs.setCurrentIndex(2)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not win.canvas.view_crs_wkt:
            qapp.processEvents()
            time.sleep(0.02)
        assert win.canvas.view_crs_wkt is not None

        # 3. 在地图 CRS 视图下真实鼠标绘制多边形 ROI（位置避开第一个 ROI 避免误触段添加手柄）
        win.canvas.set_roi_draw(ROIShape.polygon, True)
        pm1 = QPoint(320, 320)
        pm2 = QPoint(420, 320)
        pm3 = QPoint(370, 400)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pm1)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pm2)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pm3)
        QTest.mouseClick(vp, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, pm3)
        qapp.processEvents()

        rois_all = win.canvas.roi_list()
        assert len(rois_all) == 2
        roi_map = next(r for r in rois_all if r.roi_id != roi_pix.roi_id)
        assert roi_map.coordinate_space == "map"
        assert roi_map.crs_wkt is not None and len(roi_map.crs_wkt) > 0
        assert roi_map.area_pixels() > 0

        # 4. 保存项目
        proj_file = tmp_path / "rotated_affine_roi.spectra.json"
        with patch("spectra_workbench.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(proj_file), "")):
            win.save_project_action(save_as=True)
    finally:
        win.close()
        qapp.processEvents()

    # 5. 重开验证旋转 Affine 和不同坐标空间的 ROI 恢复
    win2 = MainWindow()
    win2.show()
    try:
        win2.open_project(proj_file)
        _wait_layers(win2, qapp, count=1)

        restored_rois = win2.canvas.roi_list()
        assert len(restored_rois) == 2
        r_pix = next(r for r in restored_rois if r.coordinate_space == "pixel")
        r_map = next(r for r in restored_rois if r.coordinate_space == "map")

        assert len(r_pix.points) == 3
        assert len(r_map.points) == 3
        assert r_map.crs_wkt == roi_map.crs_wkt
    finally:
        win2.close()
        qapp.processEvents()
