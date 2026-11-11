"""P9-02 复杂 API 的可运行会话入口：为 function / class 与无表单算法生成会话模板。

设计
----
- 纯 stdlib（GUI 进程可安全导入，**不** import osgeo / rasterio）；
- 模板在引擎解释器内通过 :class:`spectra_workbench.scripting.session.ScriptSession`
  真实执行：先解析并检视目标符号（模块路径、类型、签名/文档），再运行一段
  **自包含、带断言**的最小示例（内存数据，不依赖外部文件、不写用户目录）；
- 每项模板声明 ``level``：
  ``demo``  = 含带断言的可运行示例；``inspect`` = 仅符号检视（本版本无 inspect 级）。
  这样「有 Python 会话入口」绝不被说成「该 API 已逐项数值验收」——
  验收范围以 ``level`` 与测试 ``tests/integration/test_p9_capability_routing.py`` 为准；
- 引擎选择：``gdal`` 提供者（含 osgeo.ogr / osgeo.osr）→ gdal 会话；
  ``rasterio`` 提供者 → rasterio 会话。
"""

from __future__ import annotations

from dataclasses import dataclass

from spectra_workbench.domain.models import CapabilityDescriptor, CapabilityKind

__all__ = [
    "ApiSnippet",
    "SESSION_ROUTED_ALGORITHMS",
    "build_batch_script",
    "generate_api_session_snippet",
    "has_session_route",
    "session_routed_ids",
]

#: 无图形表单、但提供会话入口的现存算法（目录输出、多文件，不适合单输出表单）。
SESSION_ROUTED_ALGORITHMS: frozenset[str] = frozenset({"gdal:retile"})

_INSPECT_BODY = """\
import importlib
import inspect

MODULE = {module!r}
ATTR = {attr!r}
symbol = getattr(importlib.import_module(MODULE), ATTR)
print("符号:", MODULE + "." + ATTR, "| 类型:", type(symbol).__name__)
try:
    print("签名:", inspect.signature(symbol))
except (TypeError, ValueError):
    print("签名: 原生绑定无法反射，请参考官方文档签名")
_doc = (inspect.getdoc(symbol) or "").strip().splitlines()
print("文档:", _doc[0] if _doc else "（无）")
"""

_GDAL_PRELUDE = """\
import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()


def make_demo_raster(path, arr, nodata=None, epsg=32650,
                     gt=(500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0)):
    arr = np.asarray(arr)
    dtype = gdal.GDT_Float32 if arr.dtype.kind == "f" else gdal.GDT_Byte
    ds = gdal.GetDriverByName("GTiff").Create(
        path, arr.shape[1], arr.shape[0], 1, dtype
    )
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    band.WriteArray(arr)
    ds.FlushCache()
    ds = None


DEMO = "/vsimem/spectra_api_demo.tif"
ARR = np.arange(12, dtype=np.uint8).reshape(3, 4)
make_demo_raster(DEMO, ARR)
"""

_RASTERIO_PRELUDE = """\
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

ARR = np.arange(12, dtype=np.uint8).reshape(3, 4)
TRANSFORM = from_origin(500000.0, 3200000.0, 10.0, 10.0)


def open_memory(arr=ARR, transform=TRANSFORM, crs="EPSG:32650"):
    mf = MemoryFile()
    with mf.open(driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                 dtype=str(arr.dtype), crs=crs, transform=transform) as dst:
        dst.write(arr, 1)
    return mf
"""

