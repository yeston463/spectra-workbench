"""P6-06 去云：掩膜驱动的多时相替换，切入逐像元源与掩膜，绝不误称恢复。

方法定义
--------
- ``primaryTime`` / ``secondaryTime`` 为逐时相的元数据时间戳（可为字符串或数值），
  sidecar 记录**每个像元的来源**，保证输出诚实标注「云下信息来自其他时相」；
- 云掩膜由用户提供（每个像元：1=云，0=晴）；
- ``maskDilatePixels`` 为可选形态学膨胀（3×3 十字/8邻域），用于处理云边缘；
- ``out = primary``（晴）或 ``secondary``（云）；掩膜像元不清零、不滤波，只替换；
- sidecar ``<成果名>.cloudreplace.json`` 记录掩膜来源、n_cloud、n_clean、
  时相标签、王者像元标记与公式，使像元来源时间可独立复核；
- maskFile 必须为单波段 Byte/整数掩膜（1=云）。

明确边界
--------
- 只替换、不识别云（云/云影自动识别另行实现）；输出不声称「恢复了原时刻
  真实地表」，sidecar 暗摆位「云下来自 secondaryTime」。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["CloudReplaceError", "run_cloud_replace"]


class CloudReplaceError(ValueError):
    """云替换输入或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（云替换阶段）")


def _dilate_mask(mask: np.ndarray, pixels: int) -> np.ndarray:
    """对 1 像元宽的 mask 按 8 邻域做 N 次扩张（structural 3×3 全 1 形态学）。"""
    mask = np.asarray(mask, dtype=bool)
    if pixels <= 0:
        return mask.copy()
    result = mask.copy()
    kernel_row = np.ones(3, dtype=bool)
    kernel_col = np.ones(3, dtype=bool)
    for _ in range(int(pixels)):
        padded = np.pad(result, 1, mode="constant", constant_values=False)
        grown = np.zeros_like(result)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                grown |= padded[
                    1 + dy : 1 + dy + result.shape[0],
                    1 + dx : 1 + dx + result.shape[1],
                ]
        del kernel_row, kernel_col
        result = grown
    return result


def _load_mask(gdal: Any, path: Path) -> tuple[np.ndarray, int | None]:
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise CloudReplaceError(f"无法打开云掩膜: {path}")
    try:
        if int(ds.RasterCount) < 1:
            raise CloudReplaceError("云掩膜没有波段")
        band = ds.GetRasterBand(1)
        if gdal.DataTypeIsComplex(band.DataType):
            raise CloudReplaceError("云掩膜只支持实数波段")
        raw = band.ReadAsArray()
        if raw is None:
            raise CloudReplaceError(f"无法读取云掩膜波段: {path}")
    finally:
        ds = None
    arr = np.asarray(raw)
    cloud_mask = arr != 0
    return cloud_mask, int(arr.shape[0])


