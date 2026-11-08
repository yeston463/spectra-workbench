"""P6-03 缨帽变换（Landsat 5 TM / 7 ETM+ / 8 OLI）。

系数来源与传感器匹配规则见 ``processing/tasseledcap_registry.py``：
- 调用方必须显式给出 6 个输入波段号，顺序为 Blue, Green, Red, NIR, SWIR1, SWIR2；
- 未登记传感器、重复波段、越界波段、系数文件不匹配均明确拒绝；
- 成果为 3 波段 Float32（brightness / greenness / wetness）+ 可审计 sidecar，
  数据集与波段元数据同时写入传感器、产品级别、来源 URL 与系数顺序。

边界：整幅读入（变换逐像元线性组合，不与邻域耦合）；输入必须是实数波段；
NoData 像元在三项输出中均回填 NoData。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from spectra_workbench.processing.tasseledcap_registry import get_sensor

__all__ = [
    "TasseledCapError",
    "apply_coefficients",
    "run_tasseled_cap",
]

TASSELED_CAP_OUTPUT_BANDS = ("brightness", "greenness", "wetness")


class TasseledCapError(ValueError):
    """缨帽变换参数、输入或传感器匹配错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（缨帽变换阶段）")


def apply_coefficients(
    data: np.ndarray,
    coefficients: np.ndarray,
) -> np.ndarray:
    """纯矩阵运算：``(6, n_pixels)`` 输入 → ``(3, n_pixels)`` 三项分量。"""
    arr = np.asarray(data, dtype=np.float64)
    coeff = np.asarray(coefficients, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] != 6:
        raise TasseledCapError(f"缨帽输入必须是 6 个波段，实际形状 {arr.shape}")
    if coeff.shape != (3, 6):
        raise TasseledCapError(f"缨帽系数必须是 3×6，实际形状 {coeff.shape}")
    return coeff @ arr


def _validate_bands(bands: list[int], available: int) -> list[int]:
    if len(bands) != 6:
        raise TasseledCapError(f"tcBands 必须恰好 6 个波段号，实际 {len(bands)} 个")
    if len(set(bands)) != 6:
        raise TasseledCapError(f"tcBands 的 6 个波段号必须互不相同，实际 {bands}")
    for band in bands:
        if band < 1 or band > available:
            raise TasseledCapError(
                f"tcBands 中的波段 {band} 超出范围（数据集共 {available} 个波段）"
            )
    return bands


def run_tasseled_cap(
    out_path: Path,
    source: Path,
    *,
    sensor_key: str,
    bands: list[int],
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行缨帽变换并写 3 波段成果与来源 sidecar。"""
    gdal = _gdal()
    if not math.isfinite(dst_nodata):
        raise TasseledCapError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    sensor = get_sensor(sensor_key)
    if not source.exists():
        raise FileNotFoundError(f"输入栅格不存在: {source}")

    _progress(callback, 0.05, "打开输入栅格")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise TasseledCapError(f"无法打开输入栅格: {source}")
    try:
        available = int(ds.RasterCount)
        bands = _validate_bands([int(v) for v in bands], available)
        width, height = int(ds.RasterXSize), int(ds.RasterYSize)
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""
        arrays: list[np.ndarray] = []
        valid = np.ones(width * height, dtype=bool)
        _progress(callback, 0.15, f"读取波段 {bands}")
        for band_index in bands:
            band = ds.GetRasterBand(band_index)
            if gdal.DataTypeIsComplex(band.DataType):
                raise TasseledCapError(
                    f"缨帽变换仅支持实数波段，波段 {band_index} 是复数类型 "
                    f"({gdal.GetDataTypeName(band.DataType)})"
                )
            raw_obj = band.ReadAsArray()
            if raw_obj is None:
                raise TasseledCapError(f"无法读取波段 {band_index}")
            raw = np.asarray(raw_obj, dtype=np.float64).ravel()
            label = band.GetNoDataValue()
            nodata = float(src_nodata) if src_nodata is not None else (
                float(label) if label is not None else None
            )
            band_valid = np.isfinite(raw)
            if nodata is not None:
                band_valid &= raw != nodata
            valid &= band_valid
            arrays.append(raw)
    finally:
        ds = None

    data = np.vstack(arrays)
    coefficients = np.asarray(sensor.coefficients, dtype=np.float64)
    _progress(callback, 0.55, f"应用缨帽系数（{sensor.key}）")
    transformed = apply_coefficients(data, coefficients)

    _progress(callback, 0.80, f"写出成果（{out_format} / Float32，3 波段）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise TasseledCapError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), width, height, 3, gdal.GDT_Float32)
    if out_ds is None:
        raise TasseledCapError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(proj)
        out_ds.SetMetadataItem("TC_SENSOR", sensor.key)
        out_ds.SetMetadataItem("TC_PLATFORM", sensor.platform)
        out_ds.SetMetadataItem("TC_PRODUCT_LEVEL", sensor.product_level)
        out_ds.SetMetadataItem("TC_SOURCE", sensor.source)
        out_ds.SetMetadataItem("TC_SOURCE_URL", sensor.source_url)
        out_ds.SetMetadataItem("TC_BAND_ORDER", ",".join(sensor.band_order))
        out_ds.SetMetadataItem("TC_INPUT_BANDS", ",".join(str(b) for b in bands))
        for idx, name in enumerate(TASSELED_CAP_OUTPUT_BANDS):
            band_out = out_ds.GetRasterBand(idx + 1)
            values = np.where(valid, transformed[idx], dst_nodata).astype(np.float32)
            band_out.WriteArray(values.reshape(height, width))
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.SetDescription(name)
            band_out.SetMetadataItem("TC_SENSOR", sensor.key)
            band_out.SetMetadataItem("TC_COMPONENT", name)
            band_out.FlushCache()
    finally:
        out_ds = None

    payload = {
        "type": "spectra_tasseled_cap",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P6-03",
        "source": str(source),
        "sensor": sensor.key,
        "platform": sensor.platform,
        "product_level": sensor.product_level,
        "band_order": list(sensor.band_order),
        "input_bands": [int(v) for v in bands],
        "coefficients": [
            [float(v) for v in row] for row in sensor.coefficients
        ],
        "output_bands": list(TASSELED_CAP_OUTPUT_BANDS),
        "source_citation": sensor.source,
        "source_url": sensor.source_url,
        "notes": sensor.notes,
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
        "n_valid": int(valid.sum()),
    }
    sidecar = out_path.with_name(out_path.name + ".tasseledcap.json")
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    _progress(callback, 1.0, "完成")
    return {
        "sensor": sensor.key,
        "band_order": list(sensor.band_order),
        "input_bands": [int(v) for v in bands],
        "output_bands": list(TASSELED_CAP_OUTPUT_BANDS),
        "source": sensor.source,
        "source_url": sensor.source_url,
        "product_level": sensor.product_level,
        "n_valid": int(valid.sum()),
        "sidecar": str(sidecar),
    }
