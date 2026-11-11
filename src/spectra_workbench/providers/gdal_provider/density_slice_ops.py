"""P11-06：密度分割（Density Slice）与按掩膜分地类统计。

数学与业务语义：
1. 密度分割将单波段连续灰度/反射率栅格按阈值阶梯划分为 K 个离散地类区间；
2. 输出带标准 GDAL 颜色表（ColorTable）的单波段 Byte/UInt16 类别栅格；
3. 输出伴随 `.density_slice.json` 侧车审计元数据，详细统计每个区间的像元数、
   像元百分比与地理覆盖面积（m²）；
4. 无效值（NoData / NaN / Inf）严格赋值为 dst_nodata（默认 0），不进入任何地类区间。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["run_density_slice", "compute_density_slice_table"]


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（密度分割阶段）")


def compute_density_slice_table(
    data: np.ndarray,
    valid: np.ndarray,
    *,
    slices: list[dict[str, Any]] | None = None,
    num_slices: int = 5,
    slice_mode: str = "equal_interval",
    gt: tuple[float, ...] | list[float] | None = None,
) -> list[dict[str, Any]]:
    """生成密度分割区间定义与各区间统计信息。"""
    valid_vals = data[valid]
    if valid_vals.size == 0:
        raise ValueError("输入栅格无有效像元，无法执行密度分割")

    v_min, v_max = float(valid_vals.min()), float(valid_vals.max())
    if v_max <= v_min:
        v_max = v_min + 1.0

    # 像元实际物理面积（若有仿射变换）
    pixel_area: float | None = None
    if gt and len(gt) >= 6:
        # dx = gt[1], dy = gt[5]
        pixel_area = abs(float(gt[1]) * float(gt[5]))

    result_slices: list[dict[str, Any]] = []

    # 预置对比鲜明的配色方案（RGB 0..255）
    preset_palette = [
        [31, 119, 180],  # 蓝
        [44, 160, 44],  # 绿
        [255, 127, 14],  # 橙
        [214, 39, 40],  # 红
        [148, 103, 189],  # 紫
        [140, 86, 75],  # 棕
        [227, 119, 194],  # 粉
        [127, 127, 127],  # 灰
        [188, 189, 34],  # 黄绿
        [23, 190, 207],  # 青
    ]

    if slices and len(slices) > 0:
        for idx, sl in enumerate(slices):
            c_id = int(sl.get("class_id", idx + 1))
            lo = float(sl.get("min", v_min))
            hi = float(sl.get("max", v_max))
            lbl = str(sl.get("label", f"类别 {c_id}: [{lo:.2f}, {hi:.2f}]"))
            color = list(sl.get("color", preset_palette[(c_id - 1) % len(preset_palette)]))
            result_slices.append(
                {
                    "class_id": c_id,
                    "label": lbl,
                    "min": lo,
                    "max": hi,
                    "color_rgb": color,
                }
            )
    else:
        k = max(2, min(int(num_slices), 254))
        if slice_mode == "quantile":
            # 分位数划分
            percentiles = np.linspace(0, 100, k + 1)
            edges = [float(np.percentile(valid_vals, p)) for p in percentiles]
        else:
            # 等间距划分 (equal_interval)
            edges = [float(x) for x in np.linspace(v_min, v_max, k + 1)]

        for i in range(k):
            lo = float(edges[i])
            hi = float(edges[i + 1])
            c_id = i + 1
            result_slices.append(
                {
                    "class_id": c_id,
                    "label": f"区间 {c_id} ({lo:.2f} ~ {hi:.2f})",
                    "min": lo,
                    "max": hi,
                    "color_rgb": preset_palette[i % len(preset_palette)],
                }
            )

    # 计算各区间的实际像元数与占比
    total_valid = int(valid_vals.size)
    for idx, sl in enumerate(result_slices):
        lo, hi = sl["min"], sl["max"]
        # 最后一个区间包含右端点
        if idx == len(result_slices) - 1:
            in_slice = (valid_vals >= lo) & (valid_vals <= hi)
        else:
            in_slice = (valid_vals >= lo) & (valid_vals < hi)
        cnt = int(in_slice.sum())
        sl["pixel_count"] = cnt
        sl["pixel_percent"] = round((cnt / total_valid) * 100.0, 4) if total_valid else 0.0
        if pixel_area is not None:
            sl["area_sq_m"] = round(cnt * pixel_area, 2)
        else:
            sl["area_sq_m"] = None

    return result_slices


def run_density_slice(
    out_path: Path,
    source: Path,
    *,
    band: int = 1,
    slices: list[dict[str, Any]] | None = None,
    num_slices: int = 5,
    slice_mode: str = "equal_interval",
    dst_nodata: int = 0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行栅格密度分割，输出类别单波段栅格并附带色表与审计 sidecar。"""
    gdal = _gdal()
    if not source.exists():
        raise FileNotFoundError(f"输入栅格不存在: {source}")

    _progress(callback, 0.05, "打开源栅格")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"无法打开输入栅格: {source}")

    try:
        if band < 1 or band > int(ds.RasterCount):
            raise ValueError(f"波段 {band} 超出范围 (共 {ds.RasterCount} 个波段)")
        w, h = int(ds.RasterXSize), int(ds.RasterYSize)
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""
        src_band = ds.GetRasterBand(band)
        arr = src_band.ReadAsArray()
        if arr is None:
            raise RuntimeError(f"读取波段 {band} 失败: {source}")
        raw = np.asarray(arr, dtype=np.float64)
        nd = src_band.GetNoDataValue()

        valid = np.isfinite(raw)
        if nd is not None:
            valid &= raw != float(nd)

        _progress(callback, 0.30, "计算密度分割区间与地类色表")
        slice_table = compute_density_slice_table(
            raw,
            valid,
            slices=slices,
            num_slices=num_slices,
            slice_mode=slice_mode,
            gt=gt,
        )

        _progress(callback, 0.50, "构建离散类别栅格像元矩阵")
        # 结果类别矩阵，初始全部为 dst_nodata
        out_dtype = np.uint8 if len(slice_table) <= 254 else np.uint16
        gdal_dtype = gdal.GDT_Byte if len(slice_table) <= 254 else gdal.GDT_UInt16
        class_arr = np.full((h, w), dst_nodata, dtype=out_dtype)

        for idx, sl in enumerate(slice_table):
            lo, hi = sl["min"], sl["max"]
            c_id = int(sl["class_id"])
            if idx == len(slice_table) - 1:
                mask = valid & (raw >= lo) & (raw <= hi)
            else:
                mask = valid & (raw >= lo) & (raw < hi)
            class_arr[mask] = c_id

        _progress(callback, 0.75, "写出成果栅格与 GDAL 颜色表")
        driver = gdal.GetDriverByName(out_format)
        if driver is None:
            raise RuntimeError(f"不可用输出驱动: {out_format}")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_ds = driver.Create(str(out_path), w, h, 1, gdal_dtype)
        if out_ds is None:
            raise RuntimeError(f"创建输出栅格失败: {out_path}")

        try:
            if gt:
                out_ds.SetGeoTransform(gt)
            if proj:
                out_ds.SetProjection(proj)
            out_band = out_ds.GetRasterBand(1)
            # 必须在 WriteArray 之前设置 ColorTable 与 ColorInterpretation，
            # 否则 GTiff 驱动会锁定 PhotometricInterpretation 标签导致运行时异常
            ct = gdal.ColorTable()
            ct.SetColorEntry(dst_nodata, (0, 0, 0, 0))
            for sl in slice_table:
                c_id = int(sl["class_id"])
                r, g, b = sl["color_rgb"][:3]
                ct.SetColorEntry(c_id, (int(r), int(g), int(b), 255))
            out_band.SetColorTable(ct)
            out_band.SetColorInterpretation(gdal.GCI_PaletteIndex)
            out_band.SetNoDataValue(float(dst_nodata))
            out_band.WriteArray(class_arr)
            out_band.FlushCache()
        finally:
            out_ds = None

        _progress(callback, 0.90, "保存密度分割审计元数据")
        sidecar_path = out_path.with_name(out_path.name + ".density_slice.json")
        audit_payload = {
            "type": "spectra_density_slice",
            "schema_version": "1.0",
            "source": str(source),
            "band": band,
            "width": w,
            "height": h,
            "slice_mode": slice_mode,
            "dst_nodata": dst_nodata,
            "total_pixels": w * h,
            "valid_pixels": int(valid.sum()),
            "slices": slice_table,
        }
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(audit_payload, f, indent=2, ensure_ascii=False)

        _progress(callback, 1.0, "密度分割完成")
        return {
            "out_path": str(out_path),
            "sidecar_path": str(sidecar_path),
            "slices": slice_table,
            "valid_pixels": int(valid.sum()),
        }
    finally:
        ds = None
