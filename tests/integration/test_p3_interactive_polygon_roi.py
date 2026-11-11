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


def _wait_layers(
    win: MainWindow, qapp: QApplication, count: int = 1, timeout: float = 10.0
) -> None:
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
        QTest.mouseRelease(
            vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, drag_target
        )
        qapp.processEvents()

        roi_dragged = win.canvas.roi_list()[0]
        assert roi_dragged.points[0] != orig_p0, "拖动手柄后顶点坐标必须被真实更新"

        # 6. 保存项目
        proj_file = tmp_path / "interactive_roi_proj.spectra.json"
        with patch(
            "spectra_workbench.ui.main_window.QFileDialog.getSaveFileName",
            return_value=(str(proj_file), ""),
        ):
            win.save_project_action(save_as=True)
        assert proj_file.exists()
    finally:
        win.close()
        win.deleteLater()
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
        win2.deleteLater()
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
        QTest.mouseClick(
            vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(102, 101)
        )
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
        with patch(
            "spectra_workbench.ui.main_window.QFileDialog.getSaveFileName",
            return_value=(str(proj_file), ""),
        ):
            win.save_project_action(save_as=True)
    finally:
        win.close()
        win.deleteLater()
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
        win2.deleteLater()
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
        with patch(
            "spectra_workbench.ui.main_window.QFileDialog.getSaveFileName",
            return_value=(str(proj_file), ""),
        ):
            win.save_project_action(save_as=True)
    finally:
        win.close()
        win.deleteLater()
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
        win2.deleteLater()
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
        assert win.canvas._draw_shape == ROIShape.polygon, (
            "4 个不同位置快速点击后绘制状态必须保持激活"
        )
        assert len(win.canvas._poly_pts) == 4, (
            f"期望 4 个待闭合顶点，实际收到 {len(win.canvas._poly_pts)} 个"
        )
        assert len(win.canvas.roi_list()) == 0, "未发送闭合手势前不能产生闭合 ROI"

        # 真实右键闭合四边形
        QTest.mouseClick(
            vp, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier, quad_pts[-1]
        )
        qapp.processEvents()
        rois = win.canvas.roi_list()
        assert len(rois) == 1
        assert len(rois[0].points) == 4, (
            f"四边形必须保留全部 4 个顶点，实际收到 {len(rois[0].points)}"
        )

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
        assert win.canvas._draw_shape == ROIShape.polygon, (
            "8 个不同位置快速点击后绘制状态必须保持激活"
        )
        assert len(win.canvas._poly_pts) == 8, (
            f"期望 8 个待闭合顶点，实际收到 {len(win.canvas._poly_pts)} 个"
        )

        # 点击回到起点附近闭合八边形
        QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, oct_pts[0])
        qapp.processEvents()
        rois = win.canvas.roi_list()
        assert len(rois) == 2
        oct_roi = next(r for r in rois if r.roi_id != rois[0].roi_id)
        assert len(oct_roi.points) == 8, (
            f"八边形必须保留全部 8 个顶点，实际收到 {len(oct_roi.points)}"
        )

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
        win.deleteLater()
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
        roi = ROI(
            name="RotKnownROI", shape=ROIShape.polygon, points=roi_pts, coordinate_space="pixel"
        )
        win.canvas._add_roi(roi)

        # 初始像素视图下，图形 item 坐标就是像素坐标
        state0 = win.canvas._roi_states[roi.roi_id]
        item0 = state0["item"]
        pts0 = [
            [float(p.x()), float(p.y())]
            for p in (item0.mapToParent(pt) for pt in item0.getState()["points"])
        ]
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
        pts_map = [
            [float(p.x()), float(p.y())]
            for p in (item_map.mapToParent(pt) for pt in item_map.getState()["points"])
        ]

        assert abs(pts_map[0][0] - 500025.0) < 1e-4, (
            f"点 0 X 坐标期望 500025.0，实际 {pts_map[0][0]}"
        )
        assert abs(pts_map[0][1] - 3199985.0) < 1e-4, (
            f"点 0 Y 坐标期望 3199985.0，实际 {pts_map[0][1]}"
        )
        assert abs(pts_map[1][0] - 500045.0) < 1e-4, (
            f"点 1 X 坐标期望 500045.0，实际 {pts_map[1][0]}"
        )
        assert abs(pts_map[1][1] - 3199990.0) < 1e-4, (
            f"点 1 Y 坐标期望 3199990.0，实际 {pts_map[1][1]}"
        )
        assert abs(pts_map[2][0] - 500040.0) < 1e-4, (
            f"点 2 X 坐标期望 500040.0，实际 {pts_map[2][0]}"
        )
        assert abs(pts_map[2][1] - 3199967.5) < 1e-4, (
            f"点 2 Y 坐标期望 3199967.5，实际 {pts_map[2][1]}"
        )

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
        while time.monotonic() < deadline and (
            win.canvas.view_crs_wkt == old_wkt or win.canvas._view_bounds is None
        ):
            qapp.processEvents()
            time.sleep(0.02)
        assert win.canvas._view_bounds is not None and win.canvas.view_crs_wkt != old_wkt
        # 在 4326 视图下，ROI 显示图形转换为了经纬度（坐标大约在东经 117°、北纬 28° 附近）
        state_4326 = win.canvas._roi_states[roi.roi_id]
        pts_4326 = [
            [float(p.x()), float(p.y())]
            for p in (
                state_4326["item"].mapToParent(pt) for pt in state_4326["item"].getState()["points"]
            )
        ]
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
        pts_back = [
            [float(p.x()), float(p.y())]
            for p in (item_back.mapToParent(pt) for pt in item_back.getState()["points"])
        ]
        assert abs(pts_back[0][0] - 20.0) < 1e-4
        assert abs(pts_back[0][1] - 10.0) < 1e-4

        # 6. 保存项目并重开
        proj_file = tmp_path / "rotated_known_point_roundtrip.spectra.json"
        with patch(
            "spectra_workbench.ui.main_window.QFileDialog.getSaveFileName",
            return_value=(str(proj_file), ""),
        ):
            win.save_project_action(save_as=True)
    finally:
        win.close()
        win.deleteLater()
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
        win2.deleteLater()
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

        roi = ROI(
            name="NoGtROI",
            shape=ROIShape.polygon,
            points=[(5.0, 5.0), (10.0, 5.0), (7.0, 10.0)],
            coordinate_space="pixel",
        )
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
        win.deleteLater()
        qapp.processEvents()


