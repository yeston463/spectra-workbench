"""P4-04：直方图与基础统计（抽样 / 全量两种模式，只读、无源数据副作用）。

原验收标准：「抽样/全量统计模式有证据」。

**模式语义（不静默降级）**：
- ``sampled``：按步长抽取规则网格，明确标注 ``source=sampled_grid``
  与 ``stride``，报告里的 ``sampled_is_estimate=True`` 表明数值是**估计**；
- ``full``：读全幅像元，``source=full_raster``；若像元数超过 ``full_limit``
  **明确拒绝**，而不是偷偷改用抽样（沿用 P3 统计协议既有行为）。

**GDAL 原生对照（实测依据 work/_probe_p404.py）**：
1024x1024 随机图上 ``ComputeStatistics(True)`` 的 mean≈49.829，
``ComputeStatistics(False)`` ≈50.006，两者确实不同；``GetHistogram``
在本版本只接受**位置参数**（关键字形式不兼容）。本模块把两份原生值
一并写进报告作为「抽样 ≠ 精确」的证据。

**副作用防线**：``ComputeStatistics`` / ``GetHistogram`` 默认会向源文件写
``.aux.xml``（PAM）统计元数据。PAM 状态在 ``Open()`` 时即固定，因此必须在
**打开数据集之前**设置 ``GDAL_PAM_ENABLED=NO``，并在 dataset 销毁后恢复全局配置；
只在调用统计函数前临时禁用是无效的（第 35 轮 GUI 验收实测复现：先 Open 再设
NO，仍生成含 Histograms / STATISTICS_* 的 sidecar）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from spectra_workbench.domain.paths import is_vsi_or_remote, normalize_vsi_path

__all__ = ["compute_statistics", "write_statistics_report", "STATISTICS_MODES"]

STATISTICS_MODES: tuple[str, ...] = ("sampled", "full")

#: 超过该像元数就不做 GDAL 原生精确统计对照（全量扫描代价高），
#: 报告里会明确写「已跳过」，而不是悄悄省略证据。
MAX_REFERENCE_PIXELS = 20_000_000


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _finite_or_none(value: Any) -> float | None:
    f = float(value)
    return f if math.isfinite(f) else None


def _gdal_reference(band: Any, lo: float, hi: float, bins: int) -> dict[str, Any]:
    """采集 GDAL 原生近似/精确统计与直方图作对照；失败不阻断主流程。"""
    gdal = _gdal()
    ref: dict[str, Any] = {"pam_disabled": True}
    prev_pam = gdal.GetConfigOption("GDAL_PAM_ENABLED")
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    try:
        try:
            ref["approx_stats"] = [_finite_or_none(v) for v in band.ComputeStatistics(True)]
        except Exception as exc:  # pragma: no cover - 驱动差异保护
            ref["approx_error"] = str(exc)
        try:
            ref["exact_stats"] = [_finite_or_none(v) for v in band.ComputeStatistics(False)]
        except Exception as exc:  # pragma: no cover - 驱动差异保护
            ref["exact_error"] = str(exc)
        # GDAL 3.13.3 实测：GetHistogram 只接受位置参数 (min, max, buckets, include_out_of_range, approx_ok)
        try:
            if math.isfinite(lo) and math.isfinite(hi) and hi > lo:
                hist = band.GetHistogram(float(lo), float(hi), int(bins), 0, 0)
                ref["native_histogram_sum"] = int(sum(int(x) for x in hist))
                ref["native_histogram_bins"] = int(bins)
            else:
                ref["native_histogram_skipped"] = "有效值范围退化（min>=max 或无有效像元）"
        except Exception as exc:  # pragma: no cover - 驱动差异保护
            ref["native_histogram_error"] = str(exc)
    finally:
        gdal.SetConfigOption("GDAL_PAM_ENABLED", prev_pam)
    return ref


def compute_statistics(
    path: Path | str,
    *,
    mode: str = "sampled",
    bins: int = 256,
    max_samples: int = 1_000_000,
    full_limit: int = 50_000_000,
    band_list: list[int] | None = None,
    nodata: float | None = None,
    include_gdal_reference: bool = True,
    mask_path: Path | str | None = None,
    mask_band: int = 1,
    mask_value: float | None = None,
    mask_min: float | None = None,
    mask_max: float | None = None,
    mask_invert: bool = False,
) -> dict[str, Any]:
    """读取栅格并生成含直方图的统计报告（可 JSON 序列化）。

    支持 maskFile / maskBand / maskValue 掩膜分地类统计：仅对符合掩膜条件的有效像元计算统计。
    ``band_list`` 为 1-based 波段号；``nodata`` 显式给定时覆盖波段自带标签。
    无效像元（NoData / NaN / Inf）一律排除出全部统计与直方图。
    """
    if mode not in STATISTICS_MODES:
        raise ValueError(f"未知统计模式 {mode!r}；可用：{', '.join(STATISTICS_MODES)}")
    if not isinstance(bins, int) or isinstance(bins, bool) or bins < 2 or bins > 4096:
        raise ValueError(f"histogramBins 必须是 2..4096 的整数，实际 {bins!r}")
    if max_samples < 1 or full_limit < 1:
        raise ValueError("maxSamples / fullLimit 必须为正整数")

    gdal = _gdal()
    target_path = normalize_vsi_path(path) if is_vsi_or_remote(path) else str(Path(path))
    # PAM 状态在 Open() 时即固定，必须在打开**之前**禁用：
    # 否则 ComputeStatistics/GetHistogram 会把统计元数据写进 .aux.xml，
    # 让「只读统计」悄悄改动用户数据（第 35 轮 GUI 验收实测复现）。
    prev_pam = gdal.GetConfigOption("GDAL_PAM_ENABLED")
    gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    ds = None
    mask_ds = None
    try:
        ds = gdal.Open(target_path, gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {target_path}")
        w, h, count = int(ds.RasterXSize), int(ds.RasterYSize), int(ds.RasterCount)
        total = w * h
        if mode == "full" and total > full_limit:
            raise ValueError(
                f"全量统计被拒绝：{total} 像素超过上限 {full_limit}；请改用 sampled 或分块统计"
            )
        stride = 1
        if mode == "sampled":
            stride = max(1, int(math.ceil(math.sqrt(total / max(1, max_samples)))))
        target = band_list or list(range(1, count + 1))
        read_w = max(1, (w + stride - 1) // stride)
        read_h = max(1, (h + stride - 1) // stride)

        if mask_path is not None:
            m_target = (
                normalize_vsi_path(mask_path)
                if is_vsi_or_remote(mask_path)
                else str(Path(mask_path))
            )
            mask_ds = gdal.Open(m_target, gdal.GA_ReadOnly)
            if mask_ds is None:
                raise RuntimeError(f"无法打开掩膜数据集: {m_target}")
            if mask_band < 1 or mask_band > int(mask_ds.RasterCount):
                raise ValueError(f"掩膜波段 {mask_band} 超出范围 (共 {mask_ds.RasterCount} 个波段)")

        out_bands: list[dict[str, Any]] = []
        for b_idx in target:
            if b_idx < 1 or b_idx > count:
                raise ValueError(f"波段号 {b_idx} 超出范围 1..{count}")
            band = ds.GetRasterBand(b_idx)
            arr = band.ReadAsArray(0, 0, w, h, buf_xsize=read_w, buf_ysize=read_h)
            if arr is None:
                raise RuntimeError(f"无法读取波段 {b_idx}: {target_path}")
            data = np.asarray(arr).astype(np.float64)
            nd = (
                float(nodata)
                if nodata is not None
                else (float(band.GetNoDataValue()) if band.GetNoDataValue() is not None else None)
            )
            valid = np.isfinite(data)
            if nd is not None:
                valid &= data != nd

            mask_stat_info: dict[str, Any] | None = None
            if mask_ds is not None:
                mb = mask_ds.GetRasterBand(mask_band)
                m_arr = mb.ReadAsArray(
                    0,
                    0,
                    int(mask_ds.RasterXSize),
                    int(mask_ds.RasterYSize),
                    buf_xsize=read_w,
                    buf_ysize=read_h,
                )
                if m_arr is None:
                    raise RuntimeError(f"读取掩膜波段 {mask_band} 失败")
                m_data = np.asarray(m_arr, dtype=np.float64)
                m_nd = mb.GetNoDataValue()
                m_valid = np.isfinite(m_data)
                if m_nd is not None:
                    m_valid &= m_data != float(m_nd)

                if mask_value is not None:
                    m_active = m_valid & (m_data == float(mask_value))
                elif mask_min is not None and mask_max is not None:
                    m_active = m_valid & (m_data >= float(mask_min)) & (m_data <= float(mask_max))
                else:
                    m_active = m_valid & (m_data != 0)

                if mask_invert:
                    m_active = ~m_active

                valid &= m_active
                mask_stat_info = {
                    "enabled": True,
                    "path": str(mask_path),
                    "band": int(mask_band),
                    "value": mask_value,
                    "matched_pixels": int(m_active.sum()),
                }

            vals = data[valid]
            entry: dict[str, Any] = {
                "band": int(b_idx),
                "valid_count": int(vals.size),
                "total_count": int(data.size),
                "nodata": nd,
                "mask": mask_stat_info,
                "min": float(vals.min()) if vals.size else None,
                "max": float(vals.max()) if vals.size else None,
                "mean": float(vals.mean()) if vals.size else None,
                "std": float(vals.std()) if vals.size else None,
                "p2": float(np.percentile(vals, 2)) if vals.size else None,
                "p98": float(np.percentile(vals, 98)) if vals.size else None,
                "histogram": {"bins": int(bins), "counts": [], "edges": []},
            }
            if vals.size:
                lo, hi = float(vals.min()), float(vals.max())
                counts, edges = np.histogram(vals, bins=bins, range=(lo, hi))
                entry["histogram"] = {
                    "bins": int(bins),
                    "range": [lo, hi],
                    "counts": [int(x) for x in counts],
                    "edges": [float(x) for x in edges],
                }
            if mask_ds is not None:
                entry["gdal_reference"] = {
                    "skipped": "按掩膜分地类统计模式下 GDAL 原生统计不适用（仅统计掩膜有效区）"
                }
            elif include_gdal_reference and total <= MAX_REFERENCE_PIXELS:
                entry["gdal_reference"] = _gdal_reference(
                    band,
                    float(entry["min"]) if entry["min"] is not None else float("nan"),
                    float(entry["max"]) if entry["max"] is not None else float("nan"),
                    bins,
                )
            else:
                entry["gdal_reference"] = {
                    "skipped": (
                        "调用方关闭原生对照"
                        if not include_gdal_reference
                        else f"{total} 像素超过对照上限 {MAX_REFERENCE_PIXELS}"
                    )
                }
            out_bands.append(entry)

        return {
            "path": str(target_path),
            "mode": mode,
            "stride": stride,
            "source": "full_raster" if (mode == "full" and stride == 1) else "sampled_grid",
            "sampled_is_estimate": bool(mode == "sampled" and stride > 1),
            "width": w,
            "height": h,
            "pixel_count": total,
            "read_width": read_w,
            "read_height": read_h,
            "histogram_bins": int(bins),
            "bands": out_bands,
        }
    finally:
        # 先销毁 dataset（此时 PAM 仍禁用，析构不会补写 sidecar），再恢复全局配置。
        if mask_ds is not None:
            del mask_ds
        if ds is not None:
            del ds
        gdal.SetConfigOption("GDAL_PAM_ENABLED", prev_pam)


def write_statistics_report(report: dict[str, Any], out_path: Path) -> None:
    """把统计报告写成 UTF-8 JSON（禁止 NaN/Inf，保证是合法 JSON）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
