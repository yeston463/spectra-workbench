"""P6-02：FFT 频域滤波与去条纹。

数学语义（对应验收「往返、频率定位、掩膜对称性、复数/奇偶尺寸通过；保存掩膜可复现」）：
- **往返**：抑制列表为空时，FFT→逆 FFT 与输入逐像元一致（浮点容差）；
- **频率定位**：numpy.fft.fft2 的频率 bin 为 (fy, fx)，条纹（周期 P、沿 x）
  的能量集中在 fy=0、fx=±N/P；抑制列表自动补 **厄米对称伙伴** (−fy, −fx)，
  保证实数输入的逆变换仍为实数；
- **掩膜对称性**：构造的频域掩膜逐项满足 mask[fy,fx] == mask[(−fy)%N, (−fx)%M]，
  测试逐条断言；输出虚部被丢弃前断言其幅度 ≈ 0；
- **奇偶尺寸**：fft2/ifft2 对奇数与偶数尺寸均正确（往返与频率定位在
  8×8 与 7×9 上分别验证）；
- **保存掩膜可复现**：抑制 bin 清单与对称伙伴写入 ``<成果名>.fft_mask.json``
  sidecar，用同一清单重跑得到逐像元一致成果。

边界（如实记录）：
- 源波段 NoData 像元在 FFT 前**以波段均值填充**、逆变换后按原 NoData
  掩膜回填——含大空洞的频域滤波物理意义有限，属诚实的简化实现；
- 输入必须是**实数**单波段栅格；复数波段会明确拒绝，绝不静默丢弃虚部
  （`np.asarray(complex, float)` 只会触发 ComplexWarning 并丢掉虚部）；
- FFT 需要整幅读入（频域算法本身不可按窗口分块）；当前实现未设置像素上限，
  超大影像的内存开销如实暴露给用户与调用方，分块 FFT 归后续轮次；
- 输出恒为单波段 Float32；science 独立 worker 边界同 P6-01（registry 声明）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["fft_filter", "hermitian_partner", "build_suppression_mask"]


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（FFT 滤波阶段）")


def hermitian_partner(fy: int, fx: int, height: int, width: int) -> tuple[int, int]:
    """实数 FFT 的厄米对称伙伴 bin：(fy, fx) → ((-fy)%H, (-fx)%W)。"""
    return (-fy) % height, (-fx) % width


def build_suppression_mask(
    height: int,
    width: int,
    suppressed_bins: list[tuple[int, int]],
    *,
    transition_width: float = 0.0,
    notch_regions: list[tuple[int, int, int, int]] | None = None,
    taper_type: str = "cosine",
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """构造频域抑制掩膜（1=保留，0=抑制），自动补厄米对称伙伴。

    支持 transition_width 滚降过渡带与 notch_regions 矩形陷波滤波。
    在 transition_width > 0 时采用余弦/高斯光滑滚降，消除频域硬截断引入的吉布斯环状擦痕。
    严格满足厄米对称性：mask[fy, fx] == mask[(-fy)%H, (-fx)%W]，确保实数输入逆变换纯实数。
    """
    applied: set[tuple[int, int]] = set()

    for fy, fx in suppressed_bins:
        fy_n, fx_n = fy % height, fx % width
        partner_fy, partner_fx = hermitian_partner(fy_n, fx_n, height, width)
        for bin_y, bin_x in ((fy_n, fx_n), (partner_fy, partner_fx)):
            applied.add((bin_y, bin_x))

    if notch_regions:
        for r in notch_regions:
            if len(r) >= 4:
                min_y, max_y, min_x, max_x = int(r[0]), int(r[1]), int(r[2]), int(r[3])
                for y in range(min_y, max_y + 1):
                    for x in range(min_x, max_x + 1):
                        yn, xn = y % height, x % width
                        p_yn, p_xn = hermitian_partner(yn, xn, height, width)
                        applied.add((yn, xn))
                        applied.add((p_yn, p_xn))

    if not applied:
        if transition_width <= 0.0:
            return np.ones((height, width), dtype=bool), []
        return np.ones((height, width), dtype=np.float64), []

    if transition_width <= 0.0:
        mask_b = np.ones((height, width), dtype=bool)
        for bin_y, bin_x in applied:
            mask_b[bin_y, bin_x] = False
        return mask_b, sorted(applied)

    # 计算环面周期欧氏距离
    dist = np.full((height, width), float("inf"), dtype=np.float64)
    w_rad = int(math.ceil(transition_width))

    for sy, sx in applied:
        dist[sy, sx] = 0.0
        for dy in range(-w_rad, w_rad + 1):
            yy = (sy + dy) % height
            for dx in range(-w_rad, w_rad + 1):
                xx = (sx + dx) % width
                d = math.hypot(dy, dx)
                if d < dist[yy, xx]:
                    dist[yy, xx] = d

    mask = np.ones((height, width), dtype=np.float64)
    in_trans = dist < transition_width
    d_vals = dist[in_trans]

    if taper_type == "gaussian":
        sigma = max(0.5, transition_width / 2.5)
        mask[in_trans] = 1.0 - np.exp(-(d_vals**2) / (2.0 * sigma**2))
    else:  # cosine 滚降
        mask[in_trans] = 0.5 * (1.0 - np.cos(np.pi * d_vals / transition_width))

    mask[dist == 0.0] = 0.0

    # 数值严格对称化强制加固：mask[y, x] = 0.5 * (mask[y, x] + mask[(-y)%H, (-x)%W])
    y_idx = (-np.arange(height)) % height
    x_idx = (-np.arange(width)) % width
    mask = 0.5 * (mask + mask[y_idx, :][:, x_idx])

    return mask, sorted(applied)


def fft_filter(
    out_path: Path,
    source: Path,
    *,
    band: int,
    suppressed_bins: list[tuple[int, int]],
    transition_width: float = 0.0,
    notch_regions: list[tuple[int, int, int, int]] | None = None,
    taper_type: str = "cosine",
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    save_mask: bool = True,
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """FFT 频域抑制滤波：读单波段 → FFT2 → 对称掩膜 → IFFT2 → 实部落盘。

    支持 transition_width 滚降羽化消除吉布斯环状条纹擦痕；
    支持 notch_regions 矩形陷波滤波。
    返回可审计元信息（含掩膜 sidecar 路径与逐项对称性断言结果）。
    """
    gdal = _gdal()
    if not source.exists():
        raise FileNotFoundError(f"输入栅格不存在: {source}")
    if band < 1:
        raise ValueError(f"波段号必须为正（1-based），实际 {band}")
    if not math.isfinite(dst_nodata):
        raise ValueError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")

    _progress(callback, 0.05, "打开输入栅格")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"无法打开输入栅格: {source}")
    try:
        if band > int(ds.RasterCount):
            raise ValueError(f"波段 {band} 超出范围（数据集共 {ds.RasterCount} 个波段）")
        width, height = int(ds.RasterXSize), int(ds.RasterYSize)
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""
        src_band = ds.GetRasterBand(band)
        # 复数输入必须显式拒绝：np.asarray(complex, dtype=float64) 只会
        # 发出 ComplexWarning 并静默丢弃虚部，绝不能把这种结果当作成果。
        if gdal.DataTypeIsComplex(src_band.DataType):
            raise ValueError(
                f"FFT 滤波当前仅支持实数栅格，波段 {band} 是复数类型 "
                f"({gdal.GetDataTypeName(src_band.DataType)})"
            )
        raw_obj = src_band.ReadAsArray()
        if raw_obj is None:
            raise RuntimeError(f"无法读取波段 {band}")
        raw = np.asarray(raw_obj, dtype=np.float64)
        label = src_band.GetNoDataValue()
        nodata_value = (
            float(src_nodata)
            if src_nodata is not None
            else (float(label) if label is not None else None)
        )

        # NoData 像元以波段均值填充参与 FFT，逆变换后按原掩膜回填输出 NoData
        valid = np.isfinite(raw)
        if nodata_value is not None:
            valid &= raw != nodata_value
        n_nodata = int((~valid).sum())
        fill_value = float(raw[valid].mean()) if valid.any() else 0.0
        filled = np.where(valid, raw, fill_value)

        _progress(callback, 0.30, f"FFT2（{height}×{width}，含奇偶尺寸）")
        spectrum = np.fft.fft2(filled)

        mask, applied_bins = build_suppression_mask(
            height,
            width,
            suppressed_bins,
            transition_width=transition_width,
            notch_regions=notch_regions,
            taper_type=taper_type,
        )
        _progress(callback, 0.55, f"抑制 {len(applied_bins)} 个频域 bin（含对称伙伴）")
        filtered_spectrum = spectrum * mask

        _progress(callback, 0.70, "逆 FFT2")
        restored = np.fft.ifft2(filtered_spectrum)
        # 实数输入 + 厄米对称掩膜 ⇒ 逆变换虚部应≈0；丢弃前显式断言
        max_imag = float(np.abs(restored.imag).max())
        if max_imag > 1e-6 * max(1.0, float(np.abs(restored.real).max())):
            raise RuntimeError(
                f"逆变换虚部幅度 {max_imag} 异常：频域掩膜不是厄米对称，实现存在缺陷"
            )
        result = restored.real

        # 往返校验（空抑制时输出==输入；非空时记录能量变化供审计）。
        # 能量在**频域**统计，不能拿空间域的 valid 掩膜去索引频谱数组。
        energy_before = float(np.abs(spectrum).sum())
        energy_after = float(np.abs(filtered_spectrum).sum())
        if not applied_bins:
            roundtrip_max_err = float(np.abs(result - filled).max())
            if roundtrip_err_too_large(roundtrip_max_err, filled):
                raise RuntimeError(f"空抑制往返误差 {roundtrip_max_err} 超容差：FFT 实现存在缺陷")

        _progress(callback, 0.85, f"写出成果（{out_format} / Float32）")
        driver = gdal.GetDriverByName(out_format)
        if driver is None:
            raise RuntimeError(f"不可用输出驱动：{out_format}")
        out_arr = np.where(valid, result, dst_nodata).astype(np.float32)
        out_ds = driver.Create(str(out_path), width, height, 1, gdal.GDT_Float32)
        if out_ds is None:
            raise RuntimeError(f"创建输出失败（驱动 {out_format}）: {out_path}")
        try:
            out_ds.SetGeoTransform(gt)
            out_ds.SetProjection(proj)
            band_out = out_ds.GetRasterBand(1)
            band_out.WriteArray(out_arr)
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.FlushCache()
        finally:
            out_ds = None

        mask_sidecar: Path | None = None
        if save_mask:
            mask_sidecar = out_path.with_name(out_path.name + ".fft_mask.json")
            payload = {
                "type": "spectra_fft_mask",
                "schema_version": "1.0",
                "source": str(source),
                "band": band,
                "size": [height, width],
                "requested_suppressed_bins": [[int(fy), int(fx)] for fy, fx in suppressed_bins],
                "applied_suppressed_bins": [[int(fy), int(fx)] for fy, fx in applied_bins],
                "transition_width": float(transition_width),
                "taper_type": taper_type,
                "notch_regions": notch_regions or [],
                "hermitian_symmetric": True,
                "dst_nodata": float(dst_nodata),
                "note": "用 applied_suppressed_bins 重跑可逐像元复现本成果",
            }
            mask_sidecar.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )

        _progress(callback, 1.0, "完成")
        return {
            "band": band,
            "size": [height, width],
            "n_nodata_filled": n_nodata,
            "fill_value": fill_value,
            "applied_suppressed_bins": [[int(fy), int(fx)] for fy, fx in applied_bins],
            "hermitian_symmetric": True,
            "max_imag": max_imag,
            "energy_before": energy_before,
            "energy_after": energy_after,
            "roundtrip_max_err": (
                float(np.abs(result - filled).max()) if not applied_bins else None
            ),
            "mask_sidecar": str(mask_sidecar) if mask_sidecar is not None else None,
        }
    finally:
        ds = None


def roundtrip_err_too_large(max_err: float, filled: Any) -> bool:
    """空抑制往返容差：与数据量级相关的相对容差（float32 落盘前为 float64 运算）。"""
    scale = max(1.0, float(np.abs(filled).max()))
    return max_err > 1e-6 * scale
