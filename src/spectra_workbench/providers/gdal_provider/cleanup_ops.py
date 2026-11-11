"""P9-02 栅格清理 / 邻近 / 视域五项原生算法的 Worker 侧执行器。

覆盖 ``gdal:sieve`` / ``gdal:fillnodata`` / ``gdal:nearblack`` /
``gdal:proximity`` / ``gdal:viewshed``：全部直接调用 GDAL 原生 API
（``gdal.SieveFilter`` / ``gdal.FillNodata`` / ``gdal.Nearblack`` /
``gdal.ComputeProximity`` / ``gdal.ViewshedGenerate``），不重新发明算法。

语义约束
--------
- 每项能力输出**单个栅格文件**（无 sidecar），沿用共享的 staging → 重开校验 → 提交事务；
- 非 GTiff 目标格式先写临时 GTiff 再 ``Translate``（与 rasterize/grid 一致）；
- ``sieve`` 仅整型分类栅格；``nearblack`` 仅 Byte；复数波段一律拒绝；
- ``fillnodata`` 必须有 NoData 定义（波段标签或显式 ``srcNodata``），
  否则明确报错，绝不「假装填补」；
- ``proximity`` 若目标像元数为 0 明确拒绝，不输出全无效的假成果；
- ``viewshed`` 仅 north-up 栅格，观察点必须落在栅格范围内；
  ``viewDistance`` 会按 GDAL 语义同时裁剪输出范围。

Windows 缓存目录
----------------
实测 GDAL ``FillNodata`` 在中文用户名的系统 TEMP 路径下抛
``UnicodeDecodeError``（路径被按 UTF-8 解码）。本模块在调用原生算法期间把
``CPL_TMPDIR`` 临时指向纯 ASCII 目录，调用后恢复原配置。
"""

from __future__ import annotations

import contextlib
import math
import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

__all__ = [
    "CLEANUP_CAPABILITIES",
    "CleanupError",
    "ascii_scratch_dir",
    "plan_cleanup_output",
    "run_cleanup",
]

#: 本模块负责的能力集合（与 CONNECTED_CAPABILITIES 的子集一致）。
CLEANUP_CAPABILITIES: frozenset[str] = frozenset(
    {
        "gdal:sieve",
        "gdal:fillnodata",
        "gdal:nearblack",
        "gdal:proximity",
        "gdal:viewshed",
    }
)

_SIEVE_TYPES = ("Byte", "UInt16", "Int16", "UInt32", "Int32")
_CHUNK_ROWS = 512
_PROXIMITY_NODATA = -9999.0
_VIEWSHED_CURVATURE = 0.85714  # GDAL gdal_viewshed 默认大气折射修正系数


