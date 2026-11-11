"""P6-04 栅格差值：严格同网格比较 + 逐波段统计。

只做**明确语义**的差值，不隐式重采样：
- ``difference``：A − B（带符号）；
- ``absolute``：|A − B|；
- 两景必须同宽高、同波段数、同 GeoTransform、同 CRS；任一不一致直接拒绝；
- NoData/非有限值逐波段排除，统计报告逐波段给出有效像元数、min/max/mean/RMSE；
- 输出 Float32 + NoData，sidecar ``<成果名>.rasterdiff.json`` 记录输入、模式、网格与统计。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["DiffError", "DIFF_MODES", "compute_difference", "run_raster_diff"]

DIFF_MODES: tuple[str, ...] = ("difference", "absolute")


class DiffError(ValueError):
    """差值输入或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（差值计算阶段）")


def compute_difference(a: np.ndarray, b: np.ndarray, mode: str) -> np.ndarray:
    """纯差值：difference=A−B，absolute=|A−B|。"""
    mode_name = str(mode or "difference").strip().lower()
    if mode_name not in DIFF_MODES:
        raise DiffError(f"未知差值模式 {mode!r}；可用：{', '.join(DIFF_MODES)}")
    left = np.asarray(a, dtype=np.float64)
    right = np.asarray(b, dtype=np.float64)
    if left.shape != right.shape:
        raise DiffError(f"差值输入形状不一致：{left.shape} vs {right.shape}")
    return np.abs(left - right) if mode_name == "absolute" else left - right


def _projection_equal(a: str, b: str) -> bool:
    if not a and not b:
        return True
    if not a or not b:
        return False
    return a.strip() == b.strip()


def _read_band(ds: Any, index: int, src_nodata: float | None) -> tuple[np.ndarray, np.ndarray]:
    gdal = _gdal()
    band = ds.GetRasterBand(index)
    if gdal.DataTypeIsComplex(band.DataType):
        raise DiffError(f"差值计算仅支持实数波段，波段 {index} 是复数类型")
    raw_obj = band.ReadAsArray()
    if raw_obj is None:
        raise DiffError(f"无法读取波段 {index}")
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
    return raw, invalid


def run_raster_diff(
    out_path: Path,
    source: Path,
    reference: Path,
    *,
    mode: str = "difference",
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """计算 source 与 reference 的逐波段差值并写统计 sidecar。"""
    gdal = _gdal()
    mode_name = str(mode or "difference").strip().lower()
    if mode_name not in DIFF_MODES:
        raise DiffError(f"未知差值模式 {mode!r}；可用：{', '.join(DIFF_MODES)}")
    if not math.isfinite(dst_nodata):
        raise DiffError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    if not source.exists():
        raise DiffError(f"输入影像不存在: {source}")
    if not reference.exists():
        raise DiffError(f"参考影像不存在: {reference}")

    _progress(callback, 0.05, "打开输入影像")
    src_ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if src_ds is None:
        raise DiffError(f"无法打开输入影像: {source}")
    ref_ds = gdal.Open(str(reference), gdal.GA_ReadOnly)
    if ref_ds is None:
        src_ds = None
        raise DiffError(f"无法打开参考影像: {reference}")
    try:
        src_w, src_h = int(src_ds.RasterXSize), int(src_ds.RasterYSize)
        ref_w, ref_h = int(ref_ds.RasterXSize), int(ref_ds.RasterYSize)
        if (src_w, src_h) != (ref_w, ref_h):
            raise DiffError(
                f"两景尺寸不一致：输入 {src_w}×{src_h}，参考 {ref_w}×{ref_h}；差值不做隐式重采样"
            )
        src_bands = int(src_ds.RasterCount)
        ref_bands = int(ref_ds.RasterCount)
        if src_bands != ref_bands:
            raise DiffError(f"两景波段数不一致：输入 {src_bands}，参考 {ref_bands}")
        src_gt = tuple(float(v) for v in (src_ds.GetGeoTransform() or ()))
        ref_gt = tuple(float(v) for v in (ref_ds.GetGeoTransform() or ()))
        if src_gt != ref_gt:
            raise DiffError(f"两景 GeoTransform 不一致：{src_gt} vs {ref_gt}")
        src_proj = src_ds.GetProjectionRef() or ""
        ref_proj = ref_ds.GetProjectionRef() or ""
        if not _projection_equal(src_proj, ref_proj):
            raise DiffError("两景 CRS 不一致；差值不做隐式投影")
        src_proj_for_output = src_proj

        arrays: list[np.ndarray] = []
        stats: list[dict[str, Any]] = []
        _progress(callback, 0.25, f"读取 {src_bands} 个波段")
        for index in range(1, src_bands + 1):
            a, invalid_a = _read_band(src_ds, index, src_nodata)
            b, invalid_b = _read_band(ref_ds, index, src_nodata)
            invalid = invalid_a | invalid_b
            diff = compute_difference(a, b, mode_name)
            values = diff[~invalid]
            if values.size:
                stats.append(
                    {
                        "band": index,
                        "mode": mode_name,
                        "n_valid": int(values.size),
                        "n_total": int(diff.size),
                        "min": float(values.min()),
                        "max": float(values.max()),
                        "mean": float(values.mean()),
                        "rmse": float(np.sqrt(np.mean(values * values))),
                        "mean_abs": float(np.mean(np.abs(values))),
                    }
                )
            else:
                stats.append(
                    {
                        "band": index,
                        "mode": mode_name,
                        "n_valid": 0,
                        "n_total": int(diff.size),
                        "min": None,
                        "max": None,
                        "mean": None,
                        "rmse": None,
                        "mean_abs": None,
                    }
                )
            arrays.append(np.where(invalid, dst_nodata, diff))
    finally:
        src_ds = None
        ref_ds = None

    _progress(callback, 0.75, f"写出成果（{out_format} / Float32，{src_bands} 波段）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise DiffError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), src_w, src_h, src_bands, gdal.GDT_Float32)
    if out_ds is None:
        raise DiffError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(src_gt)
        out_ds.SetProjection(src_proj_for_output)
        out_ds.SetMetadataItem("DIFF_MODE", mode_name)
        out_ds.SetMetadataItem("DIFF_SOURCE", str(source))
        out_ds.SetMetadataItem("DIFF_REFERENCE", str(reference))
        for index, values in enumerate(arrays, start=1):
            band_out = out_ds.GetRasterBand(index)
            band_out.WriteArray(values.astype(np.float32))
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.SetDescription(f"diff_B{index}")
            band_out.FlushCache()
    finally:
        out_ds = None

    payload = {
        "type": "spectra_raster_diff",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P6-04",
        "source": str(source),
        "reference": str(reference),
        "mode": mode_name,
        "grid": {
            "width": src_w,
            "height": src_h,
            "bands": src_bands,
            "geotransform": [float(v) for v in src_gt],
        },
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
        "stats": stats,
    }
    sidecar = out_path.with_name(out_path.name + ".rasterdiff.json")
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    _progress(callback, 1.0, "完成")
    return {
        "mode": mode_name,
        "bands": src_bands,
        "stats": stats,
        "sidecar": str(sidecar),
    }