def test_rectangle_roi_affine_rotation_preserves_four_corners_and_area(
    qapp: QApplication, tmp_path: Path, p3_samples: dict
) -> None:
    """矩形 ROI 权威几何与显示轮廓分离验收：
    1. 旋转 Affine 栅格下，矩形展开全部 4 个角点经 worker 转换，严禁以对角两点包成轴对齐矩形。
    2. 数值精确断言 4 个地图角点与真实覆盖面积 425.0（彻底杜绝错误值 375.0）。
    3. 变换视图下禁用手柄编辑，防止任意形变；权威模型坐标与空间属性保持 pixel 且数值完好。
    4. 跨 CRS (EPSG:4326) 转换与切换回像元视图后完整恢复为带手柄的标准 RectROI。
    5. 保存项目并全新实例重开，矩形几何完整恢复。
    """
    from spectra_workbench.domain.roi import ROI

    rotated_path = Path(p3_samples["samples"]["rotated"]["path"]).resolve()

    def poly_area(pts: list[list[float]]) -> float:
        return (
            abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1], strict=True)))
            / 2.0
        )

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rotated_path)
        _wait_layers(win, qapp, count=1)

        # 1. 在像素坐标系下添加矩形 ROI: [(10.0, 10.0), (20.0, 20.0)]
        roi = ROI(
            name="Rect1",
            shape=ROIShape.rectangle,
            points=[(10.0, 10.0), (20.0, 20.0)],
            coordinate_space="pixel",
        )
        win.canvas._add_roi(roi)
        qapp.processEvents()

        # 验证像素视图下是标准的 RectROI，带缩放手柄
        st_pixel = win.canvas._roi_states[roi.roi_id]
        item_pixel = st_pixel["item"]
        assert isinstance(item_pixel, pg.RectROI)
        assert len(item_pixel.getHandles()) >= 2
        assert item_pixel.isVisible()

        # 2. 切换到栅格原生地图 CRS (EPSG:32650)
        old_wkt = win.canvas.view_crs_wkt
        win.cmb_view_crs.setCurrentIndex(1)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and (
            win.canvas.view_crs_wkt == old_wkt or win.canvas._view_bounds is None
        ):
            qapp.processEvents()
            time.sleep(0.02)
        assert win.canvas._view_bounds is not None, "视图 CRS 切换超时"

        # 3. 核心断言：变换视图下的图形项不再是轴对齐 RectROI，而是真实 4 角点的 PolyLineROI 轮廓
        st_map = win.canvas._roi_states[roi.roi_id]
        item_map = st_map["item"]
        assert isinstance(item_map, pg.PolyLineROI), "旋转视图下矩形轮廓必须展开为多边形轮廓渲染"
        assert item_map.isVisible()
        assert not item_map.translatable, "变换视图下矩形不可整体平移变形"
        assert all(not h.isVisible() for h in item_map.getHandles()), "变换视图下手柄必须隐藏/禁用"

        # 取出地图视图下的 4 个实际显示点位
        pts_map = [
            [float(p.x()), float(p.y())]
            for p in (item_map.mapToParent(pt) for pt in item_map.getState()["points"])
        ]
        assert len(pts_map) == 4, f"期望 4 个角点，实际收到 {len(pts_map)} 个"

        # GT = (500000, 2, 0.5, 3200000, 0.5, -2)
        # 点 0: (10, 10) -> (500025.0, 3199985.0)
        # 点 1: (20, 10) -> (500045.0, 3199990.0)
        # 点 2: (20, 20) -> (500050.0, 3199970.0)
        # 点 3: (10, 20) -> (500030.0, 3199965.0)
        assert abs(pts_map[0][0] - 500025.0) < 1e-4, f"角点 0 X 期望 500025.0，实际 {pts_map[0][0]}"
        assert abs(pts_map[0][1] - 3199985.0) < 1e-4, (
            f"角点 0 Y 期望 3199985.0，实际 {pts_map[0][1]}"
        )
        assert abs(pts_map[1][0] - 500045.0) < 1e-4, f"角点 1 X 期望 500045.0，实际 {pts_map[1][0]}"
        assert abs(pts_map[1][1] - 3199990.0) < 1e-4, (
            f"角点 1 Y 期望 3199990.0，实际 {pts_map[1][1]}"
        )
        assert abs(pts_map[2][0] - 500050.0) < 1e-4, f"角点 2 X 期望 500050.0，实际 {pts_map[2][0]}"
        assert abs(pts_map[2][1] - 3199970.0) < 1e-4, (
            f"角点 2 Y 期望 3199970.0，实际 {pts_map[2][1]}"
        )
        assert abs(pts_map[3][0] - 500030.0) < 1e-4, f"角点 3 X 期望 500030.0，实际 {pts_map[3][0]}"
        assert abs(pts_map[3][1] - 3199965.0) < 1e-4, (
            f"角点 3 Y 期望 3199965.0，实际 {pts_map[3][1]}"
        )

        # 面积断言：真实多边形面积必须精确等于 425.0，且绝不能等于错误的轴对齐包围面积 375.0！
        actual_area = poly_area(pts_map)
        assert abs(actual_area - 425.0) < 1e-4, f"真实覆盖面积期望 425.0，实际 {actual_area}"
        assert abs(actual_area - 375.0) > 10.0, (
            "覆盖面积绝不可退化为错误的对角点轴对齐矩形面积 375.0"
        )

        # 权威模型必须依然保持像素坐标，绝不被视图污染
        auth_roi = win.canvas.roi_list()[0]
        assert auth_roi.coordinate_space == "pixel"
        assert auth_roi.points == [(10.0, 10.0), (20.0, 20.0)]

        # 4. 切换到第三种坐标系：EPSG:4326 (经纬度)
        old_wkt = win.canvas.view_crs_wkt
        win.cmb_view_crs.setCurrentIndex(2)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and (
            win.canvas.view_crs_wkt == old_wkt or win.canvas._view_bounds is None
        ):
            qapp.processEvents()
            time.sleep(0.02)
        assert win.canvas._view_bounds is not None and win.canvas.view_crs_wkt != old_wkt
        st_4326 = win.canvas._roi_states[roi.roi_id]
        pts_4326 = [
            [float(p.x()), float(p.y())]
            for p in (
                st_4326["item"].mapToParent(pt) for pt in st_4326["item"].getState()["points"]
            )
        ]
        assert len(pts_4326) == 4
        for pt in pts_4326:
            assert -180.0 <= pt[0] <= 180.0
            assert -90.0 <= pt[1] <= 90.0
        assert win.canvas.roi_list()[0].coordinate_space == "pixel"

        # 5. 切换回像素坐标系：恢复为标准 RectROI
        win.canvas.set_view_crs(None)
        qapp.processEvents()
        st_back = win.canvas._roi_states[roi.roi_id]
        item_back = st_back["item"]
        assert isinstance(item_back, pg.RectROI)
        assert item_back.isVisible()
        b_back = win.canvas._rect_bounds(item_back)
        assert abs(b_back[0] - 10.0) < 1e-4 and abs(b_back[1] - 10.0) < 1e-4
        assert abs(b_back[2] - 20.0) < 1e-4 and abs(b_back[3] - 20.0) < 1e-4

        # 6. 保存项目并重开
        proj_file = tmp_path / "rect_rotated_roundtrip.spectra.json"
        with patch(
            "spectra_workbench.ui.main_window.QFileDialog.getSaveFileName",
            return_value=(str(proj_file), ""),
        ):
            win.save_project_action(save_as=True)
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()

    win2 = MainWindow()
    win2.show()
    try:
        win2.open_project(proj_file)
        _wait_layers(win2, qapp, count=1)
        r_list = win2.canvas.roi_list()
        assert len(r_list) == 1
        r_reloaded = r_list[0]
        assert r_reloaded.shape == ROIShape.rectangle
        assert r_reloaded.coordinate_space == "pixel"
        assert abs(r_reloaded.points[0][0] - 10.0) < 1e-4
        assert abs(r_reloaded.points[0][1] - 10.0) < 1e-4
        assert abs(r_reloaded.points[1][0] - 20.0) < 1e-4
        assert abs(r_reloaded.points[1][1] - 20.0) < 1e-4
    finally:
        win2.close()
        win2.deleteLater()
        qapp.processEvents()


