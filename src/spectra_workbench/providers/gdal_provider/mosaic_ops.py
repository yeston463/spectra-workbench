"""P4-02：拼接、VRT 构建、波段堆叠与波段管理。

原验收标准（docs/plan/02_里程碑与验收.md）：
「重叠策略、缺口、NoData、源路径变化及波段顺序正确」

**GDAL 3.13.3 实测结论**（本模块实现方式的依据，不是凭印象）：
- `gdal.BuildVRT(dest, [src...])`：源必须传**列表**；**支持 callback**（可用于取消/进度）。
- 重叠区取**后加入**的源（`[A,B]`→B 胜，`[B,A]`→A 胜）——
  即「后加入者优先」，**重叠优先级可由输入排序实现**（first 优先级 = 反转输入）。
- BuildVRT 默认生成 `relativeToVRT="1"` 的相对路径源引用；
  源在 VRT 父目录（需要 `..`）时 GDAL 会退化为 `relativeToVRT="0"` 绝对路径。
- `separate=True` → **每个源的所有波段依次进入**（实测 2 波段源 + 3 波段源 → 5 个波段，
  值序为 [5, 1005, 7, 1007, 2007]）；**不是**「每源一个波段」。
  非 separate 时多波段源会被**静默截断**（实测 2+3 波段源 → 仅 2 个波段）。
- `Translate(bandList=[3,1,2])` → 输出波段**严格按给定顺序**。
- 缺口：VRT 空洞为 NoData，Translate 落盘后仍为 NoData。
- **本机无 `gdal_merge.py`** → 带缺口填充的拼接走「VRT + Translate」路径。

**NoData 语义（关键，第 29 轮复核发现）**：
`Translate(noData=X)` **只改 NoData 标签，不重映射无效像元的实际值**——
实测 nodata 变为 0，但缺口像元仍是 -9999 且 mask 标为有效（255），
缺口因此变成有效像元并污染后续统计。
正确做法是：Translate 时**不设** noData（保留 VRT 的标签与实际值，二者一致），
落盘后**按 mask**把无效像元的实际值重映射为目标 NoData 值，再设标签。
实测该路径得到 nodata=0、pixels 缺口=0、mask 缺口=0，三者一致。

**源路径变化（VRT 相对引用基准）**：
VRT 里的 `relativeToVRT="1"` 路径是**相对于 VRT 文件所在目录**解析的。
但本项目的输出必须先写 staging 再 commit 到目标目录——两处目录不同，
若直接在 staging 构建 VRT，commit 后相对基准就变了、源引用失效。
因此本模块的做法是：构建 VRT 后把源引用统一改写为
**相对于最终目标目录**（相对模式）或**绝对路径**（绝对模式）。
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
    "remap_invalid_pixels",
    "detect_valid_value_conflict",
    "set_vrt_source_paths",
    "check_source_compatibility",
    "vrt_summary",
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
    callback: Any = None,
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
    if callback is not None:
        # 实测 BuildVRT 支持 callback（用于取消/进度）
        kwargs["callback"] = callback
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
    callback: Any = None,
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
        callback=callback,
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


# 匹配全部 <SourceFilename> 形态：
# GDAL 在源需要 `..` 相对表达时会写成 relativeToVRT="0" + 绝对路径，
# 只匹配 relativeToVRT="1" 会导致 staging 构建的 VRT 完全不被处理。
_SOURCE_FILENAME_RE = re.compile(
    r'<SourceFilename(?:\s+relativeToVRT="(?P<rel>[01])")?\s*>'
    r"(?P<path>[^<]*)"
    r"</SourceFilename>",
    re.DOTALL,
)


def set_vrt_source_paths(vrt_path: Path, final_dir: Path, *, use_relative: bool) -> int:
    """把 VRT 里的源引用统一设为相对路径或绝对路径。

    背景：VRT 的 `relativeToVRT` 路径相对于 **VRT 文件所在目录**解析。
    本项目先写 staging 再 commit 到目标目录，若不处理，commit 后相对基准改变、
    源引用失效。

    - `use_relative=True`：全部改为 `relativeToVRT="1"` + **相对于最终目标目录**的路径；
      这样 VRT 与源保持相对关系时，整个目录搬移后 VRT 仍可打开。
    - `use_relative=False`：全部改为 `relativeToVRT="0"` + **绝对路径**；
      不强制改成相对（此前「不改写」并不等于引擎一定输出绝对路径，
      同目录时引擎仍会输出相对路径，故必须主动处理）。

    返回被处理的源引用条数。
    """
    if not vrt_path.exists():
        return 0
    text = vrt_path.read_text(encoding="utf-8")

    def _fix(match: re.Match[str]) -> str:
        raw = (match.group("path") or "").strip()
        if not raw:
            return match.group(0)
        src = Path(raw)
        src = (vrt_path.parent / src).resolve() if not src.is_absolute() else src.resolve()
        if use_relative:
            try:
                rel = os.path.relpath(src, final_dir.resolve())
            except ValueError:  # pragma: no cover - 跨盘符
                rel = str(src)
            return f'<SourceFilename relativeToVRT="1">{rel.replace(os.sep, "/")}</SourceFilename>'
        return f'<SourceFilename relativeToVRT="0">{src}</SourceFilename>'

    rewritten, count = _SOURCE_FILENAME_RE.subn(_fix, text)
    if count and rewritten != text:
        vrt_path.write_text(rewritten, encoding="utf-8")
    return count


def detect_valid_value_conflict(path: str | Path, target_value: float) -> dict[str, Any]:
    """检测「输出 NoData 值与有效区真实值冲突」。

    背景：输出 NoData 标签、无效像元实际值、mask 三者一致 ⇒ 标签必须等于无效像元值；
    若有效区中存在恰好等于该值的真实数据，它就会被 NoData 语义判为无效，
    「三者一致」与「保全有效区」不可兼得。

    返回 {"conflict": bool, "count": int, "value": float}
    """
    gdal = _gdal()
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"无法打开成果检查 NoData 冲突：{path}")
    try:
        count = 0
        for i in range(1, ds.RasterCount + 1):
            band = ds.GetRasterBand(i)
            arr = band.ReadAsArray()
            mask = band.GetMaskBand().ReadAsArray()
            # 有效（mask != 0）且值等于 target_value 的像元 —— 会被 NoData 语义误判
            count += int(((arr == target_value) & (mask != 0)).sum())
        return {"conflict": count > 0, "count": count, "value": target_value}
    finally:
        ds = None


def remap_invalid_pixels(path: str | Path, target_value: float) -> dict[str, Any]:
    """把**无效像元的实际值**重映射为目标值，并设置 NoData 标签。

    背景（第 29 轮复核）：`Translate(noData=X)` **只改标签**，
    缺口像元仍是旧值且 mask 标为有效 —— 缺口因此变成有效像元。
    本函数按 **mask**（而非值比较）定位无效像元，
    因此有效区中恰好等于旧 NoData 值的真实数据不会被误伤。

    实测结果：nodata=0、缺口像素=0、mask 缺口=0，三者一致。
    """
    gdal = _gdal()
    ds = gdal.Open(str(path), gdal.GA_Update)
    if ds is None:
        raise RuntimeError(f"无法以更新模式打开成果：{path}")
    try:
        remapped = 0
        for i in range(1, ds.RasterCount + 1):
            band = ds.GetRasterBand(i)
            arr = band.ReadAsArray()
            mask = band.GetMaskBand().ReadAsArray()
            invalid = mask == 0
            if not invalid.any():
                band.SetNoDataValue(target_value)
                continue
            arr[invalid] = target_value
            band.WriteArray(arr)
            band.SetNoDataValue(target_value)
            band.FlushCache()
            remapped += int(invalid.sum())
        return {"remapped": remapped, "nodata": target_value}
    finally:
        ds = None


def check_source_compatibility(
    sources: list[Path],
    *,
    require_same_bands: bool,
    require_single_band: bool = False,
) -> dict[str, Any]:
    """核对多源之间的兼容性（CRS、波段结构）。

    GDAL 的 BuildVRT 对不兼容输入会**静默跳过**（实测混合 CRS 时第二个源
    被跳过但作业仍成功；非 separate 时多波段源被静默截断）。
    本项目默认**不做隐式重投影**，因此不兼容输入必须在处理前被明确拒绝。

    返回 {"compatible": bool, "reason": str | None, "details": {...}}
    """
    gdal = _gdal()
    if not sources:
        return {"compatible": False, "reason": "未提供任何输入源", "details": {}}

    epsgs: list[str | None] = []
    band_counts: list[int] = []
    for path in sources:
        ds = gdal.Open(str(path))
        if ds is None:
            return {"compatible": False, "reason": f"输入无法打开：{path}", "details": {}}
        srs = ds.GetSpatialRef()
        epsgs.append(srs.GetAuthorityCode(None) if srs is not None else None)
        band_counts.append(ds.RasterCount)
        ds = None

    distinct_epsg = sorted({e or "(无 CRS)" for e in epsgs})
    details = {"epsgs": epsgs, "band_counts": band_counts}

    if len(distinct_epsg) > 1:
        return {
            "compatible": False,
            "reason": (
                f"输入源的 CRS 不一致（{', '.join(distinct_epsg)}）："
                "GDAL 会静默跳过不匹配的源，故必须先统一投影；"
                "本能力不做隐式重投影，请先重投影到同一 CRS"
            ),
            "details": details,
        }

    if require_single_band and any(b != 1 for b in band_counts):
        return {
            "compatible": False,
            "reason": f"拼接要求每个输入为单波段，实际波段数 {band_counts}",
            "details": details,
        }
    if require_same_bands and len(set(band_counts)) > 1:
        return {
            "compatible": False,
            "reason": (
                f"输入源的波段结构不一致（{band_counts}）："
                "非 separate 的 VRT 会静默截断多波段源，故必须先统一波段结构"
            ),
            "details": details,
        }

    return {"compatible": True, "reason": None, "details": details}


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
    callback: Any = None,
) -> dict[str, Any]:
    """拼接多源为单幅成果（经 VRT 中间层）。

    本机无 `gdal_merge.py`，故用「VRT + Translate」实现：
    VRT 负责重叠与缺口定义，Translate 负责落盘目标格式。

    `dst_nodata` 语义：**实际重映射**无效像元的值并设置标签
    （不是只改标签——那会让缺口变成有效像元）。
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
        callback=callback,
    )
    vrt_ds = gdal.BuildVRT("", [str(p) for p in ordered], **kwargs)
    if vrt_ds is None:
        raise RuntimeError("拼接用的 VRT 构建失败")
    try:
        translate_kwargs: dict[str, Any] = {"format": out_format}
        # **刻意不设置 noData**：Translate(noData=X) 只改标签、不重映射无效像元，
        # 会让缺口变成有效像元。保持 VRT 的标签与实际值（两者一致），
        # 落盘后再按 mask 统一重映射。
        if x_res is not None:
            translate_kwargs["xRes"] = x_res
        if y_res is not None:
            translate_kwargs["yRes"] = y_res
        if output_bounds is not None:
            translate_kwargs["outputBounds"] = output_bounds
        if output_type:
            translate_kwargs["outputType"] = getattr(gdal, f"GDT_{output_type}")
        if callback is not None:
            translate_kwargs["callback"] = callback
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

    if dst_nodata is not None:
        conflict = detect_valid_value_conflict(dest, dst_nodata)
        if conflict["conflict"]:
            raise ValueError(
                f"输出 NoData 值 {dst_nodata} 与有效区中 {conflict['count']} 个真实像元冲突："
                "输出 NoData 标签必须等于无效像元值，否则这些有效像元会被判为无效。"
                "请改用不会与有效数据冲突的 NoData 值。"
            )
        info["nodata_remap"] = remap_invalid_pixels(dest, dst_nodata)
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
    callback: Any = None,
) -> dict[str, Any]:
    """多源堆叠为多波段成果。

    实测：`separate=True` → **每个源的所有波段依次进入**
    （2 波段源 + 3 波段源 → 5 个波段），波段顺序与源顺序一致。
    """
    gdal = _gdal()
    if not sources:
        raise ValueError("波段堆叠至少需要一个输入源")

    kwargs = _vrt_kwargs(
        resolution=None,
        src_nodata=src_nodata,
        vrt_nodata=vrt_nodata,
        separate=True,
        callback=callback,
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
        if callback is not None:
            translate_kwargs["callback"] = callback
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
    callback: Any = None,
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
    if callback is not None:
        translate_kwargs["callback"] = callback
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
    return {
        "exists": True,
        "source_count": len(sources),
        "relative_count": sum(1 for s in sources if s[0] == "1"),
        "sources": [s[1].strip() for s in sources],
    }
