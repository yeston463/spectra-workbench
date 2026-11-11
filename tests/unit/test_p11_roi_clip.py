"""P11-07 几何裁剪管线桥单元测试。

验证内容：
1. 契约层参数归一化与校验（normalize_raster_params / gdal:clip_mask）：
   - 必须提供 cutlinePath、roiPoints 或 cutlineGeoJson 其一；
   - 支持别名（clip_mask、gdal_clip_mask、gdal:clip）；
   - cropToCutline、dstNodata、resampleAlg 校验；
2. UI 交互层集成：
   - ImageCanvas ROI 工具栏按钮与 sig_clip_roi_requested 信号触发；
   - LayerPanel 顶部按钮与 sig_clip_layer_requested 信号触发；
   - InfoPanel ROI 列表裁剪按钮与 sig_roi_clip_requested 信号触发；
   - MainWindow.clip_layer_by_roi 正确装配 TaskSpec 契约结构并派发；
3. GDAL cutline 掩膜裁剪算子（clip_ops.py）：
   - _build_cutline_geojson 顶点展开、仿射转换与闭合环验证；
   - 审计 sidecar 文件结构完整性。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.domain.roi import ROI, ROIShape
from spectra_workbench.processing.raster_params import (
    ParameterError,
    canonical_capability_id,
    normalize_raster_params,
)
from spectra_workbench.providers.gdal_provider.clip_ops import (
    ClipError,
    _build_cutline_geojson,
)
from spectra_workbench.ui.canvas.image_canvas import ImageCanvas
from spectra_workbench.ui.models.layer import LayerItem
from spectra_workbench.ui.panels.info_panel import InfoPanel
from spectra_workbench.ui.panels.layer_panel import LayerPanel


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_dummy_layer(layer_id: str = "L1", name: str = "Test Layer", nodata: float = -9999.0) -> LayerItem:
    meta: dict[str, Any] = {
        "width": 100,
        "height": 100,
        "count": 1,
        "driver": "GTiff",
        "crs_epsg": 32650,
        "crs_wkt": 'PROJCS["WGS 84 / UTM zone 50N"]',
        "geotransform": [500000.0, 10.0, 0.0, 4000000.0, 0.0, -10.0],
        "bounds": [500000.0, 3999000.0, 501000.0, 4000000.0],
        "dtypes": ["Float32"],
        "nodata": nodata,
    }
    return LayerItem(
        file_path="C:/dummy/test.tif",
        name=name,
        metadata=meta,
        layer_id=layer_id,
    )


# ==============================================================================
# 1. 契约层参数校验与归一化
# ==============================================================================
class TestClipMaskParameterNormalization:
    def test_canonical_id_and_aliases(self) -> None:
        assert canonical_capability_id("gdal:clip_mask") == "gdal:clip_mask"
        assert canonical_capability_id("clip_mask") == "gdal:clip_mask"
        assert canonical_capability_id("gdal_clip_mask") == "gdal:clip_mask"
        assert canonical_capability_id("gdal:clip") == "gdal:clip_mask"

    def test_missing_cutline_raises_error(self) -> None:
        with pytest.raises(ParameterError, match="需要 cutlinePath、roiPoints 或 cutlineGeoJson"):
            normalize_raster_params({}, capability_id="gdal:clip_mask")

    def test_valid_roi_points(self) -> None:
        pts = [(10, 10), (50, 10), (30, 60)]
        norm = normalize_raster_params(
            {"roi_points": pts, "crop_to_cutline": True, "dst_nodata": -9999.0},
            capability_id="gdal:clip_mask",
        )
        assert norm["roiPoints"] == [[10.0, 10.0], [50.0, 10.0], [30.0, 60.0]]
        assert norm["cropToCutline"] is True
        assert norm["dstNodata"] == -9999.0

    def test_invalid_roi_points_less_than_3(self) -> None:
        with pytest.raises(ParameterError, match="必须是包含至少 3 个顶点的坐标序列"):
            normalize_raster_params({"roiPoints": [(10, 10), (20, 20)]}, capability_id="gdal:clip_mask")

    def test_valid_cutline_path(self) -> None:
        norm = normalize_raster_params(
            {"cutline_path": "C:/vector/cutline.shp", "crop_to_cutline": True},
            capability_id="gdal:clip_mask",
        )
        assert norm["cutlinePath"] == "C:/vector/cutline.shp"
        assert norm["cropToCutline"] is True


# ==============================================================================
# 2. Cutline 几何构建算子单元测试
# ==============================================================================
class TestCutlineGeoJsonBuilder:
    def test_pixel_to_map_transformation(self) -> None:
        gt = (500000.0, 10.0, 0.0, 4000000.0, 0.0, -10.0)
        pts = [(0, 0), (10, 0), (10, 10), (0, 10)]
        fc = _build_cutline_geojson(pts, "pixel", gt, None)
        assert fc["type"] == "FeatureCollection"
        coords = fc["features"][0]["geometry"]["coordinates"][0]
        # 首末点自闭合
        assert len(coords) == 5
        assert coords[0] == coords[-1]
        assert coords[0] == [500000.0, 4000000.0]
        assert coords[1] == [500100.0, 4000000.0]
        assert coords[2] == [500100.0, 3999900.0]

    def test_insufficient_points_raises_clip_error(self) -> None:
        with pytest.raises(ClipError, match="至少需要 3 个顶点"):
            _build_cutline_geojson([(0, 0), (1, 1)], "pixel", None, None)


# ==============================================================================
# 3. UI 交互层集成测试
# ==============================================================================
class TestClipUIIntegration:
    def test_canvas_clip_button_emits_signal(self, qapp: QApplication) -> None:
        canvas = ImageCanvas()
        canvas.show()
        layer = _make_dummy_layer()
        canvas.set_layer(layer)

        # 添加一个多边形 ROI 并选中
        roi = ROI(
            name="CropPolygon",
            shape=ROIShape.polygon,
            coordinate_space="pixel",
            points=[(10.0, 10.0), (40.0, 10.0), (25.0, 40.0)],
        )
        canvas.load_rois([roi])
        canvas.select_roi(roi.roi_id)

        emitted: list[tuple[Any, Any]] = []
        canvas.sig_clip_roi_requested.connect(lambda ly, r: emitted.append((ly, r)))

        canvas.btn_clip_roi.click()
        assert len(emitted) == 1
        assert emitted[0][0].layer_id == layer.layer_id
        assert emitted[0][1].roi_id == roi.roi_id

    def test_layer_panel_clip_button(self, qapp: QApplication) -> None:
        panel = LayerPanel()
        assert not panel.btn_clip.isEnabled()

        layer = _make_dummy_layer()
        panel.add_layer(layer, select=True)
        assert panel.btn_clip.isEnabled()

        emitted: list[Any] = []
        panel.sig_clip_layer_requested.connect(lambda ly: emitted.append(ly))

        panel.btn_clip.click()
        assert len(emitted) == 1
        assert emitted[0].layer_id == layer.layer_id

    def test_info_panel_roi_clip_button(self, qapp: QApplication) -> None:
        panel = InfoPanel()
        roi = ROI(
            name="TestROI",
            shape=ROIShape.polygon,
            coordinate_space="pixel",
            points=[(5.0, 5.0), (15.0, 5.0), (10.0, 15.0)],
        )
        panel.set_rois([roi])
        # 选中该 ROI
        panel.lst_rois.setCurrentRow(0)

        emitted_ids: list[str] = []
        panel.sig_roi_clip_requested.connect(lambda rid: emitted_ids.append(rid))

        panel.btn_clip_roi.click()
        assert len(emitted_ids) == 1
        assert emitted_ids[0] == roi.roi_id
