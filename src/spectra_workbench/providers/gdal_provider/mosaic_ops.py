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
  非 separate 时多波段源会被**静默跳过/截断**（实测 2+3 波段源 → 仅 2 个波段，
  第二个源被整源跳过），因此非 separate 的源必须同波段数且同波段类型。
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
路径改写使用 XML 结构（ElementTree），而不是正则替换字符串，
避免破坏 `&amp;`、`<`、Unicode、空格等转义/编码语义。
"""

from __future__ import annotations

import contextlib
import math
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

__all__ = [
    "IncompatibleSourcesError",
    "NodataConflictError",
    "SourceUsageError",
    "assert_sources_used",
    "assert_vrt_sources",
    "build_vrt",
    "check_source_compatibility",
    "compare_rasters",
    "detect_valid_value_conflict",
    "inspect_raster",
    "manage_bands",
    "merge_sources",
    "read_vrt_sources",
    "remap_invalid_pixels",
    "set_vrt_source_paths",
    "stack_sources",
    "vrt_summary",
]

#: 单块读取上限；NoData 冲突检测/重映射都不整幅读入。
_IO_BLOCK = 1024


class IncompatibleSourcesError(RuntimeError):
    """输入源结构不兼容，继续交给 GDAL 会导致静默跳过/截断。"""


class SourceUsageError(RuntimeError):
    """引擎实际使用的源集合与请求集合不一致（源被跳过）。"""

    def __init__(
        self, message: str, *, missing: list[str] | None = None, unexpected: list[str] | None = None
    ):
        super().__init__(message)
        self.missing = missing or []
        self.unexpected = unexpected or []


class NodataConflictError(ValueError):
    """输出 NoData 值与有效区真实值冲突，无法同时保全二者。"""


def _gdal() -> Any:
    """取 GDAL 模块；不可用时抛明确错误。"""
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


@contextlib.contextmanager
def _capture_gdal_warnings() -> Iterator[list[str]]:
    """捕获 GDAL warning/failure 文本，供输入完整性判定使用。

    GDAL 的 BuildVRT 在跳过源时只发 warning（返回的 dataset 仍非空），
    仅看返回值会误判成功。这里同时收集文本，并在源集合核验失败时随错误返回，
    让用户看到「为什么被跳过」而不是只看到一句 incompatible。
    """
    gdal = _gdal()
    messages: list[str] = []

    def _handler(err_class: int, _err_no: int, msg: str) -> None:
        if err_class in (gdal.CE_Warning, gdal.CE_Failure):
            messages.append(msg)

    gdal.PushErrorHandler(_handler)
    try:
        yield messages
    finally:
        gdal.PopErrorHandler()


# ---------------------------------------------------------------------------
# 源集合核验
# ---------------------------------------------------------------------------
def _norm_path(value: str | Path) -> str:
    try:
        return os.path.normcase(str(Path(value).resolve()))
    except Exception:
        return os.path.normcase(str(value))


def _dataset_file_list(path: str | Path) -> list[str]:
    """返回数据集自身及其 sidecar/依赖（用于允许的源集合）。"""
    gdal = _gdal()
    try:
        ds = gdal.Open(str(path), gdal.GA_ReadOnly)
        if ds is None:
            return [str(path)]
        try:
            return [str(f) for f in (ds.GetFileList() or [str(path)])]
        finally:
            ds = None
    except Exception:
        return [str(path)]


def _allowed_source_set(expected: Iterable[Path]) -> set[str]:
    allowed: set[str] = set()
    for p in expected:
        allowed.add(_norm_path(p))
        for f in _dataset_file_list(p):
            allowed.add(_norm_path(f))
    return allowed


def assert_sources_used(
    used: Iterable[str | Path],
    expected: Iterable[Path],
    *,
    warnings: Iterable[str] | None = None,
    operation: str = "多源处理",
) -> None:
    """核验引擎实际使用的源集合覆盖全部请求源，且没有无关源混入。

    判据分两层：
    1. 每个请求源必须直接出现在 used 中（不能只出现它的 .hdr）；
    2. used 只能包含请求源本身或其依赖/sidecar（ENVI .hdr、VRT 的子源等）。
    """
    expected_list = [Path(p) for p in expected]
    if not expected_list:
        raise SourceUsageError(f"{operation} 未提供任何输入源")

    used_norm = {_norm_path(p) for p in used if str(p).strip()}
    missing = [str(p) for p in expected_list if _norm_path(p) not in used_norm]
    allowed = _allowed_source_set(expected_list)
    unexpected = sorted(u for u in used_norm if u not in allowed)

    if missing or unexpected:
        detail: list[str] = []
        if missing:
            detail.append(f"缺少请求源: {missing}")
        if unexpected:
            detail.append(f"出现未请求源: {unexpected}")
        if warnings:
            detail.append("GDAL 警告: " + " | ".join(str(w) for w in warnings))
        raise SourceUsageError(
            f"{operation} 实际使用的源集合与请求不一致，拒绝把部分源成功当作整体成功；"
            + "；".join(detail),
            missing=missing,
            unexpected=unexpected,
        )


def _used_sources(ds: Any, dest: str | Path | None = None) -> list[str]:
    """读取引擎实际引用的源路径（不把 dest 自己算作源）。"""
    raw = list(ds.GetFileList() or [])
    dest_norm = _norm_path(dest) if dest else None
    out: list[str] = []
    for item in raw:
        text = str(item)
        if dest_norm and _norm_path(text) == dest_norm:
            continue
        out.append(text)
    return out


# ---------------------------------------------------------------------------
# VRT XML 路径处理
# ---------------------------------------------------------------------------
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_DATASET_PREFIX_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]*:"')


def _is_filesystem_path(raw: str) -> bool:
    """区分普通文件路径与 GDAL 数据集引用（NETCDF:...、/vsizip/... 等）。"""
    if not raw:
        return False
    if _DRIVE_RE.match(raw):
        return True
    if raw.startswith(("\\\\", "//")):
        return True
    if raw.startswith("/vsi"):
        return False
    if _URL_RE.match(raw):
        return False
    # POSIX 绝对路径与普通相对路径都交给 Path 处理；数据集前缀不是文件路径
    return not bool(_DATASET_PREFIX_RE.match(raw))


def _resolve_vrt_source(raw: str, *, vrt_dir: Path, relative: bool) -> Path | None:
    if not _is_filesystem_path(raw):
        return None
    p = Path(raw)
    if relative:
        return (vrt_dir / p).resolve()
    if p.is_absolute():
        return p.resolve()
    # relativeToVRT=0 但引擎写了相对路径：GDAL 按进程 cwd 解释。
    return p.resolve()


def _as_posix(path: Path) -> str:
    return path.as_posix()


def set_vrt_source_paths(vrt_path: Path, final_dir: Path, *, use_relative: bool) -> int:
    """把 VRT 里的源引用统一设为相对路径或绝对路径。

    背景：VRT 的 `relativeToVRT` 路径相对于 **VRT 文件所在目录**解析。
    本项目先写 staging 再 commit 到目标目录，若不处理，commit 后相对基准改变、
    源引用失效。

    - `use_relative=True`：全部改为 `relativeToVRT="1"` + **相对于最终目标目录**的路径；
      这样 VRT 与源保持相对关系时，整个目录搬移后 VRT 仍可打开。
      跨盘符无法表达相对路径时退化为 `relativeToVRT="0"` + 绝对路径，
      而不是写出一个语义错误的「相对」绝对路径。
    - `use_relative=False`：全部改为 `relativeToVRT="0"` + **绝对路径**；
      不强制改成相对（此前「不改写」并不等于引擎一定输出绝对路径，
      同目录时引擎仍会输出相对路径，故必须主动处理）。

    使用 XML 结构而不是正则：保留 `&amp;`、`&lt;` 等转义、Unicode 与空格，
    并保留 GDAL 数据集引用（`NETCDF:"x.nc":Band1`、`/vsizip/...`）原样。

    返回被处理的源引用条数。
    """
    if not vrt_path.exists():
        return 0
    tree = ET.parse(vrt_path)
    root = tree.getroot()
    count = 0
    for node in root.iter("SourceFilename"):
        raw = (node.text or "").strip()
        if not raw:
            continue
        relative = (node.get("relativeToVRT") or "0").strip() == "1"
        src = _resolve_vrt_source(raw, vrt_dir=vrt_path.parent, relative=relative)
        if src is None:
            # 数据集引用不是文件路径：只能保留原样，并明确标记为非相对。
            node.set("relativeToVRT", "0")
            node.text = raw
            count += 1
            continue
        if use_relative:
            try:
                rel = os.path.relpath(src, final_dir.resolve())
            except ValueError:  # pragma: no cover - 跨盘符
                node.set("relativeToVRT", "0")
                node.text = _as_posix(src)
            else:
                node.set("relativeToVRT", "1")
                node.text = Path(rel).as_posix()
        else:
            node.set("relativeToVRT", "0")
            node.text = _as_posix(src)
        count += 1
    tree.write(vrt_path, encoding="utf-8", xml_declaration=True)
    # 写回后立即解析一次，确保 XML 仍然合法（不让坏 XML 进入 commit）
    ET.parse(vrt_path)
    return count


def read_vrt_sources(vrt_path: Path, *, base_dir: Path | None = None) -> list[dict[str, Any]]:
    """解析 VRT 的 SourceFilename（不依赖正则，正确还原 XML 转义）。"""
    tree = ET.parse(vrt_path)
    base = (base_dir or vrt_path.parent).resolve()
    out: list[dict[str, Any]] = []
    for node in tree.getroot().iter("SourceFilename"):
        raw = (node.text or "").strip()
        rel = (node.get("relativeToVRT") or "0").strip() == "1"
        resolved = _resolve_vrt_source(raw, vrt_dir=base, relative=rel)
        out.append(
            {
                "text": raw,
                "relative": rel,
                "resolved": str(resolved) if resolved is not None else None,
                "exists": bool(resolved is not None and resolved.exists()),
            }
        )
    return out


def assert_vrt_sources(
    vrt_path: Path,
    final_dir: Path,
    expected_sources: Iterable[Path],
    *,
    require_relative: bool | None = None,
    operation: str = "VRT 成果",
) -> dict[str, Any]:
    """按**最终 VRT 所在目录**解析并核验源引用。

    VRT 先写 staging、后 commit 到最终目录；若只从 staging 打开（或只看
    SourceFilename 文本），无法证明 commit 后仍能解析。这里显式以 final_dir
    为基准解析，逐个检查源存在，并与请求源集合比对。
    """
    entries = read_vrt_sources(vrt_path, base_dir=final_dir)
    plain = [e for e in entries if e["resolved"] is not None]
    missing = [e["text"] for e in plain if not e["exists"]]
    if missing:
        raise SourceUsageError(f"{operation} 的 VRT 源引用按最终目录无法解析: {missing}")
    used = [Path(str(e["resolved"])) for e in plain]
    assert_sources_used(used, expected_sources, operation=operation)

    if require_relative is True:
        bad = [e["text"] for e in plain if not e["relative"] or Path(e["text"]).is_absolute()]
        if bad:
            raise SourceUsageError(f"{operation} 要求相对源引用，但以下引用不是相对路径: {bad}")
    elif require_relative is False:
        bad = [e["text"] for e in plain if e["relative"] or not Path(e["text"]).is_absolute()]
        if bad:
            raise SourceUsageError(f"{operation} 要求绝对源引用，但以下引用不是绝对路径: {bad}")
    return {
        "source_count": len(entries),
        "plain_path_count": len(plain),
        "relative_count": sum(1 for e in entries if e["relative"]),
        "sources": [e["text"] for e in entries],
    }


def vrt_summary(vrt_path: Path) -> dict[str, Any]:
    """读取 VRT 的结构摘要（用于验收：源引用是否相对路径、重写了多少条）。"""
    if not vrt_path.exists():
        return {"exists": False}
    entries = read_vrt_sources(vrt_path)
    return {
        "exists": True,
        "source_count": len(entries),
        "relative_count": sum(1 for e in entries if e["relative"]),
        "sources": [e["text"] for e in entries],
        "resolved_sources": [e["resolved"] for e in entries],
    }


# ---------------------------------------------------------------------------
# NoData / mask
# ---------------------------------------------------------------------------
def _band_is_alpha(band: Any) -> bool:
    gdal = _gdal()
    try:
        return int(band.GetColorInterpretation()) == int(gdal.GCI_AlphaBand)
    except Exception:
        return False


def _alpha_band_index(ds: Any) -> int | None:
    for i in range(1, ds.RasterCount + 1):
        if _band_is_alpha(ds.GetRasterBand(i)):
            return i
    return None


def _iter_blocks(width: int, height: int) -> Iterator[tuple[int, int, int, int]]:
    for y in range(0, height, _IO_BLOCK):
        ys = min(_IO_BLOCK, height - y)
        for x in range(0, width, _IO_BLOCK):
            xs = min(_IO_BLOCK, width - x)
            yield x, y, xs, ys


def detect_valid_value_conflict(path: str | Path, target_value: float) -> dict[str, Any]:
    """检测「输出 NoData 值与有效区真实值冲突」。

    背景：输出 NoData 标签、无效像元实际值、mask 三者一致 ⇒ 标签必须等于无效像元值；
    若有效区中存在恰好等于该值的真实数据，它就会被 NoData 语义判为无效，
    「三者一致」与「保全有效区」不可兼得。

    - 按 **mask** 判断有效性（不是按值比较），有效区中的真实 0 不会被当成无效；
    - 有 alpha 波段时，alpha==0 也是无效（即使驱动给 color band 的 mask 仍是
      ALL_VALID——实测 GTiff 的 alpha 不自动生成 mask）；
    - 跳过 alpha 波段本身：0 是透明度，不是「数据值 0」；
    - 分块读取，避免整幅影像进内存。

    返回 {"conflict": bool, "count": int, "value": float}
    """
    gdal = _gdal()
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"无法打开成果检查 NoData 冲突：{path}")
    try:
        alpha_idx = _alpha_band_index(ds)
        alpha_band = ds.GetRasterBand(alpha_idx) if alpha_idx is not None else None
        count = 0
        for i in range(1, ds.RasterCount + 1):
            if i == alpha_idx:
                continue
            band = ds.GetRasterBand(i)
            mask_band = band.GetMaskBand()
            for x, y, xs, ys in _iter_blocks(ds.RasterXSize, ds.RasterYSize):
                arr = band.ReadAsArray(x, y, xs, ys)
                invalid = mask_band.ReadAsArray(x, y, xs, ys) == 0
                if alpha_band is not None:
                    invalid = invalid | (alpha_band.ReadAsArray(x, y, xs, ys) == 0)
                count += int(((arr == target_value) & (~invalid)).sum())
        return {"conflict": count > 0, "count": count, "value": target_value}
    finally:
        ds = None


def _write_alpha_mask(ds: Any, alpha_idx: int) -> None:
    """把 alpha!=0 落成显式 dataset mask，使 NoData 标签与 mask 一致。

    GDAL 不会仅凭 GCI_AlphaBand 就自动把 color band 的 mask 设为 alpha
    （实测源与 Translate 输出的 mask flags 都是 GMF_ALL_VALID）。因此当用户
    请求 dstNodata 且成果带 alpha 时，必须显式写 mask，否则 alpha=0 的缺口
    会同时出现「alpha 透明」和「color mask 有效」两套矛盾语义。
    """
    gdal = _gdal()
    first = ds.GetRasterBand(1)
    flags = int(first.GetMaskFlags())
    if flags & gdal.GMF_ALPHA:
        # 驱动已经把 alpha 作为 mask：不要再写入（否则会把半透明 alpha 值
        # 改成 0/255，破坏透明度）。alpha==0 本身已由 mask 语义判为无效。
        return
    mask_band = first.GetMaskBand()
    had_explicit_mask = bool(flags & gdal.GMF_PER_DATASET)
    if not had_explicit_mask:
        first.CreateMaskBand(gdal.GMF_PER_DATASET)
        mask_band = first.GetMaskBand()
    alpha_band = ds.GetRasterBand(alpha_idx)
    for x, y, xs, ys in _iter_blocks(ds.RasterXSize, ds.RasterYSize):
        alpha = alpha_band.ReadAsArray(x, y, xs, ys)
        combined = alpha != 0
        if had_explicit_mask:
            # 已有显式 mask 时不能把它的无效区重新标成有效：取交集。
            combined = combined & (mask_band.ReadAsArray(x, y, xs, ys) != 0)
        out = combined.astype("uint8")
        out *= 255
        mask_band.WriteArray(out, x, y)
    mask_band.FlushCache()


def remap_invalid_pixels(path: str | Path, target_value: float) -> dict[str, Any]:
    """把**无效像元的实际值**重映射为目标值，并设置 NoData 标签。

    背景（第 29 轮复核）：`Translate(noData=X)` **只改标签**，
    缺口像元仍是旧值且 mask 标为有效 —— 缺口因此变成有效像元。
    本函数按 **mask**（而非值比较）定位无效像元，
    因此有效区中恰好等于旧 NoData 值的真实数据不会被误伤。

    - 有 alpha 波段时，alpha==0 参与无效判定；color band 重映射后写显式
      dataset mask，alpha 波段本身不设 NoData、不改值；
    - 分块读写，保留有效区原始像元；
    - 实测结果：nodata=0、缺口像素=0、mask 缺口=0，三者一致。
    """
    gdal = _gdal()
    ds = gdal.Open(str(path), gdal.GA_Update)
    if ds is None:
        raise RuntimeError(f"无法以更新模式打开成果：{path}")
    try:
        alpha_idx = _alpha_band_index(ds)
        alpha_band = ds.GetRasterBand(alpha_idx) if alpha_idx is not None else None
        remapped = 0
        for i in range(1, ds.RasterCount + 1):
            if i == alpha_idx:
                continue
            band = ds.GetRasterBand(i)
            mask_band = band.GetMaskBand()
            for x, y, xs, ys in _iter_blocks(ds.RasterXSize, ds.RasterYSize):
                invalid = mask_band.ReadAsArray(x, y, xs, ys) == 0
                if alpha_band is not None:
                    invalid = invalid | (alpha_band.ReadAsArray(x, y, xs, ys) == 0)
                if not invalid.any():
                    continue
                arr = band.ReadAsArray(x, y, xs, ys)
                arr[invalid] = target_value
                band.WriteArray(arr, x, y)
                remapped += int(invalid.sum())
            band.SetNoDataValue(target_value)
            band.FlushCache()
        if alpha_idx is not None:
            _write_alpha_mask(ds, alpha_idx)
        return {"remapped": remapped, "nodata": target_value}
    finally:
        ds = None


# ---------------------------------------------------------------------------
# 源兼容性
# ---------------------------------------------------------------------------
def _gt_or_none(ds: Any) -> tuple[float, ...] | None:
    try:
        gt = ds.GetGeoTransform(can_return_null=True)
    except TypeError:  # pragma: no cover - 旧版 GDAL
        gt = ds.GetGeoTransform()
    if gt is None:
        return None
    return tuple(float(v) for v in gt[:6])


def check_source_compatibility(
    sources: list[Path],
    *,
    require_same_bands: bool,
    require_single_band: bool = False,
    allow_ungeoreferenced: bool = False,
) -> dict[str, Any]:
    """核对多源之间的兼容性（CRS、地理参考、波段结构与类型）。

    GDAL 的 BuildVRT 对不兼容输入会**静默跳过**（实测混合 CRS 时第二个源
    被跳过但作业仍成功；非 separate 时波段数不同的源被整源跳过；
    波段类型不同时输出按第一个源的类型截断）。
    本项目默认**不做隐式重投影**，因此不兼容输入必须在处理前被明确拒绝。

    返回 {"compatible": bool, "reason": str | None, "details": {...}}
    """
    gdal = _gdal()
    if not sources:
        return {"compatible": False, "reason": "未提供任何输入源", "details": {}}

    srs_list: list[Any] = []
    epsgs: list[str | None] = []
    band_counts: list[int] = []
    band_types: list[list[str]] = []
    geotransforms: list[tuple[float, ...] | None] = []
    for path in sources:
        try:
            ds = gdal.Open(str(path))
        except Exception as exc:
            return {
                "compatible": False,
                "reason": f"输入无法打开：{path}（{exc}）",
                "details": {},
            }
        if ds is None:
            return {"compatible": False, "reason": f"输入无法打开：{path}", "details": {}}
        try:
            if ds.RasterCount <= 0:
                return {
                    "compatible": False,
                    "reason": f"输入不是栅格数据集或没有波段：{path}",
                    "details": {},
                }
            if ds.RasterXSize <= 0 or ds.RasterYSize <= 0:
                return {
                    "compatible": False,
                    "reason": f"输入尺寸无效：{path} ({ds.RasterXSize}x{ds.RasterYSize})",
                    "details": {},
                }
            srs = ds.GetSpatialRef()
            srs_list.append(srs)
            epsgs.append(srs.GetAuthorityCode(None) if srs is not None else None)
            band_counts.append(ds.RasterCount)
            band_types.append(
                [
                    gdal.GetDataTypeName(ds.GetRasterBand(i).DataType)
                    for i in range(1, ds.RasterCount + 1)
                ]
            )
            geotransforms.append(_gt_or_none(ds))
        except Exception as exc:
            return {
                "compatible": False,
                "reason": f"读取输入元数据失败：{path}（{exc}）",
                "details": {},
            }
        finally:
            ds = None

    distinct_epsg = sorted({e or "(无 CRS)" for e in epsgs})
    details = {
        "epsgs": epsgs,
        "band_counts": band_counts,
        "band_types": band_types,
        "has_geotransform": [gt is not None for gt in geotransforms],
    }

    # CRS：用 IsSame 而不是仅比较 EPSG 字符串；同 EPSG 但轴/定义不同的 SRS
    # 也可能被 BuildVRT 判为 heterogeneous projection。
    first_srs = srs_list[0]
    for idx, srs in enumerate(srs_list[1:], start=1):
        if (first_srs is None) != (srs is None):
            return {
                "compatible": False,
                "reason": (
                    f"输入源的地理参考不一致（源 1 {'有' if first_srs is not None else '无'} CRS，"
                    f"源 {idx + 1} {'有' if srs is not None else '无'} CRS）"
                ),
                "details": details,
            }
        if first_srs is not None and srs is not None:
            try:
                same = bool(first_srs.IsSame(srs))
            except Exception:
                same = first_srs.ExportToWkt() == srs.ExportToWkt()
            if not same:
                return {
                    "compatible": False,
                    "reason": (
                        f"输入源的 CRS 不一致（{', '.join(distinct_epsg)}）："
                        "GDAL 会静默跳过不匹配的源，故必须先统一投影；"
                        "本能力不做隐式重投影，请先重投影到同一 CRS"
                    ),
                    "details": details,
                }

    if any(gt is None for gt in geotransforms) and not allow_ungeoreferenced:
        return {
            "compatible": False,
            "reason": "输入源缺少有效 GeoTransform（无地理参考）："
            "非 separate 的 BuildVRT 会跳过无地理参考影像，不能把部分源成功当作整体成功；"
            "请先为源写入地理参考，或使用 separate=True 的独立波段堆叠",
            "details": details,
        }

    if require_single_band and any(b != 1 for b in band_counts):
        return {
            "compatible": False,
            "reason": f"拼接要求每个输入为单波段，实际波段数 {band_counts}",
            "details": details,
        }
    if require_same_bands:
        if len(set(band_counts)) > 1:
            return {
                "compatible": False,
                "reason": (
                    f"输入源的波段结构不一致（{band_counts}）："
                    "非 separate 的 VRT 会静默跳过/截断多波段源，故必须先统一波段结构"
                ),
                "details": details,
            }
        first_types = band_types[0]
        if any(types != first_types for types in band_types[1:]):
            return {
                "compatible": False,
                "reason": (
                    f"输入源的波段数据类型不一致（{band_types}）："
                    "BuildVRT 会按第一个源的类型截断其他源，故必须先统一数据类型"
                ),
                "details": details,
            }

    return {"compatible": True, "reason": None, "details": details}


# ---------------------------------------------------------------------------
# 构建 / 拼接 / 堆叠 / 波段管理
# ---------------------------------------------------------------------------
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
    """构建 VRT，返回其元数据摘要与**实际使用的源集合**。

    重叠优先级通过**排列输入顺序**实现（GDAL 实测：后加入者优先）。
    BuildVRT 跳过源时仍返回非空 dataset；这里用 GetFileList() 核验实际
    源集合，避免把部分源成功当作整体成功。
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
    with _capture_gdal_warnings() as warnings:
        ds = gdal.BuildVRT(str(dest), [str(p) for p in ordered], **kwargs)
        if ds is None:
            raise RuntimeError("BuildVRT 失败")
        try:
            used = _used_sources(ds, dest)
            info = {
                "width": ds.RasterXSize,
                "height": ds.RasterYSize,
                "bands": ds.RasterCount,
                "geotransform": list(ds.GetGeoTransform()),
                "driver": ds.GetDriver().ShortName,
                "sources_used": used,
                "warnings": list(warnings),
            }
        finally:
            ds = None

    assert_sources_used(used, sources, warnings=warnings, operation="BuildVRT")
    return info


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
    warnings: list[str] = []
    with _capture_gdal_warnings() as warn_buf:
        vrt_ds = gdal.BuildVRT("", [str(p) for p in ordered], **kwargs)
        if vrt_ds is None:
            raise RuntimeError("拼接用的 VRT 构建失败")
        try:
            used = _used_sources(vrt_ds)
            assert_sources_used(used, sources, warnings=warn_buf, operation="拼接 VRT")
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
            ds = gdal.Translate(
                str(dest), vrt_ds, options=gdal.TranslateOptions(**translate_kwargs)
            )
            if ds is None:
                raise RuntimeError("拼接落盘失败")
            try:
                info = {
                    "width": ds.RasterXSize,
                    "height": ds.RasterYSize,
                    "bands": ds.RasterCount,
                    "geotransform": list(ds.GetGeoTransform()),
                    "driver": ds.GetDriver().ShortName,
                    "sources_used": used,
                    "warnings": list(warn_buf),
                }
            finally:
                ds = None
        finally:
            vrt_ds = None
    warnings = list(warn_buf)

    if dst_nodata is not None:
        conflict = detect_valid_value_conflict(dest, dst_nodata)
        if conflict["conflict"]:
            with contextlib.suppress(OSError):
                Path(dest).unlink()
            raise NodataConflictError(
                f"输出 NoData 值 {dst_nodata} 与有效区中 {conflict['count']} 个真实像元冲突："
                "输出 NoData 标签必须等于无效像元值，否则这些有效像元会被判为无效。"
                "请改用不会与有效数据冲突的 NoData 值。"
            )
        info["nodata_remap"] = remap_invalid_pixels(dest, dst_nodata)
    info["warnings"] = warnings
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
    同时核验实际使用的源集合，防止 GDAL 静默跳过某个源。
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
    with _capture_gdal_warnings() as warn_buf:
        vrt_ds = gdal.BuildVRT("", [str(p) for p in sources], **kwargs)
        if vrt_ds is None:
            raise RuntimeError("堆叠用的 VRT 构建失败")
        try:
            used = _used_sources(vrt_ds)
            assert_sources_used(used, sources, warnings=warn_buf, operation="波段堆叠 VRT")
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
            ds = gdal.Translate(
                str(dest), vrt_ds, options=gdal.TranslateOptions(**translate_kwargs)
            )
            if ds is None:
                raise RuntimeError("波段堆叠落盘失败")
            try:
                info = {
                    "width": ds.RasterXSize,
                    "height": ds.RasterYSize,
                    "bands": ds.RasterCount,
                    "geotransform": list(ds.GetGeoTransform()),
                    "driver": ds.GetDriver().ShortName,
                    "sources_used": used,
                    "warnings": list(warn_buf),
                }
            finally:
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
    with _capture_gdal_warnings() as warn_buf:
        ds = gdal.Translate(
            str(dest), str(source), options=gdal.TranslateOptions(**translate_kwargs)
        )
        if ds is None:
            raise RuntimeError("波段管理落盘失败")
        try:
            info = {
                "width": ds.RasterXSize,
                "height": ds.RasterYSize,
                "bands": ds.RasterCount,
                "geotransform": list(ds.GetGeoTransform()),
                "driver": ds.GetDriver().ShortName,
                "warnings": list(warn_buf),
            }
        finally:
            ds = None
    return info