def run_cloud_replace(
    out_path: Path,
    primary: Path,
    secondary: Path,
    mask_path: Path,
    *,
    bands: list[int] | None = None,
    primary_time: Any = None,
    secondary_time: Any = None,
    mask_dilate_pixels: int = 0,
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """把 primary 的云像元替换成 secondary 同位置像元，保留来源标记。"""
    gdal = _gdal()
    if not math.isfinite(dst_nodata):
        raise CloudReplaceError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    if not primary.exists():
        raise CloudReplaceError(f"一期影像不存在: {primary}")
    if not secondary.exists():
        raise CloudReplaceError(f"二期影像（云下替换源）不存在: {secondary}")
    if not mask_path.exists():
        raise CloudReplaceError(f"云掩膜不存在: {mask_path}")

    _progress(callback, 0.05, "打开一期影像")
    pr_ds = gdal.Open(str(primary), gdal.GA_ReadOnly)
    if pr_ds is None:
        raise CloudReplaceError(f"无法打开一期影像: {primary}")
    _progress(callback, 0.12, "打开二期影像")
    sec_ds = gdal.Open(str(secondary), gdal.GA_ReadOnly)
    if sec_ds is None:
        pr_ds = None
        raise CloudReplaceError(f"无法打开二期影像: {secondary}")
    try:
        pr_w, pr_h = int(pr_ds.RasterXSize), int(pr_ds.RasterYSize)
        ref_w, ref_h = int(sec_ds.RasterXSize), int(sec_ds.RasterYSize)
        if (pr_w, pr_h) != (ref_w, ref_h):
            raise CloudReplaceError(
                f"两时相影像尺寸不一致：{pr_w}x{pr_h} vs {ref_w}x{ref_h}"
            )
        band_count = int(pr_ds.RasterCount)
        if band_count != int(sec_ds.RasterCount):
            raise CloudReplaceError(
                f"两时相波段数不一致：{band_count} vs {int(sec_ds.RasterCount)}"
            )
        if bands is None:
            band_list = list(range(1, band_count + 1))
        else:
            band_list = [int(v) for v in bands]
            if not band_list or min(band_list) < 1 or max(band_list) > band_count:
                raise CloudReplaceError(
                    f"bands 必须是 1..{band_count} 内的正整数列表，实际 {band_list}"
                )
        pr_gt = tuple(float(v) for v in (pr_ds.GetGeoTransform() or ()))
        ref_gt = tuple(float(v) for v in (sec_ds.GetGeoTransform() or ()))
        if pr_gt != ref_gt:
            raise CloudReplaceError(f"两时相 GeoTransform 不一致：{pr_gt} vs {ref_gt}")
        pr_proj = pr_ds.GetProjectionRef() or ""
        ref_proj = sec_ds.GetProjectionRef() or ""
        if pr_proj != ref_proj:
            raise CloudReplaceError("两时相 CRS 不一致；不做隐式投影")

        _progress(callback, 0.25, "读取云掩膜")
        cloud_mask, _mask_h = _load_mask(gdal, mask_path)
        if cloud_mask.shape != (pr_h, pr_w):
            raise CloudReplaceError(
                f"掩膜形状 {cloud_mask.shape} 与影像网格 {(pr_h, pr_w)} 不一致"
            )
        if int(mask_dilate_pixels) > 0:
            cloud_mask = _dilate_mask(cloud_mask, int(mask_dilate_pixels))

        _progress(callback, 0.35, f"读取 {len(band_list)} 个波段")
        primary_arrs: list[np.ndarray] = []
        primary_labels: list[float | None] = []
        for index in band_list:
            band = pr_ds.GetRasterBand(index)
            if gdal.DataTypeIsComplex(band.DataType):
                raise CloudReplaceError(f"一期波段 {index} 是复数类型")
            raw = band.ReadAsArray()
            if raw is None:
                raise CloudReplaceError(f"无法读取一期波段 {index}")
            primary_arrs.append(np.asarray(raw, dtype=np.float64))
            label = band.GetNoDataValue()
            primary_labels.append(
                float(label) if label is not None else None
            )
        secondary_arrs: list[np.ndarray] = []
        secondary_labels: list[float | None] = []
        for index in band_list:
            band = sec_ds.GetRasterBand(index)
            if gdal.DataTypeIsComplex(band.DataType):
                raise CloudReplaceError(f"二期波段 {index} 是复数类型")
            raw = band.ReadAsArray()
            if raw is None:
                raise CloudReplaceError(f"无法读取二期波段 {index}")
            secondary_arrs.append(np.asarray(raw, dtype=np.float64))
            label = band.GetNoDataValue()
            secondary_labels.append(
                float(label) if label is not None else None
            )
    finally:
        sec_ds = None
        pr_ds = None

    _progress(callback, 0.55, "替换云像元")
    merged_outputs: list[np.ndarray] = []
    n_cloud = int(cloud_mask.sum())
    n_clean = int((~cloud_mask).sum())
    n_both_nodata = 0
    for pr_idx, (primary_arr, sec_arr) in enumerate(
        zip(primary_arrs, secondary_arrs, strict=True)
    ):
        pr_label = primary_labels[pr_idx]
        sec_label = secondary_labels[pr_idx]
        label = (
            float(src_nodata)
            if src_nodata is not None
            else (
                float(pr_label)
                if pr_label is not None
                else (float(sec_label) if sec_label is not None else None)
            )
        )
        pr_valid = np.isfinite(primary_arr)
        sec_valid = np.isfinite(sec_arr)
        if label is not None:
            pr_valid &= primary_arr != label
            sec_valid &= sec_arr != label
        out_arr = np.where(cloud_mask, sec_arr, primary_arr)
        effective_valid = np.where(cloud_mask, sec_valid, pr_valid) & (
            ~cloud_mask | sec_valid
        )
        out_arr = np.where(effective_valid, out_arr, dst_nodata)
        n_both_nodata += int(
            np.count_nonzero(cloud_mask & ~sec_valid & ~pr_valid)
        )
        merged_outputs.append(np.where(effective_valid, out_arr, dst_nodata))

    _progress(callback, 0.78, f"写出成果（{out_format} / Float32，{len(merged_outputs)} 波段）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise CloudReplaceError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(
        str(out_path), pr_w, pr_h, len(merged_outputs), gdal.GDT_Float32
    )
    if out_ds is None:
        raise CloudReplaceError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(pr_gt)
        out_ds.SetProjection(pr_proj)
        out_ds.SetMetadataItem("CLOUDSOURCE_PRIMARY", str(primary))
        out_ds.SetMetadataItem("CLOUDSOURCE_SECONDARY", str(secondary))
        if primary_time is not None:
            out_ds.SetMetadataItem("CLOUDSOURCE_PRIMARY_TIME", str(primary_time))
        if secondary_time is not None:
            out_ds.SetMetadataItem("CLOUDSOURCE_SECONDARY_TIME", str(secondary_time))
        out_ds.SetMetadataItem("CLOUDSOURCE_METHOD", "mask_driven_multitemporal")
        for idx, arr in enumerate(merged_outputs):
            band_out = out_ds.GetRasterBand(idx + 1)
            band_out.WriteArray(np.asarray(arr, dtype=np.float32))
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.SetDescription(f"cloudfree_B{band_list[idx]}")
            band_out.FlushCache()
    finally:
        out_ds = None

    payload = {
        "type": "spectra_cloudreplace",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P6-06",
        "primary": str(primary),
        "secondary": str(secondary),
        "mask_file": str(mask_path),
        "bands": [int(v) for v in band_list],
        "primary_time": (
            str(primary_time) if primary_time is not None else None
        ),
        "secondary_time": (
            str(secondary_time) if secondary_time is not None else None
        ),
        "mask_dilate_pixels": int(mask_dilate_pixels),
        "n_cloud": n_cloud,
        "n_clean": n_clean,
        "n_both_nodata": int(n_both_nodata),
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
        "pixel_source_rule": (
            "clean: primary, cloud: secondary; source recorded via sidecar"
        ),
        "disclaimer": (
            f"输出是 mask 驱动的两时相替换；云下来的值来自 secondTime="
            f"{secondary_time!r}（若提供），不是「恢复了原时刻真实地表」。"
        ),
    }
    sidecar = out_path.with_name(out_path.name + ".cloudreplace.json")
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    _progress(callback, 1.0, "完成")
    return {
        "bands": [int(v) for v in band_list],
        "n_cloud": n_cloud,
        "n_clean": n_clean,
        "n_both_nodata": int(n_both_nodata),
        "primary_time": (
            str(primary_time) if primary_time is not None else None
        ),
        "secondary_time": (
            str(secondary_time) if secondary_time is not None else None
        ),
        "sidecar": str(sidecar),
    }