# --------------------------------------------------------------------------
# 带断言的最小示例（内存数据）
# --------------------------------------------------------------------------
_GDAL_DEMOS: dict[str, str] = {
    "gdal:api:ApplyGeoTransform": """\
gt = (500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0)
x, y = gdal.ApplyGeoTransform(gt, 5, 5)
assert (x, y) == (500050.0, 3199950.0), (x, y)
print("像元(5,5)中心 ->", x, y)
""",
    "gdal:api:InvGeoTransform": """\
gt = (500000.0, 10.0, 0.0, 3200000.0, 0.0, -10.0)
inv = gdal.InvGeoTransform(gt)
col, row = gdal.ApplyGeoTransform(inv, 500050.0, 3199950.0)
assert (round(col, 9), round(row, 9)) == (5.0, 5.0), (col, row)
print("逆变换往返 ->", col, row)
""",
    "gdal:api:Open": """\
ds = gdal.Open(DEMO)
assert (ds.RasterXSize, ds.RasterYSize, ds.RasterCount) == (4, 3, 1)
print("尺寸", ds.RasterXSize, ds.RasterYSize)
""",
    "gdal:api:OpenEx": """\
ds = gdal.OpenEx(DEMO, gdal.OF_RASTER | gdal.OF_READONLY)
assert ds.GetDriver().ShortName == "GTiff"
print("驱动", ds.GetDriver().ShortName)
""",
    "gdal:api:Dataset": """\
ds = gdal.Open(DEMO)
srs = osr.SpatialReference(wkt=ds.GetProjection())
assert ds.GetGeoTransform()[1] == 10.0 and srs.GetAuthorityCode(None) == "32650"
print("GT", ds.GetGeoTransform(), "EPSG", srs.GetAuthorityCode(None))
""",
    "gdal:api:Band": """\
ds = gdal.Open(DEMO)  # 必须持有 Dataset，Band 才不会悬空
band = ds.GetRasterBand(1)
assert np.array_equal(band.ReadAsArray(), ARR)
assert band.GetNoDataValue() is None
print("Band 读回一致", band.ReadAsArray().shape)
""",
    "gdal:api:Driver": """\
drv = gdal.GetDriverByName("GTiff")
assert drv.ShortName == "GTiff"
out = drv.Create("/vsimem/spectra_api_driver.tif", 2, 2, 1, gdal.GDT_Byte)
assert (out.RasterXSize, out.RasterYSize) == (2, 2)
print("Driver.Create 成功")
""",
    "gdal:api:ColorTable": """\
ct = gdal.ColorTable()
ct.SetColorEntry(0, (0, 0, 0, 255))
ct.SetColorEntry(1, (255, 0, 0, 255))
assert ct.GetCount() == 2 and ct.GetColorEntry(1) == (255, 0, 0, 255)
print("调色板条目数", ct.GetCount())
""",
    "gdal:api:RasterAttributeTable": """\
rat = gdal.RasterAttributeTable()
rat.CreateColumn("Count", gdal.GFT_Integer, gdal.GFU_PixelCount)
rat.SetRowCount(2)
rat.SetValueAsInt(0, 0, 5)
assert rat.GetRowCount() == 2 and rat.GetValueAsInt(0, 0) == 5
print("RAT 行数", rat.GetRowCount())
""",
    "gdal:api:Translate": """\
out = gdal.Translate("/vsimem/spectra_api_translate.tif", DEMO,
                     format="GTiff", outputType=gdal.GDT_Float32)
assert gdal.GetDataTypeName(out.GetRasterBand(1).DataType) == "Float32"
assert np.array_equal(out.GetRasterBand(1).ReadAsArray(), ARR.astype(np.float32))
print("Byte -> Float32 数值保持")
""",
    "gdal:api:Warp": """\
out = gdal.Warp("/vsimem/spectra_api_warp.tif", DEMO, dstSRS="EPSG:4326")
srs = osr.SpatialReference(wkt=out.GetProjection())
assert srs.GetAuthorityCode(None) == "4326"
print("重投影后 EPSG", srs.GetAuthorityCode(None), "尺寸", out.RasterXSize, out.RasterYSize)
""",
    "gdal:api:BuildVRT": """\
vrt = gdal.BuildVRT("/vsimem/spectra_api.vrt", [DEMO])
assert (vrt.RasterXSize, vrt.RasterYSize) == (4, 3)
assert np.array_equal(vrt.GetRasterBand(1).ReadAsArray(), ARR)
print("VRT 读回一致")
""",
    "gdal:api:Dem": """\
ramp = (np.arange(5, dtype=np.float32) * 10.0)[None, :].repeat(5, axis=0)
make_demo_raster("/vsimem/spectra_api_ramp.tif", ramp)
slope = gdal.DEMProcessing("/vsimem/spectra_api_slope.tif",
                           "/vsimem/spectra_api_ramp.tif", "slope")
value = float(slope.GetRasterBand(1).ReadAsArray()[2, 2])
assert abs(value - 45.0) < 1e-4, value
print("斜坡 10m/10m 像元 -> 坡度", value)
""",
    "gdal:api:Grid": """\
mem = ogr.GetDriverByName("MEM").CreateDataSource("")
lyr = mem.CreateLayer("pts", geom_type=ogr.wkbPoint)
lyr.CreateField(ogr.FieldDefn("z", ogr.OFTReal))
for x, y, z in ((0, 0, 1.0), (4, 0, 1.0), (0, 4, 1.0), (4, 4, 1.0)):
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("z", z)
    f.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT({x} {y})"))
    lyr.CreateFeature(f)
out = gdal.Grid("/vsimem/spectra_api_grid.tif", mem, algorithm="invdist",
                zfield="z", outputBounds=[0, 0, 4, 4], width=5, height=5)
arr = out.GetRasterBand(1).ReadAsArray()
assert arr.shape == (5, 5) and np.allclose(arr, 1.0)
print("常数点云插值 -> 全 1.0")
""",
    "gdal:api:Rasterize": """\
mem = ogr.GetDriverByName("MEM").CreateDataSource("")
lyr = mem.CreateLayer("poly", geom_type=ogr.wkbPolygon)
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON((0 0,2 0,2 2,0 2,0 0))"))
lyr.CreateFeature(f)
out = gdal.Rasterize("/vsimem/spectra_api_rasterize.tif", mem, format="GTiff",
                     outputBounds=[0, 0, 4, 4], xRes=1, yRes=1,
                     burnValues=[7], outputType=gdal.GDT_Byte)
arr = out.GetRasterBand(1).ReadAsArray()
assert int((arr == 7).sum()) == 4, arr
print("2x2 矩形烧录 4 个像元")
""",
    "gdal:api:ContourGenerate": """\
ramp = (np.arange(5, dtype=np.float32) * 10.0)[None, :].repeat(5, axis=0)
make_demo_raster("/vsimem/spectra_api_contour_src.tif", ramp)
src = gdal.Open("/vsimem/spectra_api_contour_src.tif")
mem = ogr.GetDriverByName("MEM").CreateDataSource("")
lyr = mem.CreateLayer("c", geom_type=ogr.wkbLineString)
lyr.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
gdal.ContourGenerate(src.GetRasterBand(1), 20.0, 0.0, [], 0, 0.0, lyr, -1, 0)
levels = sorted({f.GetField("ELEV") for f in lyr})
assert levels == [20.0, 40.0], levels
print("等值线高程", levels)
""",
    "gdal:api:Polygonize": """\
labels = np.array([[1, 1, 2, 2], [1, 1, 2, 2], [1, 1, 2, 2]], dtype=np.uint8)
make_demo_raster("/vsimem/spectra_api_poly_src.tif", labels)
src = gdal.Open("/vsimem/spectra_api_poly_src.tif")
mem = ogr.GetDriverByName("MEM").CreateDataSource("")
lyr = mem.CreateLayer("p", geom_type=ogr.wkbPolygon)
lyr.CreateField(ogr.FieldDefn("DN", ogr.OFTInteger))
gdal.Polygonize(src.GetRasterBand(1), None, lyr, 0, [])
areas = sorted(f.GetGeometryRef().GetArea() for f in lyr)
assert areas == [600.0, 600.0], areas
print("两个标签多边形面积", areas)
""",
    "ogr:api:DataSource": """\
ds = ogr.GetDriverByName("MEM").CreateDataSource("")
ds.CreateLayer("a", geom_type=ogr.wkbPoint)
ds.CreateLayer("b", geom_type=ogr.wkbPoint)
assert ds.GetLayerCount() == 2 and ds.GetLayer(1).GetName() == "b"
print("数据源图层数", ds.GetLayerCount())
""",
    "ogr:api:Layer": """\
ds = ogr.GetDriverByName("MEM").CreateDataSource("")
lyr = ds.CreateLayer("pts", geom_type=ogr.wkbPoint)
lyr.CreateField(ogr.FieldDefn("k", ogr.OFTInteger))
for i in range(3):
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("k", i)
    f.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT({i} 0)"))
    lyr.CreateFeature(f)
lyr.SetAttributeFilter("k >= 1")
assert lyr.GetFeatureCount() == 2
lyr.SetAttributeFilter(None)
lyr.SetSpatialFilterRect(-0.5, -1, 0.5, 1)
assert lyr.GetFeatureCount() == 1
print("属性过滤 2 / 空间过滤 1")
""",
    "ogr:api:Feature": """\
ds = ogr.GetDriverByName("MEM").CreateDataSource("")
lyr = ds.CreateLayer("pts", geom_type=ogr.wkbPoint)
lyr.CreateField(ogr.FieldDefn("name", ogr.OFTString))
f = ogr.Feature(lyr.GetLayerDefn())
f.SetField("name", "甲")
f.SetGeometry(ogr.CreateGeometryFromWkt("POINT(1 2)"))
assert f.GetField("name") == "甲" and f.GetGeometryRef().GetX() == 1.0
print("要素字段/几何读回一致")
""",
    "ogr:api:Geometry": """\
g = ogr.CreateGeometryFromWkt("POLYGON((0 0,4 0,4 3,0 3,0 0))")
assert g.Area() == 12.0 and g.GetGeometryName() == "POLYGON"
print("矩形面积", g.Area())
""",
    "ogr:api:Open": """\
src = ogr.GetDriverByName("MEM").CreateDataSource("")
lyr = src.CreateLayer("pts", geom_type=ogr.wkbPoint)
f = ogr.Feature(lyr.GetLayerDefn())
f.SetGeometry(ogr.CreateGeometryFromWkt("POINT(1 2)"))
lyr.CreateFeature(f)
ogr.GetDriverByName("GeoJSON").CopyDataSource(src, "/vsimem/spectra_api_open.geojson")
ds = ogr.Open("/vsimem/spectra_api_open.geojson")
assert ds.GetLayer(0).GetFeatureCount() == 1
print("GeoJSON 重开要素数", ds.GetLayer(0).GetFeatureCount())
""",
    "osr:api:SpatialReference": """\
srs = osr.SpatialReference()
srs.ImportFromEPSG(32650)
assert srs.GetAuthorityCode(None) == "32650" and srs.IsProjected() == 1
assert srs.GetLinearUnits() == 1.0
print("EPSG:32650 投影坐标系，单位", srs.GetLinearUnitsName())
""",
    "osr:api:CoordinateTransformation": """\
src = osr.SpatialReference()
src.ImportFromEPSG(4326)
src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
dst = osr.SpatialReference()
dst.ImportFromEPSG(32650)
dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
x, y, _ = osr.CoordinateTransformation(src, dst).TransformPoint(117.0, 0.0)
assert abs(x - 500000.0) < 1e-3 and abs(y) < 1e-3, (x, y)
print("中央经线 117°E,0°N -> UTM50", round(x, 3), round(y, 3))
""",
    "gdal:retile": """\
import os
from osgeo_utils import gdal_retile

out_dir = os.path.join(workspace, "retile_tiles")
os.makedirs(out_dir, exist_ok=True)
src = os.path.join(workspace, "retile_src.tif")
make_demo_raster(src, np.arange(16, dtype=np.uint8).reshape(4, 4))
rc = gdal_retile.main(["gdal_retile.py", "-ps", "2", "2", "-targetDir", out_dir, src])
tiles = sorted(n for n in os.listdir(out_dir) if n.lower().endswith(".tif"))
assert len(tiles) == 4, tiles
print("切出瓦片", tiles)
""",
}

