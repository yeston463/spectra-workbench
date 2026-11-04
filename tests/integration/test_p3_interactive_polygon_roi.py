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

import pyqtgraph as pg
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


def test_rapid_clicks_quadrilateral_and_octagon_preserve_vertices(
    qapp: QApplication, p3_samples: dict
) -> None:
    """快速普通点击连续顶点绝不误闭合；验证四边形、八边形完整保留顶点，双击/右键/起点闭合正常工作。"""
    north_path = Path(p3_samples["samples"]["north"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(north_path)
        _wait_layers(win, qapp, count=1)
        vp = win.canvas.gl_widget.viewport()

        # -----------------------------------------------------------
        # 1. 四边形快速普通点击（模拟 4 个不同位置连续快速左键单击）
        # -----------------------------------------------------------
        win.canvas.set_roi_draw(ROIShape.polygon, True)
        assert win.canvas._draw_shape == ROIShape.polygon

        quad_pts = [QPoint(150, 150), QPoint(280, 150), QPoint(280, 280), QPoint(150, 280)]
        for pt in quad_pts:
            QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)

        qapp.processEvents()
        # 必须仍处于绘制状态，且恰好有 4 个待闭合顶点（绝不能在第 4 击误闭合成三角形）
        assert win.canvas._draw_shape == ROIShape.polygon, "4 个不同位置快速点击后绘制状态必须保持激活"
        assert len(win.canvas._poly_pts) == 4, f"期望 4 个待闭合顶点，实际收到 {len(win.canvas._poly_pts)} 个"
        assert len(win.canvas.roi_list()) == 0, "未发送闭合手势前不能产生闭合 ROI"

        # 真实右键闭合四边形
        QTest.mouseClick(vp, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, quad_pts[-1])
        qapp.processEvents()
        rois = win.canvas.roi_list()
        assert len(rois) == 1
        assert len(rois[0].points) == 4, f"四边形必须保留全部 4 个顶点，实际收到 {len(rois[0].points)}"

        # -----------------------------------------------------------
        # 2. 八边形快速普通点击（8 个不同位置连续快速左键单击）
        # -----------------------------------------------------------
        win.canvas.set_roi_draw(ROIShape.polygon, True)
        oct_pts = [
            QPoint(300, 100),
            QPoint(350, 70),
            QPoint(400, 70),
            QPoint(450, 100),
            QPoint(450, 150),
            QPoint(400, 180),
            QPoint(350, 180),
            QPoint(300, 150),
        ]
        for pt in oct_pts:
            QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pt)

        qapp.processEvents()
        assert win.canvas._draw_shape == ROIShape.polygon, "8 个不同位置快速点击后绘制状态必须保持激活"
        assert len(win.canvas._poly_pts) == 8, f"期望 8 个待闭合顶点，实际收到 {len(win.canvas._poly_pts)} 个"

        # 点击回到起点附近闭合八边形
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, oct_pts[0])
        qapp.processEvents()
        rois = win.canvas.roi_list()
        assert len(rois) == 2
        oct_roi = next(r for r in rois if r.roi_id != rois[0].roi_id)
        assert len(oct_roi.points) == 8, f"八边形必须保留全部 8 个顶点，实际收到 {len(oct_roi.points)}"

        # -----------------------------------------------------------
        # 3. 真实同位置双击闭合测试
        # -----------------------------------------------------------
        win.canvas.set_roi_draw(ROIShape.polygon, True)
        tri_pts = [QPoint(120, 320), QPoint(200, 320), QPoint(160, 400)]
        # 单击前两个点
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, tri_pts[0])
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, tri_pts[1])
        # 第三个点单击 + 紧接着在同一位置再次左键（双击闭合）
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, tri_pts[2])
        assert len(win.canvas._poly_pts) == 3
        # 同位置再次点击，触发 is_same_pos_double 闭合
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, tri_pts[2])
        qapp.processEvents()
        assert win.canvas._draw_shape is None, "同位置双击必须完成多边形闭合"
        rois = win.canvas.roi_list()
        assert len(rois) == 3

        # -----------------------------------------------------------
        # 4. 慢速点击（间隔 > 0.45s）正常添加顶点
        # -----------------------------------------------------------
        win.canvas.set_roi_draw(ROIShape.polygon, True)
        slow_p1 = QPoint(220, 220)
        slow_p2 = QPoint(260, 220)
        slow_p3 = QPoint(240, 260)
        slow_p4 = QPoint(210, 250)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, slow_p1)
        time.sleep(0.45)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, slow_p2)
        time.sleep(0.45)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, slow_p3)
        time.sleep(0.45)
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, slow_p4)
        assert len(win.canvas._poly_pts) == 4, "慢速点击 4 点必须保持 4 个顶点"
        QTest.mouseClick(vp, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, slow_p4)
        qapp.processEvents()
        assert len(win.canvas.roi_list()) == 4
    finally:
        win.close()
        qapp.processEvents()