class CleanupError(ValueError):
    """清理/邻近/视域算法的输入或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（栅格清理阶段）")


def ascii_scratch_dir() -> Path | None:
    """返回一个可写的纯 ASCII 临时目录；找不到则返回 None（不强行改配置）。"""
    candidates: list[Path] = [Path(tempfile.gettempdir())]
    public = os.environ.get("PUBLIC")
    if public:
        candidates.append(Path(public) / "spectra-workbench-tmp")
    drive = os.environ.get("SYSTEMDRIVE", "C:")
    candidates.append(Path(drive + os.sep) / "spectra-workbench-tmp")
    for cand in candidates:
        if not str(cand).isascii():
            continue
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / f".probe-{os.getpid()}"
            probe.write_bytes(b"x")
            probe.unlink()
        except OSError:
            continue
        return cand
    return None


@contextlib.contextmanager
def _scoped_cpl_tmpdir(gdal: Any) -> Iterator[None]:
    """调用原生算法期间把 CPL_TMPDIR 指向 ASCII 目录，结束后恢复。"""
    scratch = ascii_scratch_dir()
    previous = gdal.GetConfigOption("CPL_TMPDIR")
    if scratch is not None:
        gdal.SetConfigOption("CPL_TMPDIR", str(scratch))
    try:
        yield
    finally:
        gdal.SetConfigOption("CPL_TMPDIR", previous)


def _open_source(gdal: Any, source: Path) -> Any:
    if not source.exists():
        raise CleanupError(f"输入栅格不存在: {source}")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise CleanupError(f"无法打开输入栅格: {source}")
    return ds


def _pick_band(gdal: Any, ds: Any, params: dict[str, Any]) -> tuple[int, Any]:
    index = int(params.get("band") or 1)
    if index < 1 or index > int(ds.RasterCount):
        raise CleanupError(f"band={index} 超出范围（数据集共 {ds.RasterCount} 个波段）")
    band = ds.GetRasterBand(index)
    if gdal.DataTypeIsComplex(band.DataType):
        raise CleanupError(f"波段 {index} 是复数类型；本算法仅支持实数波段")
    return index, band


def plan_cleanup_output(capability_id: str, source: Path, params: dict[str, Any]) -> dict[str, Any]:
    """在写盘前推断输出 dtype/波段数/NoData，供格式矩阵预检使用。"""
    if capability_id not in CLEANUP_CAPABILITIES:
        raise CleanupError(f"未知清理能力 {capability_id!r}")
    gdal = _gdal()
    ds = _open_source(gdal, source)
    try:
        if capability_id == "gdal:nearblack":
            band = ds.GetRasterBand(1)
            if gdal.DataTypeIsComplex(band.DataType):
                raise CleanupError("nearblack 不支持复数波段")
            dtype = gdal.GetDataTypeName(band.DataType)
            if dtype != "Byte":
                raise CleanupError(f"nearblack 仅支持 Byte 影像，输入为 {dtype}")
            return {"dtype": "Byte", "bands": int(ds.RasterCount), "has_nodata": False}
        index, band = _pick_band(gdal, ds, params)
        dtype = gdal.GetDataTypeName(band.DataType)
        if capability_id == "gdal:sieve":
            if dtype not in _SIEVE_TYPES:
                raise CleanupError(
                    f"sieve 仅支持整型分类栅格（{', '.join(_SIEVE_TYPES)}），输入为 {dtype}"
                )
            return {
                "dtype": dtype,
                "bands": 1,
                "has_nodata": band.GetNoDataValue() is not None,
            }
        if capability_id == "gdal:fillnodata":
            label = band.GetNoDataValue()
            if label is None and params.get("srcNodata") is None:
                raise CleanupError(
                    f"fillnodata 需要 NoData 定义：波段 {index} 无 NoData 标签，"
                    "请显式给出 srcNodata"
                )
            return {"dtype": dtype, "bands": 1, "has_nodata": True}
        if capability_id == "gdal:proximity":
            return {"dtype": "Float32", "bands": 1, "has_nodata": True}
        # viewshed
        gt = ds.GetGeoTransform()
        if not gt or abs(gt[2]) > 0.0 or abs(gt[4]) > 0.0:
            raise CleanupError("viewshed 仅支持无旋转（north-up）且有地理变换的栅格")
        return {"dtype": "Byte", "bands": 1, "has_nodata": False}
    finally:
        ds = None


def _new_gtiff(
    gdal: Any,
    path: Path,
    width: int,
    height: int,
    bands: int,
    dtype: int,
    src: Any,
    creation_options: list[str],
) -> Any:
    out = gdal.GetDriverByName("GTiff").Create(
        str(path), width, height, bands, dtype, options=creation_options
    )
    if out is None:
        raise CleanupError(f"创建输出失败: {path}")
    gt = src.GetGeoTransform(can_return_null=True)
    if gt is not None:
        out.SetGeoTransform(gt)
    proj = src.GetProjectionRef()
    if proj:
        out.SetProjection(proj)
    return out


def _count_changed(src_band: Any, dst_band: Any, width: int, height: int) -> int:
    import numpy as np

    changed = 0
    for y in range(0, height, _CHUNK_ROWS):
        rows = min(_CHUNK_ROWS, height - y)
        a = src_band.ReadAsArray(0, y, width, rows)
        b = dst_band.ReadAsArray(0, y, width, rows)
        changed += int(np.count_nonzero(a != b))
    return changed


def _count_nodata(band: Any, width: int, height: int) -> int:
    import numpy as np

    mask = band.GetMaskBand()
    invalid = 0
    for y in range(0, height, _CHUNK_ROWS):
        rows = min(_CHUNK_ROWS, height - y)
        invalid += int(np.count_nonzero(mask.ReadAsArray(0, y, width, rows) == 0))
    return invalid


def _finalize(
    gdal: Any,
    raw_path: Path,
    out_path: Path,
    out_format: str,
    creation_options: list[str],
    callback: Callable[..., Any] | None,
) -> None:
    """GTiff 直写时仅重命名语义已由调用方处理；其余格式 Translate 到目标。"""
    final = gdal.Translate(
        str(out_path),
        str(raw_path),
        options=gdal.TranslateOptions(
            format=out_format, creationOptions=creation_options, callback=callback
        ),
    )
    if final is None:
        raise CleanupError(f"转换到 {out_format} 失败: {gdal.GetLastErrorMsg()}")
    final = None


def run_cleanup(
    capability_id: str,
    out_path: Path,
    source: Path,
    *,
    params: dict[str, Any],
    out_format: str = "GTiff",
    creation_options: list[str] | None = None,
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行五项能力之一，返回可审计元信息（单输出文件、无 sidecar）。"""
    if capability_id not in CLEANUP_CAPABILITIES:
        raise CleanupError(f"未知清理能力 {capability_id!r}")
    gdal = _gdal()
    cos = list(creation_options or [])
    direct = out_format.upper() == "GTIFF"
    work_path = out_path if direct else out_path.with_name(out_path.stem + "_raw.tif")
    work_cos = cos if direct else []
    _progress(callback, 0.05, "打开输入栅格")
    plan_cleanup_output(capability_id, source, params)  # 预检（含明确错误）
    ds = _open_source(gdal, source)
    try:
        with _scoped_cpl_tmpdir(gdal):
            if capability_id == "gdal:sieve":
                info = _run_sieve(gdal, ds, work_path, params, work_cos, callback)
            elif capability_id == "gdal:fillnodata":
                info = _run_fillnodata(gdal, ds, work_path, params, work_cos, callback)
            elif capability_id == "gdal:nearblack":
                info = _run_nearblack(gdal, ds, source, work_path, params, work_cos, callback)
            elif capability_id == "gdal:proximity":
                info = _run_proximity(gdal, ds, work_path, params, work_cos, callback)
            else:
                info = _run_viewshed(gdal, ds, work_path, params, work_cos, callback)
    except BaseException:
        if not direct:
            work_path.unlink(missing_ok=True)
        raise
    finally:
        ds = None
    if not direct:
        try:
            _finalize(gdal, work_path, out_path, out_format, cos, callback)
        finally:
            work_path.unlink(missing_ok=True)
    _progress(callback, 1.0, "完成")
    info["capability"] = capability_id
    info["out_format"] = out_format
    return info