_RASTERIO_DEMOS: dict[str, str] = {
    "rasterio:api:open": """\
with open_memory() as mf:
    with rasterio.open(mf.name) as ds:
        assert ds.shape == (3, 4) and ds.crs.to_epsg() == 32650
        assert np.array_equal(ds.read(1), ARR)
        print("shape", ds.shape, "EPSG", ds.crs.to_epsg())
""",
    "rasterio:api:MemoryFile": """\
with open_memory() as mf:
    payload = mf.read()
    assert len(payload) > 0
    with MemoryFile(payload) as again, again.open() as ds:
        assert np.array_equal(ds.read(1), ARR)
print("内存文件字节往返", len(payload))
""",
    "rasterio:api:Window": """\
from rasterio.windows import Window

w = Window(1, 1, 2, 2)
sub = ARR[w.toslices()]
assert (w.width, w.height) == (2, 2) and sub.shape == (2, 2)
assert sub.tolist() == [[5, 6], [9, 10]], sub.tolist()
print("Window(1,1,2,2) 子块", sub.tolist())
""",
    "rasterio:api:from_bounds": """\
from rasterio.windows import from_bounds

t = from_origin(0.0, 10.0, 1.0, 1.0)
w = from_bounds(2.0, 4.0, 5.0, 8.0, t)
assert (w.col_off, w.row_off, w.width, w.height) == (2.0, 2.0, 3.0, 4.0), w
print("地理范围 -> 窗口", w)
""",
    "rasterio:api:band": """\
with open_memory() as mf:
    with rasterio.open(mf.name) as ds:
        b = rasterio.band(ds, 1)
        assert b.bidx == 1 and b.shape == (3, 4)
        print("Band 代理", b.bidx, b.shape)
""",
    "rasterio:api:pad": """\
padded, new_t = rasterio.pad(ARR, TRANSFORM, 1, mode="constant")
assert padded.shape == (5, 6), padded.shape
assert new_t.c == TRANSFORM.c - 10.0 and new_t.f == TRANSFORM.f + 10.0
print("填充后", padded.shape)
""",
    "rasterio:api:merge": """\
from rasterio.merge import merge

left = open_memory(ARR, from_origin(500000.0, 3200000.0, 10.0, 10.0))
right = open_memory(ARR + 100, from_origin(500040.0, 3200000.0, 10.0, 10.0))
with left, right, rasterio.open(left.name) as a, rasterio.open(right.name) as b:
    data, _ = merge([a, b])
assert data.shape == (1, 3, 8)
assert np.array_equal(data[0, :, :4], ARR) and np.array_equal(data[0, :, 4:], ARR + 100)
print("相邻拼接", data.shape)
""",
    "rasterio:api:mask": """\
from rasterio.mask import mask

poly = {"type": "Polygon", "coordinates": [[
    (500010.0, 3199990.0), (500030.0, 3199990.0),
    (500030.0, 3199970.0), (500010.0, 3199970.0), (500010.0, 3199990.0)]]}
with open_memory() as mf, rasterio.open(mf.name) as ds:
    out, _ = mask(ds, [poly], crop=True, nodata=0)
assert out.shape == (1, 2, 2), out.shape
assert out[0].tolist() == [[5, 6], [9, 10]], out[0].tolist()
print("裁剪掩膜", out[0].tolist())
""",
    "rasterio:api:reproject": """\
from rasterio.warp import reproject, Resampling

src = ARR.astype(np.float32)
dst = np.zeros_like(src)
reproject(src, dst, src_transform=TRANSFORM, src_crs="EPSG:32650",
          dst_transform=TRANSFORM, dst_crs="EPSG:32650",
          resampling=Resampling.nearest)
assert np.array_equal(dst, src)
print("同网格重投影无损")
""",
    "rasterio:api:calculate_default_transform": """\
from rasterio.warp import calculate_default_transform

t, w, h = calculate_default_transform(
    "EPSG:32650", "EPSG:4326", 4, 3, 500000.0, 3199970.0, 500040.0, 3200000.0)
assert w > 0 and h > 0 and t.a > 0
print("默认输出网格", w, h)
""",
    "rasterio:api:transform_bounds": """\
from rasterio.warp import transform_bounds

left, bottom, right, top = transform_bounds("EPSG:4326", "EPSG:3857", 0.0, 0.0, 1.0, 1.0)
assert abs(left) < 1e-6 and abs(right - 111319.49079327357) < 1e-3, right
print("1° 经度 -> Web 墨卡托米", round(right, 3))
""",
    "rasterio:api:rasterize": """\
from rasterio.features import rasterize

poly = {"type": "Polygon", "coordinates": [[(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]]}
out = rasterize([(poly, 1)], out_shape=(4, 4), transform=from_origin(0.0, 4.0, 1.0, 1.0))
assert int(out.sum()) == 4
print("2x2 烧录像元数", int(out.sum()))
""",
    "rasterio:api:shapes": """\
from rasterio.features import shapes

img = np.array([[1, 1, 2], [1, 1, 2]], dtype=np.uint8)
found = {int(v): g for g, v in shapes(img, transform=from_origin(0.0, 2.0, 1.0, 1.0))}
assert sorted(found) == [1, 2]
print("连通域标签", sorted(found))
""",
    "rasterio:api:geometry_mask": """\
from rasterio.features import geometry_mask

poly = {"type": "Polygon", "coordinates": [[(1, 1), (3, 1), (3, 3), (1, 3), (1, 1)]]}
m = geometry_mask([poly], out_shape=(4, 4), transform=from_origin(0.0, 4.0, 1.0, 1.0))
assert int(m.sum()) == 12
print("目标外 True 像元数", int(m.sum()))
""",
    "rasterio:api:sieve": """\
from rasterio.features import sieve

img = np.zeros((4, 4), dtype=np.uint8)
img[0, 0] = 1
out = sieve(img, size=2)
assert int(out.sum()) == 0
print("孤立单像元被滤除")
""",
    "rasterio:api:copy": """\
import rasterio.shutil as rshutil

with open_memory() as mf:
    rshutil.copy(mf.name, "/vsimem/spectra_api_rio_copy.tif", driver="GTiff")
with rasterio.open("/vsimem/spectra_api_rio_copy.tif") as ds:
    assert np.array_equal(ds.read(1), ARR)
print("shutil.copy 数据一致")
""",
}

