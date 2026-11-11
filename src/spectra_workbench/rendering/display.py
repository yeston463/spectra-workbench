"""显示变换（P3-04）。

纯 numpy，不导入 Qt 与任何原生地理库，因此可脱离界面单测。

三条硬规则：
1. **原值 / 物理值 / 显示值分离**：本模块只把原值映射成 0-255 显示字节，绝不回写源数组。
2. **NoData、mask、alpha=0 与合法 0 值互不混淆**：无效像元走 alpha 通道（透明），
   合法 0 值照常参与拉伸与显示。
3. 统计来源必须可标注：拉伸界来自抽样还是全量，调用方传入并在返回值里保留。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np
from pydantic import BaseModel, ConfigDict


class StretchMode(StrEnum):
    none = "none"
    min_max = "min_max"
    percentile = "percentile"
    stddev = "stddev"


class DisplayMode(StrEnum):
    gray = "gray"
    rgb = "rgb"
    pseudo = "pseudo"


class DisplaySettings(BaseModel):
    """单个图层的显示参数（可序列化进项目文件）。

    用 pydantic 而非 dataclass：字符串会被强制成枚举，避免 str/enum 混用导致的序列化歧义。
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    mode: DisplayMode = DisplayMode.gray
    gray_band: int = 1
    rgb_bands: tuple[int, int, int] = (1, 2, 3)
    stretch: StretchMode = StretchMode.percentile
    percentiles: tuple[float, float] = (2.0, 98.0)
    stddev_k: float = 2.0
    pseudocolor: str = "viridis"
    opacity: float = 1.0

    def selected_bands(self, band_count: int) -> list[int]:
        want = list(self.rgb_bands) if self.mode is DisplayMode.rgb else [self.gray_band]
        if band_count <= 0:
            return want
        out = []
        for b in want:
            if b < 1 or b > band_count:
                raise ValueError(f"波段 {b} 越界（数据集有 {band_count} 个波段）")
            out.append(b)
        return out

    def to_dict(self) -> dict:
        return {
            "mode": str(self.mode.value),
            "gray_band": int(self.gray_band),
            "rgb_bands": [int(x) for x in self.rgb_bands],
            "stretch": str(self.stretch.value),
            "percentiles": [float(self.percentiles[0]), float(self.percentiles[1])],
            "stddev_k": float(self.stddev_k),
            "pseudocolor": str(self.pseudocolor),
            "opacity": float(self.opacity),
        }

    @classmethod
    def from_dict(cls, d: dict) -> DisplaySettings:
        if not isinstance(d, dict):
            raise ValueError("display 配置必须是对象")
        rgb = d.get("rgb_bands") or [1, 2, 3]
        pct = d.get("percentiles") or [2.0, 98.0]
        return cls(
            mode=DisplayMode(str(d.get("mode", "gray"))),
            gray_band=int(d.get("gray_band", 1)),
            rgb_bands=(int(rgb[0]), int(rgb[1]), int(rgb[2])),
            stretch=StretchMode(str(d.get("stretch", "percentile"))),
            percentiles=(float(pct[0]), float(pct[1])),
            stddev_k=float(d.get("stddev_k", 2.0)),
            pseudocolor=str(d.get("pseudocolor", "viridis")),
            opacity=float(d.get("opacity", 1.0)),
        )


# 控制点式伪彩 LUT（不依赖 matplotlib，端点线性插值到 256 级）
_COLORMAP_STOPS: dict[str, list[tuple[int, int, int]]] = {
    "gray": [(0, 0, 0), (255, 255, 255)],
    "viridis": [(68, 1, 84), (59, 82, 139), (33, 145, 140), (94, 201, 98), (253, 231, 37)],
    "terrain": [(38, 51, 112), (74, 122, 74), (204, 181, 122), (145, 91, 45), (255, 255, 255)],
    "ndvi": [(165, 0, 38), (255, 170, 0), (255, 255, 160), (130, 200, 100), (0, 128, 0)],
    "jet": [(0, 0, 160), (0, 160, 255), (0, 255, 120), (255, 220, 0), (255, 60, 0), (120, 0, 0)],
}


