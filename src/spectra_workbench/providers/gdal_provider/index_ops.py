"""P6-01：光谱指数计算（NDVI/NDWI/NBR/SAVI）。

数学语义（对应验收「物理量/原值与波段元数据对应；合成值和无效区符合公式」）：
- **物理量/原值**：``use_physical=True``（默认）且波段元数据提供 scale/offset
  （scale≠0 或 offset≠0）时，按领域模型同一语义换算物理值
  （物理值 = 原值 × scale + offset）再进公式；否则直接用原始像元值。
  两个波段是否实际应用了物理变换逐波段如实返回，不静默；
- **无效区三源**：源波段 NoData 标签（或 srcNodata 覆盖）、分母为零、
  非有限值——三类都进输出 NoData，且「标签 / 无效区 / 像元值」三者一致；
- **公式**：ndvi/ndwi/nbr 为 (A−B)/(A+B)，savi 为 (A−B)/(A+B+L)×(1+L)；
  波段角色随指数在契约里写明（如 NDVI 的 A=NIR、B=Red）。

架构边界（如实记录）：方案规划 science 独立 worker；当前 sw-science 环境
仅 numpy/scipy（无 pydantic、无栅格 IO），无法独立承载 JSONL worker，
故本期在 gdal worker 内以 numpy 实现（QGIS 运行时实测自带 numpy 2.4.6）；
science 独立 worker（含环境补装）归后续轮次。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["compute_index", "INDEX_FORMULAS"]

#: 指数公式表：canonical 名 → (公式文本, 波段角色说明)。
INDEX_FORMULAS: dict[str, tuple[str, str]] = {
    "ndvi": ("(A - B) / (A + B)", "A=NIR（近红外）, B=Red（红光）"),
    "ndwi": ("(A - B) / (A + B)", "A=Green（绿光）, B=NIR（近红外）"),
    "nbr": ("(A - B) / (A + B)", "A=NIR（近红外）, B=SWIR（短波红外）"),
    "savi": ("((A - B) / (A + B + L)) * (1 + L)", "A=NIR（近红外）, B=Red（红光）, L=土壤调节因子"),
}


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（光谱指数计算阶段）")


def _band_array(ds: Any, band_index: int, role: str) -> tuple[Any, Any]:
    """读取 1-based 波段为 float64 数组，返回 (数组, 波段对象)。"""
    band = ds.GetRasterBand(band_index)
    if band is None:
        raise ValueError(f"波段 {band_index}（{role}）不存在")
    arr = band.ReadAsArray()
    if arr is None:
        raise RuntimeError(f"无法读取波段 {band_index}（{role}）")
    return np.asarray(arr, dtype=np.float64), band


def _physical_values(raw: Any, band: Any, role: str) -> tuple[Any, bool]:
    """按波段元数据换算物理值（物理值 = 原值 × scale + offset）。

    仅当 scale≠0 或 offset≠0 时才存在有意义的变换；GDAL 默认 scale=1.0 /
    offset=0.0 视为「未提供」，保持原值。是否实际应用逐波段返回。
    """
    scale = band.GetScale()
    offset = band.GetOffset()
    has_transform = (scale is not None and float(scale) != 0.0 and float(scale) != 1.0) or (
        offset is not None and float(offset) != 0.0
    )
    if not has_transform:
        return raw, False
    s = 1.0 if scale is None else float(scale)
    o = 0.0 if offset is None else float(offset)
    return raw * s + o, True


def compute_index(
    out_path: Path,
    source: Path,
    *,
    index: str,
    band_a: int,
    band_b: int,
    savi_l: float = 0.5,
    use_physical: bool = True,
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """计算光谱指数并写单波段 Float32 成果，返回可审计元信息。"""
    gdal = _gdal()
    if index not in INDEX_FORMULAS:
        raise ValueError(f"未知光谱指数 {index!r}；可用：{', '.join(sorted(INDEX_FORMULAS))}")
    if not source.exists():
        raise FileNotFoundError(f"输入栅格不存在: {source}")
    if band_a < 1 or band_b < 1:
        raise ValueError(f"波段号必须为正（1-based），实际 A={band_a}、B={band_b}")
    if index == "savi" and not (0.0 <= savi_l <= 1.0):
        raise ValueError(f"SAVI 的土壤调节因子 L 必须在 [0, 1]，实际 {savi_l}")
    if not math.isfinite(dst_nodata):
        raise ValueError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")

    _progress(callback, 0.05, "打开输入栅格")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"无法打开输入栅格: {source}")
    try:
        width, height = int(ds.RasterXSize), int(ds.RasterYSize)
        for role, idx in (("A", band_a), ("B", band_b)):
            if idx > int(ds.RasterCount):
                raise ValueError(f"波段 {role}={idx} 超出范围（数据集共 {ds.RasterCount} 个波段）")
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""

        _progress(callback, 0.15, f"读取波段 A={band_a}、B={band_b}")
        raw_a, band_a_obj = _band_array(ds, band_a, "A")
        raw_b, band_b_obj = _band_array(ds, band_b, "B")

        # 无效区第一源：波段 NoData 标签（srcNodata 优先覆盖）。
        # 必须在物理变换**之前**用原始值判定——变换会平移像元数值，
        # 变换后再比对标签会漏掉 NoData 像元（第 58 轮自审发现的顺序缺陷）。
        label_a = band_a_obj.GetNoDataValue()
        label_b = band_b_obj.GetNoDataValue()
        nodata_a = (
            float(src_nodata)
            if src_nodata is not None
            else (float(label_a) if label_a is not None else None)
        )
        nodata_b = (
            float(src_nodata)
            if src_nodata is not None
            else (float(label_b) if label_b is not None else None)
        )
        invalid = np.zeros(raw_a.shape, dtype=bool)
        n_input_invalid = 0
        for arr, nodata in ((raw_a, nodata_a), (raw_b, nodata_b)):
            band_invalid = ~np.isfinite(arr)
            if nodata is not None:
                band_invalid |= arr == nodata
            n_input_invalid += int(band_invalid.sum())
            invalid |= band_invalid

        physical_a = physical_b = False
        if use_physical:
            raw_a, physical_a = _physical_values(raw_a, band_a_obj, "A")
            raw_b, physical_b = _physical_values(raw_b, band_b_obj, "B")

        _progress(callback, 0.55, f"计算 {index}")
        formula, roles = INDEX_FORMULAS[index]
        denominator = raw_a + raw_b + (savi_l if index == "savi" else 0.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            if index == "savi":
                result = (raw_a - raw_b) / denominator * (1.0 + savi_l)
            else:
                result = (raw_a - raw_b) / denominator
        math_invalid = ~np.isfinite(result) | (denominator == 0)
        result[math_invalid] = np.nan
        n_math_invalid = int(math_invalid.sum())
        invalid = invalid | math_invalid

        _progress(callback, 0.80, f"写出成果（{out_format} / Float32）")
        driver = gdal.GetDriverByName(out_format)
        if driver is None:
            raise RuntimeError(f"不可用输出驱动：{out_format}")
        out_ds = driver.Create(str(out_path), width, height, 1, gdal.GDT_Float32)
        if out_ds is None:
            raise RuntimeError(f"创建输出失败（驱动 {out_format}）: {out_path}")
        try:
            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            band_out = out_ds.GetRasterBand(1)
            out_arr = np.where(invalid, np.float64(dst_nodata), result).astype(np.float32)
            band_out.WriteArray(out_arr)
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.FlushCache()
        finally:
            out_ds = None
        _progress(callback, 1.0, "完成")

        return {
            "index": index,
            "formula": formula,
            "roles": roles,
            "band_a": band_a,
            "band_b": band_b,
            "physical_a": physical_a,
            "physical_b": physical_b,
            "use_physical": bool(use_physical),
            "n_input_invalid": n_input_invalid,
            "n_math_invalid": n_math_invalid,
            "dst_nodata": float(dst_nodata),
            "driver": out_format,
        }
    finally:
        ds = None