#: 示例名 -> 引擎
_ENGINE_FOR_PROVIDER = {"gdal": "gdal", "rasterio": "rasterio"}


@dataclass(frozen=True)
class ApiSnippet:
    """一项能力的会话模板。"""

    capability_id: str
    engine: str
    module: str
    attr: str
    kind: str
    level: str  # "demo"（含断言示例）| "inspect"（仅符号检视）
    code: str


def _split_target(target: str) -> tuple[str, str]:
    module, _, attr = target.rpartition(".")
    if not module or not attr:
        raise ValueError(f"无法解析脚本入口目标: {target!r}")
    return module, attr


def _script_target(cap: CapabilityDescriptor) -> str | None:
    for entry in cap.entrypoints:
        if entry.kind == "script" and entry.available:
            return entry.target
    return None


def has_session_route(cap: CapabilityDescriptor) -> bool:
    """能力是否应有会话入口：function/class 符号，或登记为会话路由的算法。"""
    if cap.kind in (CapabilityKind.function, CapabilityKind.class_):
        return _script_target(cap) is not None
    return cap.capability_id in SESSION_ROUTED_ALGORITHMS


def session_routed_ids(
    capabilities: list[CapabilityDescriptor] | None = None,
) -> list[str]:
    if capabilities is None:
        from spectra_workbench.registry import get_default_registry

        capabilities = get_default_registry().list_all()
    return sorted(c.capability_id for c in capabilities if has_session_route(c))