def available_colormaps() -> list[str]:
    return sorted(_COLORMAP_STOPS)


def colormap_lut(name: str) -> np.ndarray:
    if name not in _COLORMAP_STOPS:
        raise ValueError(f"未知伪彩 {name}，可选：{available_colormaps()}")
    stops = np.asarray(_COLORMAP_STOPS[name], dtype=np.float64)
    idx = np.linspace(0.0, 1.0, len(stops))
    pos = np.linspace(0.0, 1.0, 256)
    lut = np.empty((256, 3), dtype=np.uint8)
    for ch in range(3):
        lut[:, ch] = np.interp(pos, idx, stops[:, ch]).round().clip(0, 255).astype(np.uint8)
    return lut


@dataclass
class WindowData:
    """worker raw 响应解码后的窗口。"""

    bands: list[np.ndarray] = field(default_factory=list)
    valid: list[np.ndarray] = field(default_factory=list)
    band_meta: list[dict] = field(default_factory=list)
    # 像元窗口下是整数行列；重投影窗口下是地图坐标，因此用 float
    x_off: float = 0.0
    y_off: float = 0.0
    win_w: float = 0.0
    win_h: float = 0.0
    out_w: int = 0
    out_h: int = 0
    overview_used: int = -1
    stats_source: str = "window_sampled"

    @property
    def shape(self) -> tuple[int, int]:
        return (self.out_h, self.out_w)


def decode_window(payload: dict) -> WindowData:
    """把 read_window(raw=True) 的响应还原成数组 + 有效性掩膜。

    只读解码：不对源数据做任何写操作。
    """
    if not payload.get("raw"):
        raise ValueError("decode_window 只接受 raw=True 的窗口响应")
    dtype = np.dtype(payload["numpy_dtype"])
    nb, h, w = (int(v) for v in payload["shape"])
    flat = np.frombuffer(base64.b64decode(payload["data_b64"]), dtype=dtype)
    if flat.size != nb * h * w:
        raise ValueError(f"窗口数据尺寸不符：期望 {nb * h * w}，实得 {flat.size}")
    stacked = flat.reshape(nb, h, w)

    count = int(payload["valid_bits_count"])
    bits = np.unpackbits(np.frombuffer(base64.b64decode(payload["valid_bits_b64"]), dtype=np.uint8))
    if bits.size < count:
        raise ValueError(f"有效性位图不足：期望 {count} 位，实得 {bits.size}")
    valid = bits[:count].astype(bool).reshape(nb, h, w)

    return WindowData(
        bands=[stacked[i] for i in range(nb)],
        valid=[valid[i] for i in range(nb)],
        band_meta=list(payload.get("band_meta") or []),
        x_off=float(payload["x_off"]),
        y_off=float(payload["y_off"]),
        win_w=float(payload["win_w"]),
        win_h=float(payload["win_h"]),
        out_w=int(payload["out_w"]),
        out_h=int(payload["out_h"]),
        overview_used=int(payload.get("overview_used", -1)),
    )


def physical_values(raw: np.ndarray, meta: dict) -> np.ndarray:
    """物理值 = 原值 * scale + offset；未设置时退化为原值（不修改入参）。"""
    scale = meta.get("scale")
    offset = meta.get("offset")
    arr = raw.astype(np.float64, copy=True)
    if scale is not None:
        arr *= float(scale)
    if offset is not None:
        arr += float(offset)
    return arr


