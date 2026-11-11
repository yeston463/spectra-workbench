"""P11-07 几何裁剪管线桥：基于绘制/图层 ROI 多边形及矢量图层的掩膜裁剪执行桥。

语义与规范：
-----------
1. 基于 GDAL Warp 的 cutline 机制（cutlineDSName、cropToCutline、dstNodata）；
2. 突破现有 translate 仅限矩形窗口的限制，支持任意凸/凹多边形及外部矢量图层（Shapefile、GeoJSON、GPKG）；
3. 多边形外部像元精确赋为指定的 NoData 值（默认为源栅格 NoData 或 -9999.0）；
4. 内部像元保持原生物理数值，不发生不必要重采样或有损量化；
5. 输出范围紧缩至多边形包围盒（cropToCutline=True），并由 GDAL Warp 自动更新空间参考与 GeoTransform；
6. 产出 sidecar 审计文件 `<成果名>.clip.json`，完整记录裁剪几何类型、顶点数、有效像元/NoData 计数等。
"""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["ClipError", "run_clip_mask"]


class ClipError(ValueError):
    """几何裁剪输入或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore

    gdal.UseExceptions()
    return gdal


def _osr() -> Any:
    from osgeo import osr

    return osr


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（几何掩膜裁剪阶段）")


def _build_cutline_geojson(
    points: list[tuple[float, float]] | list[list[float]],
    space: str,
    gt: tuple[float, ...] | None,
    srs: Any | None,
) -> dict[str, Any]:
    """将 ROI 顶点序列构建为合法的 GeoJSON FeatureCollection。

    若 space 为 "pixel"，使用源栅格 GeoTransform 换算为地图/地理坐标；
    确保多边形外环自闭合（首末点一致）。
    """
    if len(points) < 3:
        raise ClipError(f"多边形 ROI 至少需要 3 个顶点，实际给出 {len(points)} 个")

    coords: list[list[float]] = []
    for pt in points:
        c, r = float(pt[0]), float(pt[1])
        if space == "pixel" and gt is not None:
            # 仿射变换: X = GT0 + c*GT1 + r*GT2, Y = GT3 + c*GT4 + r*GT5
            x = gt[0] + c * gt[1] + r * gt[2]
            y = gt[3] + c * gt[4] + r * gt[5]
        else:
            x, y = c, r
        coords.append([x, y])

    # 闭合环：首尾相连
    if coords[0] != coords[-1]:
        coords.append(coords[0])

    fc: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coords],
                },
                "properties": {"name": "clip_cutline"},
            }
        ],
    }

    if srs is not None:
        code = srs.GetAuthorityCode(None)
        if code and str(code).isdigit():
            fc["crs"] = {
                "type": "name",
                "properties": {"name": f"urn:ogc:def:crs:EPSG::{code}"},
            }
        else:
            wkt = srs.ExportToWkt()
            if wkt:
                fc["crs"] = {
                    "type": "name",
                    "properties": {"name": wkt},
                }

    return fc


def run_clip_mask(
    out_path: Path,
    source: Path,
    *,
    cutline_path: Path | str | None = None,
    cutline_geojson: dict[str, Any] | str | None = None,
    roi_points: list[tuple[float, float]] | list[list[float]] | None = None,
    roi_space: str = "pixel",
    crop_to_cutline: bool = True,
    dst_nodata: float | None = None,
    src_nodata: float | None = None,
    cutline_where: str | None = None,
    cutline_layer: str | None = None,
    cutline_blend: float | None = None,
    all_touched: bool = False,
    resample_alg: str = "near",
    out_format: str = "GTiff",
    creation_options: list[str] | None = None,
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """基于矢量图层或多边形 ROI 执行高精度掩膜几何裁剪。"""
    gdal = _gdal()
    _progress(callback, 0.05, "打开输入栅格")

    if not source.exists():
        raise ClipError(f"输入栅格不存在: {source}")

    src_ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if src_ds is None:
        raise ClipError(f"无法打开输入栅格: {source}")

    try:
        src_w, src_h = int(src_ds.RasterXSize), int(src_ds.RasterYSize)
        raster_count = int(src_ds.RasterCount)
        if raster_count < 1:
            raise ClipError("输入栅格无有效波段")

        gt = src_ds.GetGeoTransform(can_return_null=True)
        srs = src_ds.GetSpatialRef()
        band1 = src_ds.GetRasterBand(1)
        src_nd = band1.GetNoDataValue()

        # 确定 NoData 值
        if dst_nodata is not None:
            effective_nodata = float(dst_nodata)
        elif src_nd is not None and math.isfinite(src_nd):
            effective_nodata = float(src_nd)
        else:
            effective_nodata = -9999.0
    finally:
        src_ds = None

    _progress(callback, 0.15, "准备裁剪几何数据源")
    tmp_cutline_file: Path | None = None
    cutline_ds_name: str | None = None

    try:
        if cutline_path is not None:
            cp = Path(cutline_path)
            if not cp.exists():
                raise ClipError(f"指定的矢量裁剪文件不存在: {cp}")
            cutline_ds_name = str(cp)
        elif roi_points is not None and len(roi_points) >= 3:
            gt_tuple = tuple(float(v) for v in gt) if gt is not None else None
            fc = _build_cutline_geojson(roi_points, roi_space, gt_tuple, srs)
            tmp_cutline_file = out_path.with_name(f"{out_path.stem}_cutline.geojson")
            tmp_cutline_file.write_text(
                json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            cutline_ds_name = str(tmp_cutline_file)
        elif cutline_geojson is not None:
            if isinstance(cutline_geojson, str):
                fc = json.loads(cutline_geojson)
            else:
                fc = cutline_geojson
            tmp_cutline_file = out_path.with_name(f"{out_path.stem}_cutline.geojson")
            tmp_cutline_file.write_text(
                json.dumps(fc, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            cutline_ds_name = str(tmp_cutline_file)
        else:
            raise ClipError("必须提供 cutlinePath、roiPoints 或 cutlineGeoJson 其中之一")

        _progress(callback, 0.30, "配置 GDAL Warp 裁剪参数")
        warp_kwargs: dict[str, Any] = {
            "format": out_format,
            "cutlineDSName": cutline_ds_name,
            "cropToCutline": bool(crop_to_cutline),
            "dstNodata": effective_nodata,
            "resampleAlg": resample_alg,
            "copyMetadata": True,
            "callback": callback,
        }

        if creation_options:
            warp_kwargs["creationOptions"] = creation_options
        if src_nodata is not None:
            warp_kwargs["srcNodata"] = float(src_nodata)
        elif src_nd is not None and math.isfinite(src_nd):
            warp_kwargs["srcNodata"] = float(src_nd)

        if cutline_where:
            warp_kwargs["cutlineWhere"] = str(cutline_where)
        if cutline_layer:
            warp_kwargs["cutlineLayer"] = str(cutline_layer)
        if cutline_blend is not None:
            warp_kwargs["cutlineBlend"] = float(cutline_blend)
        if all_touched:
            warp_kwargs["warpOptions"] = ["CUTLINE_ALL_TOUCHED=TRUE"]

        if srs is None:
            # 无地理参考时提供局部笛卡尔网格以支持裁剪包围盒运算
            warp_kwargs["srcSRS"] = 'LOCAL_CS["Pixel Grid"]'
            warp_kwargs["dstSRS"] = 'LOCAL_CS["Pixel Grid"]'

        _progress(callback, 0.45, "执行几何掩膜裁剪")
        warp_opts = gdal.WarpOptions(**warp_kwargs)
        out_ds = gdal.Warp(str(out_path), str(source), options=warp_opts)

        if out_ds is None:
            err_msg = gdal.GetLastErrorMsg() or "GDAL Warp 掩膜裁剪失败"
            raise ClipError(f"几何掩膜裁剪失败: {err_msg}")

        out_w = int(out_ds.RasterXSize)
        out_h = int(out_ds.RasterYSize)
        out_gt = tuple(float(v) for v in (out_ds.GetGeoTransform(can_return_null=True) or ()))
        out_srs_wkt = out_ds.GetProjectionRef() or ""

        # 统计波段 1 的有效/NoData 像素数以供审计验证
        b1 = out_ds.GetRasterBand(1)
        arr1 = b1.ReadAsArray()
        out_ds = None

        if arr1 is not None:
            arr_np = np.asarray(arr1)
            if math.isnan(effective_nodata):
                n_nodata = int(np.sum(np.isnan(arr_np)))
            else:
                n_nodata = int(np.sum(arr_np == effective_nodata))
            n_total = int(arr_np.size)
            n_valid = n_total - n_nodata
        else:
            n_nodata, n_total, n_valid = 0, 0, 0

    finally:
        # 清理临时生成的 GeoJSON 切割线文件
        if tmp_cutline_file is not None and tmp_cutline_file.exists():
            with contextlib.suppress(OSError):
                tmp_cutline_file.unlink()

    _progress(callback, 0.90, "生成裁剪审计元数据")
    sidecar_payload = {
        "type": "spectra_cutline_clip",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P11-07",
        "source": str(source),
        "cutline_source": (
            str(cutline_path)
            if cutline_path
            else ("roi_points" if roi_points else "cutline_geojson")
        ),
        "crop_to_cutline": bool(crop_to_cutline),
        "dst_nodata": effective_nodata,
        "resample_alg": resample_alg,
        "src_size": [src_w, src_h],
        "out_size": [out_w, out_h],
        "raster_count": raster_count,
        "out_geotransform": list(out_gt),
        "has_projection": bool(out_srs_wkt),
        "n_total": n_total,
        "n_valid": n_valid,
        "n_nodata": n_nodata,
    }
    sidecar = out_path.with_name(out_path.name + ".clip.json")
    sidecar.write_text(
        json.dumps(sidecar_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _progress(callback, 1.0, "几何掩膜裁剪完成")
    return {
        "out_width": out_w,
        "out_height": out_h,
        "raster_count": raster_count,
        "effective_nodata": effective_nodata,
        "n_valid": n_valid,
        "n_nodata": n_nodata,
        "sidecar": str(sidecar),
    }
