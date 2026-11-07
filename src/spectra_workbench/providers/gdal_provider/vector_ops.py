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

from pathlib import Path
from typing import Any

__all__ = ["inspect_vector"]


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