def _run_sieve(
    gdal: Any,
    ds: Any,
    out_path: Path,
    params: dict[str, Any],
    cos: list[str],
    callback: Callable[..., Any] | None,
) -> dict[str, Any]:
    index, src_band = _pick_band(gdal, ds, params)
    threshold = int(params["threshold"]) if params.get("threshold") is not None else None
    if threshold is None:
        raise CleanupError("gdal:sieve 需要 threshold（最小保留斑块像元数）")
    connectedness = int(params.get("connectedness") or 4)
    width, height = int(ds.RasterXSize), int(ds.RasterYSize)
    out = _new_gtiff(gdal, out_path, width, height, 1, src_band.DataType, ds, cos)
    try:
        dst_band = out.GetRasterBand(1)
        nodata = src_band.GetNoDataValue()
        if nodata is not None:
            dst_band.SetNoDataValue(nodata)
        mask = src_band.GetMaskBand() if nodata is not None else None
        _progress(callback, 0.2, "SieveFilter 滤除小斑块")
        rc = gdal.SieveFilter(
            src_band,
            mask,
            dst_band,
            threshold,
            connectedness,
            callback=callback,
            callback_data=None,
        )
        if rc != 0:
            raise CleanupError(f"gdal.SieveFilter 返回错误码 {rc}: {gdal.GetLastErrorMsg()}")
        dst_band.FlushCache()
        changed = _count_changed(src_band, dst_band, width, height)
    finally:
        out = None
    return {
        "band": index,
        "threshold": threshold,
        "connectedness": connectedness,
        "n_changed_pixels": changed,
    }