# ---------------------------------------------------------------------------
# 成果快速检查（供 adapter 在提交前后做真实重开校验）
# ---------------------------------------------------------------------------
def _sample_positions(width: int, height: int) -> list[tuple[int, int]]:
    xs = sorted({0, max(0, width - 1), width // 2})
    ys = sorted({0, max(0, height - 1), height // 2})
    return [(x, y) for y in ys for x in xs]


def _same_value(a: Any, b: Any) -> bool:
    try:
        if isinstance(a, float) and isinstance(b, float):
            if math.isnan(a) and math.isnan(b):
                return True
            return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
        return a == b
    except Exception:
        return False


def inspect_raster(path: str | Path, *, sample: bool = True) -> dict[str, Any]:
    """真实重开一个成果并读取尺寸/波段/驱动/NoData 与必要像元。

    返回值中的 `samples` 是**实际读出的像元**，不是处理函数的返回值。
    adapter 用它在 commit 前后对比，证明不是只检查文件存在/非空。
    """
    gdal = _gdal()
    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"成果无法重开：{path}")
    try:
        width = int(ds.RasterXSize)
        height = int(ds.RasterYSize)
        bands = int(ds.RasterCount)
        if width <= 0 or height <= 0 or bands <= 0:
            raise RuntimeError(f"成果尺寸/波段无效：{path} ({width}x{height}, {bands} bands)")
        driver = ds.GetDriver().ShortName
        types: list[str] = []
        nodata: list[float | None] = []
        color_interpretations: list[int] = []
        mask_flags: list[int] = []
        samples: list[dict[str, Any]] = []
        mask_samples: list[list[int]] = []
        positions = _sample_positions(width, height) if sample else []
        for i in range(1, bands + 1):
            band = ds.GetRasterBand(i)
            types.append(gdal.GetDataTypeName(band.DataType))
            nodata.append(band.GetNoDataValue())
            color_interpretations.append(int(band.GetColorInterpretation()))
            mask_flags.append(int(band.GetMaskFlags()))
            if sample:
                values: list[Any] = []
                masks: list[int] = []
                for x, y in positions:
                    arr = band.ReadAsArray(x, y, 1, 1)
                    if arr is None:
                        raise RuntimeError(f"成果像元读取失败：{path} 波段 {i} ({x},{y})")
                    raw_value = arr[0, 0]
                    # CFloat32 等复数据类型不能直接 float()；保留 complex 供比较
                    try:
                        value: Any = (
                            complex(raw_value)
                            if isinstance(raw_value, complex)
                            else float(raw_value)
                        )
                    except (TypeError, ValueError):
                        value = raw_value.item() if hasattr(raw_value, "item") else raw_value
                    values.append(value)
                    mask_arr = band.GetMaskBand().ReadAsArray(x, y, 1, 1)
                    masks.append(int(mask_arr[0, 0]))
                samples.append({"positions": positions, "values": values})
                mask_samples.append(masks)
        return {
            "driver": driver,
            "width": width,
            "height": height,
            "bands": bands,
            "types": types,
            "nodata": nodata,
            "color_interpretations": color_interpretations,
            "mask_flags": mask_flags,
            "samples": samples,
            "mask_samples": mask_samples,
        }
    finally:
        ds = None


def compare_rasters(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    """比较提交前后两次真实重开的驱动/尺寸/波段/类型/NoData/像元/mask。"""
    for key in ("driver", "width", "height", "bands", "types"):
        if expected.get(key) != actual.get(key):
            raise RuntimeError(
                f"提交前后成果 {key} 不一致：期望 {expected.get(key)!r}，实际 {actual.get(key)!r}"
            )
    exp_nodata = expected.get("nodata") or []
    act_nodata = actual.get("nodata") or []
    if len(exp_nodata) != len(act_nodata) or not all(
        _same_value(a, b) for a, b in zip(exp_nodata, act_nodata, strict=False)
    ):
        raise RuntimeError(f"提交前后成果 NoData 不一致：期望 {exp_nodata!r}，实际 {act_nodata!r}")
    exp_samples = expected.get("samples") or []
    act_samples = actual.get("samples") or []
    if len(exp_samples) != len(act_samples):
        raise RuntimeError("提交前后成果波段数不一致（像元样本数量不同）")
    for band_idx, (exp, act) in enumerate(zip(exp_samples, act_samples, strict=False), start=1):
        ev = exp.get("values", [])
        av = act.get("values", [])
        if len(ev) != len(av) or not all(_same_value(a, b) for a, b in zip(ev, av, strict=False)):
            raise RuntimeError(
                f"提交前后成果波段 {band_idx} 必要像元不一致：期望 {ev!r}，实际 {av!r}"
            )
    exp_masks = expected.get("mask_samples") or []
    act_masks = actual.get("mask_samples") or []
    if exp_masks != act_masks:
        raise RuntimeError("提交前后成果 mask 不一致")
