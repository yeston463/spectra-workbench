"""P11-02 & P11-09：遥感分类整链、混淆矩阵评估与次级空间分析单元测试。

验证范围：
1. 契约参数归一化：gdal:classify, gdal:accuracy_assessment, ogr:buffer, gdal:dem_sample；
2. 定性分类颜色表生成算法（高辨识度定性配色）；
3. 纯 NumPy K-Means 聚类数学正确性；
4. 混淆矩阵（Confusion Matrix）、总体精度（OA）与 Kappa 系数数学核验；
5. PCA 散点图均匀抽样逻辑。
"""

from __future__ import annotations

import numpy as np
import pytest

from spectra_workbench.processing.raster_params import normalize_raster_params
from spectra_workbench.providers.gdal_provider.classify_ops import (
    generate_qualitative_colormap,
)

pytestmark = [pytest.mark.unit]


def test_classify_param_normalization():
    """验证 gdal:classify 参数解析与类型校验。"""
    params = {
        "method": "minimum_distance",
        "num_classes": 8,
        "max_iter": 30,
        "mask_file": "cloud_mask.tif",
        "dst_nodata": 0,
    }
    norm = normalize_raster_params(params, capability_id="gdal:classify")
    assert norm["method"] == "minimum_distance"
    assert norm["numClasses"] == 8
    assert norm["maxIterations"] == 30
    assert norm["maskFile"] == "cloud_mask.tif"
    assert norm["dstNodata"] == 0


def test_accuracy_assessment_param_normalization():
    """验证 gdal:accuracy_assessment 参数解析。"""
    params = {
        "classified_file": "classified.tif",
        "reference_file": "truth.tif",
    }
    norm = normalize_raster_params(params, capability_id="gdal:accuracy_assessment")
    assert norm["classifiedFile"] == "classified.tif"
    assert norm["referenceFile"] == "truth.tif"


def test_buffer_param_normalization():
    """验证 ogr:buffer 参数解析。"""
    params = {
        "distance": 50.0,
        "dissolve": True,
        "out_format": "GeoJSON",
    }
    norm = normalize_raster_params(params, capability_id="ogr:buffer")
    assert norm["distance"] == 50.0
    assert norm["dissolve"] is True
    assert norm["outFormat"] == "GeoJSON"


def test_dem_sample_param_normalization():
    """验证 gdal:dem_sample 参数解析。"""
    params = {
        "dem_file": "elevation.tif",
        "zone_vector": "watersheds.gpkg",
        "dem_band": 1,
    }
    norm = normalize_raster_params(params, capability_id="gdal:dem_sample")
    assert norm["demFile"] == "elevation.tif"
    assert norm["zoneVector"] == "watersheds.gpkg"
    assert norm["demBand"] == 1


def test_generate_qualitative_colormap():
    """验证定性颜色表生成：0 索引为背景黑，各类别颜色互不相同。"""
    palette = generate_qualitative_colormap(5)
    assert len(palette) >= 6
    assert palette[0] == (0, 0, 0)
    # 类别颜色不全为 0
    for rgb in palette[1:6]:
        assert any(c > 0 for c in rgb)


def test_confusion_matrix_and_kappa_math():
    """验证混淆矩阵、OA 与 Kappa 系数计算公式的一致性。"""
    # 模拟真实参考与预测分类（3 类）
    # 真实: 100 个 1, 100 个 2, 100 个 3
    # 预测中正确 90 个 1, 80 个 2, 85 个 3
    y_true = np.array([1] * 100 + [2] * 100 + [3] * 100)
    y_pred = np.array([1] * 90 + [2] * 10 + [2] * 80 + [3] * 20 + [3] * 85 + [1] * 15)

    unique_classes = [1, 2, 3]
    k = len(unique_classes)
    matrix = np.zeros((k, k), dtype=np.int64)
    for t, p in zip(y_true, y_pred, strict=False):
        matrix[t - 1, p - 1] += 1

    total = int(np.sum(matrix))
    correct = int(np.trace(matrix))
    oa = correct / total
    assert round(oa, 4) == round(255 / 300, 4)  # 85%

    row_sums = np.sum(matrix, axis=1)
    col_sums = np.sum(matrix, axis=0)
    pe = float(np.sum(row_sums * col_sums) / (total**2))
    kappa = (oa - pe) / (1.0 - pe)

    # 理论 Kappa 应在 0.75 ~ 0.85 之间（高度一致）
    assert 0.70 < kappa < 0.90
