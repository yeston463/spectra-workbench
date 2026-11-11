"""P6-04 影像融合：Brovey 与 HSV（只实现并命名已落实的方法）。

方法定义
--------
- **Brovey**（比值融合）：``out_i = MS_i * Pan_scaled / sum(MS_1..3)``；
  Pan 先按 dtype 量程缩放到 MS 的量程；分母 ≤ 0 的像元输出 NoData。
  适用于 3 波段多光谱 + 单波段全色，且两景同 CRS、全色为 north-up 网格。
- **HSV**（强度替换）：把 MS 的 3 波段归一化到 [0,1] → RGB→HSV，
  用归一化 Pan 替换 V（H/S 保留）→ HSV→RGB → 乘回 MS 量程。
  要求 MS 为 RGB 近似反射率（Float32 假设 [0,1]；Byte 按 255 归一化）。

明确不提供 NNDiffuse / Gram-Schmidt / PCA 融合入口；未落实依赖与方法前不冒充
可执行能力。sidecar ``<成果名>.pansharpen.json`` 记录方法、公式、波段映射、
量程、重采样与适用范围。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "FUSION_METHODS",
    "FusionError",
    "brovey_fusion",
    "hsv_fusion",
    "run_pansharpen",
]

FUSION_METHODS: tuple[str, ...] = ("brovey", "hsv")
_FUSION_RESAMPLE = ("nearest", "bilinear")


class FusionError(ValueError):
    """融合输入或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（融合阶段）")


def _dtype_scale(gdal: Any, data_type: int) -> float:
    """按数据类型给出 [0,1] 归一化量程；Float 假设输入已是 0..1 反射率。"""
    if data_type == gdal.GDT_Byte:
        return 255.0
    if data_type == gdal.GDT_UInt16:
        return 65535.0
    if data_type == gdal.GDT_Int16:
        return 32767.0
    if data_type == gdal.GDT_UInt32:
        return 4294967295.0
    if data_type == gdal.GDT_Int32:
        return 2147483647.0
    return 1.0


def _read_band(ds: Any, index: int, src_nodata: float | None) -> tuple[np.ndarray, np.ndarray, int]:
    gdal = _gdal()
    band = ds.GetRasterBand(index)
    if gdal.DataTypeIsComplex(band.DataType):
        raise FusionError(f"融合仅支持实数波段，波段 {index} 是复数类型")
    raw_obj = band.ReadAsArray()
    if raw_obj is None:
        raise FusionError(f"无法读取波段 {index}")
    raw = np.asarray(raw_obj, dtype=np.float64)
    label = band.GetNoDataValue()
    nodata = (
        float(src_nodata)
        if src_nodata is not None
        else (float(label) if label is not None else None)
    )
    invalid = ~np.isfinite(raw)
    if nodata is not None:
        invalid |= raw == nodata
    return raw, invalid, int(band.DataType)


