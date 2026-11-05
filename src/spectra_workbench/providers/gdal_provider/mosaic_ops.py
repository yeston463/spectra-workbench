"""P4-02：拼接、VRT 构建、波段堆叠与波段管理。

原验收标准（docs/plan/02_里程碑与验收.md）：
「重叠策略、缺口、NoData、源路径变化及波段顺序正确」

**GDAL 3.13.3 实测结论**（本模块实现方式的依据，不是凭印象）：
- `gdal.BuildVRT(dest, [src...])`：源必须传**列表**；
  重叠区取**后加入**的源（`[A,B]`→B 胜，`[B,A]`→A 胜）——
  即「后加入者优先」，**重叠优先级可由输入排序实现**（first 优先级 = 反转输入）。
- BuildVRT 默认生成 `relativeToVRT="1"` 的**相对路径**源引用。
- `separate=True` → 每个源独立成波段（波段堆叠）。
- `Translate(bandList=[3,1,2])` → 输出波段**严格按给定顺序**。
- 缺口：VRT 空洞为 NoData，Translate 落盘后仍为 NoData。
- **本机无 `gdal_merge.py`** → 带缺口填充的拼接走「VRT + Translate/Warp」路径。

**源路径变化（VRT 相对引用基准）**：
VRT 里的 `relativeToVRT="1"` 路径是**相对于 VRT 文件所在目录**解析的。
但本项目的输出必须先写 staging 再 commit 到目标目录——两处目录不同，
若直接在 staging 构建 VRT，commit 后相对基准就变了、源引用失效。
因此本模块的做法是：
1. 在 staging 构建 VRT（得到尺寸/范围/源引用结构）；
2. 把源引用**重写为相对于最终目标目录**的路径；
3. 写回 staging 的 VRT 文件，再由既有事务 commit 到目标。
这样 VRT 与源保持相对关系时，**整个目录搬移后 VRT 仍可打开**。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

__all__ = [
    "build_vrt",
    "merge_sources",
    "stack_sources",
    "manage_bands",
    "rewrite_vrt_relative_sources",
]


def _gdal() -> Any:
    """取 GDAL 模块；不可用时抛明确错误。"""
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _resolve_priority_order(paths: list[Path], priority: str) -> list[Path]:
    """按重叠优先级排列输入源。

    GDAL BuildVRT 的重叠区取**后加入**的源：
    - "last"（默认）：保持原顺序 → 后面的源在重叠区获胜；
    - "first"：**反转**顺序 → 原先靠前的源在重叠区获胜。
    """
    if priority == "first":
        return list(reversed(paths))
    return list(paths)


def _vrt_kwargs(
    *,
    resolution: str | None,
    src_nodata: float | None,
    vrt_nodata: float | None,
    separate: bool,
    use_relative: bool,
) -> dict[str, Any]:
    """构造 BuildVRT 的 kwargs（只传实际提供的项）。"""
    kwargs: dict[str, Any] = {}
    if resolution:
        kwargs["resolution"] = resolution
    if src_nodata is not None:
        kwargs["srcNodata"] = src_nodata
    if vrt_nodata is not None:
        kwargs["VRTNodata"] = vrt_nodata
    if separate:
        kwargs["separate"] = True
    # 注：GDAL BuildVRT 没有「强制绝对路径」的开关——它自行决定用
    # relativeToVRT="1"（相对）还是 "0"（绝对）。因此相对路径的最终形态
    # 由 `rewrite_vrt_relative_sources` 统一改写（相对**最终目标目录**）；
    # use_relative=False 表示不改写、保留 GDAL 原样（绝对路径）。
    return kwargs


def build_vrt(
    dest: str | Path,
    sources: list[Path],
    *,
    overlap_priority: str = "last",
    resolution: str | None = None,
    src_nodata: float | None = None,
    vrt_nodata: float | None = None,
    separate: bool = False,
    use_relative: bool = True,
) -> dict[str, Any]:
    """构建 VRT，返回其元数据摘要。

    重叠优先级通过**排列输入顺序**实现（GDAL 实测：后加入者优先）。
    """
    gdal = _gdal()
    if not sources:
        raise ValueError("构建 VRT 至少需要一个输入源")

    ordered = _resolve_priority_order(list(sources), overlap_priority)
    kwargs = _vrt_kwargs(
        resolution=resolution,
        src_nodata=src_nodata,
        vrt_nodata=vrt_nodata,
        separate=separate,
        use_relative=use_relative,
    )
    ds = gdal.BuildVRT(str(dest), [str(p) for p in ordered], **kwargs)
    if ds is None:
        raise RuntimeError("BuildVRT 失败")
    info = {
        "width": ds.RasterXSize,
        "height": ds.RasterYSize,
        "bands": ds.RasterCount,
        "geotransform": list(ds.GetGeoTransform()),
        "driver": ds.GetDriver().ShortName,
    }
    ds = None
    return info


def rewrite_vrt_relative_sources(vrt_path: Path, final_dir: Path) -> int:
    """把 VRT 里的源引用重写为**相对于最终目标目录**的路径。

    背景：VRT 的 `relativeToVRT="1"` 路径相对于 **VRT 文件所在目录**解析。
    本项目先写 staging 再 commit 到目标目录，若不重写，commit 后相对基准改变、
    源引用失效。返回被重写的源引用条数。
    """
    if not vrt_path.exists():
        return 0
    text = vrt_path.read_text(encoding="utf-8")
    # 必须覆盖**所有** <SourceFilename> 形态：
    # GDAL 在源需要 `..` 相对表达时会写成 relativeToVRT="0" + 绝对路径，
    # 只匹配 relativeToVRT="1" 会导致 staging 构建的 VRT 完全不被重写。
    pattern = re.compile(
        r'<SourceFilename(?:\s+relativeToVRT="(?P<rel>[01])")?\s*>'
        r"(?P<path>[^<]*)"
        r"</SourceFilename>",
        re.DOTALL,
    )

    def _fix(match: re.Match[str]) -> str:
        raw = (match.group("path") or "").strip()
        if not raw:
            return match.group(0)
        src = Path(raw)
        src = (vrt_path.parent / src).resolve() if not src.is_absolute() else src.resolve()
        try:
            rel = os.path.relpath(src, final_dir.resolve())
        except ValueError:  # pragma: no cover - 跨盘符
            rel = str(src)
        return f'<SourceFilename relativeToVRT="1">{rel.replace(os.sep, "/")}</SourceFilename>'

    rewritten, count = pattern.subn(_fix, text)
    if count and rewritten != text:
        vrt_path.write_text(rewritten, encoding="utf-8")
    return count


def merge_sources(
    dest: str | Path,
    sources: list[Path],
    *,
    overlap_priority: str = "last",
    resolution: str | None = None,
    src_nodata: float | None = None,
    vrt_nodata: float | None = None,
    dst_nodata: float | None = None,
    x_res: float | None = None,
    y_res: float | None = None,
    output_bounds: list[float] | None = None,
    output_type: str | None = None,
    out_format: str = "GTiff",
) -> dict[str, Any]:
    """拼接多源为单幅成果（经 VRT 中间层）。

    本机无 `gdal_merge.py`，故用「VRT + Translate」实现：
    VRT 负责重叠与缺口定义，Translate 负责落盘目标格式与 NoData。
    """
    gdal = _gdal()
    if not sources:
        raise ValueError("拼接至少需要一个输入源")

    ordered = _resolve_priority_order(list(sources), overlap_priority)
    kwargs = _vrt_kwargs(
        resolution=resolution,
        src_nodata=src_nodata,
        vrt_nodata=vrt_nodata,
        separate=False,
        use_relative=True,
    )
    vrt_ds = gdal.BuildVRT("", [str(p) for p in ordered], **kwargs)
    if vrt_ds is None:
        raise RuntimeError("拼接用的 VRT 构建失败")
    try:
        translate_kwargs: dict[str, Any] = {"format": out_format}
        if dst_nodata is not None:
            translate_kwargs["noData"] = dst_nodata
        if x_res is not None:
            translate_kwargs["xRes"] = x_res
        if y_res is not None:
            translate_kwargs["yRes"] = y_res
        if output_bounds is not None:
            translate_kwargs["outputBounds"] = output_bounds
        if output_type:
            translate_kwargs["outputType"] = getattr(gdal, f"GDT_{output_type}")
        ds = gdal.Translate(str(dest), vrt_ds, options=gdal.TranslateOptions(**translate_kwargs))
        if ds is None:
            raise RuntimeError("拼接落盘失败")
        info = {
            "width": ds.RasterXSize,
            "height": ds.RasterYSize,
            "bands": ds.RasterCount,
            "geotransform": list(ds.GetGeoTransform()),
        }
        ds = None
    finally:
        vrt_ds = None
    return info


def stack_sources(
    dest: str | Path,
    sources: list[Path],
    *,
    src_nodata: float | None = None,
    vrt_nodata: float | None = None,
    x_res: float | None = None,
    y_res: float | None = None,
    output_bounds: list[float] | None = None,
    output_type: str | None = None,
    out_format: str = "GTiff",
) -> dict[str, Any]:
    """多源堆叠为多波段成果（每个源一个波段，按给定顺序）。

    实测：`BuildVRT(separate=True)` → 每源独立成波段，且**波段顺序与源顺序一致**。
    """
    gdal = _gdal()
    if not sources:
        raise ValueError("波段堆叠至少需要一个输入源")

    kwargs = _vrt_kwargs(
        resolution=None,
        src_nodata=src_nodata,
        vrt_nodata=vrt_nodata,
        separate=True,
        use_relative=True,
    )
    vrt_ds = gdal.BuildVRT("", [str(p) for p in sources], **kwargs)
    if vrt_ds is None:
        raise RuntimeError("堆叠用的 VRT 构建失败")
    try:
        translate_kwargs: dict[str, Any] = {"format": out_format}
        if x_res is not None:
            translate_kwargs["xRes"] = x_res
        if y_res is not None:
            translate_kwargs["yRes"] = y_res
        if output_bounds is not None:
            translate_kwargs["outputBounds"] = output_bounds
        if output_type:
            translate_kwargs["outputType"] = getattr(gdal, f"GDT_{output_type}")
        ds = gdal.Translate(str(dest), vrt_ds, options=gdal.TranslateOptions(**translate_kwargs))
        if ds is None:
            raise RuntimeError("波段堆叠落盘失败")
        info = {
            "width": ds.RasterXSize,
            "height": ds.RasterYSize,
            "bands": ds.RasterCount,
            "geotransform": list(ds.GetGeoTransform()),
        }
        ds = None
    finally:
        vrt_ds = None
    return info


def manage_bands(
    dest: str | Path,
    source: Path,
    *,
    band_list: list[int] | None = None,
    output_type: str | None = None,
    out_format: str = "GTiff",
) -> dict[str, Any]:
    """波段管理：选择并按给定顺序重排波段。

    实测：`Translate(bandList=[3,1,2])` → 输出波段**严格按给定顺序**。
    """
    gdal = _gdal()
    translate_kwargs: dict[str, Any] = {"format": out_format}
    if band_list:
        translate_kwargs["bandList"] = list(band_list)
    if output_type:
        translate_kwargs["outputType"] = getattr(gdal, f"GDT_{output_type}")
    ds = gdal.Translate(
        str(dest), str(source), options=gdal.TranslateOptions(**translate_kwargs)
    )
    if ds is None:
        raise RuntimeError("波段管理落盘失败")
    info = {
        "width": ds.RasterXSize,
        "height": ds.RasterYSize,
        "bands": ds.RasterCount,
        "geotransform": list(ds.GetGeoTransform()),
    }
    ds = None
    return info


def vrt_summary(vrt_path: Path) -> dict[str, Any]:
    """读取 VRT 的结构摘要（用于验收：源引用是否相对路径、重写了多少条）。"""
    if not vrt_path.exists():
        return {"exists": False}
    text = vrt_path.read_text(encoding="utf-8")
    sources = re.findall(
        r'<SourceFilename(?:\s+relativeToVRT="(?P<rel>[01])")?\s*>(?P<path>[^<]*)</SourceFilename>',
        text,
    )
    # re.findall 返回元组；rel 在索引 0，path 在索引 1
    return {
        "exists": True,
        "source_count": len(sources),
        "relative_count": sum(1 for s in sources if s[0] == "1"),
        "sources": [s[1].strip() for s in sources],
    }
