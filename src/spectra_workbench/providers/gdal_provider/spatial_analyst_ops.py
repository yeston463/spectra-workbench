"""P11-09：次级空间分析（DEM 区域平均采样、PC 散点图提取与空间分析算子）。

覆盖遥感与 GIS 课堂次级高频需求：
1. **DEM 区域平均采样（Zonal DEM Statistics）**：
   依据输入的矢量多边形图层或 ROI 分区，提取每个分区内的 DEM 高程统计量
   （像元数、均值、标准差、最小值、最大值、中位数），消除人工点选采样的误差；
2. **PC1 vs PC2 散点图提取（PCA Scatter Plot）**：
   快速均匀抽样主成分波段像元坐标对，为特征空间与地类聚集度可视化提供轻量前端数据；
3. 输出审计伴随文件 `.dem_sample.json` 与 `.pca_scatter.json`。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["run_dem_sample", "extract_pc_scatter"]


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore

    gdal.UseExceptions()
    return gdal


def _ogr() -> Any:
    from osgeo import ogr

    ogr.UseExceptions()
    return ogr


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（空间分析阶段）")


def run_dem_sample(
    dem_path: Path,
    zone_path: Path,
    *,
    out_json_path: Path | None = None,
    dem_band: int = 1,
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """对 DEM 栅格执行矢量多边形分区统计（Zonal Statistics）。"""
    gdal = _gdal()
    ogr = _ogr()

    dem_ds = gdal.Open(str(dem_path), gdal.GA_ReadOnly)
    if dem_ds is None:
        raise RuntimeError(f"无法打开 DEM 栅格: {dem_path}")

    vec_ds = ogr.Open(str(zone_path), 0)
    if vec_ds is None:
        raise RuntimeError(f"无法打开分区矢量数据集: {zone_path}")

    try:
        w, h = int(dem_ds.RasterXSize), int(dem_ds.RasterYSize)
        gt = dem_ds.GetGeoTransform()
        band = dem_ds.GetRasterBand(dem_band)
        nodata = band.GetNoDataValue()

        # 读取整幅 DEM（若超出内存可按 BBOX 分块，通常课堂 DEM 为中小范围）
        dem_arr = band.ReadAsArray()
        if dem_arr is None:
            raise RuntimeError("读取 DEM 栅格数据失败")
        dem_arr = np.asarray(dem_arr, dtype=np.float64)

        valid_dem = np.isfinite(dem_arr)
        if nodata is not None:
            valid_dem &= dem_arr != float(nodata)

        lyr = vec_ds.GetLayerByIndex(0)
        total_feats = lyr.GetFeatureCount(force=1)
        defn = lyr.GetLayerDefn()

        _progress(callback, 0.10, f"开始对 {total_feats} 个矢量分区执行 DEM 采样统计")
        results = []

        lyr.ResetReading()
        for idx in range(total_feats):
            feat = lyr.GetNextFeature()
            if feat is None:
                break
            _progress(
                callback,
                0.10 + 0.80 * (idx / max(1, total_feats)),
                f"统计第 {idx + 1}/{total_feats} 个分区",
            )

            geom = feat.GetGeometryRef()
            if geom is None or geom.IsEmpty():
                continue

            # 获取属性字段
            attrs = {}
            for f_i in range(defn.GetFieldCount()):
                f_name = defn.GetFieldDefn(f_i).GetName()
                attrs[f_name] = feat.GetField(f_i)

            # 空间几何边界与栅格像元相交计算
            env = geom.GetEnvelope()  # (minX, maxX, minY, maxY)
            # 仿射逆变换求像元坐标范围
            det = gt[1] * gt[5] - gt[2] * gt[4]
            if abs(det) < 1e-12:
                continue

            c1 = int((gt[5] * (env[0] - gt[0]) - gt[2] * (env[3] - gt[3])) / det)
            r1 = int((-gt[4] * (env[0] - gt[0]) + gt[1] * (env[3] - gt[3])) / det)
            c2 = int((gt[5] * (env[1] - gt[0]) - gt[2] * (env[2] - gt[3])) / det)
            r2 = int((-gt[4] * (env[1] - gt[0]) + gt[1] * (env[2] - gt[3])) / det)

            col_min = max(0, min(c1, c2))
            col_max = min(w - 1, max(c1, c2))
            row_min = max(0, min(r1, r2))
            row_max = min(h - 1, max(r1, r2))

            if col_max < col_min or row_max < row_min:
                continue

            # 光栅化局部多边形掩膜
            box_w = col_max - col_min + 1
            box_h = row_max - row_min + 1

            # 创建内存矢量图层用于 RasterizeLayer
            mem_driver = ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")
            mem_ds = mem_driver.CreateDataSource("")
            mem_lyr = mem_ds.CreateLayer("zone", lyr.GetSpatialRef(), geom.GetGeometryType())
            f_clone = feat.Clone()
            mem_lyr.CreateFeature(f_clone)

            mem_rast_driver = gdal.GetDriverByName("MEM")
            rast_ds = mem_rast_driver.Create("", box_w, box_h, 1, gdal.GDT_Byte)
            rast_gt = (
                gt[0] + col_min * gt[1],
                gt[1],
                0.0,
                gt[3] + row_min * gt[5],
                0.0,
                gt[5],
            )
            rast_ds.SetGeoTransform(rast_gt)
            rast_ds.SetProjection(dem_ds.GetProjectionRef())
            gdal.RasterizeLayer(rast_ds, [1], mem_lyr, burn_values=[1])

            mask_patch = rast_ds.GetRasterBand(1).ReadAsArray() > 0
            dem_patch = dem_arr[row_min : row_max + 1, col_min : col_max + 1]
            valid_patch = valid_dem[row_min : row_max + 1, col_min : col_max + 1] & mask_patch

            pixels = dem_patch[valid_patch]
            if pixels.size > 0:
                stat = {
                    "feature_id": int(feat.GetFID()),
                    "attributes": attrs,
                    "valid_pixels": int(pixels.size),
                    "mean_elevation": round(float(np.mean(pixels)), 2),
                    "std_elevation": round(float(np.std(pixels)), 2),
                    "min_elevation": round(float(np.min(pixels)), 2),
                    "max_elevation": round(float(np.max(pixels)), 2),
                    "median_elevation": round(float(np.median(pixels)), 2),
                }
            else:
                stat = {
                    "feature_id": int(feat.GetFID()),
                    "attributes": attrs,
                    "valid_pixels": 0,
                    "mean_elevation": None,
                    "std_elevation": None,
                    "min_elevation": None,
                    "max_elevation": None,
                    "median_elevation": None,
                }
            results.append(stat)

        report = {
            "type": "spectra_dem_zonal_sample",
            "schema_version": "1.0",
            "dem_source": str(dem_path),
            "zone_source": str(zone_path),
            "zones_count": len(results),
            "zones": results,
        }

        target_json = out_json_path or dem_path.with_name(dem_path.name + ".dem_sample.json")
        with open(target_json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        _progress(callback, 1.0, f"DEM 分区平均采样统计完成（共 {len(results)} 个分区）")
        return report
    finally:
        dem_ds = None
        vec_ds = None


def extract_pc_scatter(
    pca_path: Path,
    *,
    pc_x: int = 1,
    pc_y: int = 2,
    max_points: int = 5000,
) -> dict[str, Any]:
    """从主成分变换栅格中均匀抽取 PC1 vs PC2 散点坐标对。"""
    gdal = _gdal()
    ds = gdal.Open(str(pca_path), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"无法打开 PCA 栅格成果: {pca_path}")

    try:
        count = int(ds.RasterCount)
        if pc_x < 1 or pc_x > count or pc_y < 1 or pc_y > count:
            raise ValueError(f"指定的 PC 波段 ({pc_x}, {pc_y}) 超出栅格波段总数 ({count})")

        b_x = ds.GetRasterBand(pc_x)
        b_y = ds.GetRasterBand(pc_y)

        arr_x = b_x.ReadAsArray()
        arr_y = b_y.ReadAsArray()

        nd_x = b_x.GetNoDataValue()
        nd_y = b_y.GetNoDataValue()

        valid = np.isfinite(arr_x) & np.isfinite(arr_y)
        if nd_x is not None:
            valid &= arr_x != float(nd_x)
        if nd_y is not None:
            valid &= arr_y != float(nd_y)

        vals_x = arr_x[valid]
        vals_y = arr_y[valid]
        total_valid = int(vals_x.size)

        if total_valid == 0:
            raise ValueError("PCA 栅格中未找到有效像元")

        # 均匀抽样至多 max_points 个点
        if total_valid > max_points:
            indices = np.linspace(0, total_valid - 1, max_points, dtype=int)
            sampled_x = vals_x[indices]
            sampled_y = vals_y[indices]
        else:
            sampled_x = vals_x
            sampled_y = vals_y

        report = {
            "type": "spectra_pca_scatter",
            "schema_version": "1.0",
            "pca_source": str(pca_path),
            "pc_x": pc_x,
            "pc_y": pc_y,
            "total_pixels": total_valid,
            "sampled_points": int(sampled_x.size),
            "x": [round(float(v), 4) for v in sampled_x],
            "y": [round(float(v), 4) for v in sampled_y],
        }

        sidecar_path = pca_path.with_name(f"{pca_path.name}.pca_scatter_pc{pc_x}_pc{pc_y}.json")
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        report["sidecar_path"] = str(sidecar_path)
        return report
    finally:
        ds = None