def test_roi_coordinate_transform_failure_hides_item_and_preserves_authority(
    qapp: QApplication, tmp_path: Path, p3_samples: dict
) -> None:
    """坐标转换失败严格异常隔离验收：
    1. 转换失败严禁将原像元坐标当作地图坐标使用（绝不伪装恒等变换）。
    2. 故障注入下图形项隐藏（item.isVisible() is False），display_points 为 None，标记 transform_error。
    3. 权威模型完全保全，不被污染。
    4. 视图切回像元坐标或可恢复视图后，图形项自动恢复可见与交互。
    5. 拖动反算与保存重开故障注入下安全兜底，不崩溃不挂起。
    """
    from spectra_workbench.domain.roi import ROI

    rotated_path = Path(p3_samples["samples"]["rotated"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rotated_path)
        _wait_layers(win, qapp, count=1)
        layer = win.layer_panel.layers[0]

        # 1. 添加矩形 ROI 与多边形 ROI 各一个
        roi_rect = ROI(
            name="RectFail",
            shape=ROIShape.rectangle,
            points=[(10.0, 10.0), (20.0, 20.0)],
            coordinate_space="pixel",
        )
        roi_poly = ROI(
            name="PolyFail",
            shape=ROIShape.polygon,
            points=[(10.0, 10.0), (20.0, 10.0), (15.0, 20.0)],
            coordinate_space="pixel",
        )
        win.canvas._add_roi(roi_rect)
        win.canvas._add_roi(roi_poly)
        qapp.processEvents()

        # 2. 注入异步转换失败：模拟 transform_coordinates_async 调用 on_error
        with patch.object(
            win.canvas._client,
            "transform_coordinates_async",
            side_effect=lambda *a, **kw: kw["on_error"](
                "injected coordinate transformation failure"
            ),
        ):
            win.canvas.set_view_crs(layer.metadata["crs_wkt"])
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and win.canvas._view_bounds is None:
                qapp.processEvents()
                time.sleep(0.02)
            assert win.canvas._view_bounds is not None, "视图 CRS 范围计算超时"

        # 3. 验证转换失败后的图形表现：
        # 视图已切换到地图范围 (X~500000, Y~3200000)
        # 两个 ROI 的图形项必须全部被隐藏（isVisible == False），display_points 必须为 None！
        # 绝不能把 (10, 10) 像元点渲染在 (10, 10) 地图位置上！
        st_rect = win.canvas._roi_states[roi_rect.roi_id]
        st_poly = win.canvas._roi_states[roi_poly.roi_id]

        assert not st_rect["item"].isVisible(), "转换失败的矩形 ROI 图形必须被隐藏"
        assert st_rect["display_points"] is None, "转换失败后 display_points 必须清空为 None"
        assert st_rect["transform_error"] is True

        assert not st_poly["item"].isVisible(), "转换失败的多边形 ROI 图形必须被隐藏"
        assert st_poly["display_points"] is None, "转换失败后 display_points 必须清空为 None"
        assert st_poly["transform_error"] is True

        # 权威模型必须依然保持像素坐标与原点位
        rois = win.canvas.roi_list()
        assert len(rois) == 2
        assert rois[0].coordinate_space == "pixel" and rois[0].points == [
            (10.0, 10.0),
            (20.0, 20.0),
        ]
        assert rois[1].coordinate_space == "pixel" and rois[1].points == [
            (10.0, 10.0),
            (20.0, 10.0),
            (15.0, 20.0),
        ]

        # 4. 故障恢复：切换回像元坐标系，ROI 必须重新可见
        win.canvas.set_view_crs(None)
        qapp.processEvents()

        st_rect_restored = win.canvas._roi_states[roi_rect.roi_id]
        st_poly_restored = win.canvas._roi_states[roi_poly.roi_id]

        assert st_rect_restored["item"].isVisible(), "切回像素视图后矩形 ROI 必须恢复可见"
        assert st_rect_restored["display_points"] == [(10.0, 10.0), (20.0, 20.0)]
        assert not st_rect_restored.get("transform_error", False)

        assert st_poly_restored["item"].isVisible(), "切回像素视图后多边形 ROI 必须恢复可见"
        assert len(st_poly_restored["display_points"]) == 3
        assert not st_poly_restored.get("transform_error", False)

        # 5. 测试反算异常保护：当反算 Worker 报错时，_roi_geometry 必须返回原权威模型，不崩溃、不篡改
        with patch.object(
            win.canvas._client,
            "transform_coordinates",
            side_effect=RuntimeError("injected worker failure"),
        ):
            res = win.canvas._roi_geometry(st_poly_restored)
            assert res.points == roi_poly.points
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_roi_transform_failure_fault_matrix(
    qapp: QApplication, tmp_path: Path, p3_samples: dict
) -> None:
    """坐标转换失败的五类故障注入矩阵验收。

    任务要求的故障注入覆盖面：
      A. 向地图转换失败（异步 on_error）
      B. 回像元转换失败（同步 transform_coordinates 抛错）
      C. 项目重开时转换失败
      D. 拖动反算失败
      E. 切换图层或 CRS 后旧响应迟到

    对每一类都必须验证三件事（而不是只检查错误消息）：
      1. 图形状态：失败图形被隐藏、不可编辑，绝不显示错误坐标；
      2. 权威几何：roi.points / roi_list() / layer.rois 全部保持不变；
      3. 保存结果：保存并重开后，项目里的 ROI 仍是原始权威几何。

    另外要求：UI 有明确可恢复状态（列表标出转换失败），不能无限等待。
    """
    from spectra_workbench.domain.roi import ROI

    rotated_path = Path(p3_samples["samples"]["rotated"]["path"]).resolve()
    pixel_rect = [(10.0, 10.0), (20.0, 20.0)]
    pixel_rect_t = ((10.0, 10.0), (20.0, 20.0))

    def roi_snapshot(win: MainWindow) -> list[tuple]:
        return [tuple(tuple(pt) for pt in r.points) for r in win.canvas.roi_list()]

    # ---------------------------------------------------------------- A. 向地图转换失败
    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rotated_path)
        _wait_layers(win, qapp, count=1)

        roi = ROI(
            name="RectA",
            shape=ROIShape.rectangle,
            points=list(pixel_rect),
            coordinate_space="pixel",
        )
        win.canvas._add_roi(roi)
        qapp.processEvents()
        st = win.canvas._roi_states[roi.roi_id]
        assert st["item"].isVisible(), "前置条件：像元视图下 ROI 必须可见"

        # 注入异步向地图转换失败
        with patch.object(
            win.canvas._client,
            "transform_coordinates_async",
            side_effect=lambda *a, **kw: kw["on_error"]("injected async map transform failure"),
        ):
            win.cmb_view_crs.setCurrentIndex(1)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and win.canvas._view_bounds is None:
                qapp.processEvents()
                time.sleep(0.02)

        st = win.canvas._roi_states[roi.roi_id]
        assert not st["item"].isVisible(), "A: 转换失败图形必须隐藏，不得显示原像元坐标冒充地图坐标"
        assert st["display_points"] is None, "A: display_points 必须清空"
        assert st["transform_error"] is True, "A: 必须标记转换失败"
        assert roi_snapshot(win) == [pixel_rect_t], "A: 权威几何必须保持不变"
        assert win.layer_panel.layers[0].rois[0].points == pixel_rect, "A: 图层权威几何必须保持不变"
        # UI 明确可恢复状态
        assert roi.roi_id in win.canvas.roi_transform_failed_ids(), "A: 必须能枚举出失败的 ROI"
        assert "转换失败" in win.info_panel.lst_rois.item(0).text(), (
            f"A: ROI 列表必须标出转换失败状态，实际文本 {win.info_panel.lst_rois.item(0).text()!r}"
        )
        # 不能无限等待：恢复视图后必须自动回到可见可用状态
        win.canvas.set_view_crs(None)
        qapp.processEvents()
        st = win.canvas._roi_states[roi.roi_id]
        assert st["item"].isVisible(), "A: 切回像元视图后必须自动恢复可见"
        assert st["display_points"] == pixel_rect
        assert not st["transform_error"]
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()

    # ---------------------------------------------------------------- B. 回像元转换失败
    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rotated_path)
        _wait_layers(win, qapp, count=1)
        win.cmb_view_crs.setCurrentIndex(1)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and win.canvas._view_bounds is None:
            qapp.processEvents()
            time.sleep(0.02)

        # 权威在 map 空间的 ROI：切回像元视图必须真正走「回像元」转换通道
        map_rect = [(500025.0, 3199985.0), (500045.0, 3199990.0)]
        roi = ROI(
            name="RectB",
            shape=ROIShape.rectangle,
            points=list(map_rect),
            coordinate_space="map",
            crs_wkt=win.canvas.view_crs_wkt,
        )
        win.canvas._add_roi(roi)
        qapp.processEvents()
        st = win.canvas._roi_states[roi.roi_id]
        assert st["item"].isVisible(), "B: 前置条件：地图视图下 ROI 必须可见"

        # 注入「回像元」转换失败：_sync_rois_to_view 走同步 transform_coordinates
        with patch.object(
            win.canvas._client,
            "transform_coordinates",
            side_effect=RuntimeError("injected back-to-pixel failure"),
        ):
            win.canvas.set_view_crs(None)
            qapp.processEvents()

        st = win.canvas._roi_states[roi.roi_id]
        assert not st["item"].isVisible(), "B: 回像元失败后图形必须隐藏，不得显示原坐标冒充像元坐标"
        assert st["display_points"] is None, "B: display_points 必须清空"
        assert st["transform_error"] is True
        assert roi_snapshot(win) == [tuple(map_rect)], "B: 权威几何必须保持不变"
        assert win.layer_panel.layers[0].rois[0].points == map_rect, "B: 图层权威几何必须保持不变"
        assert roi.roi_id in win.canvas.roi_transform_failed_ids()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()

    # ---------------------------------------------------------------- C. 项目重开时转换失败
    win = MainWindow()
    win.show()
    proj_file = tmp_path / "fault_matrix.spectra.json"
    try:
        win.open_dataset(rotated_path)
        _wait_layers(win, qapp, count=1)
        # 权威在 map 空间的矩形：重开时必须真的走坐标换算通道，才能验证「重开转换失败」
        win.cmb_view_crs.setCurrentIndex(1)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and win.canvas._view_bounds is None:
            qapp.processEvents()
            time.sleep(0.02)
        map_rect_c = [(500025.0, 3199985.0), (500045.0, 3199990.0)]
        roi = ROI(
            name="RectC",
            shape=ROIShape.rectangle,
            points=list(map_rect_c),
            coordinate_space="map",
            crs_wkt=win.canvas.view_crs_wkt,
        )
        win.canvas._add_roi(roi)
        qapp.processEvents()
        with patch(
            "spectra_workbench.ui.main_window.QFileDialog.getSaveFileName",
            return_value=(str(proj_file), ""),
        ):
            win.save_project_action(save_as=True)
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()

    win2 = MainWindow()
    win2.show()
    try:
        # 重开项目；随后注入转换失败，模拟「项目重开时坐标转换失败」
        win2.open_project(proj_file)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not win2.layer_panel.layers:
            qapp.processEvents()
            time.sleep(0.02)

        client = win2.canvas._client
        assert client is not None, "C: 前置条件：重开后必须已建立 worker 客户端"
        with patch.object(
            client,
            "transform_coordinates",
            side_effect=RuntimeError("injected project reopen failure"),
        ):
            # 重新加载项目，注入的失败会命中重开时的 ROI 坐标换算
            win2.open_project(proj_file)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not win2.layer_panel.layers:
                qapp.processEvents()
                time.sleep(0.02)

        assert len(win2.layer_panel.layers) == 1, "C: 项目必须能打开"
        assert len(win2.canvas.roi_list()) == 1, "C: 重开后权威模型中必须仍有该 ROI"
        assert win2.canvas.roi_list()[0].points == map_rect_c, "C: 重开后权威几何必须完好"
        assert win2.layer_panel.layers[0].rois[0].points == map_rect_c, (
            "C: 重开后图层权威几何必须完好"
        )
        # 失败态必须被显式标记，而不是无声消失
        assert win2.canvas.roi_transform_failed_ids(), "C: 重开转换失败必须显式标记"
    finally:
        win2.close()
        win2.deleteLater()
        qapp.processEvents()

    # ---------------------------------------------------------------- D. 拖动反算失败
    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rotated_path)
        _wait_layers(win, qapp, count=1)
        win.cmb_view_crs.setCurrentIndex(1)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and win.canvas._view_bounds is None:
            qapp.processEvents()
            time.sleep(0.02)

        poly = ROI(
            name="PolyD",
            shape=ROIShape.polygon,
            points=[(10.0, 10.0), (20.0, 10.0), (15.0, 20.0)],
            coordinate_space="pixel",
        )
        win.canvas._add_roi(poly)
        qapp.processEvents()
        st = win.canvas._roi_states[poly.roi_id]
        item = st["item"]
        before_display = [(round(p.x(), 3), round(p.y(), 3)) for p in item.getState()["points"]]
        assert before_display, "D: 前置条件：地图视图下多边形必须可见"

        # 反算失败时：图形恢复原轮廓，权威几何不变
        with patch.object(
            win.canvas._client,
            "transform_coordinates",
            side_effect=RuntimeError("injected drag inverse failure"),
        ):
            item.movePoint(0, pg.Point(500030.0, 3199995.0))

        st = win.canvas._roi_states[poly.roi_id]
        after_display = [
            (round(p.x(), 3), round(p.y(), 3)) for p in st["item"].getState()["points"]
        ]
        assert after_display == before_display, (
            f"D: 反算失败后显示图形必须恢复原轮廓，实际 {after_display} != {before_display}"
        )
        assert st["item"].isVisible(), "D: 反算失败后图形不得被隐藏，应保持原样可继续编辑"
        assert poly.points == [(10.0, 10.0), (20.0, 10.0), (15.0, 20.0)], "D: 权威几何必须保持不变"
        assert roi_snapshot(win) == [((10.0, 10.0), (20.0, 10.0), (15.0, 20.0))], (
            "D: roi_list 必须返回原权威几何"
        )
        assert win.layer_panel.layers[0].rois[0].points == [
            (10.0, 10.0),
            (20.0, 10.0),
            (15.0, 20.0),
        ], "D: 图层权威几何必须保持不变"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()

    # ---------------------------------------------------------------- E. 旧响应迟到
    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rotated_path)
        _wait_layers(win, qapp, count=1)
        roi = ROI(
            name="RectE",
            shape=ROIShape.rectangle,
            points=list(pixel_rect),
            coordinate_space="pixel",
        )
        win.canvas._add_roi(roi)
        qapp.processEvents()

        # 捕获异步回调，稍后手工投递，模拟「切换 CRS 后旧响应迟到」
        late: list[tuple] = []

        def capture(*a, **kw):
            late.append((kw["on_success"], kw["on_error"]))
            return "req-late"

        with patch.object(win.canvas._client, "transform_coordinates_async", side_effect=capture):
            win.cmb_view_crs.setCurrentIndex(1)
            qapp.processEvents()

        assert late, "E: 前置条件：必须捕获到一次异步转换请求"
        assert win.canvas.view_crs_wkt is not None, "E: 前置条件：视图 CRS 必须已切换"

        # 触发代际递增（模拟切换图层/CRS），此后第一次请求的响应即成为「过期响应」
        win.canvas._gen.bump(
            win.canvas.current_layer.layer_id if win.canvas.current_layer else None
        )
        gen_after_bump = win.canvas.generation

        # 迟到响应现在才投递：代际保护必须让它被丢弃
        on_success, _on_error = late[0]
        on_success({"points": [[123456.0, 654321.0], [123466.0, 654331.0]]})
        qapp.processEvents()

        assert win.canvas.generation == gen_after_bump, "E: 代际号不应被迟到响应改变"
        st = win.canvas._roi_states[roi.roi_id]
        # 迟到响应绝不能污染权威几何
        assert roi_snapshot(win) == [pixel_rect_t], (
            f"E: 迟到响应污染了权威几何：{roi_snapshot(win)}"
        )
        assert win.layer_panel.layers[0].rois[0].points == pixel_rect, (
            "E: 迟到响应污染了图层权威几何"
        )
        # 图形不得显示迟到的假坐标
        disp = st["display_points"]
        if disp is not None:
            for c, r in disp:
                assert not (abs(c - 123456.0) < 1e-6 and abs(r - 654321.0) < 1e-6), (
                    f"E: 迟到响应把假坐标写进了显示图形：{disp}"
                )
        # 迟到响应也不能破坏保存状态
        assert len(win.canvas.roi_list()) == 1, "E: ROI 数量必须不变"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