def brovey_fusion(
    ms: np.ndarray, pan: np.ndarray, *, ms_scale: float, pan_scale: float
) -> tuple[np.ndarray, np.ndarray]:
    """Brovey 比值融合；返回 (3, H, W) 与有效掩膜。"""
    arr = np.asarray(ms, dtype=np.float64)
    pan_arr = np.asarray(pan, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[0] != 3:
        raise FusionError(f"Brovey 需要 3 个 MS 波段，实际形状 {arr.shape}")
    total = arr[0] + arr[1] + arr[2]
    valid = np.isfinite(total) & (total > 0) & np.isfinite(pan_arr)
    scaled_pan = pan_arr * (ms_scale / pan_scale) if pan_scale else pan_arr
    out = np.zeros_like(arr)
    if valid.any():
        ratio = scaled_pan[valid] / total[valid]
        for idx in range(3):
            out[idx][valid] = arr[idx][valid] * ratio
    return out, valid


def _rgb_to_hsv(
    r: np.ndarray, g: np.ndarray, b: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    delta = maxc - minc
    h = np.zeros_like(maxc)
    nonzero = delta > 0
    rmask = nonzero & (maxc == r)
    gmask = nonzero & (maxc == g) & ~rmask
    bmask = nonzero & ~rmask & ~gmask
    h[rmask] = ((g[rmask] - b[rmask]) / delta[rmask]) % 6.0
    h[gmask] = (b[gmask] - r[gmask]) / delta[gmask] + 2.0
    h[bmask] = (r[bmask] - g[bmask]) / delta[bmask] + 4.0
    h = h / 6.0
    s = np.zeros_like(maxc)
    smask = maxc > 0
    s[smask] = delta[smask] / maxc[smask]
    return h, s, maxc


def _hsv_to_rgb(
    h: np.ndarray, s: np.ndarray, v: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    h6 = (h % 1.0) * 6.0
    i = np.floor(h6).astype(np.int64) % 6
    f = h6 - np.floor(h6)
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    r = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [v, q, p, p, t, v])
    g = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [t, v, v, q, p, p])
    b = np.select([i == 0, i == 1, i == 2, i == 3, i == 4, i == 5], [p, p, t, v, v, q])
    return r, g, b


def hsv_fusion(
    ms: np.ndarray,
    pan: np.ndarray,
    *,
    ms_scale: float,
    pan_scale: float,
) -> tuple[np.ndarray, np.ndarray]:
    """HSV 强度替换融合；返回 (3, H, W) 与有效掩膜。"""
    arr = np.asarray(ms, dtype=np.float64)
    pan_arr = np.asarray(pan, dtype=np.float64)
    if arr.ndim != 3 or arr.shape[0] != 3:
        raise FusionError(f"HSV 需要 3 个 MS 波段，实际形状 {arr.shape}")
    normalized = arr / (ms_scale if ms_scale else 1.0)
    pan_norm = pan_arr / (pan_scale if pan_scale else 1.0)
    valid = np.all(np.isfinite(normalized), axis=0) & np.isfinite(pan_norm)
    h, s, _v = _rgb_to_hsv(normalized[0], normalized[1], normalized[2])
    v_new = np.clip(pan_norm, 0.0, 1.0)
    r, g, b = _hsv_to_rgb(h, s, v_new)
    out = np.stack([r, g, b]) * ms_scale
    return out, valid


def _pan_bounds(gt: tuple[float, ...], width: int, height: int) -> tuple[float, ...]:
    gdal = _gdal()
    corners = [
        gdal.ApplyGeoTransform(gt, 0.0, 0.0),
        gdal.ApplyGeoTransform(gt, float(width), 0.0),
        gdal.ApplyGeoTransform(gt, 0.0, float(height)),
        gdal.ApplyGeoTransform(gt, float(width), float(height)),
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _resample_ms_to_pan(
    ms_path: Path,
    pan_w: int,
    pan_h: int,
    pan_gt: tuple[float, ...],
    pan_srs: Any,
    *,
    resample_alg: str,
    src_nodata: float | None,
) -> Any:
    gdal = _gdal()
    if pan_gt[2] != 0.0 or pan_gt[4] != 0.0:
        raise FusionError("全色影像必须是 north-up 网格（当前 GeoTransform 含旋转/错切）")
    bounds = _pan_bounds(pan_gt, pan_w, pan_h)
    return gdal.Warp(
        "",
        str(ms_path),
        options=gdal.WarpOptions(
            format="VRT",
            outputBounds=bounds,
            width=pan_w,
            height=pan_h,
            dstSRS=pan_srs.ExportToWkt() if pan_srs is not None else None,
            resampleAlg=resample_alg,
            srcNodata=(float(src_nodata) if src_nodata is not None else None),
        ),
    )


def run_pansharpen(
    out_path: Path,
    pan_path: Path,
    ms_path: Path,
    *,
    method: str = "brovey",
    ms_bands: list[int],
    resample_alg: str = "bilinear",
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行 Brovey/HSV 融合，输出 3 波段 Float32 + 来源/公式 sidecar。"""
    gdal = _gdal()
    method_name = str(method or "brovey").strip().lower()
    if method_name not in FUSION_METHODS:
        raise FusionError(f"未知融合方法 {method!r}；可用：{', '.join(FUSION_METHODS)}")
    alg = str(resample_alg or "bilinear").strip().lower()
    if alg not in _FUSION_RESAMPLE:
        raise FusionError(f"不支持的融合重采样算法 {resample_alg!r}")
    if not math.isfinite(dst_nodata):
        raise FusionError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    if len(ms_bands) != 3:
        raise FusionError(f"msBands 必须恰好 3 个波段号，实际 {ms_bands}")
    if len(set(ms_bands)) != 3:
        raise FusionError(f"msBands 的 3 个波段号必须互不相同，实际 {ms_bands}")
    if not pan_path.exists():
        raise FusionError(f"全色影像不存在: {pan_path}")
    if not ms_path.exists():
        raise FusionError(f"多光谱影像不存在: {ms_path}")

    _progress(callback, 0.05, "打开全色影像")
    pan_ds = gdal.Open(str(pan_path), gdal.GA_ReadOnly)
    if pan_ds is None:
        raise FusionError(f"无法打开全色影像: {pan_path}")
    ms_ds = gdal.Open(str(ms_path), gdal.GA_ReadOnly)
    if ms_ds is None:
        pan_ds = None
        raise FusionError(f"无法打开多光谱影像: {ms_path}")
    try:
        pan_w, pan_h = int(pan_ds.RasterXSize), int(pan_ds.RasterYSize)
        if int(pan_ds.RasterCount) != 1:
            raise FusionError(f"全色影像必须恰好 1 个波段，实际 {pan_ds.RasterCount} 个")
        pan_gt = tuple(float(v) for v in (pan_ds.GetGeoTransform() or ()))
        pan_srs = pan_ds.GetSpatialRef()
        ms_srs = ms_ds.GetSpatialRef()
        if pan_srs is None or ms_srs is None:
            raise FusionError("全色与多光谱影像都必须有 CRS")
        if not pan_srs.IsSame(ms_srs):
            raise FusionError("全色与多光谱影像 CRS 不一致；融合不做隐式投影")
        for band in ms_bands:
            if band < 1 or band > int(ms_ds.RasterCount):
                raise FusionError(
                    f"msBands 中的波段 {band} 超出范围（多光谱共 {ms_ds.RasterCount} 个波段）"
                )
        pan_arr, pan_invalid, pan_dtype = _read_band(pan_ds, 1, src_nodata)
        pan_scale = _dtype_scale(gdal, pan_dtype)
    finally:
        pan_ds = None

    _progress(callback, 0.25, f"重采样多光谱到全色网格（{alg}）")
    ms_resampled = _resample_ms_to_pan(
        ms_path,
        pan_w,
        pan_h,
        pan_gt,
        pan_srs,
        resample_alg=alg,
        src_nodata=src_nodata,
    )
    if ms_resampled is None:
        raise FusionError("多光谱重采样失败")
    ms_arrays: list[np.ndarray] = []
    ms_invalid = np.zeros((pan_h, pan_w), dtype=bool)
    ms_scale = 1.0
    try:
        for band in ms_bands:
            arr, invalid, dtype = _read_band(ms_resampled, band, src_nodata)
            if arr.shape != (pan_h, pan_w):
                raise FusionError(f"重采样后波段 {band} 尺寸异常: {arr.shape}")
            ms_arrays.append(arr)
            ms_invalid |= invalid
            ms_scale = max(ms_scale, _dtype_scale(gdal, dtype))
    finally:
        ms_resampled = None

    _progress(callback, 0.55, f"执行 {method_name} 融合")
    ms_stack = np.stack(ms_arrays)
    if method_name == "brovey":
        fused, valid = brovey_fusion(ms_stack, pan_arr, ms_scale=ms_scale, pan_scale=pan_scale)
        formula = "out_i = MS_i * Pan_scaled / sum(MS_1..3)"
    else:
        fused, valid = hsv_fusion(ms_stack, pan_arr, ms_scale=ms_scale, pan_scale=pan_scale)
        formula = "RGB->HSV; V := Pan_normalized; HSV->RGB"
    valid &= ~pan_invalid & ~ms_invalid

    _progress(callback, 0.80, f"写出成果（{out_format} / Float32，3 波段）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise FusionError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), pan_w, pan_h, 3, gdal.GDT_Float32)
    if out_ds is None:
        raise FusionError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(pan_gt)
        out_ds.SetProjection(pan_srs.ExportToWkt() if pan_srs is not None else "")
        out_ds.SetMetadataItem("FUSION_METHOD", method_name)
        out_ds.SetMetadataItem("FUSION_FORMULA", formula)
        out_ds.SetMetadataItem("FUSION_MS_BANDS", ",".join(str(b) for b in ms_bands))
        out_ds.SetMetadataItem("FUSION_RESAMPLE", alg)
        for idx, name in enumerate(("R", "G", "B")):
            band_out = out_ds.GetRasterBand(idx + 1)
            arr = np.where(valid, fused[idx], dst_nodata).astype(np.float32)
            band_out.WriteArray(arr)
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.SetDescription(f"{method_name}_{name}")
            band_out.FlushCache()
    finally:
        out_ds = None

    payload = {
        "type": "spectra_pansharpen",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P6-04",
        "method": method_name,
        "formula": formula,
        "pan_source": str(pan_path),
        "ms_source": str(ms_path),
        "ms_bands": [int(v) for v in ms_bands],
        "output_bands": [
            f"{method_name}_R",
            f"{method_name}_G",
            f"{method_name}_B",
        ],
        "resample": alg,
        "pan_scale": pan_scale,
        "ms_scale": ms_scale,
        "output_grid": {
            "width": pan_w,
            "height": pan_h,
            "geotransform": [float(v) for v in pan_gt],
        },
        "n_valid": int(valid.sum()),
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
        "applicability": (
            "3 波段 MS + 单波段 Pan，同 CRS，Pan 为 north-up 网格；"
            "HSV 假设 Float32 MS/Pan 近似 [0,1] 反射率或 Byte 量程；"
            "NNDiffuse/Gram-Schmidt/PCA 融合未实现，不提供入口"
        ),
    }
    sidecar = out_path.with_name(out_path.name + ".pansharpen.json")
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    _progress(callback, 1.0, "完成")
    return {
        "method": method_name,
        "formula": formula,
        "ms_bands": [int(v) for v in ms_bands],
        "n_valid": int(valid.sum()),
        "sidecar": str(sidecar),
    }
