"""P3-02 元数据实现边界的针对性回归测试（第 23 轮）。

覆盖复核独立复现的四项缺口：

1. **要素数强制扫描**：`GetFeatureCount` 必须显式传 `force=0`。
   用 force 默认为 1 的 spy 断言实现确实传 0；驱动取不到廉价计数时，
   必须返回未知并置 `feature_count_expensive=True`，不得隐藏地发起强制扫描。

2. **图层类型路由**：矢量图层（仅元数据）不得进入栅格读取流程，
   不得对矢量派发栅格窗口请求，旧栅格图像必须清除。
   覆盖初次打开、重选、栅格→矢量→栅格切换与项目恢复。

3. **元数据请求上下文**：旧图层的迟到成功/失败响应都不得覆盖当前选择。
   用**反向顺序投递**回调的确定性测试（不用 sleep 猜时序）。

4. **子数据集仿射范围**：按四个像元外边界角点计算，
   对 GTIFF_DIR 合成引用断言独立预期的四角范围。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.domain.roi import ROI, ROIShape
from spectra_workbench.ui.fonts import ensure_application_fonts
from spectra_workbench.ui.main_window import MainWindow
from spectra_workbench.ui.models.layer import LayerItem

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


# ------------------------------------------------------------------ 1. 要素数
class _SpyDefinition:
    def GetFieldCount(self) -> int:  # noqa: N802 - OGR 命名
        return 0


class _SpyLayer:
    """OGR-like Layer：GetFeatureCount 的 force 默认为 1（与真实绑定一致）。"""

    def __init__(self) -> None:
        self.force_argument_seen: int | None = None

    def GetName(self) -> str:  # noqa: N802
        return "expensive_count_spy"

    def GetGeomType(self) -> int:  # noqa: N802
        return 0

    def GetSpatialRef(self) -> Any:  # noqa: N802
        return None

    def GetExtent(self, force: int = 1) -> Any:  # noqa: N802
        return None

    def GetFeatureCount(self, force: int = 1) -> int:  # noqa: N802
        self.force_argument_seen = force
        return 900

    def GetLayerDefn(self) -> _SpyDefinition:  # noqa: N802
        return _SpyDefinition()


def test_feature_count_uses_force_zero() -> None:
    """实现必须显式传 force=0，否则驱动会强制全表扫描。"""
    from spectra_workbench.providers.gdal_provider.adapter import GDALProviderAdapter

    layer = _SpyLayer()
    meta = GDALProviderAdapter._describe_vector_layer(layer, 0)
    assert layer.force_argument_seen == 0, (
        f"GetFeatureCount 必须传 force=0，实际传了 {layer.force_argument_seen}"
    )
    # 廉价计数可取时如实返回
    assert meta["feature_count"] == 900
    assert meta["feature_count_expensive"] is False


class _UnknownCountLayer(_SpyLayer):
    """驱动取不到廉价计数：应返回未知并如实标记昂贵。"""

    def GetFeatureCount(self, force: int = 1) -> int:  # noqa: N802
        self.force_argument_seen = force
        raise RuntimeError("driver refuses cheap count")


def test_feature_count_unknown_is_marked_expensive() -> None:
    """取不到廉价计数时必须返回未知并标记昂贵，不得代为发起强制扫描。"""
    from spectra_workbench.providers.gdal_provider.adapter import GDALProviderAdapter

    layer = _UnknownCountLayer()
    meta = GDALProviderAdapter._describe_vector_layer(layer, 0)
    assert layer.force_argument_seen == 0, "仍必须传 force=0"
    assert meta["feature_count"] is None, "未知时不得编造数值"
    assert meta["feature_count_expensive"] is True, "必须如实标记为昂贵/未知"


# ---------------------------------------------------------- 2. 图层类型路由
class _RecordingClient:
    """记录客户端收到的栅格窗口请求与元数据探测，不触碰真实引擎。"""

    is_alive = False
    _closed = False

    def __init__(self) -> None:
        self.reads: list[dict] = []
        self.vectors: list[tuple[str, Any, Any]] = []
        self.subs: list[tuple[str, Any, Any]] = []

    def probe_vector_async(self, path, on_success=None, on_error=None):  # noqa: ANN001
        self.vectors.append((str(path), on_success, on_error))
        return "req"

    def list_subdatasets_async(self, path, on_success=None, on_error=None):  # noqa: ANN001
        self.subs.append((str(path), on_success, on_error))
        return "req"

    def read_window_async(self, **kwargs):  # noqa: ANN003
        self.reads.append(kwargs)
        return "req"

    def stop(self, timeout: float = 1.0) -> int:  # noqa: ANN001
        self._closed = True
        return 0


def _make_vector_layer(name: str, tmp_path: Path) -> LayerItem:
    path = tmp_path / f"{name}.gpkg"
    path.write_bytes(b"synthetic vector reference; no geo engine")
    return LayerItem(
        name,
        path,
        {"kind": "vector", "renderable": False, "spatial_model": "vector", "layer_count": 1},
    )


def test_vector_layer_selection_does_not_enter_raster_pipeline(
    qapp: QApplication, tmp_path: Path
) -> None:
    """重选矢量图层不得进入栅格流程：画布不得接收矢量、旧影像必须清除、
    不得对矢量派发栅格窗口请求。"""
    win = MainWindow(project_root=tmp_path)
    win.show()
    try:
        client = _RecordingClient()
        win._clients["gdal"] = client

        vec = _make_vector_layer("A", tmp_path)
        win.layer_panel.add_layer(vec, select=False)

        # 预置一幅旧影像，模拟「先看栅格、再选矢量」
        win.canvas.img_item.setImage(np.full((2, 2), 7, dtype=np.uint8), autoLevels=False)
        assert win.canvas.img_item.image is not None, "前置条件：旧影像应已存在"

        win.layer_panel.select_layer(vec.layer_id)
        qapp.processEvents()

        assert win.canvas.current_layer is not vec, "矢量图层不得进入栅格画布"
        assert win.canvas.img_item.image is None, "选择矢量后旧栅格影像必须清除"
        assert not client.reads, f"不得对矢量派发栅格窗口请求：{client.reads}"
        # 保留明确说明
        assert "矢量" in win.statusBar().currentMessage()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_raster_to_vector_to_raster_switching(
    qapp: QApplication, tmp_path: Path
) -> None:
    """栅格 → 矢量 → 栅格：矢量阶段清除旧影像，回到栅格后恢复可渲染状态。"""
    win = MainWindow(project_root=tmp_path)
    win.show()
    try:
        client = _RecordingClient()
        win._clients["gdal"] = client

        raster = LayerItem(
            "R",
            tmp_path / "r.tif",
            {
                "kind": "raster",
                "width": 4,
                "height": 4,
                "count": 1,
                "spatial_model": "affine",
                "crs_epsg": 4326,
            },
        )
        vec = _make_vector_layer("V", tmp_path)
        win.layer_panel.add_layer(raster, select=False)
        win.layer_panel.add_layer(vec, select=False)

        win.layer_panel.select_layer(raster.layer_id)
        qapp.processEvents()
        assert win.canvas.current_layer is raster, "栅格图层应进入画布"

        win.layer_panel.select_layer(vec.layer_id)
        qapp.processEvents()
        assert win.canvas.current_layer is not vec, "矢量图层不得进入画布"
        assert win.canvas.img_item.image is None, "矢量阶段旧影像必须清除"

        win.layer_panel.select_layer(raster.layer_id)
        qapp.processEvents()
        assert win.canvas.current_layer is raster, "回到栅格图层应恢复画布"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


# ------------------------------------------------------- 3. 元数据请求上下文
def test_late_metadata_response_cannot_overwrite_current_layer(
    qapp: QApplication, tmp_path: Path
) -> None:
    """旧图层的迟到成功响应不得覆盖当前选中的图层元数据。

    用**反向顺序投递**（先 B 后 A）确定性复现，不依赖 sleep 猜时序。
    """
    win = MainWindow(project_root=tmp_path)
    win.show()
    try:
        client = _RecordingClient()
        win._clients["gdal"] = client

        a = _make_vector_layer("A", tmp_path)
        b = _make_vector_layer("B", tmp_path)
        win.layer_panel.add_layer(a, select=False)
        win.layer_panel.add_layer(b, select=False)

        win.layer_panel.select_layer(a.layer_id)
        qapp.processEvents()
        win.layer_panel.select_layer(b.layer_id)
        qapp.processEvents()

        # 捕获 A 与 B 各自的元数据回调
        by_stem = {Path(p).stem: (ok, err) for p, ok, err in client.vectors}
        assert "A" in by_stem and "B" in by_stem, "两个图层都应发起过矢量探测"

        def _info(name: str) -> dict:
            return {
                "driver": "spy",
                "layer_count": 1,
                "layers": [{"name": name, "fields": []}],
            }

        # 反向投递：B 先回，A 后回
        by_stem["B"][0](_info("B"))
        qapp.processEvents()
        assert win.metadata_panel.lst_vector_layers.item(0).text() == "B"

        by_stem["A"][0](_info("A"))
        qapp.processEvents()
        assert win.metadata_panel.lst_vector_layers.item(0).text() == "B", (
            "迟到的 A 响应不得覆盖当前选中的 B"
        )
        assert win._selected_layer().name == "B"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_late_metadata_error_cannot_overwrite_current_layer(
    qapp: QApplication, tmp_path: Path
) -> None:
    """旧图层的迟到**失败**响应同样不得覆盖当前状态（不只保护成功路径）。"""
    win = MainWindow(project_root=tmp_path)
    win.show()
    try:
        client = _RecordingClient()
        win._clients["gdal"] = client

        a = _make_vector_layer("A", tmp_path)
        b = _make_vector_layer("B", tmp_path)
        win.layer_panel.add_layer(a, select=False)
        win.layer_panel.add_layer(b, select=False)

        win.layer_panel.select_layer(a.layer_id)
        qapp.processEvents()
        win.layer_panel.select_layer(b.layer_id)
        qapp.processEvents()

        by_stem = {Path(p).stem: (ok, err) for p, ok, err in client.vectors}

        by_stem["B"][0](
            {
                "driver": "spy",
                "layer_count": 1,
                "layers": [{"name": "B", "fields": []}],
            }
        )
        qapp.processEvents()

        # A 的迟到失败不得把 B 的元数据清掉
        by_stem["A"][1]("late failure from A")
        qapp.processEvents()
        assert win.metadata_panel.lst_vector_layers.count() == 1, "B 的元数据不应被清空"
        assert win.metadata_panel.lst_vector_layers.item(0).text() == "B"
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


# ------------------------------------------------- 4. 子数据集仿射范围（四角）
def test_affine_bounds_uses_four_outer_corners() -> None:
    """旋转/倾斜仿射下范围必须按四个像元外边界角点计算。"""
    from spectra_workbench.providers.gdal_provider.adapter import _affine_bounds

    # 正北向
    assert _affine_bounds([100.0, 2.0, 0.0, 200.0, 0.0, -2.0], 10, 20) == [
        100.0,
        160.0,
        120.0,
        200.0,
    ]
    # 旋转：只算两个对角点会漏角
    rotated = _affine_bounds([500000.0, 2.0, 0.5, 3200000.0, 0.5, -2.0], 200, 150)
    assert rotated == [500000.0, 3199700.0, 500475.0, 3200100.0], f"旋转范围错误：{rotated}"
    # 负步长
    assert _affine_bounds([0.0, -1.0, 0.0, 0.0, 0.0, 1.0], 10, 10) == [
        -10.0,
        0.0,
        0.0,
        10.0,
    ]
    # 无 GeoTransform
    assert _affine_bounds(None, 10, 10) is None
    assert _affine_bounds([1.0, 2.0], 10, 10) is None


def test_probe_subdataset_bounds_match_four_corner_expectation(
    qapp: QApplication, p3_samples: dict
) -> None:
    """真实 worker 探测 GTIFF_DIR 数据源引用，范围必须等于四角独立计算值。"""
    from spectra_workbench.scheduler.client import QtWorkerClient

    rotated = Path(p3_samples["samples"]["rotated"]["path"]).resolve()
    uri = "GTIFF_DIR:1:" + str(rotated)

    client = QtWorkerClient(provider_id="gdal", project_root=str(rotated.parent))
    try:
        detail = client.probe_subdataset(uri, timeout=20.0)
    finally:
        client.stop(timeout=3.0)

    gt = detail["geotransform"]
    w = detail["width"]
    h = detail["height"]
    corners = [
        (gt[0] + x * gt[1] + y * gt[2], gt[3] + x * gt[4] + y * gt[5])
        for x, y in ((0, 0), (w, 0), (w, h), (0, h))
    ]
    expected = [
        min(x for x, _ in corners),
        min(y for _, y in corners),
        max(x for x, _ in corners),
        max(y for _, y in corners),
    ]
    assert detail["bounds"] == expected, (
        f"子数据集范围错误：报告 {detail['bounds']}，四角预期 {expected}"
    )


def test_probe_dataset_bounds_match_four_corner_expectation(
    qapp: QApplication, p3_samples: dict
) -> None:
    """栅格探测的范围同样必须按四角计算（与子数据集复用同一算法）。"""
    from spectra_workbench.scheduler.client import QtWorkerClient

    rotated = Path(p3_samples["samples"]["rotated"]["path"]).resolve()

    client = QtWorkerClient(provider_id="gdal", project_root=str(rotated.parent))
    try:
        meta = client.probe_dataset(rotated, timeout=20.0)
    finally:
        client.stop(timeout=3.0)

    gt = meta["geotransform"]
    w = meta["width"]
    h = meta["height"]
    corners = [
        (gt[0] + x * gt[1] + y * gt[2], gt[3] + x * gt[4] + y * gt[5])
        for x, y in ((0, 0), (w, 0), (w, h), (0, h))
    ]
    expected = [
        min(x for x, _ in corners),
        min(y for _, y in corners),
        max(x for x, _ in corners),
        max(y for _, y in corners),
    ]
    assert meta["bounds"] == expected, f"栅格范围错误：报告 {meta['bounds']}，预期 {expected}"


# ------------------------------------------------ 既有功能未被破坏（回归护栏）
def test_raster_roi_flow_still_works(qapp: QApplication, p3_samples: dict) -> None:
    """矢量相关改动不得破坏既有栅格 ROI 流程。"""
    rotated = Path(p3_samples["samples"]["rotated"]["path"]).resolve()

    win = MainWindow()
    win.show()
    try:
        win.open_dataset(rotated)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not win.layer_panel.layers:
            qapp.processEvents()
            time.sleep(0.02)
        assert win.layer_panel.layers, "栅格图层应能加载"

        roi = ROI(
            name="R",
            shape=ROIShape.rectangle,
            points=[(10.0, 10.0), (20.0, 20.0)],
            coordinate_space="pixel",
        )
        win.canvas._add_roi(roi)
        qapp.processEvents()
        assert len(win.canvas.roi_list()) == 1
        assert win.canvas.roi_list()[0].points == [(10.0, 10.0), (20.0, 20.0)]
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()
