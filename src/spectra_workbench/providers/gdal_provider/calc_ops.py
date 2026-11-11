"""P4-04：栅格表达式计算的 GDAL 读写通道（只在 worker 进程导入 osgeo）。

与 :mod:`expression_ops` 的分工：
- ``expression_ops`` 只做纯 numpy 的安全解析与求值（GUI 侧也可导入测试）；
- 本模块负责真实栅格的打开、对齐预检、写出与主影像空间参考的继承。

**输入对齐是硬要求**：第 29 轮复核已实测 GDAL 对不兼容输入会静默跳过，
因此这里对每个输入的尺寸 / 仿射变换 / 投影逐一核对，不一致直接拒绝，
绝不「按第一个源凑合算」。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from spectra_workbench.providers.gdal_provider.expression_ops import (
    ExpressionError,
    band_names_for_inputs,
    evaluate_expression,
)

__all__ = ["run_calc", "GDAL_TYPE_TO_NUMPY"]

#: 输出类型名（契约 canonical）-> (GDAL GDT 名, numpy dtype)。
#: 整型输出会由 evaluate_expression 做范围与取整校验，避免静默回绕/截断。
GDAL_TYPE_TO_NUMPY: dict[str, tuple[str, Any]] = {
    "Byte": ("GDT_Byte", np.uint8),
    "UInt16": ("GDT_UInt16", np.uint16),
    "Int16": ("GDT_Int16", np.int16),
    "UInt32": ("GDT_UInt32", np.uint32),
    "Int32": ("GDT_Int32", np.int32),
    "Float32": ("GDT_Float32", np.float32),
    "Float64": ("GDT_Float64", np.float64),
}


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    """把阶段进度交给 adapter 的取消/进度回调；返回 0 表示请求取消。"""
    if callback is None:
        return
    try:
        keep_going = callback(ratio, message)
    except Exception:
        return  # 进度回调异常不应中断计算
    if keep_going == 0:
        raise RuntimeError("任务已取消（表达式计算阶段）")


def run_calc(
    out_path: Path,
    sources: list[Path],
    *,
    expression: str,
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    output_type: str = "Float32",
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """对多个输入执行受限表达式，写单波段成果并返回可审计的元信息。

    - 每个输入取**第 1 波段**（多波段源需先用波段管理抽取，见 limitations）；
    - 波段名按输入顺序为 A/B/C…（大小写不敏感）；
    - 源 NoData 优先用 ``src_nodata``，否则取各波段自带的 NoData 标签；
      非有限值（NaN/Inf）一律视为无效；
    - 输出继承第一个输入的 GeoTransform 与投影，写出后设置 NoData 标签，
      保证「标签 / 无效区 / 像元值」三者一致（第 29 轮同类缺陷的防线）。
    """
    gdal = _gdal()
    if not sources:
        raise ValueError("表达式计算至少需要 1 个输入栅格")
    if output_type not in GDAL_TYPE_TO_NUMPY:
        raise ValueError(f"未知输出类型 {output_type!r}；可用：{', '.join(GDAL_TYPE_TO_NUMPY)}")
    gdt_name, np_dtype = GDAL_TYPE_TO_NUMPY[output_type]

    names = band_names_for_inputs(len(sources))
    arrays: dict[str, np.ndarray] = {}
    nodata_values: dict[str, float | None] = {}
    # 首个输入的空间参考用标量字段保存，避免 Optional[dict] 在循环后无法收窄。
    ref_w = 0
    ref_h = 0
    ref_gt: tuple[float, ...] = ()
    ref_proj = ""
    ref_ready = False

    for idx, (source, name) in enumerate(zip(sources, names, strict=True)):
        _progress(
            callback, 0.05 + 0.45 * idx / max(1, len(sources)), f"读取输入 {name}: {source.name}"
        )
        ds = gdal.Open(str(source), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开输入栅格：{source}")
        try:
            w, h = int(ds.RasterXSize), int(ds.RasterYSize)
            gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
            proj = ds.GetProjectionRef() or ""
            if not ref_ready:
                ref_w, ref_h, ref_gt, ref_proj, ref_ready = w, h, gt, proj, True
            else:
                if (w, h) != (ref_w, ref_h):
                    raise ValueError(
                        f"输入 {name} 尺寸 {w}x{h} 与首个输入 {ref_w}x{ref_h} 不一致，拒绝错位计算"
                    )
                if gt != ref_gt:
                    raise ValueError(f"输入 {name} 的仿射变换与首个输入不一致，拒绝错位计算")
                if proj != ref_proj:
                    raise ValueError(f"输入 {name} 的投影与首个输入不一致，拒绝跨 CRS 直接计算")
            band = ds.GetRasterBand(1)
            arr = band.ReadAsArray()
            if arr is None:
                raise RuntimeError(f"无法读取输入栅格第 1 波段：{source}")
            arrays[name] = np.asarray(arr)
            label = band.GetNoDataValue()
            nodata_values[name] = (
                float(src_nodata)
                if src_nodata is not None
                else (float(label) if label is not None else None)
            )
        finally:
            del ds

    if not ref_ready:
        raise RuntimeError("未能从输入栅格读取空间参考")

    _progress(callback, 0.60, "求值受限表达式")
    try:
        result, info = evaluate_expression(
            expression,
            arrays,
            src_nodata=nodata_values,
            out_nodata=float(dst_nodata),
            out_dtype=np_dtype,
        )
    except ExpressionError:
        raise
    except Exception as exc:  # 求值期异常统一带上表达式上下文
        raise RuntimeError(f"表达式求值失败（{expression!r}）：{exc}") from exc

    _progress(callback, 0.80, f"写出成果（{out_format} / {output_type}）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise RuntimeError(f"不可用输出驱动：{out_format}")
    gdt = getattr(gdal, gdt_name)
    out_ds = driver.Create(str(out_path), ref_w, ref_h, 1, gdt)
    if out_ds is None:
        raise RuntimeError(f"无法创建输出栅格：{out_path}")
    try:
        if ref_gt:
            out_ds.SetGeoTransform(list(ref_gt))
        if ref_proj:
            out_ds.SetProjection(ref_proj)
        out_band = out_ds.GetRasterBand(1)
        out_band.WriteArray(result)
        out_band.SetNoDataValue(float(dst_nodata))
        out_band.FlushCache()
        out_ds.FlushCache()
    finally:
        del out_ds

    _progress(callback, 0.95, "表达式计算完成")
    info.update(
        {
            "driver": out_format,
            "output_type": output_type,
            "width": ref_w,
            "height": ref_h,
            "source_count": len(sources),
            "source_nodata": nodata_values,
            "source_bands": {name: 1 for name in names},
            "geotransform_inherited": bool(ref_gt),
        }
    )
    return info
