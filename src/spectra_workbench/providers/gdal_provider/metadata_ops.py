"""P4-03：元数据编辑（ENVI 头文件）与概览构建。

原验收标准（docs/plan/02_里程碑与验收.md）：
「不覆盖未知字段；不改原始数据尺寸契约；多文件提交可恢复」

**GDAL 3.13.3 实测结论**（本模块实现方式的依据，不是凭印象）：
- **GDAL 重写 ENVI .hdr 会丢失全部未知字段**：向 .hdr 手工追加
  `custom_field_a`/`my_project_tag` 等自定义字段后，经 GDAL
  `SetNoDataValue`（或 SetMetadataItem）写回，这些字段**全部消失**。
  因此元数据编辑**不能依赖 GDAL 重写头文件**，必须「读-合并-写」：
  先快照原头文件全部字段，GDAL 应用变更后，把缺失的未知字段补回。
- GTiff 的概览（BuildOverviews）**内嵌**在主文件内（不产生 .ovr）；
  ENVI 的概览产生**外部 `<dat>.ovr`** 文件（GTiff 格式），属 sidecar，
  由 adapter 的多文件提交机制成套提交。
- BuildOverviews 天然不改变主影像尺寸（实测 8×8 构建后仍 8×8），
  但「不改原始数据尺寸契约」仍以断言守护，防未来实现回归。

ENVI .hdr 编码：实测含非 ASCII 字节（如含中文的路径写入 description），
读写统一用 latin-1（保字节往返，不做转码解释）。
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

__all__ = [
    "snapshot_hdr_lines",
    "apply_nodata",
    "restore_missing_hdr_fields",
    "build_overviews",
]


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def snapshot_hdr_lines(hdr_path: Path) -> list[str]:
    """快照 ENVI 头文件的全部行（latin-1，保字节）。文件不存在返回空表。"""
    if not hdr_path.exists():
        return []
    return hdr_path.read_text(encoding="latin-1").splitlines()


def _parse_key(line: str) -> str | None:
    """解析 `key = value` 行的 key；非字段行（空行/注释/花括号续行）返回 None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("{") or "=" not in line:
        return None
    key = line.split("=", 1)[0].strip()
    return key or None


def restore_missing_hdr_fields(hdr_path: Path, snapshot: list[str]) -> int:
    """把「原头文件里有、当前头文件里没有」的字段补回（未知字段保留的核心）。

    GDAL 重写 .hdr 后调用。只补回 **GDAL 未写出** 的字段——
    若某个原字段被 GDAL 正常写出（如 data ignore value），不重复追加。
    返回补回的字段数。
    """
    if not snapshot:
        return 0
    current_keys: set[str] = set()
    if hdr_path.exists():
        for line in hdr_path.read_text(encoding="latin-1").splitlines():
            key = _parse_key(line)
            if key:
                current_keys.add(key.lower())

    missing_lines = [
        line for line in snapshot
        if (key := _parse_key(line)) is not None and key.lower() not in current_keys
    ]
    if not missing_lines:
        return 0

    with hdr_path.open("a", encoding="latin-1", newline="") as f:
        f.write("\n")
        for line in missing_lines:
            f.write(line + "\n")
    return len(missing_lines)


def apply_nodata(data_path: Path, nodata: float) -> dict[str, Any]:
    """对目标数据集的全部波段设置 NoData（GA_Update，in-place）。

    ENVI：GDAL 会把 `data ignore value` 写入 .hdr（同时重写头文件、
    丢失未知字段）——调用方必须在调用前后配合
    snapshot/restore_missing_hdr_fields 完成未知字段保留。
    """
    gdal = _gdal()
    ds = gdal.Open(str(data_path), gdal.GA_Update)
    if ds is None:
        raise RuntimeError(f"无法以更新模式打开：{data_path}")
    try:
        for i in range(1, ds.RasterCount + 1):
            ds.GetRasterBand(i).SetNoDataValue(float(nodata))
        width, height, bands = ds.RasterXSize, ds.RasterYSize, ds.RasterCount
    finally:
        ds = None
    return {"width": width, "height": height, "bands": bands, "nodata": float(nodata)}


def build_overviews(
    data_path: Path,
    *,
    resampling: str = "NEAREST",
    levels: list[int],
    compress: str | None = None,
    callback: Any = None,
) -> dict[str, Any]:
    """构建概览（in-place），并断言主影像尺寸不变（尺寸契约守护）。

    `compress` 经 COMPRESS_OVERVIEW 配置传递，对 GTiff 内嵌概览与
    外部 .ovr（本身是 GTiff）均有效；None 表示不设置。
    """
    gdal = _gdal()
    ds = gdal.Open(str(data_path), gdal.GA_Update)
    if ds is None:
        raise RuntimeError(f"无法以更新模式打开：{data_path}")
    before = (ds.RasterXSize, ds.RasterYSize, ds.RasterCount)
    try:
        if compress:
            gdal.SetConfigOption("COMPRESS_OVERVIEW", compress)
        try:
            ds.BuildOverviews(resampling, [int(x) for x in levels], callback=callback)
        finally:
            if compress:
                gdal.SetConfigOption("COMPRESS_OVERVIEW", None)
        band = ds.GetRasterBand(1)
        count = band.GetOverviewCount()
        sizes: list[list[int]] = []
        for i in range(count):
            ov = band.GetOverview(i)
            sizes.append([ov.XSize, ov.YSize])
    finally:
        ds = None

    after = _read_shape(data_path)
    if after != before:
        raise RuntimeError(
            f"概览构建改变了主影像尺寸契约：{before} -> {after}（验收红线）"
        )
    return {
        "width": before[0],
        "height": before[1],
        "bands": before[2],
        "overview_count": count,
        "overview_sizes": sizes,
        "size_contract_ok": True,
    }


def _read_shape(data_path: Path) -> tuple[int, int, int]:
    gdal = _gdal()
    with contextlib.suppress(Exception):
        ds = gdal.Open(str(data_path), gdal.GA_ReadOnly)
        if ds is not None:
            try:
                return (ds.RasterXSize, ds.RasterYSize, ds.RasterCount)
            finally:
                ds = None
    return (-1, -1, -1)
