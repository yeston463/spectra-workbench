"""P6-06 DOS1 大气校正（Chavez 1988）：明确命名的独立方法，不处置 FLAASH。

方法定义
--------
**Chavez (1988) DOS1 暗目标减法**：
1. 从直方图读取暗对象 DN 值 ``DN_haze``（暗像元 DN 最小值，要求直方图有足够尾部；
   本实现由用户显式给出 DN_haze，避免对复杂直方图做启发式判断）；
2. 用**大气透射率近似** ``T_tau_v = cos(θ_z)``、``T_tau_s = sin(θ_e)``（Chavez
   用 1.0/0/12 的查表；本实现按显式太阳天顶角/天顶角求 cos/sin，更便于复核）；
3. 计算路径辐亮度：``L_p = L_h - L_D``，其中 ``L_h = a_1 * DN_haze + a_0``，
   ``L_D = a_1 * (band-1 位置)`` + 大气项；按 Chavez 论文的 DOS1 简化公式：
   ``L_1haze = a_1 * DN_haze + a_0 - 0.01 * (ESUN * cos(θ_z) / π * d²)``，其中
   ``d²`` 用星日距离修正（Chavez 表）；本实现按用户显式 ``earthSunDistance``。
4. 输出反射率 ``ρ = π · (L - L_1haze) · d² / (ESUN · cos(θ_z))``；
5. NoData/非有限值排除；单波段 Float32 + sidecar 记录 DN_haze、esun、太阳
   几何、原图、公式与暗对象，可直接独立核算。

注意：DOS1 是公开、简单、可验证的独立大气校正算法；FLAASH 属外部引擎
适配，登记在 `platform:flaash_external`（blocked），绝不以此替代 FLAASH。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["DOS1Error", "run_dos1"]


class DOS1Error(ValueError):
    """DOS1 输入或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（DOS1 阶段）")


