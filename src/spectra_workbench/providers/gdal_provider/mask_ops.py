"""P6-05 掩膜生成：按波段阈值生成二值掩膜。

语义
----
- 二值掩膜输出 **Byte 1 波段**：``1`` 表示掩膜为真，``0`` 表示掩膜为假；
  **不设置 NoData**——0/1 之间没有无效区歧义，避免把 0 算成 NoData；
- 有效源像元 = 有限值 且 不等于源 NoData；
- 阈值为**闭区间**：``low <= value <= high``；只给 low/high 时为单边比较；
  两个阈值都不给时，掩膜为「全部有效源像元」；
- ``maskInvert=true`` 对掩膜结果取反；
- sidecar ``<成果名>.mask.json`` 记录波段、阈值、取反、```n_true``/``n_false``、
  源 NoData 与显式语义，供后续能力（如 FVC 的 ``maskFile``）直接消费。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["MaskError", "compute_mask", "run_mask"]


class MaskError(ValueError):
    """掩膜输入或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（掩膜生成阶段）")


def compute_mask(
    values: np.ndarray,
    valid: np.ndarray,
    *,
    low: float | None,
    high: float | None,
    invert: bool,
) -> np.ndarray:
    """按闭区间阈值计算掩膜；``valid`` 是「源像元可用」掩膜。

    返回的掩膜已经与 ``valid`` 取交集；``invert`` 对最终掩膜取反。
    """
    arr = np.asarray(values, dtype=np.float64)
    keep = np.asarray(valid, dtype=bool)
    if arr.shape != keep.shape:
        raise MaskError("值与有效掩膜形状不一致")
    if low is not None and not math.isfinite(low):
        raise MaskError("maskLow 必须为有限值")
    if high is not None and not math.isfinite(high):
        raise MaskError("maskHigh 必须为有限值")
    if low is not None and high is not None and low > high:
        raise MaskError(f"maskLow 必须小于等于 maskHigh，实际 low={low}, high={high}")
    mask = keep & (arr >= low) if low is not None else keep.copy()
    if high is not None:
        mask &= arr <= high
    if invert:
        # 反转只在有效源像元内进行；源无效像元永远为假
        return valid & ~mask
    return mask


def _read_band(ds: Any, band_index: int, src_nodata: float | None) -> tuple[np.ndarray, np.ndarray]:
    gdal = _gdal()
    if band_index < 1 or band_index > int(ds.RasterCount):
        raise MaskError(f"maskBand={band_index} 超出范围（数据集共 {ds.RasterCount} 个波段）")
    band = ds.GetRasterBand(band_index)
    if gdal.DataTypeIsComplex(band.DataType):
        raise MaskError(f"掩膜生成仅支持实数波段，波段 {band_index} 是复数类型")
    raw_obj = band.ReadAsArray()
    if raw_obj is None:
        raise MaskError(f"无法读取波段 {band_index}")
    raw = np.asarray(raw_obj, dtype=np.float64)
    label = band.GetNoDataValue()
    nodata = (
        float(src_nodata)
        if src_nodata is not None
        else (float(label) if label is not None else None)
    )
    valid = np.isfinite(raw)
    if nodata is not None:
        valid &= raw != nodata
    return raw, valid


def run_mask(
    out_path: Path,
    source: Path,
    *,
    band: int = 1,
    low: float | None = None,
    high: float | None = None,
    invert: bool = False,
    src_nodata: float | None = None,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """写入二值 Byte 掩膜并返回可审计元信息。"""
    gdal = _gdal()
    _progress(callback, 0.05, "打开输入栅格")
    if not source.exists():
        raise MaskError(f"输入栅格不存在: {source}")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise MaskError(f"无法打开输入栅格: {source}")
    try:
        width, height = int(ds.RasterXSize), int(ds.RasterYSize)
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""
        raw, valid = _read_band(ds, int(band), src_nodata)
    finally:
        ds = None

    _progress(callback, 0.55, "计算掩膜")
    final = compute_mask(raw, valid, low=low, high=high, invert=invert)
    out_arr = final.astype(np.uint8)

    _progress(callback, 0.80, f"写出成果（{out_format} / Byte）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise MaskError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), width, height, 1, gdal.GDT_Byte)
    if out_ds is None:
        raise MaskError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(proj)
        out_ds.SetMetadataItem("MASK_SOURCE", str(source))
        out_ds.SetMetadataItem("MASK_BAND", str(band))
        if low is not None:
            out_ds.SetMetadataItem("MASK_LOW", f"{low:.12g}")
        if high is not None:
            out_ds.SetMetadataItem("MASK_HIGH", f"{high:.12g}")
        out_ds.SetMetadataItem("MASK_INVERT", "true" if invert else "false")
        out_ds.SetMetadataItem("MASK_TRUE", "1")
        out_ds.SetMetadataItem("MASK_FALSE", "0")
        band_out = out_ds.GetRasterBand(1)
        band_out.WriteArray(out_arr)
        band_out.SetDescription("mask")
        band_out.FlushCache()
    finally:
        out_ds = None

    payload = {
        "type": "spectra_mask",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P6-05",
        "source": str(source),
        "band": int(band),
        "low": low,
        "high": high,
        "invert": bool(invert),
        "src_nodata": src_nodata,
        "semantics": "1=mask true, 0=mask false; no nodata set",
        "n_true": int(final.sum()),
        "n_false": int((~final).sum()),
    }
    sidecar = out_path.with_name(out_path.name + ".mask.json")
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    _progress(callback, 1.0, "完成")
    return {
        "band": int(band),
        "n_true": int(final.sum()),
        "n_false": int((~final).sum()),
        "sidecar": str(sidecar),
    }
