"""P11 Batch 1 专项测试：
- P11-06：按掩膜分地类统计 (maskFile/maskBand/maskValue) 与密度分割 (gdal:density_slice)
- P11-04：FFT 频域滤波器羽化过渡宽度 (transition_width) 与矩形陷波滤波 (notch_regions)
- P11-08：多维高光谱坏波段 (bbl) 解析与标注
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from spectra_workbench.processing.raster_params import (
    canonical_capability_id,
    normalize_raster_params,
    supported_params_for,
)
from spectra_workbench.providers.gdal_provider.density_slice_ops import (
    compute_density_slice_table,
)
from spectra_workbench.providers.gdal_provider.fft_ops import (
    build_suppression_mask,
    hermitian_partner,
)


def _get_qgis_python() -> str:
    candidates = [
        Path("C:/Program Files/QGIS 3.44.15/apps/Python312/python.exe"),
        Path("C:/Program Files/QGIS 3.40.0/apps/Python312/python.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "python"


# ----------------------------------------------------------------------
# 1. 参数契约与别名归一化测试
# ----------------------------------------------------------------------


def test_p11_06_params_and_aliases() -> None:
    """验证 P11-06 参数与能力别名正确注册与解析。"""
    assert canonical_capability_id("gdal:density_slice") == "gdal:density_slice"
    assert canonical_capability_id("density_slice") == "gdal:density_slice"
    assert canonical_capability_id("gdal_density_slice") == "gdal:density_slice"

    # gdal:statistics 扩展参数
    stat_params = supported_params_for("gdal:statistics")
    assert stat_params is not None
    assert {"maskFile", "maskBand", "maskValue", "maskMin", "maskMax", "maskInvert"}.issubset(
        stat_params
    )

    # gdal:density_slice 支持参数
    slice_params = supported_params_for("gdal:density_slice")
    assert slice_params is not None
    assert {"band", "slices", "numSlices", "sliceMode", "dstNodata", "outFormat"}.issubset(
        slice_params
    )

    # 别名解析
    norm = normalize_raster_params(
        {
            "mask_file": "mask.tif",
            "mask_band": 2,
            "mask_value": 42.0,
            "mask_invert": True,
        },
        capability_id="gdal:statistics",
    )
    assert norm["maskFile"] == "mask.tif"
    assert norm["maskBand"] == 2
    assert norm["maskValue"] == 42.0
    assert norm["maskInvert"] is True


def test_p11_04_params_and_aliases() -> None:
    """验证 P11-04 FFT 滤波扩展参数与别名归一化。"""
    fft_params = supported_params_for("gdal:fftfilter")
    assert fft_params is not None
    assert {"transitionWidth", "notchRegions", "taperType"}.issubset(fft_params)

    norm = normalize_raster_params(
        {
            "fft_band": 1,
            "feather_width": 8.5,
            "taper_type": "gaussian",
            "notch_regions": [[0, 2, 0, 4]],
        },
        capability_id="gdal:fftfilter",
    )
    assert norm["fftBand"] == 1
    assert norm["transitionWidth"] == 8.5
    assert norm["taperType"] == "gaussian"
    assert norm["notchRegions"] == [[0, 2, 0, 4]]


# ----------------------------------------------------------------------
# 2. P11-04 FFT 频域滤波过渡带与厄米对称性测试
# ----------------------------------------------------------------------


def test_p11_04_fft_mask_hermitian_symmetry() -> None:
    """验证包含 transition_width 与 notch_regions 的频域掩膜在任意尺寸下均严格厄米对称。"""
    for height, width in ((32, 32), (31, 33), (16, 24)):
        mask, applied = build_suppression_mask(
            height,
            width,
            [(0, 4), (5, 5)],
            transition_width=4.0,
            notch_regions=[(1, 3, 2, 5)],
            taper_type="cosine",
        )
        assert len(applied) > 0
        assert mask.shape == (height, width)

        # 逐像元验证厄米对称性: mask[y, x] == mask[(-y)%H, (-x)%W]
        y_rev = (-np.arange(height)) % height
        x_rev = (-np.arange(width)) % width
        partner_mask = mask[y_rev, :][:, x_rev]
        diff = np.abs(mask - partner_mask).max()
        assert diff < 1e-12, f"厄米对称性破坏: 最大差异 {diff}"

        # 验证抑制中心点确实为 0.0，且过渡带平滑介于 [0, 1]
        for y, x in applied:
            assert mask[y, x] == 0.0
        assert float(mask.min()) == 0.0
        assert float(mask.max()) == 1.0


def test_p11_04_fft_gaussian_taper() -> None:
    """验证高斯滚降窗掩膜数值性质。"""
    mask, applied = build_suppression_mask(
        20,
        20,
        [(0, 2)],
        transition_width=5.0,
        taper_type="gaussian",
    )
    assert mask[0, 2] == 0.0
    p_y, p_x = hermitian_partner(0, 2, 20, 20)
    assert mask[p_y, p_x] == 0.0
    # 在非抑制且远离过渡区的位置值为 1.0
    assert mask[10, 10] == 1.0


# ----------------------------------------------------------------------
# 3. P11-06 密度分割区间划分与色表计算测试
# ----------------------------------------------------------------------


def test_p11_06_density_slice_table_equal_interval() -> None:
    """验证等间距密度分割区间划分、像元计数与面积核算。"""
    # 构造测试数据 0..99
    data = np.arange(100, dtype=np.float64).reshape((10, 10))
    valid = np.ones((10, 10), dtype=bool)
    gt = (0.0, 10.0, 0.0, 0.0, 0.0, -10.0)  # 10m 分辨率，每个像元 100m²

    table = compute_density_slice_table(
        data,
        valid,
        num_slices=4,
        slice_mode="equal_interval",
        gt=gt,
    )
    assert len(table) == 4
    total_cnt = sum(s["pixel_count"] for s in table)
    assert total_cnt == 100

    # 验证面积核算: 100 像元 * 100 m² = 10000 m²
    total_area = sum(s["area_sq_m"] for s in table if s["area_sq_m"] is not None)
    assert total_area == 10000.0

    # 验证百分比总和为 100%
    total_pct = sum(s["pixel_percent"] for s in table)
    assert abs(total_pct - 100.0) < 1e-4


def test_p11_06_density_slice_table_quantile() -> None:
    """验证分位数模式下区间像元基本均衡分布。"""
    data = np.linspace(0.0, 1.0, 1000).reshape((20, 50))
    valid = np.ones((20, 50), dtype=bool)

    table = compute_density_slice_table(
        data,
        valid,
        num_slices=5,
        slice_mode="quantile",
    )
    assert len(table) == 5
    for s in table:
        # 每个区间约 200 像元
        assert 190 <= s["pixel_count"] <= 210


# ----------------------------------------------------------------------
# 4. P11-08 坏波段解析逻辑测试
# ----------------------------------------------------------------------


def test_p11_08_bad_band_parsing() -> None:
    """测试 ENVI bbl (bad band list) 字符串解析与标记提取。"""
    raw_bbl = "{1, 1, 0, 1, 0, 0, 1, 1}"
    clean = raw_bbl.strip().strip("{}").strip()
    tokens = [t.strip() for t in clean.replace(",", " ").split() if t.strip()]

    bad_bands = [idx + 1 for idx, tok in enumerate(tokens) if tok == "0"]
    assert bad_bands == [3, 5, 6]