def _run_fillnodata(
    gdal: Any,
    ds: Any,
    out_path: Path,
    params: dict[str, Any],
    cos: list[str],
    callback: Callable[..., Any] | None,
) -> dict[str, Any]:
    index, src_band = _pick_band(gdal, ds, params)
    label = src_band.GetNoDataValue()
    override = params.get("srcNodata")
    nodata = float(override) if override is not None else label
    if nodata is None:
        raise CleanupError("fillnodata 需要 NoData 定义（波段标签或 srcNodata）")
    distance = float(params.get("fillDistance") or 10.0)
    smooth = int(params.get("smoothIterations") or 0)
    width, height = int(ds.RasterXSize), int(ds.RasterYSize)
    out = _new_gtiff(gdal, out_path, width, height, 1, src_band.DataType, ds, cos)
    try:
        dst_band = out.GetRasterBand(1)
        dst_band.SetNoDataValue(nodata)
        for y in range(0, height, _CHUNK_ROWS):
            rows = min(_CHUNK_ROWS, height - y)
            dst_band.WriteArray(src_band.ReadAsArray(0, y, width, rows), 0, y)
        dst_band.FlushCache()
        before = _count_nodata(dst_band, width, height)
        _progress(callback, 0.3, "FillNodata 插值填补")
        rc = gdal.FillNodata(
            dst_band,
            dst_band.GetMaskBand(),
            distance,
            smooth,
            callback=callback,
            callback_data=None,
        )
        if rc != 0:
            raise CleanupError(f"gdal.FillNodata 返回错误码 {rc}: {gdal.GetLastErrorMsg()}")
        dst_band.FlushCache()
        after = _count_nodata(dst_band, width, height)
    finally:
        out = None
    return {
        "band": index,
        "fill_distance": distance,
        "smooth_iterations": smooth,
        "nodata": nodata,
        "n_nodata_before": before,
        "n_nodata_after": after,
        "n_filled": before - after,
    }


def _run_nearblack(
    gdal: Any,
    ds: Any,
    source: Path,
    out_path: Path,
    params: dict[str, Any],
    cos: list[str],
    callback: Callable[..., Any] | None,
) -> dict[str, Any]:
    near = int(params["near"]) if params.get("near") is not None else 15
    white = bool(params.get("white") or False)
    width, height = int(ds.RasterXSize), int(ds.RasterYSize)
    _progress(callback, 0.2, "Nearblack 清理近黑/近白边")
    out = gdal.Nearblack(
        str(out_path),
        str(source),
        options=gdal.NearblackOptions(
            format="GTiff",
            creationOptions=cos,
            nearDist=near,
            white=white,
            callback=callback,
        ),
    )
    if out is None:
        raise CleanupError(f"gdal.Nearblack 失败: {gdal.GetLastErrorMsg()}")
    try:
        out.FlushCache()
        changed = 0
        for b in range(1, int(ds.RasterCount) + 1):
            changed += _count_changed(ds.GetRasterBand(b), out.GetRasterBand(b), width, height)
    finally:
        out = None
    return {"near": near, "white": white, "n_changed_samples": changed}