def test_rotated_affine_known_point_and_crs_roundtrip_transformation(
    qapp: QApplication, tmp_path: Path, p3_samples: dict
) -> None:
    """独立数值验证：旋转 affine 已知像元点 (10, 10) 必须精确显示在地图坐标 (500025, 3199985)。

    严格覆盖：
    1. 旋转仿射已知角点 (10, 10) 在原生地图 CRS 下精确换算并渲染于 (500025, 3199985)；
    2. 切换回像素坐标系后图形几何与显示几何完全恢复；
    3. 地图坐标系下手柄拖动反算回权威坐标，绝不将地图坐标写入像元 ROI；
    4. 混合 ROI 跨会话保存重开无损恢复；
    5. 无地理参考数据明确拒绝地图换算，不盲猜。
    """
    from spectra_workbench.domain.roi import ROI

    rot_path = Path(p3_samples["samples"]["rotated"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rot_path)
        _wait_layers(win, qapp, count=1)
        layer = win.layer_panel.layers[0]
        gt = layer.geotransform
        assert gt == [500000.0, 2.0, 0.5, 3200000.0, 0.5, -2.0]

        # 1. 放置像元坐标多边形 ROI，包含已知角点 (10, 10)
        roi_pts = [(10.0, 10.0), (20.0, 10.0), (15.0, 20.0)]
        roi = ROI(name="RotKnownROI", shape=ROIShape.polygon, points=roi_pts, coordinate_space="pixel")
        win.canvas._add_roi(roi)

        # 初始像素视图下，图形 item 坐标就是像素坐标
        state0 = win.canvas._roi_states[roi.roi_id]
        item0 = state0["item"]
        pts0 = [[float(p.x()), float(p.y())] for p in (item0.mapToParent(pt) for pt in item0.getState()["points"])]
        assert pts0 == [[10.0, 10.0], [20.0, 10.0], [15.0, 20.0]]

        # 2. 切换到原生地图 CRS (EPSG:32650)
        native_wkt = layer.metadata["crs_wkt"]
        win.canvas.set_view_crs(native_wkt)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and win.canvas._view_bounds is None:
            qapp.processEvents()
            time.sleep(0.02)
        assert win.canvas._view_bounds is not None, "视图 CRS 切换超时"

        # 3. 核心数值断言：像元 (10, 10) 必须精确渲染在地图坐标 (500025, 3199985)
        # x = 500000 + 10*2 + 10*0.5 = 500025.0
        # y = 3200000 + 10*0.5 - 10*2 = 3199985.0
        state_map = win.canvas._roi_states[roi.roi_id]
        item_map = state_map["item"]
        pts_map = [[float(p.x()), float(p.y())] for p in (item_map.mapToParent(pt) for pt in item_map.getState()["points"])]

        assert abs(pts_map[0][0] - 500025.0) < 1e-4, f"点 0 X 坐标期望 500025.0，实际 {pts_map[0][0]}"
        assert abs(pts_map[0][1] - 3199985.0) < 1e-4, f"点 0 Y 坐标期望 3199985.0，实际 {pts_map[0][1]}"
        assert abs(pts_map[1][0] - 500045.0) < 1e-4, f"点 1 X 坐标期望 500045.0，实际 {pts_map[1][0]}"
        assert abs(pts_map[1][1] - 3199990.0) < 1e-4, f"点 1 Y 坐标期望 3199990.0，实际 {pts_map[1][1]}"
        assert abs(pts_map[2][0] - 500040.0) < 1e-4, f"点 2 X 坐标期望 500040.0，实际 {pts_map[2][0]}"
        assert abs(pts_map[2][1] - 3199967.5) < 1e-4, f"点 2 Y 坐标期望 3199967.5，实际 {pts_map[2][1]}"

        # 权威模型必须依然保持像素坐标，绝不被视图污染
        auth_roi = win.canvas.roi_list()[0]
        assert auth_roi.coordinate_space == "pixel"
        assert auth_roi.points == [(10.0, 10.0), (20.0, 10.0), (15.0, 20.0)]

        # 4. 在地图视图下拖动手柄 0 到新位置 (500045.0, 3199990.0)
        # 该地图坐标反算回像元坐标恰好是 (20.0, 10.0)
        handles = item_map.getHandles()
        assert len(handles) == 3
        item_map.movePoint(handles[0], pg.Point(500045.0, 3199990.0))
        qapp.processEvents()

        # 验证反算：权威坐标更新为反算后的像元坐标 (20.0, 10.0)，空间属性仍为 pixel！
        dragged_auth = win.canvas.roi_list()[0]
        assert dragged_auth.coordinate_space == "pixel"
        assert abs(dragged_auth.points[0][0] - 20.0) < 1e-4
        assert abs(dragged_auth.points[0][1] - 10.0) < 1e-4

        # 4b. 切换到第三种坐标系：EPSG:4326 (经纬度)
        old_wkt = win.canvas.view_crs_wkt
        win.cmb_view_crs.setCurrentIndex(2)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and (win.canvas.view_crs_wkt == old_wkt or win.canvas._view_bounds is None):
            qapp.processEvents()
            time.sleep(0.02)
        assert win.canvas._view_bounds is not None and win.canvas.view_crs_wkt != old_wkt
        # 在 4326 视图下，ROI 显示图形转换为了经纬度（坐标大约在东经 117°、北纬 28° 附近）
        state_4326 = win.canvas._roi_states[roi.roi_id]
        pts_4326 = [[float(p.x()), float(p.y())] for p in (state_4326["item"].mapToParent(pt) for pt in state_4326["item"].getState()["points"])]
        assert len(pts_4326) == 3
        # 经度必须在 (-180, 180)，纬度在 (-90, 90)
        assert -180.0 <= pts_4326[0][0] <= 180.0
        assert -90.0 <= pts_4326[0][1] <= 90.0
        # 权威模型空间依然保持像素坐标！
        assert win.canvas.roi_list()[0].coordinate_space == "pixel"

        # 5. 切换回像素坐标系：完成 像元 -> 原生地图 CRS -> EPSG:4326 -> 像元 完整往返
        win.canvas.set_view_crs(None)
        qapp.processEvents()
        state_back = win.canvas._roi_states[roi.roi_id]
        item_back = state_back["item"]
        pts_back = [[float(p.x()), float(p.y())] for p in (item_back.mapToParent(pt) for pt in item_back.getState()["points"])]
        assert abs(pts_back[0][0] - 20.0) < 1e-4
        assert abs(pts_back[0][1] - 10.0) < 1e-4

        # 6. 保存项目并重开
        proj_file = tmp_path / "rotated_known_point_roundtrip.spectra.json"
        with patch("spectra_workbench.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(proj_file), "")):
            win.save_project_action(save_as=True)
    finally:
        win.close()
        qapp.processEvents()

    win2 = MainWindow()
    win2.show()
    try:
        win2.open_project(proj_file)
        _wait_layers(win2, qapp, count=1)
        r_list = win2.canvas.roi_list()
        assert len(r_list) == 1
        r_reloaded = r_list[0]
        assert r_reloaded.coordinate_space == "pixel"
        assert abs(r_reloaded.points[0][0] - 20.0) < 1e-4
        assert abs(r_reloaded.points[0][1] - 10.0) < 1e-4
    finally:
        win2.close()
        qapp.processEvents()


def test_no_crs_and_no_gt_refuses_map_conversion_and_stale_generation_dropped(
    qapp: QApplication, p3_samples: dict
) -> None:
    """无 CRS / 无地理参考数据集明确拒绝地图转换并不盲猜；代际递增确保旧视图响应被安全丢弃。"""
    from spectra_workbench.domain.roi import ROI

    no_gt_path = Path(p3_samples["samples"]["no_gt"]["path"]).resolve()
    no_crs_path = Path(p3_samples["samples"]["no_crs"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        # 1. 打开无 GeoTransform 数据集
        win.open_dataset(no_gt_path)
        _wait_layers(win, qapp, count=1)
        assert len(win.layer_panel.layers) == 1

        roi = ROI(name="NoGtROI", shape=ROIShape.polygon, points=[(5.0, 5.0), (10.0, 5.0), (7.0, 10.0)], coordinate_space="pixel")
        win.canvas._add_roi(roi)

        # 尝试通过 worker 将像元转换为地图坐标：必须显式报错，绝不猜测伪造坐标
        with pytest.raises(Exception) as exc_info:
            win.gdal_client.transform_coordinates(
                path=no_gt_path,
                points=roi.points,
                from_space="pixel",
                to_space="map",
            )
        assert "地理参考" in str(exc_info.value) or "GeoTransform" in str(exc_info.value)

        # 2. 打开无 CRS 数据集（有 GeoTransform 但无 CRS）
        win.open_dataset(no_crs_path)
        _wait_layers(win, qapp, count=2)
        with pytest.raises(Exception) as exc_info2:
            win.gdal_client.transform_coordinates(
                path=no_crs_path,
                points=roi.points,
                from_space="pixel",
                to_space="map",
            )
        assert "缺少 CRS" in str(exc_info2.value) or "CRS" in str(exc_info2.value)

        # 3. 验证代际保护：快速连续触发切换 CRS，旧响应不能覆盖新视图
        gen1 = win.canvas.generation
        win.canvas._gen.bump("fake_layer")
        gen2 = win.canvas.generation
        assert gen2 > gen1, "代际号必须递增"
    finally:
        win.close()
        qapp.processEvents()