def _bounds_for(
    win: WindowData, idx: int, settings: DisplaySettings, stats: dict | None
) -> tuple[float, float]:
    arr = win.bands[idx]
    valid = win.valid[idx]
    vals = arr[valid].astype(np.float64)
    if vals.size == 0:
        return (0.0, 1.0)
    if settings.stretch is StretchMode.none:
        meta = win.band_meta[idx] if idx < len(win.band_meta) else {}
        lo = meta.get("nodata")
        return (0.0, float(np.nanmax(vals)) if lo is None else max(1.0, float(np.nanmax(vals))))
    if settings.stretch is StretchMode.min_max:
        return (float(vals.min()), float(vals.max()))
    if settings.stretch is StretchMode.stddev:
        mean = float(vals.mean())
        std = float(vals.std())
        return (mean - settings.stddev_k * std, mean + settings.stddev_k * std)
    lo_q, hi_q = settings.percentiles
    if stats and stats.get("source") and stats.get("bands"):
        # 允许用外部（抽样或全量）统计作为拉伸依据，来源在返回值中标注
        entry = stats["bands"][0]
        if entry.get("p2") is not None:
            return (float(entry["p2"]), float(entry["p98"]))
    return (float(np.percentile(vals, lo_q)), float(np.percentile(vals, hi_q)))


def stretch_to_uint8(arr: np.ndarray, valid: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """按 [lo, hi] 线性拉伸为 uint8；区间退化时输出 0，无效像元由 alpha 负责。"""
    out = np.zeros(arr.shape, dtype=np.uint8)
    if hi <= lo:
        return out
    scaled = (arr.astype(np.float64) - lo) / (hi - lo)
    np.clip(scaled, 0.0, 1.0, out=scaled)
    out[valid] = (scaled[valid] * 255.0).round().astype(np.uint8)
    return out


def compute_stretch_bounds(
    win: WindowData, settings: DisplaySettings, stats: dict | None = None
) -> list[tuple[float, float]]:
    """计算每通道拉伸区间。画布在同一视图代际内复用该区间，避免分块之间接缝变色。"""
    n = len(win.bands) if settings.mode is DisplayMode.rgb else 1
    n = max(1, min(n, len(win.bands)))
    return [_bounds_for(win, i, settings, stats) for i in range(n)]


def to_rgba(
    win: WindowData,
    settings: DisplaySettings,
    stats: dict | None = None,
    bounds_override: list[tuple[float, float]] | None = None,
) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """生成 (H,W,4) uint8 RGBA 显示缓冲。

    无效像元（NoData / mask=0 / alpha=0）透明；合法 0 值正常显示。
    返回 (rgba, 每通道拉伸区间)，区间用于状态栏标注，避免"显示值当原值"。
    """
    if not win.bands:
        raise ValueError("窗口没有波段数据")
    opacity = float(np.clip(settings.opacity, 0.0, 1.0))
    alpha_full = int(round(255 * opacity))

    if settings.mode is DisplayMode.rgb:
        if len(win.bands) < 3:
            raise ValueError(f"RGB 合成需要 3 个波段，当前只有 {len(win.bands)}")
        bounds = (
            list(bounds_override)
            if bounds_override
            else compute_stretch_bounds(win, settings, stats)
        )
        chans = []
        for i in range(3):
            lo, hi = bounds[i]
            chans.append(stretch_to_uint8(win.bands[i], win.valid[i], lo, hi))
        rgba = np.dstack([chans[0], chans[1], chans[2]]).astype(np.uint8)
        alpha = np.where(win.valid[0], alpha_full, 0).astype(np.uint8)
        return np.dstack([rgba, alpha]), bounds

    idx = 0
    if bounds_override:
        lo, hi = bounds_override[0]
    else:
        lo, hi = compute_stretch_bounds(win, settings, stats)[0]
    gray = stretch_to_uint8(win.bands[idx], win.valid[idx], lo, hi)
    if settings.mode is DisplayMode.pseudo:
        rgb = colormap_lut(settings.pseudocolor)[gray]
    else:
        rgb = np.dstack([gray, gray, gray])
    alpha = np.where(win.valid[idx], alpha_full, 0).astype(np.uint8)
    return np.dstack([rgb, alpha]).astype(np.uint8), [(lo, hi)]


def stats_source_label(stats: dict | None) -> str:
    """统计来源标注（抽样 vs 全量），界面必须显示。"""
    if not stats:
        return "窗口抽样(当前视图)"
    mode = stats.get("mode")
    src = stats.get("source")
    if mode == "full":
        return "全量统计"
    if src == "sampled_grid":
        return f"抽样统计(stride={stats.get('stride')})"
    return "窗口抽样(当前视图)"
