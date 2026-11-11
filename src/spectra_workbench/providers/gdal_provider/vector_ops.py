"""P5-02：矢量（OGR）成果重开校验。

polygonize / contour 的成果是**矢量数据集**，不能复用仅栅格的
``inspect_raster``（``gdal.Open`` 读不到 GPKG/GeoJSON 图层）。
本模块提供 OGR 侧的对应物：真实重开、逐图层收集几何/字段/范围，
并**实际读取要素样本**（WKT + 字段值）——对齐栅格侧
「samples 是真实读出的像元，不是处理函数的返回值」的哲学，
杜绝只检查文件存在就登记为成果。

**GDAL 3.13.3 实测依据**（QGIS 3.44.15 运行时，2026-11）：
- ``ogr.Open(path, 0)`` 只读打开 GPKG / GeoJSON 成功；
- 图层 SRS、字段定义、``GetFeatureCount``、``GetExtent(force=0)`` 均可用；
- polygonize 写出 ``Polygon``、contour 写出 ``3D Line String``（wkbLineString25D）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["inspect_vector", "run_vector_buffer"]


#: 实际读取要素样本的最大条数（对齐 inspect_raster 的采样哲学）。
_MAX_SAMPLES = 5


def inspect_vector(path: str | Path, *, sample: bool = True) -> dict[str, Any]:
    """真实重开一个矢量成果并读取图层/几何/字段信息。

    返回值中的 ``samples`` 是**实际读出的要素**（WKT 与字段值），
    不是创建图层的返回值；adapter 用它在 commit 前后对比，
    证明成果不是只检查了文件存在/非空。

    与展示向的 ``probe_vector`` 不同，这里是**提交校验**：
    ``feature_count`` 必须是精确值（commit 前后比对依赖它），
    因此用 ``force=1``——对象是刚生成的单文件成果，全表计数成本可忽略；
    ``probe_vector`` 面向可能巨大的用户数据集才刻意 ``force=0``。
    """
    # 惰性导入：与 mosaic_ops._gdal() 同理，模块本身必须能在无 osgeo 的
    # 环境（GUI 侧单测导入 adapter）下被导入。
    from osgeo import ogr  # type: ignore[import-not-found]

    ds = ogr.Open(str(path), 0)
    if ds is None:
        raise RuntimeError(f"Unable to reopen vector dataset {path}")
    try:
        driver = ds.GetDriver()
        layers: list[dict[str, Any]] = []
        for idx in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(idx)
            if lyr is None:
                layers.append({"index": idx, "error": "layer unavailable"})
                continue
            defn = lyr.GetLayerDefn()
            fields: list[dict[str, str]] = []
            for f_idx in range(defn.GetFieldCount()):
                fd = defn.GetFieldDefn(f_idx)
                fields.append({"name": fd.GetName(), "type": fd.GetTypeName()})
            srs = lyr.GetSpatialRef()
            crs_epsg: int | None = None
            if srs is not None:
                code = srs.GetAuthorityCode(None)
                if code and str(code).isdigit():
                    crs_epsg = int(code)
            try:
                ext = lyr.GetExtent(force=0)
                extent = [float(v) for v in ext] if ext is not None else None
                if extent is not None and any(v != v for v in extent):
                    extent = None
            except Exception:
                extent = None
            samples: list[dict[str, Any]] = []
            if sample:
                lyr.ResetReading()
                for _ in range(_MAX_SAMPLES):
                    feat = lyr.GetNextFeature()
                    if feat is None:
                        break
                    geom = feat.GetGeometryRef()
                    sample_fields: dict[str, Any] = {}
                    for f_idx in range(defn.GetFieldCount()):
                        fd = defn.GetFieldDefn(f_idx)
                        sample_fields[fd.GetName()] = feat.GetField(f_idx)
                    samples.append(
                        {
                            "geom_type": (
                                ogr.GeometryTypeToName(geom.GetGeometryType())
                                if geom is not None
                                else None
                            ),
                            "wkt": geom.ExportToWkt() if geom is not None else None,
                            "fields": sample_fields,
                        }
                    )
                lyr.ResetReading()
            layers.append(
                {
                    "index": idx,
                    "name": lyr.GetName() or f"layer_{idx}",
                    "geometry_type": ogr.GeometryTypeToName(lyr.GetGeomType()),
                    "feature_count": int(lyr.GetFeatureCount(force=1)),
                    "crs_epsg": crs_epsg,
                    "extent": extent,
                    "field_count": defn.GetFieldCount(),
                    "fields": fields,
                    "samples": samples,
                }
            )
        return {
            "driver": driver.GetName() if driver is not None else None,
            "layer_count": ds.GetLayerCount(),
            "layers": layers,
        }
    finally:
        ds = None


def run_vector_buffer(
    in_path: str | Path,
    out_path: str | Path,
    distance: float,
    *,
    dissolve: bool = False,
    out_format: str = "GPKG",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行矢量缓冲区分析（OGR Geometry Buffer 与可选要素融合 Dissolve）。"""
    from osgeo import ogr

    src_ds = ogr.Open(str(in_path), 0)
    if src_ds is None:
        raise RuntimeError(f"无法打开输入矢量文件: {in_path}")

    driver = ogr.GetDriverByName(out_format)
    if driver is None:
        raise RuntimeError(f"不支持的矢量输出驱动: {out_format}")

    out_p = Path(out_path)
    if out_p.exists():
        driver.DeleteDataSource(str(out_p))

    out_ds = driver.CreateDataSource(str(out_p))
    if out_ds is None:
        raise RuntimeError(f"创建输出矢量数据集失败: {out_p}")

    try:
        layer_count = src_ds.GetLayerCount()
        for l_idx in range(layer_count):
            in_lyr = src_ds.GetLayerByIndex(l_idx)
            if in_lyr is None:
                continue

            lyr_name = in_lyr.GetName() or f"buffer_{l_idx}"
            srs = in_lyr.GetSpatialRef()
            geom_type = ogr.wkbMultiPolygon if dissolve else ogr.wkbPolygon

            out_lyr = out_ds.CreateLayer(lyr_name, srs, geom_type)
            in_defn = in_lyr.GetLayerDefn()

            # 复制字段结构
            if not dissolve:
                for f_idx in range(in_defn.GetFieldCount()):
                    out_lyr.CreateField(in_defn.GetFieldDefn(f_idx))
            else:
                id_fld = ogr.FieldDefn("dissolved_id", ogr.OFTInteger)
                out_lyr.CreateField(id_fld)

            in_feat_count = in_lyr.GetFeatureCount(force=1)
            in_lyr.ResetReading()

            if dissolve:
                dissolved_geom = None
                for _f_i in range(in_feat_count):
                    f = in_lyr.GetNextFeature()
                    if f is None:
                        break
                    g = f.GetGeometryRef()
                    if g is not None and not g.IsEmpty():
                        buf_g = g.Buffer(float(distance))
                        if buf_g is not None and not buf_g.IsEmpty():
                            if dissolved_geom is None:
                                dissolved_geom = buf_g.Clone()
                            else:
                                dissolved_geom = dissolved_geom.Union(buf_g)
                if dissolved_geom is not None:
                    out_feat = ogr.Feature(out_lyr.GetLayerDefn())
                    out_feat.SetField("dissolved_id", 1)
                    out_feat.SetGeometry(dissolved_geom)
                    out_lyr.CreateFeature(out_feat)
            else:
                for _f_i in range(in_feat_count):
                    f = in_lyr.GetNextFeature()
                    if f is None:
                        break
                    g = f.GetGeometryRef()
                    if g is not None and not g.IsEmpty():
                        buf_g = g.Buffer(float(distance))
                        out_feat = ogr.Feature(out_lyr.GetLayerDefn())
                        # 拷贝字段值
                        for f_idx in range(in_defn.GetFieldCount()):
                            out_feat.SetField(f_idx, f.GetField(f_idx))
                        out_feat.SetGeometry(buf_g)
                        out_lyr.CreateFeature(out_feat)

        out_ds.FlushCache()
    finally:
        out_ds = None
        src_ds = None

    return inspect_vector(out_p, sample=True)
