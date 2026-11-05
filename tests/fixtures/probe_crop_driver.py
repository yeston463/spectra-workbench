"""探测 GDAL 源窗口预裁剪在 VRT 与 MEM 两种方案下的行为对比。

独立成文件是为了避免在 pytest 内做多层字符串转义（转义层数一多极易写坏）。
输出 JSON：VRT 是否惰性（只含 XML）、SrcRect 是否表达源窗口、两者像元是否一致。
"""

import json
import sys

from osgeo import gdal

gdal.UseExceptions()


def probe(src: str) -> dict:
    """对源栅格做 srcWin=[2,3,8,9] 的预裁剪，比较 VRT 与 MEM。"""
    vrt = gdal.Translate("", src, format="VRT", srcWin=[2, 3, 8, 9])
    xml = vrt.GetMetadata("xml:VRT")[0]
    vrt_info = {
        "driver": vrt.GetDriver().ShortName,
        "w": vrt.RasterXSize,
        "h": vrt.RasterYSize,
        "has_SrcRect": "SrcRect" in xml,
        "xml_len": len(xml),
        "nodata": vrt.GetRasterBand(1).GetNoDataValue(),
        "gt": list(vrt.GetGeoTransform()),
        "arr": vrt.GetRasterBand(1).ReadAsArray().tolist(),
    }
    vrt = None

    mem = gdal.Translate("", src, format="MEM", srcWin=[2, 3, 8, 9])
    mem_info = {
        "driver": mem.GetDriver().ShortName,
        "w": mem.RasterXSize,
        "h": mem.RasterYSize,
        "arr": mem.GetRasterBand(1).ReadAsArray().tolist(),
    }
    mem = None

    vrt_info["pixels_equal_to_mem"] = vrt_info["arr"] == mem_info["arr"]
    return vrt_info


if __name__ == "__main__":
    print(json.dumps(probe(sys.argv[1])))
