"""P6-05 FVC（植被覆盖度）：分位端元/显式端元 + 分级统计。

数学定义
--------
- 输入是 **NDVI 栅格**（可由 ``gdal:sindex`` 生成），单波段 0-based：``ndvi``；
- 有效像元 = 有限值 且 不等于源 NoData 且（可选）``maskFile`` 非零；
- **端元**：
  - ``endmemberMode=percentile``：``soil = percentile(ndvi, lowPercentile)``、
    ``veg = percentile(ndvi, highPercentile)``（默认 2%/98%）；
  - ``endmemberMode=values``：用户显式 ``fvcSoil``/``fvcVeg``，要求 ``soil < veg``；
- ``fvc = (ndvi - soil) / (veg - soil)``；
- **越界质量标记**：``fvc < 0`` 记入 ``n_below_soil``、``fvc > 1`` 记入
  ``n_above_veg``；``fvcCrop=true``（默认）将输出夹到 [0, 1]，``false`` 保留原始值
  ——裁剪是显式选项，质量标记始终在 sidecar；
- **分级**：``fvcClassEdges``（缺省 0.25/0.5/0.75）严格递增、在 (0, 1) 内；
  分级区间为 `[0, e1) / [e1, e2) / ... / [ek, 1)`，输出第 2 波段
  Byte 类别号 ``1..k+1``（无效区类别节点 = 0 显式 NoData）；
- sidecar ``<成果名>.fvc.json`` 记录端元来源/百分位值/公式/像元面积/分级统计，
  支持独立核算；源 NDVI 无效区策略显式（mask + NoData + invert 选项）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "FVCError",
    "FVC_ENDMEMBER_MODES",
    "compute_fvc",
    "run_fvc",
    "validate_class_edges",
]

FVC_ENDMEMBER_MODES: tuple[str, ...] = ("percentile", "values")


class FVCError(ValueError):
    """FVC 输入或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（FVC 阶段）")


