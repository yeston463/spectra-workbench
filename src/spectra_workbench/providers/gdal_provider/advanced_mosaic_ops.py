"""P11-03：图像镶嵌高级化（直方图匹配消色差、重叠区 50 像元羽化渐变与接缝线过渡）。

解决遥感拼接课堂核心返工点：硬接边阶跃与相邻景光照/时相色差。
数学与算子架构：
1. **直方图匹配（Histogram Matching / 相对辐射归一化）**：
   在重叠区（Overlap Region）提取参考景（Master）与待校正景（Slave）的累积经验分布函数（CDF），
   通过分位数映射方程将 Slave 的像元辐射量调整为与 Master 一致的对比度与均值，
   彻底消除景间曝光与大气色差；
2. **重叠区羽化（Feathering Blending）**：
   在重叠区基于边界距离场计算归一化渐变权重 $W_i(x, y)$，
   以可配置宽度（默认 50 像元）执行余弦/线性加权无缝融合，
   消除接边处的阶跃突变；
3. **输出与审计**：
   写出多波段成果栅格并伴随生成 `.mosaic.json` 侧车审计元数据。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["run_advanced_mosaic", "match_histogram_1d"]


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（高级图像镶嵌阶段）")


def match_histogram_1d(
    source_vals: np.ndarray,
    reference_vals: np.ndarray,
    source_full: np.ndarray,
) -> np.ndarray:
    """基于重叠区采样执行单波段经验直方图匹配，映射 source_full 全图像元。"""
    if source_vals.size < 10 or reference_vals.size < 10:
        return source_full.copy()

    s_sorted = np.sort(source_vals)
    r_sorted = np.sort(reference_vals)

    # 经验累积概率分位数
    s_quantiles = np.linspace(0.0, 1.0, len(s_sorted))
    r_quantiles = np.linspace(0.0, 1.0, len(r_sorted))

    # 计算 source_full 在源经验 CDF 下的分位值
    mapped_quantiles = np.interp(source_full, s_sorted, s_quantiles)
    # 通过参考经验 CDF 逆函数插值求得匹配后的目标像元值
    matched = np.interp(mapped_quantiles, r_quantiles, r_sorted)
    return matched.astype(source_full.dtype)


def _compute_distance_weights(valid_mask: np.ndarray, feather_width: int) -> np.ndarray:
    """计算二值掩膜至外部无效区的有效距离场权重（边界为 0，深入 feather_width 像元达到 1.0）。"""
    if feather_width <= 0 or not valid_mask.any():
        return valid_mask.astype(np.float64)

    h, w = valid_mask.shape
    dist = np.zeros((h, w), dtype=np.float64)

    # 纯 numpy 近似距离场计算（无需 scipy 强依赖）：
    # 从无效区向内部逐层膨胀/腐蚀衰减
    current = valid_mask.copy()
    steps = max(1, int(feather_width))

    # 采样 10 个均匀步长加速大型图层距离场构造
    step_size = max(1, steps // 10)
    for s in range(1, steps + 1, step_size):
        # 简单 4 邻域腐蚀
        eroded = np.pad(current[1:-1, 1:-1], 1, mode="constant", constant_values=False)
        eroded = (
            eroded
            & np.roll(current, 1, axis=0)
            & np.roll(current, -1, axis=0)
            & np.roll(current, 1, axis=1)
            & np.roll(current, -1, axis=1)
        )
        dist[current & ~eroded] = min(1.0, s / steps)
        current = eroded
        if not current.any():
            break

    dist[current] = 1.0
    dist[~valid_mask] = 0.0

    # 余弦平滑滚降：W = 0.5 * (1 - cos(pi * dist))
    weights = np.where(valid_mask, 0.5 * (1.0 - np.cos(np.pi * dist)), 0.0)
    return weights


def run_advanced_mosaic(
    out_path: Path,
    sources: list[Path],
    *,
    match_histogram: bool = True,
    master_index: int = 0,
    feather_width: int = 50,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行高级镶嵌：直方图匹配消色差 + 50 像元重叠羽化平滑过渡。"""
    gdal = _gdal()
    if not sources or len(sources) < 2:
        raise ValueError("高级镶嵌至少需要 2 个输入栅格源")

    _progress(callback, 0.05, "解析多景栅格空间参考与几何包络")
    src_datasets = []
    for s in sources:
        ds = gdal.Open(str(s), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开输入栅格: {s}")
        src_datasets.append(ds)

    try:
        ref_ds = src_datasets[master_index % len(src_datasets)]
        ref_srs = ref_ds.GetProjectionRef()
        band_count = int(ref_ds.RasterCount)

        # 统一重采样到统一全局网格（基于 VRT / Warp）
        _progress(callback, 0.20, "构建统一度量基准 VRT 画布")
        vrt_options = gdal.BuildVRTOptions(resampleAlg="bilinear")
        union_vrt = gdal.BuildVRT("", [str(p) for p in sources], options=vrt_options)
        if union_vrt is None:
            raise RuntimeError("无法为输入栅格构建联合几何外包画布")

        union_w = int(union_vrt.RasterXSize)
        union_h = int(union_vrt.RasterYSize)
        union_gt = tuple(float(x) for x in union_vrt.GetGeoTransform())
        union_proj = union_vrt.GetProjectionRef()
        union_vrt = None

        _progress(callback, 0.35, "对齐各景栅格至统一坐标视口")
        aligned_layers: list[tuple[np.ndarray, np.ndarray]] = []
        # 将各景独立投影/重采样至目标全局网格
        for idx, s_path in enumerate(sources):
            _progress(
                callback,
                0.35 + 0.20 * (idx / len(sources)),
                f"对齐重投影第 {idx + 1}/{len(sources)} 景",
            )
            mem_path = f"/vsimem/aligned_{idx}.tif"
            warp_opts = gdal.WarpOptions(
                format="GTiff",
                width=union_w,
                height=union_h,
                outputBounds=(
                    union_gt[0],
                    union_gt[3] + union_h * union_gt[5],
                    union_gt[0] + union_w * union_gt[1],
                    union_gt[3],
                ),
                dstSRS=union_proj or ref_srs,
                resampleAlg="bilinear",
                dstNodata=dst_nodata,
            )
            w_ds = gdal.Warp(mem_path, str(s_path), options=warp_opts)
            if w_ds is None:
                raise RuntimeError(f"对齐重采样栅格失败: {s_path}")

            # 读取波段数据与有效掩膜
            bands_data = []
            for b_i in range(1, band_count + 1):
                b_arr = w_ds.GetRasterBand(b_i).ReadAsArray()
                bands_data.append(np.asarray(b_arr, dtype=np.float64))

            layer_arr = np.stack(bands_data, axis=0)  # (B, H, W)
            # 有效像元掩膜：非 NaN 且不等于 dst_nodata
            v_mask = np.isfinite(layer_arr[0]) & (layer_arr[0] != dst_nodata)
            aligned_layers.append((layer_arr, v_mask))
            w_ds = None
            gdal.Unlink(mem_path)

        _progress(callback, 0.60, "执行重叠区直方图匹配相对辐射归一化")
        master_arr, master_mask = aligned_layers[master_index]

        if match_histogram and len(sources) > 1:
            for s_idx in range(len(sources)):
                if s_idx == master_index:
                    continue
                s_arr, s_mask = aligned_layers[s_idx]
                overlap = master_mask & s_mask

                for b in range(band_count):
                    if overlap.sum() >= 50:
                        ref_sample = master_arr[b][overlap]
                        src_sample = s_arr[b][overlap]
                    else:
                        ref_sample = master_arr[b][master_mask]
                        src_sample = s_arr[b][s_mask]

                    if src_sample.size > 0 and ref_sample.size > 0:
                        s_arr[b] = match_histogram_1d(src_sample, ref_sample, s_arr[b])

        _progress(callback, 0.75, f"执行重叠区余弦羽化融合（过渡宽度 {feather_width} 像元）")
        weight_sum = np.zeros((union_h, union_w), dtype=np.float64)
        layer_weights: list[np.ndarray] = []

        for _, v_mask in aligned_layers:
            w = _compute_distance_weights(v_mask, feather_width)
            layer_weights.append(w)
            weight_sum += w

        # 加权融合各波段
        out_mosaic = np.full((band_count, union_h, union_w), dst_nodata, dtype=np.float32)
        valid_any = weight_sum > 0.0

        for b in range(band_count):
            blended = np.zeros((union_h, union_w), dtype=np.float64)
            for idx, (l_arr, _) in enumerate(aligned_layers):
                blended += l_arr[b] * layer_weights[idx]
            # 归一化加权平均
            blended_norm = np.where(valid_any, blended / np.maximum(weight_sum, 1e-9), dst_nodata)
            out_mosaic[b] = blended_norm.astype(np.float32)

        _progress(callback, 0.90, f"写出镶嵌成果（{union_w}x{union_h}x{band_count}）")
        driver = gdal.GetDriverByName(out_format)
        if driver is None:
            raise RuntimeError(f"不可用输出驱动: {out_format}")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_ds = driver.Create(str(out_path), union_w, union_h, band_count, gdal.GDT_Float32)
        if out_ds is None:
            raise RuntimeError(f"创建输出成果失败: {out_path}")

        try:
            out_ds.SetGeoTransform(union_gt)
            out_ds.SetProjection(union_proj or ref_srs)
            for b in range(1, band_count + 1):
                band = out_ds.GetRasterBand(b)
                band.WriteArray(out_mosaic[b - 1])
                band.SetNoDataValue(float(dst_nodata))
            out_ds.FlushCache()
        finally:
            out_ds = None

        # 写入 sidecar 审计记录
        sidecar_path = out_path.with_name(out_path.name + ".mosaic.json")
        audit_payload = {
            "type": "spectra_advanced_mosaic",
            "schema_version": "1.0",
            "sources": [str(s) for s in sources],
            "master_source": str(sources[master_index]),
            "match_histogram": match_histogram,
            "feather_width": feather_width,
            "dimensions": [union_h, union_w, band_count],
            "dst_nodata": dst_nodata,
            "total_pixels": union_w * union_h,
            "valid_pixels": int(valid_any.sum()),
        }
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(audit_payload, f, indent=2, ensure_ascii=False)

        _progress(callback, 1.0, "高级镶嵌完成")
        return {
            "out_path": str(out_path),
            "sidecar_path": str(sidecar_path),
            "dimensions": [union_w, union_h, band_count],
            "valid_pixels": int(valid_any.sum()),
        }
    finally:
        for ds in src_datasets:
            del ds
