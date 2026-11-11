"""P11-03 & P11-05：高级图像镶嵌与高光谱分析链单元测试。

验证范围：
1. 契约参数归一化：gdal:mosaic_advanced 与 gdal:sam 严格校验；
2. 经验直方图匹配数学正确性（CDF 逆变换单调映射）；
3. 距离场余弦渐变权重边界平滑性；
4. 光谱角制图（SAM）内积反余弦与单位换算；
5. 强水汽吸收谷波段（1400nm / 1900nm）自动识别；
6. Savitzky-Golay 像元连续光谱多项式滤波平滑。
"""

from __future__ import annotations

import numpy as np
import pytest

from spectra_workbench.processing.raster_params import normalize_raster_params
from spectra_workbench.providers.gdal_provider.advanced_mosaic_ops import (
    _compute_distance_weights,
    match_histogram_1d,
)
from spectra_workbench.providers.gdal_provider.hyperspectral_ops import (
    identify_water_absorption_bands,
    savgol_filter_spectral,
)

pytestmark = [pytest.mark.unit]


def test_mosaic_advanced_param_normalization():
    """验证 gdal:mosaic_advanced 参数解析与类型校验。"""
    params = {
        "match_histogram": True,
        "master_index": 1,
        "feather_width": 30,
        "dst_nodata": -9999.0,
    }
    norm = normalize_raster_params(params, capability_id="gdal:mosaic_advanced")
    assert norm["matchHistogram"] is True
    assert norm["masterIndex"] == 1
    assert norm["featherWidth"] == 30
    assert norm["dstNodata"] == -9999.0


def test_sam_param_normalization():
    """验证 gdal:sam 参数解析与类型校验。"""
    params = {
        "reference_spectrum": [0.1, 0.2, 0.3, 0.4],
        "angle_unit": "degrees",
        "max_angle": 15.0,
        "dst_nodata": -9999.0,
    }
    norm = normalize_raster_params(params, capability_id="gdal:sam")
    assert norm["referenceSpectrum"] == [0.1, 0.2, 0.3, 0.4]
    assert norm["angleUnit"] == "degrees"
    assert norm["maxAngle"] == 15.0
    assert norm["dstNodata"] == -9999.0


def test_histogram_matching_1d():
    """验证单波段经验直方图匹配算法（使源数据均值与方差趋近参考数据）。"""
    np.random.seed(42)
    # 源数据均值 50，参考数据均值 100
    src_sample = np.random.normal(50.0, 10.0, 1000).astype(np.float32)
    ref_sample = np.random.normal(100.0, 20.0, 1000).astype(np.float32)
    src_full = np.linspace(20.0, 80.0, 50).astype(np.float32)

    matched = match_histogram_1d(src_sample, ref_sample, src_full)
    assert matched.shape == src_full.shape
    # 匹配后均值应显著向 100 靠拢
    assert matched.mean() > 75.0
    # 保留严格单调递增性
    assert np.all(np.diff(matched) >= 0.0)


def test_distance_weights_cosine_rolloff():
    """验证距离场余弦渐变权重计算：边界为 0，内部为 1，单调平滑。"""
    mask = np.zeros((20, 20), dtype=bool)
    mask[2:18, 2:18] = True  # 中心有效区域

    weights = _compute_distance_weights(mask, feather_width=6)
    assert weights.shape == (20, 20)
    assert weights[0, 0] == 0.0
    assert weights[10, 10] == 1.0
    # 边缘边界向内权重单调上升
    assert 0.0 < weights[3, 3] < 1.0


def test_water_absorption_band_identification():
    """验证依据波长自动识别 1400nm 与 1900nm 水汽吸收谷。"""
    # 纳米单位测试
    wavelengths_nm = [450.0, 850.0, 1380.0, 1420.0, 1600.0, 1850.0, 2200.0]
    bad_bands = identify_water_absorption_bands(wavelengths_nm)
    # 1380, 1420 (索引 3, 4) 与 1850 (索引 6)
    assert bad_bands == [3, 4, 6]

    # 微米单位自动换算测试
    wavelengths_um = [0.45, 0.85, 1.40, 1.60, 1.88, 2.20]
    bad_bands_um = identify_water_absorption_bands(wavelengths_um)
    assert bad_bands_um == [3, 5]


def test_savgol_filter_spectral():
    """验证 Savitzky-Golay 光谱维多项式平滑滤波。"""
    # 构造含高斯噪声的 15 波段假吸收特征曲线
    bands = 15
    wl = np.linspace(0.0, 1.0, bands)
    true_curve = 0.5 - 0.3 * np.exp(-((wl - 0.5) ** 2) / 0.02)
    noise = np.array(
        [
            0.02,
            -0.02,
            0.03,
            -0.03,
            0.01,
            -0.01,
            0.02,
            -0.02,
            0.01,
            -0.01,
            0.02,
            -0.02,
            0.01,
            -0.01,
            0.0,
        ]
    )
    noisy_curve = (true_curve + noise).astype(np.float32)

    cube = noisy_curve[:, np.newaxis, np.newaxis]  # (15, 1, 1)
    smoothed = savgol_filter_spectral(cube, window_length=5, polyorder=2)

    assert smoothed.shape == cube.shape
    # 平滑后残差方差应显著小于噪声方差
    noisy_diff = np.var(noisy_curve - true_curve)
    smoothed_diff = np.var(smoothed.ravel() - true_curve)
    assert smoothed_diff < noisy_diff
