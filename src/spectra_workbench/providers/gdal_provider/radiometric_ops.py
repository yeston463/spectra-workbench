"""P6-06 辐射定标：显式增益/偏置与 TOA 反射率（命名清晰，不做 FLAASH）。

方法定义
--------
- ``calibrateMode=physical``：``physical = DN * gain + offset``，逐波段提供
  Gain/Offset（维度=波段数）；用于 DN→辐亮度/反射率因子的线性定标；
- ``calibrateMode=toa_reflectance``：``ρ = π · L · d² / (ESUN · cos(θ_z))``，
  需要波特征值 ``ESUN`` 列表（与 Radiance / Gain+Offset 无关，直接对波段做
  ESUN 归一化）与太阳天顶角 ``sunZenAngle``（度）；输出为逐波段 TOA 反射率；
- **热红外波段（TIRS/KL1）的亮温 BT（K）不落入本能力**——BT 与反射率/辐亮度
  是三种不同的物理量，混称是明确禁止的；BT 归后续专项任务（未实现）；
- 输出恒为逐波段 Float32，NoData/非有限值/复数输入明确拒绝；
- sidecar ``<成果名>.calibrate.json`` 记录逐波段 gain/offset 或 esun、公式、
  sun angle、单位、光谱类型（reflectance / radiance），便于真实性检查；
- 注意：这不是上证工程意义上的 FLAASH；**FLAASH 属外部引擎适配**，已义
  登记 `platform:flaash_external`（blocked），当前不提供假入口。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "CalibrateError",
    "CALIBRATE_MODES",
    "run_calibration",
]

CALIBRATE_MODES: tuple[str, ...] = ("physical", "toa_reflectance")


class CalibrateError(ValueError):
    """定标输入或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（定标阶段）")


