"""P6-02 FFT 频域滤波的纯数学契约测试。

覆盖验收中的数学核心：
- 掩膜对称性（厄米伙伴）在偶数 8×8 与奇数 7×9 尺寸下均成立；
- 复数频谱经对称掩膜逆变换后虚部可忽略（实数成果的前提）；
- 空抑制列表是恒等变换（往返）；
- 负频率 bin 按 fftfreq 语义模归一化，重复 bin 去重；
- fftBand/suppressBins/srcNodata/dstNodata 的共享参数契约。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from spectra_workbench.processing.raster_params import (
    ParameterError,
    normalize_raster_params,
    supported_params_for,
)
from spectra_workbench.providers.gdal_provider.fft_ops import (
    build_suppression_mask,
    hermitian_partner,
)

pytestmark = [pytest.mark.unit]


def _pattern(rows: int, cols: int) -> np.ndarray:
    """常量 + x 方向周期 2 的余弦 + y 方向周期 1 的正弦（整数周期，无泄漏）。"""
    y, x = np.mgrid[0:rows, 0:cols]
    return (
        10.0 + 3.0 * np.cos(2.0 * math.pi * 2.0 * x / cols) + 5.0 * np.sin(2.0 * math.pi * y / rows)
    )


@pytest.mark.parametrize(("rows", "cols"), [(8, 8), (7, 9)])
def test_mask_is_hermitian_symmetric_for_even_and_odd_sizes(rows: int, cols: int) -> None:
    mask, applied = build_suppression_mask(rows, cols, [(0, 2)])

    assert mask.shape == (rows, cols)
    assert mask.dtype == bool
    partner = hermitian_partner(0, 2, rows, cols)
    assert partner == (0, (-2) % cols)
    assert not mask[0, 2]
    assert not mask[partner]
    for fy, fx in applied:
        assert not mask[fy, fx], f"({fy},{fx}) 应被抑制"
        assert not mask[hermitian_partner(fy, fx, rows, cols)], f"({fy},{fx}) 的厄米伙伴未被抑制"
    # 除了实际抑制的 bin，其余全部保留（1）。
    assert int(mask.sum()) == rows * cols - len(applied)


def test_negative_bins_are_normalized_and_duplicates_are_collapsed() -> None:
    mask, applied = build_suppression_mask(8, 8, [(0, -2), (0, 6), (-8, 0)])

    assert (0, 2) in applied
    assert (0, 6) in applied
    assert (0, 0) in applied
    assert applied == sorted(set(applied))
    assert int(mask.sum()) == 64 - len(applied)
    for fy, fx in applied:
        assert not mask[fy, fx]


@pytest.mark.parametrize(("rows", "cols"), [(8, 8), (7, 9)])
def test_complex_spectrum_with_symmetric_mask_inverse_is_real(rows: int, cols: int) -> None:
    image = _pattern(rows, cols)
    spectrum = np.fft.fft2(image)
    assert np.iscomplexobj(spectrum)
    # 频率定位：整数周期正弦/余弦的能量精确落在预期 bin。
    assert abs(spectrum[1, 0]) == pytest.approx(5.0 * rows * cols / 2.0)
    assert abs(spectrum[0, 2]) == pytest.approx(3.0 * rows * cols / 2.0)
    assert abs(spectrum[0, cols - 2]) == pytest.approx(3.0 * rows * cols / 2.0)

    mask, _ = build_suppression_mask(rows, cols, [(0, 2)])
    restored = np.fft.ifft2(spectrum * mask)

    # 实数输入 + 厄米对称掩膜 ⇒ 逆变换虚部应≈0；这是 fft_ops 丢弃虚部前的断言。
    assert float(np.max(np.abs(restored.imag))) < 1e-10
    expected = 10.0 + 5.0 * np.sin(2.0 * math.pi * np.arange(rows)[:, None] / rows)
    assert np.allclose(restored.real, np.broadcast_to(expected, (rows, cols)), atol=1e-9)


def test_empty_suppression_mask_is_identity_roundtrip() -> None:
    image = _pattern(8, 8)
    mask, applied = build_suppression_mask(8, 8, [])

    assert applied == []
    assert mask.all()
    restored = np.fft.ifft2(np.fft.fft2(image) * mask).real
    assert np.allclose(restored, image, atol=1e-9)


def test_fftfilter_param_contract_and_zero_nodata() -> None:
    supported = supported_params_for("gdal:fftfilter")
    assert supported is not None
    assert {"fftBand", "suppressBins", "srcNodata", "dstNodata"} <= set(supported)

    norm = normalize_raster_params(
        {
            "fft_band": 2,
            "suppress_bins": [[0, 4], [0, -4]],
            "srcNodata": -1,
            "dstNodata": 0,
            "format": "GTiff",
        },
        capability_id="gdal:fftfilter",
    )
    assert norm["fftBand"] == 2
    assert norm["suppressBins"] == [(0, 4), (0, -4)]
    assert norm["srcNodata"] == -1.0
    assert norm["dstNodata"] == 0.0, "零值 NoData 必须保留，不能被 truthiness 丢掉"


@pytest.mark.parametrize(
    "raw",
    [
        {"fftBand": 0},
        {"fftBand": 1.5},
        {"suppressBins": [[0]]},
        {"suppressBins": [[0, 1.5]]},
        {"suppressBins": "0,4"},
        {"sindex": "ndvi"},  # 其它能力的参数必须被拒绝
        {"xRes": 1.0},  # FFT 不消费几何参数
    ],
)
def test_invalid_fftfilter_params_are_rejected(raw: dict) -> None:
    with pytest.raises(ParameterError):
        normalize_raster_params(raw, capability_id="gdal:fftfilter")
