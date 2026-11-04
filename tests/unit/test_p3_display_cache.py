"""P3-04 显示变换与 P3-03 缓存/代际的单元测试。"""

from __future__ import annotations

import base64

import numpy as np
import pytest

from spectra_workbench.rendering.cache import TileCache, TileKey, ViewGeneration
from spectra_workbench.rendering.display import (
    DisplayMode,
    DisplaySettings,
    StretchMode,
    available_colormaps,
    colormap_lut,
    decode_window,
    physical_values,
    stats_source_label,
    stretch_to_uint8,
    to_rgba,
)

pytestmark = pytest.mark.unit


def make_payload(arr: np.ndarray, valid: np.ndarray, **over) -> dict:
    """构造一份 read_window(raw=True) 形状的响应。"""
    stacked = np.stack([arr]) if arr.ndim == 2 else arr
    vstack = np.stack([valid]) if valid.ndim == 2 else valid
    nb, h, w = stacked.shape
    bits = np.packbits(vstack.reshape(-1).astype(np.uint8), axis=None)
    payload = {
        "raw": True,
        "numpy_dtype": str(stacked.dtype),
        "shape": [nb, h, w],
        "data_b64": base64.b64encode(np.ascontiguousarray(stacked).tobytes()).decode("ascii"),
        "valid_bits_b64": base64.b64encode(bits.tobytes()).decode("ascii"),
        "valid_bits_count": int(vstack.size),
        "x_off": 0,
        "y_off": 0,
        "win_w": w,
        "win_h": h,
        "out_w": w,
        "out_h": h,
        "band_meta": [{"band": i + 1, "nodata": None, "scale": None, "offset": None} for i in range(nb)],
        "overview_used": -1,
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------- display


def test_decode_window_preserves_raw_values():
    arr = np.arange(64, dtype=np.uint16).reshape(8, 8)
    valid = np.ones((8, 8), dtype=bool)
    win = decode_window(make_payload(arr, valid))
    assert np.array_equal(win.bands[0], arr)
    assert win.valid[0].all()
    assert win.out_w == 8 and win.overview_used == -1


def test_decode_window_rejects_non_raw_payload():
    payload = make_payload(np.zeros((4, 4), np.uint8), np.ones((4, 4), bool))
    payload["raw"] = False
    with pytest.raises(ValueError, match="raw"):
        decode_window(payload)


def test_decode_window_detects_size_mismatch():
    payload = make_payload(np.zeros((8, 8), np.uint16), np.ones((8, 8), bool))
    payload["shape"] = [1, 8, 7]
    with pytest.raises(ValueError, match="尺寸不符"):
        decode_window(payload)


def test_percentile_stretch_ignores_single_spike():
    arr = np.full((10, 10), 100, dtype=np.uint16)
    arr[0, 0] = 60000
    valid = np.ones((10, 10), dtype=bool)
    win = decode_window(make_payload(arr, valid))

    s_pct = DisplaySettings(stretch=StretchMode.percentile, percentiles=(2.0, 98.0))
    rgba_pct, bounds_pct = to_rgba(win, s_pct)
    s_mm = DisplaySettings(stretch=StretchMode.min_max)
    _, bounds_mm = to_rgba(win, s_mm)

    assert bounds_mm[0] == (100.0, 60000.0)
    lo, hi = bounds_pct[0]
    assert hi < 60000.0, f"百分位拉伸不应把孤立尖峰当上限：{bounds_pct}"
    # 尖峰自身在百分位下被截到上限（仍可见），其余像元铺满
    assert rgba_pct.shape == (10, 10, 4)


def test_invalid_pixels_are_transparent_and_valid_zero_is_visible():
    arr = np.array([[0, 7], [65535, 9]], dtype=np.uint16)
    valid = np.array([[True, True], [False, True]], dtype=bool)
    win = decode_window(make_payload(arr, valid))
    settings = DisplaySettings(stretch=StretchMode.min_max)
    rgba, _ = to_rgba(win, settings)

    assert rgba[0, 0, 3] != 0, "合法 0 值必须正常显示，不能被当成 NoData"
    assert rgba[0, 0, 0] == 0, "0 值在 min-max 下应落在区间低端"
    assert rgba[1, 0, 3] == 0, "NoData 像元必须透明"
    assert rgba[1, 1, 3] != 0


def test_opacity_applies_to_valid_pixels_only():
    arr = np.array([[1, 2], [3, 4]], dtype=np.uint16)
    valid = np.array([[True, False], [True, True]], dtype=bool)
    win = decode_window(make_payload(arr, valid))
    rgba, _ = to_rgba(win, DisplaySettings(stretch=StretchMode.min_max, opacity=0.5))
    assert rgba[0, 0, 3] == 128
    assert rgba[0, 1, 3] == 0


def test_to_rgba_does_not_mutate_source_arrays():
    arr = np.array([[10, 20], [30, 40]], dtype=np.uint16)
    valid = np.ones((2, 2), dtype=bool)
    win = decode_window(make_payload(arr, valid))
    before = win.bands[0].copy()
    to_rgba(win, DisplaySettings(stretch=StretchMode.percentile))
    assert np.array_equal(win.bands[0], before), "显示变换不得回写源数组"
    assert win.bands[0].dtype == np.uint16


def test_rgb_mode_requires_three_bands_and_maps_channels():
    a = np.zeros((4, 4), np.uint16)
    arr = np.stack([a + 0, a + 50, a + 100])
    valid = np.ones((3, 4, 4), dtype=bool)
    win = decode_window(make_payload(arr, valid))
    rgba, bounds = to_rgba(win, DisplayMode and DisplaySettings(mode=DisplayMode.rgb, stretch=StretchMode.min_max))
    assert len(bounds) == 3
    # 每波段各自 min-max 后都应是同一个低值 -> 三通道相等
    assert rgba[0, 0, 0] == rgba[0, 0, 1] == rgba[0, 0, 2]

    single = decode_window(make_payload(a, np.ones((4, 4), bool)))
    with pytest.raises(ValueError, match="3 个波段"):
        to_rgba(single, DisplaySettings(mode=DisplayMode.rgb))


def test_pseudocolor_maps_gray_through_lut():
    arr = np.array([[0, 255], [128, 64]], dtype=np.uint8)
    valid = np.ones((2, 2), dtype=bool)
    win = decode_window(make_payload(arr, valid))
    settings = DisplaySettings(mode=DisplayMode.pseudo, pseudocolor="viridis", stretch=StretchMode.min_max)
    rgba, _ = to_rgba(win, settings)
    lut = colormap_lut("viridis")
    assert lut.shape == (256, 3)
    assert np.array_equal(rgba[0, 0, :3], lut[0])
    assert np.array_equal(rgba[0, 1, :3], lut[255])
    assert "jet" in available_colormaps()
    with pytest.raises(ValueError, match="未知伪彩"):
        colormap_lut("rainbow_not_exist")


def test_physical_values_apply_scale_offset_without_mutating():
    arr = np.array([1, 2], dtype=np.uint16)
    out = physical_values(arr, {"scale": 1e-4, "offset": 0.5})
    assert np.allclose(out, [0.5001, 0.5002])
    assert arr.dtype == np.uint16 and arr[0] == 1
    plain = physical_values(arr, {})
    assert np.allclose(plain, [1.0, 2.0])


def test_stretch_degenerate_range_returns_zero():
    arr = np.full((3, 3), 7, dtype=np.uint16)
    valid = np.ones((3, 3), dtype=bool)
    out = stretch_to_uint8(arr, valid, 7.0, 7.0)
    assert out.dtype == np.uint8 and (out == 0).all()


def test_stats_source_label_distinguishes_sampled_and_full():
    assert stats_source_label(None) == "窗口抽样(当前视图)"
    assert stats_source_label({"mode": "full", "source": "full_raster"}) == "全量统计"
    assert stats_source_label({"mode": "sampled", "source": "sampled_grid", "stride": 4}) == "抽样统计(stride=4)"


def test_display_settings_round_trip_and_band_validation():
    s = DisplaySettings(mode=DisplayMode.pseudo, gray_band=3, rgb_bands=(2, 3, 4), opacity=0.7)
    again = DisplaySettings.from_dict(s.to_dict())
    assert again == s
    assert again.selected_bands(4) == [3]
    rgb = DisplaySettings(mode=DisplayMode.rgb, rgb_bands=(2, 3, 4))
    assert rgb.selected_bands(4) == [2, 3, 4]
    with pytest.raises(ValueError, match="越界"):
        rgb.selected_bands(3)
    with pytest.raises(ValueError, match="display 配置"):
        DisplaySettings.from_dict("nope")  # type: ignore[arg-type]


# ---------------------------------------------------------------- cache


def _key(bands=(1,), x=0, ov=-1, source="a.tif"):
    return TileKey(source=source, bands=tuple(bands), x_off=x, y_off=0, win_w=64, win_h=64, out_w=64, out_h=64, overview=ov)


def test_cache_respects_byte_budget_with_lru():
    cache = TileCache(budget_bytes=1000)
    payload = b"x" * 400
    cache.put(_key(x=0), payload)
    cache.put(_key(x=1), payload)
    assert cache.stats.bytes == 800
    cache.put(_key(x=2), payload)  # 超预算 -> 逐出最久未用
    assert cache.stats.bytes <= 1000
    assert cache.stats.evictions >= 1
    assert cache.get(_key(x=0)) is None, "最久未用的条目应被逐出"
    assert cache.get(_key(x=2)) == payload


def test_cache_hit_refreshes_recency():
    cache = TileCache(budget_bytes=1000)
    cache.put(_key(x=0), b"x" * 400)
    cache.put(_key(x=1), b"x" * 400)
    assert cache.get(_key(x=0)) is not None  # x0 变成最近使用
    cache.put(_key(x=2), b"x" * 400)
    assert cache.get(_key(x=1)) is None, "x1 更久未用，应先被逐出"
    assert cache.get(_key(x=0)) is not None


def test_oversized_single_entry_is_not_cached_and_keeps_others():
    cache = TileCache(budget_bytes=500)
    cache.put(_key(x=0), b"x" * 200)
    cache.put(_key(x=9), b"x" * 900)
    assert cache.get(_key(x=9)) is None
    assert cache.get(_key(x=0)) is not None, "超大条目不应清空已有缓存"
    assert cache.stats.evictions == 0


def test_cache_key_separates_bands_and_overview_and_source():
    cache = TileCache(budget_bytes=2000)
    cache.put(_key(bands=(1,)), b"one")
    cache.put(_key(bands=(2,)), b"two")
    cache.put(_key(bands=(1,), ov=2), b"ov")
    cache.put(_key(bands=(1,), source="b.tif"), b"other")
    assert cache.get(_key(bands=(1,))) == b"one"
    assert cache.get(_key(bands=(2,))) == b"two"
    assert cache.get(_key(bands=(1,), ov=2)) == b"ov"
    assert cache.get(_key(bands=(1,), source="b.tif")) == b"other"
    assert cache.stats.entries == 4


def test_invalidate_source_only_touches_that_source():
    cache = TileCache(budget_bytes=4000)
    cache.put(_key(source="a.tif"), b"1")
    cache.put(_key(x=1, source="a.tif"), b"2")
    cache.put(_key(source="b.tif"), b"3")
    assert cache.invalidate_source("a.tif") == 2
    assert cache.get(_key(source="b.tif")) == b"3"
    assert cache.get(_key(source="a.tif")) is None


def test_set_budget_trims_immediately():
    cache = TileCache(budget_bytes=2000)
    for i in range(4):
        cache.put(_key(x=i), b"x" * 400)
    cache.set_budget(800)
    assert cache.stats.bytes <= 800
    with pytest.raises(ValueError, match="正数"):
        cache.set_budget(0)


def test_default_budget_is_bounded_not_huge(monkeypatch):
    monkeypatch.delenv("SPECTRA_TILE_CACHE_MIB", raising=False)
    from spectra_workbench.rendering.cache import default_budget_bytes

    got = default_budget_bytes()
    assert 16 * 1024 * 1024 <= got <= 512 * 1024 * 1024


def test_view_generation_rejects_stale_responses():
    gen = ViewGeneration()
    g1 = gen.bump("layerA")
    assert gen.is_current(g1, "layerA")
    g2 = gen.bump("layerA")
    assert not gen.is_current(g1, "layerA"), "旧代际响应必须丢弃"
    assert gen.is_current(g2, "layerA")
    g3 = gen.bump("layerB")
    assert not gen.is_current(g3, "layerA"), "切图层后旧图层响应必须丢弃"


def test_pending_refresh_flag_pattern_survives_burst():
    """模拟快速连续缩放：只渲染最后一次请求的结果。"""
    gen = ViewGeneration()
    rendered: list[int] = []
    pending = {"flag": False}

    def request():
        g = gen.bump("L")
        return g

    def on_response(g: int, layer: str):
        if not gen.is_current(g, layer):
            pending["flag"] = True
            return
        rendered.append(g)
        pending["flag"] = False

    gs = [request() for _ in range(5)]  # 连续 5 次视图变化
    for g in gs:  # 响应乱序返回
        on_response(g, "L")
    assert rendered == [gs[-1]], "只有最新视图的结果被渲染"
