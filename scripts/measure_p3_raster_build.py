"""P3-03 测量用大合成栅格生成器（必须由 GDAL 运行时解释器执行）。

写成 4x4 分块 + 概览层级，尽量接近真实大栅格的读取形态。
数据是可复现的确定性图案：value = (col + 3*row) % 60000，并在固定位置放 NoData 块。

用法：<gdal_python> scripts/measure_p3_raster_build.py --out <path> --size 6144 --bands 3
结束时打印一行 JSON 清单，供测量脚本记录。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()

NODATA = 65535
OVERVIEW_LEVELS = (2, 4, 8, 16, 32, 64)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=6144)
    ap.add_argument("--bands", type=int, default=3)
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    n = int(args.size)
    nb = int(args.bands)
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(
        str(out),
        n,
        n,
        nb,
        gdal.GDT_UInt16,
        options=["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256", "COMPRESS=NONE", "SPARSE_OK=NO"],
    )
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32650)
    ds.SetProjection(srs.ExportToWkt())
    pixel = 2.0
    ds.SetGeoTransform([500000.0, pixel, 0.0, 3200000.0, 0.0, -pixel])

    rows = np.arange(n, dtype=np.int64)[:, None]
    cols = np.arange(n, dtype=np.int64)[None, :]
    for b in range(1, nb + 1):
        band = ds.GetRasterBand(b)
        band.SetNoDataValue(NODATA)
        for y0 in range(0, n, 256):
            h = min(256, n - y0)
            rr = rows[y0 : y0 + h]
            tile = ((cols + 3 * rr) * b) % 60000
            band.WriteArray(tile.astype(np.uint16), 0, y0)
        # 固定 NoData 块（右下角 64x64），用于校验无效像元在分块读取下仍被正确标记
        band.WriteArray(np.full((64, 64), NODATA, dtype=np.uint16), n - 64, n - 64)
        band.FlushCache()

    ds.BuildOverviews("AVERAGE", list(OVERVIEW_LEVELS))
    sizes = []
    band1 = ds.GetRasterBand(1)
    for i in range(int(band1.GetOverviewCount())):
        ov = band1.GetOverview(i)
        sizes.append([int(ov.XSize), int(ov.YSize)])
    width, height, count = int(ds.RasterXSize), int(ds.RasterYSize), int(ds.RasterCount)
    ds = None

    print(
        json.dumps(
            {
                "path": str(out),
                "width": width,
                "height": height,
                "bands": count,
                "dtype": "UInt16",
                "nodata": NODATA,
                "epsg": 32650,
                "overviews": list(OVERVIEW_LEVELS),
                "overview_sizes": sizes,
                "pattern": "(col + 3*row) * band % 60000",
                "nodata_block": [height - 64, width - 64, height, width],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
