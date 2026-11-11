"""P3 验收样例生成器（必须由 GDAL 运行时解释器执行）。

产出覆盖 P3-02/P3-05 验收所需的边界样例：旋转 affine、无 CRS、无 GeoTransform、
跨 CRS、NoData 与合法 0 值共存、独立 mask 波段、alpha 波段、scale/offset、概览层级。

用法： <gdal_python> tests/fixtures/make_p3_samples.py --out <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()

ROT_GT = (500000.0, 2.0, 0.5, 3200000.0, 0.5, -2.0)
NORTH_GT = (500000.0, 2.0, 0.0, 3200000.0, 0.0, -2.0)
GEO_GT = (116.0, 0.001, 0.0, 40.0, 0.0, -0.001)


def _srs(epsg: int | None) -> str | None:
    if not epsg:
        return None
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    return s.ExportToWkt()


def write_raster(
    path: Path,
    w: int,
    h: int,
    bands: int,
    array_fn,
    gt=None,
    epsg: int | None = 32650,
    nodata: float | None = None,
    dtype=gdal.GDT_UInt16,
    overviews: tuple[int, ...] = (),
    scale: float | None = None,
    offset: float | None = None,
    extra_options: list[str] | None = None,
    mask_fn=None,
    alpha_band: bool = False,
    nodata_block: tuple[int, int, int, int] | None = None,
) -> dict:
    drv = gdal.GetDriverByName("GTiff")
    opts = ["TILED=YES", "BLOCKXSIZE=128", "BLOCKYSIZE=128"]
    if extra_options:
        opts = opts + list(extra_options)
    ds = drv.Create(str(path), w, h, bands, dtype, options=opts)
    if gt is not None:
        ds.SetGeoTransform(gt)
    wkt = _srs(epsg)
    if wkt:
        ds.SetProjection(wkt)

    shapes = {}
    for b in range(1, bands + 1):
        arr = array_fn(b, w, h)
        if nodata_block is not None and nodata is not None:
            r0, c0, r1, c1 = nodata_block
            arr[r0:r1, c0:c1] = int(nodata)  # 写入真实的 NoData 像元，而不只是声明一个值
        band = ds.GetRasterBand(b)
        band.WriteArray(arr)
        if nodata is not None and not alpha_band:
            band.SetNoDataValue(nodata)
        if scale is not None:
            band.SetScale(scale)
        if offset is not None:
            band.SetOffset(offset)
        if alpha_band and b == bands:
            band.SetColorInterpretation(gdal.GCI_AlphaBand)
        band.FlushCache()
        shapes[f"band{b}"] = {
            "dtype": str(arr.dtype),
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
            "zero_count": int((arr == 0).sum()),
        }

    if mask_fn is not None:
        # 独立 mask 波段（GMF_PER_DATASET），与 NoData 是两件事
        ds.GetRasterBand(1).CreateMaskBand(gdal.GMF_PER_DATASET)
        m = mask_fn(w, h).astype(np.uint8)
        ds.GetRasterBand(1).GetMaskBand().WriteArray(m)
        ds.GetRasterBand(1).GetMaskBand().FlushCache()

    if overviews:
        ds.BuildOverviews("NEAREST", list(overviews))
    band = ds.GetRasterBand(1)
    ov_sizes = []
    for i in range(int(band.GetOverviewCount())):
        ov = band.GetOverview(i)
        ov_sizes.append([int(ov.XSize), int(ov.YSize)])
    gt_back = ds.GetGeoTransform(can_return_null=True)
    ds = None
    return {
        "path": str(path),
        "width": w,
        "height": h,
        "bands": bands,
        "epsg": epsg,
        "nodata": nodata,
        "geotransform": list(gt) if gt else None,
        "stored_geotransform": [float(x) for x in gt_back] if gt_back else None,
        "overviews": list(overviews),
        "overview_sizes": ov_sizes,
        "scale": scale,
        "offset": offset,
        "shapes": shapes,
        "nodata_block": list(nodata_block) if nodata_block else None,
    }


def ramp(b: int, w: int, h: int) -> np.ndarray:
    """每列值 = col*b，含 0 值列与一个 NoData 像元位置约定。"""
    arr = (np.arange(w, dtype=np.int64)[None, :] * b) % 60000
    return np.broadcast_to(arr, (h, w)).astype(np.uint16).copy()


def spike(b: int, w: int, h: int) -> np.ndarray:
    """常规值 100~200，另有一个 60000 的孤立高值，用于检验百分位拉伸。"""
    arr = np.full((h, w), 100 + b * 10, dtype=np.uint16)
    arr[h // 2, w // 2] = 60000
    return arr


def write_vector_samples(out: Path) -> dict:
    """生成矢量样例：多图层 GPKG（含中文字段、空图层、无 CRS 图层）与 GeoJSON。"""
    from osgeo import ogr, osr

    result: dict = {}

    # --- 多图层 GPKG ---
    gpkg_path = out / "p3_vector.gpkg"
    if gpkg_path.exists():
        gpkg_path.unlink()
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(str(gpkg_path))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)

    # 图层 1：点图层，含中文字段名与一个可空字段
    lyr = ds.CreateLayer("观测点", srs, ogr.wkbPoint)
    fld_name = ogr.FieldDefn("站点名称", ogr.OFTString)
    fld_name.SetWidth(64)
    lyr.CreateField(fld_name)
    fld_value = ogr.FieldDefn("数值", ogr.OFTInteger)
    lyr.CreateField(fld_value)
    fld_note = ogr.FieldDefn("备注", ogr.OFTString)
    fld_note.SetWidth(128)
    fld_note.SetNullable(True)
    lyr.CreateField(fld_note)

    feat = ogr.Feature(lyr.GetLayerDefn())
    feat.SetField("站点名称", "甲站")
    feat.SetField("数值", 42)
    feat.SetField("备注", "正常")
    feat.SetGeometry(ogr.CreateGeometryFromWkt("POINT (100.5 29.5)"))
    lyr.CreateFeature(feat)

    feat2 = ogr.Feature(lyr.GetLayerDefn())
    feat2.SetField("站点名称", "乙站")
    feat2.SetField("数值", 7)
    feat2.SetFieldNull("备注")
    feat2.SetGeometry(ogr.CreateGeometryFromWkt("POINT (101.25 29.75)"))
    lyr.CreateFeature(feat2)
    lyr = None

    # 图层 2：面图层
    poly = ds.CreateLayer("区域面", srs, ogr.wkbPolygon)
    poly.CreateField(ogr.FieldDefn("区域编号", ogr.OFTInteger))
    pf = ogr.Feature(poly.GetLayerDefn())
    pf.SetField("区域编号", 1)
    pf.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON ((100 29, 101 29, 101 30, 100 30, 100 29))"))
    poly.CreateFeature(pf)
    poly = None

    # 图层 3：空图层（零要素）
    empty = ds.CreateLayer("空图层", srs, ogr.wkbLineString)
    empty.CreateField(ogr.FieldDefn("名称", ogr.OFTString))
    empty = None

    # 图层 4：无 CRS 图层
    nocrs = ds.CreateLayer("无坐标系图层", None, ogr.wkbPoint)
    nocrs.CreateField(ogr.FieldDefn("标记", ogr.OFTString))
    nf = ogr.Feature(nocrs.GetLayerDefn())
    nf.SetField("标记", "x")
    nf.SetGeometry(ogr.CreateGeometryFromWkt("POINT (1 2)"))
    nocrs.CreateFeature(nf)
    nocrs = None
    ds = None

    result["vector_gpkg"] = {
        "path": str(gpkg_path),
        "layer_names": ["观测点", "区域面", "空图层", "无坐标系图层"],
        "empty_layer": "空图层",
        "no_crs_layer": "无坐标系图层",
        "point_layer": "观测点",
        "field_names": ["站点名称", "数值", "备注"],
        "feature_count": 2,
    }

    # --- 中文路径的 GeoJSON ---
    zh_dir = out / "中文 目录"
    zh_dir.mkdir(exist_ok=True)
    gj_path = zh_dir / "要素 数据.json"
    if gj_path.exists():
        gj_path.unlink()
    gjdrv = ogr.GetDriverByName("GeoJSON")
    gjds = gjdrv.CreateDataSource(str(gj_path))
    gjlayer = gjds.CreateLayer("线要素", srs, ogr.wkbLineString)
    gjlayer.CreateField(ogr.FieldDefn("编号", ogr.OFTInteger))
    gjf = ogr.Feature(gjlayer.GetLayerDefn())
    gjf.SetField("编号", 9)
    gjf.SetGeometry(ogr.CreateGeometryFromWkt("LINESTRING (100 29, 101 30)"))
    gjlayer.CreateFeature(gjf)
    gjds = None
    result["vector_geojson"] = {
        "path": str(gj_path),
        "layer_names": ["线要素"],
        "field_names": ["编号"],
    }

    return result


def write_subdataset_samples(out: Path) -> dict:
    """生成含多变量的 netCDF 容器（GDAL 会把每个变量暴露为子数据集）。"""
    from osgeo import gdal, osr

    result: dict = {}
    # netCDF 库对含中文/空格的路径无法创建，容器必须放在纯 ASCII 路径下
    ascii_dir = out / "ascii_sub"
    ascii_dir.mkdir(exist_ok=True)
    nc_path = ascii_dir / "p3_multi.nc"
    if nc_path.exists():
        nc_path.unlink()

    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    mem = gdal.GetDriverByName("MEM").Create("", 8, 8, 2, gdal.GDT_Float32)
    mem.SetGeoTransform([100.0, 0.25, 0.0, 30.0, 0.0, -0.25])
    mem.SetProjection(srs.ExportToWkt())
    for i in range(1, 3):
        band = mem.GetRasterBand(i)
        band.Fill(float(i))
        band.SetNoDataValue(-9999.0)
        band.SetDescription(f"Band{i}")
    # netCDF 库在本运行时无法用绝对路径创建（报 "non existing output directory"），
    # 只能在目标目录内以相对路径创建：这是运行时的真实限制，不是绕过。
    cwd = os.getcwd()
    try:
        os.chdir(ascii_dir)
        gdal.GetDriverByName("netCDF").CreateCopy(nc_path.name, mem)
    finally:
        os.chdir(cwd)
    mem = None

    ds = gdal.Open(str(nc_path))
    subs = ds.GetSubDatasets()
    ds = None

    result["subdataset_container"] = {
        "path": str(nc_path),
        "subdataset_count": len(subs),
        "uri_prefix": "NETCDF:",
        # netCDF 库无法在含中文/空格的路径下创建容器，如实记录该限制
        "chinese_path_unsupported": True,
    }
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    man: dict = {"samples": {}}

    man["samples"]["rotated"] = write_raster(
        out / "p3_rotated.tif",
        200,
        150,
        3,
        ramp,
        gt=ROT_GT,
        epsg=32650,
        nodata=65535,
        overviews=(2, 4, 8),
        nodata_block=(100, 150, 104, 154),
    )
    man["samples"]["north"] = write_raster(
        out / "p3_north.tif",
        200,
        150,
        3,
        ramp,
        gt=NORTH_GT,
        epsg=32650,
        nodata=65535,
        overviews=(2, 4, 8),
        nodata_block=(100, 150, 104, 154),
    )
    man["samples"]["spike"] = write_raster(
        out / "p3_spike.tif",
        64,
        64,
        1,
        spike,
        gt=NORTH_GT,
        epsg=32650,
        nodata=None,
    )
    man["samples"]["geographic"] = write_raster(
        out / "p3_geo.tif",
        64,
        64,
        1,
        ramp,
        gt=GEO_GT,
        epsg=4326,
        nodata=65535,
        nodata_block=(40, 40, 44, 44),
    )
    man["samples"]["no_crs"] = write_raster(
        out / "p3_nocrs.tif",
        64,
        64,
        1,
        ramp,
        gt=NORTH_GT,
        epsg=None,
        nodata=65535,
    )
    man["samples"]["no_gt"] = write_raster(
        out / "p3_nogt.tif",
        64,
        64,
        1,
        ramp,
        gt=None,
        epsg=None,
        nodata=65535,
    )
    man["samples"]["scale_offset"] = write_raster(
        out / "p3_phys.tif",
        64,
        64,
        1,
        ramp,
        gt=NORTH_GT,
        epsg=32650,
        nodata=None,
        scale=1e-4,
        offset=0.5,
    )

    MASK_BLOCK = (20, 40, 30, 50)  # (r0, c0, r1, c1)：被掩膜但像元值合法的区域

    def mask_all_valid(w: int, h: int) -> np.ndarray:
        m = np.full((h, w), 255, dtype=np.uint8)
        r0, c0, r1, c1 = MASK_BLOCK
        m[r0:r1, c0:c1] = 0
        return m

    man["samples"]["masked"] = write_raster(
        out / "p3_masked.tif",
        64,
        64,
        1,
        ramp,
        gt=NORTH_GT,
        epsg=32650,
        nodata=None,
        mask_fn=mask_all_valid,
    )
    man["samples"]["masked"]["mask_block"] = list(MASK_BLOCK)
    ALPHA_BLOCK = (5, 7, 13, 15)  # (r0, c0, r1, c1)：alpha=0 的区域，RGB 值本身合法

    def alpha_array(b: int, w: int, h: int) -> np.ndarray:
        if b < 4:
            return np.full((h, w), b * 60, dtype=np.uint8)
        a = np.full((h, w), 255, dtype=np.uint8)
        r0, c0, r1, c1 = ALPHA_BLOCK
        a[r0:r1, c0:c1] = 0
        return a

    man["samples"]["alpha"] = write_raster(
        out / "p3_alpha.tif",
        64,
        64,
        4,
        alpha_array,
        gt=NORTH_GT,
        epsg=32650,
        nodata=None,
        dtype=gdal.GDT_Byte,
        extra_options=["PHOTOMETRIC=RGB", "ALPHA=YES"],
        alpha_band=True,
    )
    man["samples"]["alpha"]["alpha_block"] = list(ALPHA_BLOCK)

    # 中文与空格路径（A12）
    zh = out / "中文 目录"
    zh.mkdir(exist_ok=True)
    man["samples"]["chinese_path"] = write_raster(
        zh / "影像 1.tif",
        64,
        64,
        1,
        ramp,
        gt=NORTH_GT,
        epsg=32650,
        nodata=65535,
    )

    # ------------------------------------------------------- P3-02 矢量数据集
    vector_info = write_vector_samples(out)
    man["samples"].update(vector_info)

    # ------------------------------------------------------ P3-02 子数据集容器
    subdataset_info = write_subdataset_samples(out)
    man["samples"].update(subdataset_info)

    (out / "manifest.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps(man, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