def _run_proximity(
    gdal: Any,
    ds: Any,
    out_path: Path,
    params: dict[str, Any],
    cos: list[str],
    callback: Callable[..., Any] | None,
) -> dict[str, Any]:
    import numpy as np

    index, src_band = _pick_band(gdal, ds, params)
    width, height = int(ds.RasterXSize), int(ds.RasterYSize)
    values = params.get("values")
    units = str(params.get("distunits") or "PIXEL").upper()
    max_dist = params.get("maxDist")
    nodata_src = src_band.GetNoDataValue()

    targets = 0
    for y in range(0, height, _CHUNK_ROWS):
        rows = min(_CHUNK_ROWS, height - y)
        arr = src_band.ReadAsArray(0, y, width, rows)
        hit = np.isin(arr, [float(v) for v in values]) if values else arr != 0
        if nodata_src is not None:
            hit &= arr != nodata_src
        targets += int(np.count_nonzero(hit))
    if targets == 0:
        raise CleanupError(
            "没有任何像元等于目标值"
            + (f" {list(values)}" if values else "（默认：非零像元）")
            + "，拒绝输出全无效的邻近图"
        )

    out = _new_gtiff(gdal, out_path, width, height, 1, gdal.GDT_Float32, ds, cos)
    try:
        dst_band = out.GetRasterBand(1)
        dst_band.SetNoDataValue(_PROXIMITY_NODATA)
        options = [f"DISTUNITS={units}", f"NODATA={_PROXIMITY_NODATA:g}"]
        if values:
            options.append("VALUES=" + ",".join(f"{float(v):.12g}" for v in values))
        if max_dist is not None:
            options.append(f"MAXDIST={float(max_dist):.12g}")
        _progress(callback, 0.2, "ComputeProximity 计算邻近距离")
        rc = gdal.ComputeProximity(
            src_band, dst_band, options, callback=callback, callback_data=None
        )
        if rc != 0:
            raise CleanupError(f"gdal.ComputeProximity 返回错误码 {rc}: {gdal.GetLastErrorMsg()}")
        dst_band.FlushCache()
        n_nodata = _count_nodata(dst_band, width, height)
    finally:
        out = None
    return {
        "band": index,
        "values": list(values) if values else None,
        "distunits": units,
        "max_dist": float(max_dist) if max_dist is not None else None,
        "n_target_pixels": targets,
        "n_nodata_out": n_nodata,
    }


def _run_viewshed(
    gdal: Any,
    ds: Any,
    out_path: Path,
    params: dict[str, Any],
    cos: list[str],
    callback: Callable[..., Any] | None,
) -> dict[str, Any]:
    if params.get("ox") is None or params.get("oy") is None:
        raise CleanupError("gdal:viewshed 需要观察点坐标 ox 与 oy（数据集 CRS 的地图坐标）")
    index, src_band = _pick_band(gdal, ds, params)
    width, height = int(ds.RasterXSize), int(ds.RasterYSize)
    gt = ds.GetGeoTransform()
    ox, oy = float(params["ox"]), float(params["oy"])
    col = (ox - gt[0]) / gt[1]
    row = (oy - gt[3]) / gt[5]
    if not (0.0 <= col < width and 0.0 <= row < height):
        raise CleanupError(
            f"观察点 ({ox}, {oy}) 不在栅格范围内（像元坐标 col={col:.3f}, row={row:.3f}，"
            f"尺寸 {width}×{height}）"
        )
    observer_h = (
        float(params["observerHeight"]) if params.get("observerHeight") is not None else 2.0
    )
    target_h = float(params["targetHeight"]) if params.get("targetHeight") is not None else 0.0
    if params.get("viewDistance") is not None:
        max_distance = float(params["viewDistance"])
    else:
        # 未限制：取栅格对角线，保证输出范围覆盖全图
        max_distance = math.hypot(width * abs(gt[1]), height * abs(gt[5]))
    _progress(callback, 0.2, "ViewshedGenerate 计算通视")
    result = gdal.ViewshedGenerate(
        src_band,
        "GTiff",
        str(out_path),
        cos,
        ox,
        oy,
        observer_h,
        target_h,
        255.0,
        0.0,
        0.0,
        -1.0,
        _VIEWSHED_CURVATURE,
        gdal.GVM_Edge,
        max_distance,
        callback=callback,
        callback_data=None,
    )
    if result is None:
        raise CleanupError(f"gdal.ViewshedGenerate 失败: {gdal.GetLastErrorMsg()}")
    try:
        result.FlushCache()
        out_w, out_h = int(result.RasterXSize), int(result.RasterYSize)
        visible = 0
        import numpy as np

        vb = result.GetRasterBand(1)
        for y in range(0, out_h, _CHUNK_ROWS):
            rows = min(_CHUNK_ROWS, out_h - y)
            visible += int(np.count_nonzero(vb.ReadAsArray(0, y, out_w, rows) == 255))
    finally:
        result = None
    return {
        "band": index,
        "observer": [ox, oy],
        "observer_height": observer_h,
        "target_height": target_h,
        "view_distance": max_distance,
        "curvature_coeff": _VIEWSHED_CURVATURE,
        "out_size": [out_w, out_h],
        "n_visible": visible,
    }