def generate_api_session_snippet(
    cap: CapabilityDescriptor | str, registry: object | None = None
) -> ApiSnippet:
    """生成能力的会话模板；``cap`` 可为描述符或能力 ID。"""
    resolved: CapabilityDescriptor
    if isinstance(cap, str):
        if registry is None:
            from spectra_workbench.registry import get_default_registry

            registry = get_default_registry()
        found = registry.get(cap)  # type: ignore[attr-defined]
        if found is None:
            raise KeyError(f"未登记的能力: {cap}")
        resolved = found
    else:
        resolved = cap
    if not has_session_route(resolved):
        raise ValueError(f"能力 {resolved.capability_id} 没有会话入口")

    target = _script_target(resolved)
    if target is None:
        # 会话路由算法（如 gdal:retile）：入口取其 Python 工具模块
        target = "osgeo_utils.gdal_retile.main"
    module, attr = _split_target(target)
    provider = resolved.engine.provider_id
    engine = _ENGINE_FOR_PROVIDER.get(provider, "gdal")

    demos = _RASTERIO_DEMOS if engine == "rasterio" else _GDAL_DEMOS
    prelude = _RASTERIO_PRELUDE if engine == "rasterio" else _GDAL_PRELUDE
    header = (
        "# Spectra Workbench · API 会话模板 (P9-02)\n"
        f"# 能力: {resolved.capability_id} ({resolved.kind.value})  引擎: {engine}\n"
        "# 在引擎解释器内真实运行；示例使用内存数据，可按需修改。\n"
        "# 注意：Python 会话执行的是本机代码，不是安全沙箱。\n"
    )
    inspect_code = _INSPECT_BODY.format(module=module, attr=attr)
    demo = demos.get(resolved.capability_id)
    if demo is None:
        code = header + inspect_code
        level = "inspect"
    else:
        code = (
            header
            + inspect_code
            + "\n# ---- 带断言的最小示例（可编辑）----\n"
            + prelude
            + "\n"
            + demo
        )
        level = "demo"
    return ApiSnippet(
        capability_id=resolved.capability_id,
        engine=engine,
        module=module,
        attr=attr,
        kind=resolved.kind.value,
        level=level,
        code=code,
    )