def compute_fvc(
    ndvi: np.ndarray,
    soil: float,
    veg: float,
    *,
    mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """逐像元 FVC；返回 (fvc, mask, 计数)。

    ``mask`` 为「参与计算」的像元（源 NoData + 可选掩膜文件为真）。
    """
    ndvi_arr = np.asarray(ndvi, dtype=np.float64)
    if ndvi_arr.ndim != 2:
        raise FVCError("NDVI 必须是二维数组")
    if not math.isfinite(soil) or not math.isfinite(veg):
        raise FVCError("FVC 端元必须为有限值")
    if soil >= veg:
        raise FVCError("FVC 端元必须 soil < veg")
    if mask is None:
        mask_arr = np.ones(ndvi_arr.shape, dtype=bool)
    else:
        mask_arr = np.asarray(mask, dtype=bool)
        if mask_arr.shape != ndvi_arr.shape:
            raise FVCError("掩膜形状与 NDVI 不一致")
    denom = veg - soil
    fvc = (ndvi_arr - soil) / denom
    below = mask_arr & (fvc < 0.0)
    above = mask_arr & (fvc > 1.0)
    counts = {
        "n_below_soil": int(below.sum()),
        "n_above_veg": int(above.sum()),
    }
    return fvc, mask_arr, counts


def _load_mask_file(gdal: Any, path: Path) -> np.ndarray:
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise FVCError(f"无法打开掩膜文件: {path}")
    try:
        if int(ds.RasterCount) < 1:
            raise FVCError("掩膜文件没有波段")
        raw_obj = ds.GetRasterBand(1).ReadAsArray()
        if raw_obj is None:
            raise FVCError(f"无法读取掩膜文件第 1 波段: {path}")
        raw = np.asarray(raw_obj, dtype=np.float64)
    finally:
        ds = None
    return raw != 0.0


def validate_class_edges(edges: list[float]) -> list[float]:
    """校验分级边界：至少 1 个、严格递增、全部在 (0, 1) 内。"""
    if len(edges) < 1:
        raise FVCError("fvcClassEdges 不能为空")
    for value in edges:
        if not math.isfinite(value) or not (0.0 < value < 1.0):
            raise FVCError(
                f"fvcClassEdges 必须在 (0, 1) 内且有限，实际 {value!r}"
            )
    if any(b <= a for a, b in zip(edges, edges[1:], strict=False)):
        raise FVCError("fvcClassEdges 必须严格递增")
    return edges


def run_fvc(
    out_path: Path,
    source: Path,
    *,
    endmember_mode: str = "percentile",
    ndvi_band: int = 1,
    low_percentile: float = 2.0,
    high_percentile: float = 98.0,
    soil: float | None = None,
    veg: float | None = None,
    class_edges: list[float] | None = None,
    mask_file: Path | None = None,
    crop: bool = True,
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """计算 FVC 并输出 FVC + 分级两波段 + 统计 sidecar。"""
    gdal = _gdal()
    mode = str(endmember_mode or "percentile").strip().lower()
    if mode not in FVC_ENDMEMBER_MODES:
        raise FVCError(
            f"未知 FVC 端元模式 {endmember_mode!r}；可用：{', '.join(FVC_ENDMEMBER_MODES)}"
        )
    if not math.isfinite(dst_nodata):
        raise FVCError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    if not (0.0 <= float(low_percentile) <= 100.0):
        raise FVCError("lowPercentile 必须在 [0, 100]")
    if not (0.0 <= float(high_percentile) <= 100.0):
        raise FVCError("highPercentile 必须在 [0, 100]")
    if float(low_percentile) >= float(high_percentile):
        raise FVCError("lowPercentile 必须小于 highPercentile")
    if not source.exists():
        raise FVCError(f"输入 NDVI 栅格不存在: {source}")

    _progress(callback, 0.05, "打开 NDVI 栅格")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise FVCError(f"无法打开输入栅格: {source}")
    try:
        if ndvi_band < 1 or ndvi_band > int(ds.RasterCount):
            raise FVCError(
                f"fvcBand={ndvi_band} 超出范围（数据集共 {ds.RasterCount} 个波段）"
            )
        band = ds.GetRasterBand(ndvi_band)
        if gdal.DataTypeIsComplex(band.DataType):
            raise FVCError("FVC 仅支持实数 NDVI 波段")
        raw_obj = band.ReadAsArray()
        if raw_obj is None:
            raise FVCError(f"无法读取波段 {ndvi_band}")
        ndvi_raw = np.asarray(raw_obj, dtype=np.float64)
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""
        label = band.GetNoDataValue()
        nodata = float(src_nodata) if src_nodata is not None else (
            float(label) if label is not None else None
        )
        width, height = int(ds.RasterXSize), int(ds.RasterYSize)
    finally:
        ds = None

    valid = np.isfinite(ndvi_raw)
    if nodata is not None:
        valid &= ndvi_raw != nodata
    if mask_file is not None:
        _progress(callback, 0.15, f"读取掩膜 {mask_file.name}")
        mask_arr = _load_mask_file(gdal, Path(mask_file))
        if mask_arr.shape != (height, width):
            raise FVCError(
                f"掩膜形状 {mask_arr.shape} 与 NDVI 网格 {(height, width)} 不一致"
            )
        valid &= mask_arr

    valid_values = ndvi_raw[valid]
    if valid_values.size == 0:
        raise FVCError("NDVI 无有效像元，无法计算 FVC")

    if mode == "percentile":
        soil_value = float(np.percentile(valid_values, float(low_percentile)))
        veg_value = float(np.percentile(valid_values, float(high_percentile)))
        if soil_value >= veg_value:
            raise FVCError(
                f"分位端元退化：soil={soil_value:.6g} >= veg={veg_value:.6g}；"
                f"请调整 lowPercentile/highPercentile"
            )
    else:
        if soil is None or veg is None:
            raise FVCError("endmemberMode=values 需要显式 fvcSoil 与 fvcVeg")
        soil_value = float(soil)
        veg_value = float(veg)

    _progress(callback, 0.45, "计算 FVC")
    fvc, _mask, counts = compute_fvc(ndvi_raw, soil_value, veg_value, mask=valid)
    fvc_values = fvc[valid]
    n_below = counts["n_below_soil"]
    n_above = counts["n_above_veg"]
    fvc_output = np.clip(fvc, 0.0, 1.0) if crop else fvc

    _progress(callback, 0.65, "分级")
    if class_edges is None:
        edges = [0.25, 0.5, 0.75]
    else:
        edges = validate_class_edges([float(v) for v in class_edges])
    class_index = np.searchsorted(edges, fvc_output, side="right")
    class_arr = (class_index + 1).astype(np.uint8)

    _progress(callback, 0.80, f"写出成果（{out_format} / Float32 FVC）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise FVCError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), width, height, 1, gdal.GDT_Float32)
    if out_ds is None:
        raise FVCError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(proj)
        out_ds.SetMetadataItem("FVC_MODE", mode)
        out_ds.SetMetadataItem("FVC_SOIL", f"{soil_value:.12g}")
        out_ds.SetMetadataItem("FVC_VEG", f"{veg_value:.12g}")
        out_ds.SetMetadataItem("FVC_CROP", "true" if crop else "false")
        fvc_band = out_ds.GetRasterBand(1)
        fvc_band.WriteArray(
            np.where(valid, fvc_output, dst_nodata).astype(np.float32)
        )
        fvc_band.SetNoDataValue(float(dst_nodata))
        fvc_band.SetDescription("fvc")
        fvc_band.FlushCache()
    finally:
        out_ds = None

    _progress(callback, 0.90, "写出分级类别栅格（Byte）")
    class_path = out_path.with_name(out_path.name + ".fvc_class.tif")
    class_driver = gdal.GetDriverByName("GTiff")
    if class_driver is None:
        raise FVCError("GTiff 驱动不可用（分级类别栅格）")
    class_ds = class_driver.Create(str(class_path), width, height, 1, gdal.GDT_Byte)
    if class_ds is None:
        raise FVCError(f"创建分级类别栅格失败: {class_path}")
    try:
        class_ds.SetGeoTransform(gt)
        class_ds.SetProjection(proj)
        class_ds.SetMetadataItem("FVC_MODE", mode)
        class_ds.SetMetadataItem(
            "FVC_CLASS_EDGES", ",".join(str(v) for v in edges)
        )
        class_band = class_ds.GetRasterBand(1)
        class_band.WriteArray(
            np.where(valid, class_arr, np.uint8(0)).astype(np.uint8)
        )
        # 类别波段显式用 0 作为 NoData（0 不是任何有效类别号）
        class_band.SetNoDataValue(0.0)
        class_band.SetDescription("fvc_class")
        class_band.FlushCache()
    finally:
        class_ds = None

    pixel_area = abs(gt[1] * gt[5] - gt[2] * gt[4])
    class_counts: list[dict[str, Any]] = []
    for index in range(len(edges) + 1):
        low = 0.0 if index == 0 else edges[index - 1]
        high = edges[index] if index < len(edges) else 1.0
        count = int(np.count_nonzero(valid & (class_index == index)))
        class_counts.append(
            {
                "class": index + 1,
                "range": [low, high],
                "inclusive": [index == 0, index < len(edges)],
                "count": count,
                "pixel_area": pixel_area * count,
            }
        )
    payload = {
        "type": "spectra_fvc",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P6-05",
        "source": str(source),
        "ndvi_band": int(ndvi_band),
        "mask_file": str(mask_file) if mask_file is not None else None,
        "endmember_mode": mode,
        "endmembers": {"soil": soil_value, "veg": veg_value},
        "percentiles_requested": [float(low_percentile), float(high_percentile)],
        "crop": bool(crop),
        "class_edges": edges,
        "class_counts": class_counts,
        "n_valid": int(valid.sum()),
        "n_total": int(ndvi_raw.size),
        "n_below_soil": n_below,
        "n_above_veg": n_above,
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
        "pixel_area": pixel_area,
        "formula": "fvc = (ndvi - soil) / (veg - soil)",
        "value_range_observed": {
            "min": float(np.min(fvc_values)),
            "max": float(np.max(fvc_values)),
        },
    }
    sidecar = out_path.with_name(out_path.name + ".fvc.json")
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    _progress(callback, 1.0, "完成")
    return {
        "mode": mode,
        "soil": soil_value,
        "veg": veg_value,
        "n_valid": int(valid.sum()),
        "n_below_soil": n_below,
        "n_above_veg": n_above,
        "class_counts": class_counts,
        "pixel_area": pixel_area,
        "class_raster": str(class_path),
        "sidecar": str(sidecar),
    }