def run_calibration(
    out_path: Path,
    source: Path,
    *,
    mode: str = "physical",
    gains: list[float] | None = None,
    offsets: list[float] | None = None,
    esun: list[float] | None = None,
    sun_zen_angle: float | None = None,
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """按模式执行定标并写 Float32 成果 + sidecar 审计。"""
    gdal = _gdal()
    mode_name = str(mode or "physical").strip().lower()
    if mode_name not in CALIBRATE_MODES:
        raise CalibrateError(f"未知定标模式 {mode!r}；可用：{', '.join(CALIBRATE_MODES)}")
    if not math.isfinite(dst_nodata):
        raise CalibrateError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    if not source.exists():
        raise CalibrateError(f"输入栅格不存在: {source}")

    _progress(callback, 0.05, "打开输入栅格")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise CalibrateError(f"无法打开输入栅格: {source}")
    try:
        n_bands = int(ds.RasterCount)
        if n_bands < 1:
            raise CalibrateError("输入栅格没有波段")
        width, height = int(ds.RasterXSize), int(ds.RasterYSize)
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""
        _progress(callback, 0.15, f"读取 {n_bands} 个波段")
        arrays: list[np.ndarray] = []
        labels: list[float | None] = []
        for band_index in range(1, n_bands + 1):
            band = ds.GetRasterBand(band_index)
            if gdal.DataTypeIsComplex(band.DataType):
                raise CalibrateError(f"定标仅支持实数波段，波段 {band_index} 是复数类型")
            raw_obj = band.ReadAsArray()
            if raw_obj is None:
                raise CalibrateError(f"无法读取波段 {band_index}")
            raw = np.asarray(raw_obj, dtype=np.float64)
            label = band.GetNoDataValue()
            labels.append(float(label) if label is not None else None)
            arrays.append(raw)
    finally:
        ds = None

    # 参数维度必须等于波段数（不允许隐式广播或复用常数）
    gains_norm = [float(v) for v in gains] if gains else None
    offsets_norm = [float(v) for v in offsets] if offsets else None
    esun_norm = [float(v) for v in esun] if esun else None
    if mode_name == "physical":
        if gains_norm is not None and len(gains_norm) != n_bands:
            raise CalibrateError(f"calibrateGains 数量 {len(gains_norm)} 与波段数 {n_bands} 不一致")
        if offsets_norm is not None and len(offsets_norm) != n_bands:
            raise CalibrateError(
                f"calibrateOffsets 数量 {len(offsets_norm)} 与波段数 {n_bands} 不一致"
            )
    else:
        if esun_norm is None or len(esun_norm) != n_bands:
            raise CalibrateError(
                "endmemberMode=toa_reflectance 需要 esun 与波段数一致；"
                f"当前 {len(esun_norm or [])} vs {n_bands}"
            )
        if any(not math.isfinite(v) or v <= 0.0 for v in esun_norm):
            raise CalibrateError("esun 必须为正数的有限值")
        if sun_zen_angle is None:
            raise CalibrateError("endmemberMode=toa_reflectance 需要 sunZenAngle")
        if not 0.0 <= float(sun_zen_angle) < 180.0:
            raise CalibrateError(f"sunZenAngle 必须在 [0, 180) 度，实际 {sun_zen_angle}")
        sun_zen_rad = math.radians(float(sun_zen_angle))
        cos_term = math.cos(sun_zen_rad)
        if cos_term <= 0.0:
            raise CalibrateError("太阳天顶角必须在 (-90, 90) 度范围内才有意义")
        sqrt_term = math.pi / cos_term

    if mode_name == "physical":
        if gains_norm is None:
            raise CalibrateError("endmemberMode=physical 需要 calibrateGains")
        spec_kind = "linear_gain_offset"
        formula = "physical = DN * gain + offset"
        spectral_type = "radiance_or_reflectance_factor"
    else:
        spec_kind = "toa_reflectance"
        formula = "rho = pi * L * d^2 / (ESUN * cos(theta_z))"
        spectral_type = "toa_reflectance"

    output_values: list[np.ndarray] = []
    invalid_masks: list[np.ndarray] = []
    for index, raw in enumerate(arrays):
        idx = index
        nodata = float(src_nodata) if src_nodata is not None else labels[idx]
        invalid = ~np.isfinite(raw)
        if nodata is not None:
            invalid |= raw == nodata
        invalid_masks.append(invalid)
        if mode_name == "physical":
            g = gains_norm[idx] if gains_norm else 1.0
            o = offsets_norm[idx] if offsets_norm else 0.0
            output_values.append(np.where(invalid, raw, raw * g + o))
        else:
            if esun_norm is None:  # mypy: 已在验证分支强制
                raise CalibrateError("endmemberMode=toa_reflectance 需要 calibrateEsun")
            output_values.append(np.where(invalid, raw, sqrt_term * raw / esun_norm[idx]))
    _progress(callback, 0.65, f"定标 {n_bands} 个波段")

    _progress(callback, 0.80, f"写出成果（{out_format} / Float32，{n_bands} 波段）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise CalibrateError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), width, height, n_bands, gdal.GDT_Float32)
    if out_ds is None:
        raise CalibrateError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(proj)
        out_ds.SetMetadataItem("CALIBRATE_MODE", mode_name)
        out_ds.SetMetadataItem("CALIBRATION_SPEC", spec_kind)
        out_ds.SetMetadataItem("CALIBRATION_FORMULA", formula)
        out_ds.SetMetadataItem("CALIBRATION_SPECTRAL_TYPE", spectral_type)
        if mode_name == "toa_reflectance":
            out_ds.SetMetadataItem("CALIBRATION_SUN_ZEN", f"{sun_zen_angle:.12g}")
        for idx in range(n_bands):
            band_out = out_ds.GetRasterBand(idx + 1)
            arr = np.where(invalid_masks[idx], dst_nodata, output_values[idx]).astype(np.float32)
            band_out.WriteArray(arr)
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.SetDescription(f"calibrated_B{idx + 1}")
            if mode_name == "physical":
                band_out.SetMetadataItem(
                    "CALIB_GAIN", f"{gains_norm[idx] if gains_norm else 0.0:.12g}"
                )
                band_out.SetMetadataItem(
                    "CALIB_OFFSET", f"{offsets_norm[idx] if offsets_norm else 0.0:.12g}"
                )
            else:
                if esun_norm is None:
                    raise CalibrateError("endmemberMode=toa_reflectance 需要 calibrateEsun")
                band_out.SetMetadataItem("CALIB_ESUN", f"{esun_norm[idx]:.12g}")
            band_out.FlushCache()
    finally:
        out_ds = None

    payload = {
        "type": "spectra_calibrate",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P6-06",
        "source": str(source),
        "mode": mode_name,
        "spec_kind": spec_kind,
        "formula": formula,
        "spectral_type": spectral_type,
        "bands": n_bands,
        "sun_zen_angle": (float(sun_zen_angle) if sun_zen_angle is not None else None),
        "gains": ([float(v) for v in gains_norm] if gains_norm is not None else None),
        "offsets": ([float(v) for v in offsets_norm] if offsets_norm is not None else None),
        "esun": [float(v) for v in esun_norm] if esun_norm is not None else None,
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
        "disclaimer": (
            "热红外亮温（BT）不属于本能力输出；FLAASH 为外部引擎适配有序登记，"
            "见 platform:flaash_external"
        ),
    }
    sidecar = out_path.with_name(out_path.name + ".calibrate.json")
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    _progress(callback, 1.0, "完成")
    return {
        "mode": mode_name,
        "bands": n_bands,
        "spec_kind": spec_kind,
        "sidecar": str(sidecar),
    }