def build_batch_script(snippets: list[ApiSnippet]) -> str:
    """把同引擎的多个模板合成一个批量脚本：每项独立命名空间，输出 JSON 结果行。

    用于一次会话内验证多项模板（避免每项重启解释器），结果行为
    ``SPECTRA_API_RESULT {"id":..., "ok":..., "error":...}``。
    """
    import json

    payload = {s.capability_id: s.code for s in snippets}
    return (
        "import json, traceback, io, contextlib, sys\n"
        f"_SNIPPETS = json.loads({json.dumps(json.dumps(payload, ensure_ascii=True))})\n"
        "for _cid, _code in _SNIPPETS.items():\n"
        "    sys.stdout.flush()\n"
        "    _buf = io.StringIO()\n"
        "    _res = {'id': _cid, 'ok': True, 'error': None}\n"
        "    try:\n"
        "        with contextlib.redirect_stdout(_buf):\n"
        "            exec(compile(_code, '<' + _cid + '>', 'exec'), "
        "{'__name__': '__api_snippet__', 'workspace': workspace})\n"
        "    except BaseException as _exc:\n"
        "        _res['ok'] = False\n"
        "        _res['error'] = (type(_exc).__name__ + ': ' + str(_exc))[:300]\n"
        "    _res['stdout'] = _buf.getvalue()[-300:]\n"
        "    sys.stdout.flush()\n"
        "    print('\\nSPECTRA_API_RESULT ' + json.dumps(_res, ensure_ascii=True), flush=True)\n"
    )