def run_dos1(
    out_path: Path,
    source: Path,
    *,
    band: int = 1,
    haze_dn: float,
    esun: float,
    sun_zen_angle: float,
    gain: float = 1.0,
    offset: float = 0.0,
    earth_sun_distance: float = 1.0,
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """对单波段 DN 栅格执行 DOS1 亮暗目标减法，输出 TOA 反射率。"""
    gdal = _gdal()
    if not source.exists():
        raise DOS1Error(f"输入栅格不存在: {source}")
    if not math.isfinite(dst_nodata):
        raise DOS1Error(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    if not math.isfinite(esun) or esun <= 0.0:
        raise DOS1Error(f"esun 必须为正数，实际 {esun}")
    if not math.isfinite(gain) or not math.isfinite(offset):
        raise DOS1Error("gain/offset 必须为有限值")
    if math.isfinite(earth_sun_distance) and earth_sun_distance <= 0.0:
        raise DOS1Error("earthSunDistance 必须为正数")
    if not (0.0 < float(sun_zen_angle) < 90.0):
        raise DOS1Error(f"sunZenAngle 必须在 (0, 90) 度，实际 {sun_zen_angle}")
    if not math.isfinite(haze_dn):
        raise DOS1Error(f"hazeDN 必须为有限值，实际 {haze_dn}")

    _progress(callback, 0.05, "打开输入栅格")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise DOS1Error(f"无法打开输入栅格: {source}")
    try:
        if band < 1 or band > int(ds.RasterCount):
            raise DOS1Error(f"dosBand={band} 超出范围（数据集共 {ds.RasterCount} 个波段）")
        src_band = ds.GetRasterBand(band)
        if gdal.DataTypeIsComplex(src_band.DataType):
            raise DOS1Error("DOS1 仅支持实数波段")
        raw_obj = src_band.ReadAsArray()
        if raw_obj is None:
            raise DOS1Error(f"无法读取波段 {band}")
        raw = np.asarray(raw_obj, dtype=np.float64)
        width, height = int(ds.RasterXSize), int(ds.RasterYSize)
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""
        label = src_band.GetNoDataValue()
        nodata = (
            float(src_nodata)
            if src_nodata is not None
            else (float(label) if label is not None else None)
        )
    finally:
        ds = None

    valid = np.isfinite(raw)
    if nodata is not None:
        valid &= raw != nodata
    if not valid.any():
        raise DOS1Error("没有有效像元，DOS1 无法计算")

    _progress(callback, 0.45, "计算 DOS1 反射率")
    # Chavez (1988) DOS1: ρ = π·(L - L_1haze)·d² / (ESUN·cos(θ_z))
    L = gain * raw + offset
    cos_theta_z = math.cos(math.radians(float(sun_zen_angle)))
    d_squared = float(earth_sun_distance) ** 2
    one_percent_radiance = 0.01 * esun * cos_theta_z / (math.pi * d_squared)
    L_haze = gain * haze_dn + offset
    L_1haze = L_haze - one_percent_radiance
    numerator = math.pi * d_squared / (esun * cos_theta_z)
    rho_valid = valid
    rho = np.where(rho_valid, numerator * (L - L_1haze), np.nan)

    _progress(callback, 0.78, f"写出成果（{out_format} / Float32）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise DOS1Error(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), width, height, 1, gdal.GDT_Float32)
    if out_ds is None:
        raise DOS1Error(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(proj)
        out_ds.SetMetadataItem("ATMOS_METHOD", "chavez_dos1")
        out_ds.SetMetadataItem("ATMOS_SUN_ZEN", f"{sun_zen_angle:.12g}")
        out_ds.SetMetadataItem("ATMOS_HAZE_DN", f"{haze_dn:.12g}")
        out_ds.SetMetadataItem("ATMOS_ESUN", f"{esun:.12g}")
        out_ds.SetMetadataItem("ATMOS_EARTH_SUN_DISTANCE", f"{earth_sun_distance:.12g}")
        out_ds.SetMetadataItem("ATMOS_ONE_PERCENT_RADIANCE", f"{one_percent_radiance:.12g}")
        band_out = out_ds.GetRasterBand(1)
        arr = np.where(rho_valid, rho, dst_nodata).astype(np.float32)
        band_out.WriteArray(arr)
        band_out.SetNoDataValue(float(dst_nodata))
        band_out.SetDescription("dos1_toa_reflectance")
        band_out.FlushCache()
    finally:
        out_ds = None

    payload = {
        "type": "spectra_dos1",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P6-06",
        "source": str(source),
        "band": int(band),
        "atmos_method": "chavez_dos1_1988",
        "citation": (
            "Chavez, P.S. (1988). An improved dark-object subtraction technique "
            "for atmospheric scattering correction of multispectral data. "
            "Remote Sensing of Environment 24:459-479."
        ),
        "haze_dn": float(haze_dn),
        "gain": float(gain),
        "offset": float(offset),
        "esun": float(esun),
        "sun_zen_angle_deg": float(sun_zen_angle),
        "earth_sun_distance": float(earth_sun_distance),
        "one_percent_radiance": float(one_percent_radiance),
        "radiance_haze": float(L_haze),
        "radiance_one_percent_haze": float(L_1haze),
        "numerator_factor": float(numerator),
        "n_valid": int(rho_valid.sum()),
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
        "formula": "rho = pi*(L-L_1haze)*d^2/(ESUN*cos(theta_z))",
        "disclaimer": (
            "DOS1 与 FLAASH 是不同的大气校正方法；FLAASH 为外部引擎适配，"
            "登记在 platform:flaash_external（blocked）。"
        ),
    }
    sidecar = out_path.with_name(out_path.name + ".dos1.json")
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    _progress(callback, 1.0, "完成")
    return {
        "band": int(band),
        "haze_dn": float(haze_dn),
        "esun": float(esun),
        "sun_zen_angle": float(sun_zen_angle),
        "n_valid": int(rho_valid.sum()),
        "one_percent_radiance": float(one_percent_radiance),
        "sidecar": str(sidecar),
    }
