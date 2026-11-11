r"""P11-05：高光谱与多光谱分析链。

覆盖遥感教学核心算法：
1. **ROI 端元采集器（ROI Endmember Extractor）**：
   从多边形 ROI 区域采样计算纯净地类均值/分位数，为 FVC（像元二分法）与线性混合像元分解提供权威真实端元；
2. **强水汽吸收谷自动识别与排除（Water Absorption Masking）**：
   自动检测并标识 1.35~1.45 μm (1400 nm) 与 1.80~1.95 μm (1900 nm) 强吸收严重失真波段；
3. **SAM（Spectral Angle Mapper / 光谱角制图）**：
   计算像元连续光谱与参考端元光谱的广义夹角 $\theta = \arccos\left(\frac{X \cdot S}{\|X\|_2 \|S\|_2}\right)$，
   输出广义夹角制图栅格（Float32，弧度/度），不受光照阴影强弱干扰；
4. **Savitzky-Golay 像元连续光谱平滑**：
   基于时域/频域多项式最小二乘滑动平滑，保持吸收特征谷深度同时滤除探测器高频噪声。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "extract_roi_endmember",
    "identify_water_absorption_bands",
    "run_sam",
    "savgol_filter_spectral",
]


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（高光谱分析阶段）")


def identify_water_absorption_bands(
    wavelengths: list[float],
    *,
    valley_1400: tuple[float, float] = (1340.0, 1460.0),
    valley_1900: tuple[float, float] = (1790.0, 1960.0),
) -> list[int]:
    """依据波长清单（单位 nm）自动识别强水汽吸收谷波段（1-based 序号）。"""
    bad_indices: list[int] = []
    for idx, wl in enumerate(wavelengths):
        w = float(wl)
        # 若以微米 (μm) 给出，归一化到纳米 (nm)
        if w < 10.0:
            w *= 1000.0
        if (valley_1400[0] <= w <= valley_1400[1]) or (valley_1900[0] <= w <= valley_1900[1]):
            bad_indices.append(idx + 1)
    return bad_indices


def savgol_filter_spectral(
    cube: np.ndarray,
    window_length: int = 5,
    polyorder: int = 2,
) -> np.ndarray:
    """沿波段维度（axis 0）执行 Savitzky-Golay 多项式最小二乘平滑滤波。"""
    bands, height, width = cube.shape
    if window_length >= bands or window_length < 3 or window_length % 2 == 0:
        return cube.copy()

    # 构造 Savitzky-Golay 卷积核矩阵
    half = window_length // 2
    x = np.arange(-half, half + 1)
    # Vandermonde 矩阵: A[i, j] = x[i]^j
    A = np.vander(x, polyorder + 1)[:, ::-1]
    # 最小二乘投影矩阵: (A^T A)^(-1) A^T 的第 0 行对应平滑卷积核
    pinv = np.linalg.pinv(A)
    kernel = pinv[0]  # (window_length,)

    # 沿第 0 轴（波段轴）应用 1D 卷积
    smoothed = np.empty_like(cube, dtype=np.float32)
    # 边缘复制填充
    padded = np.pad(cube, ((half, half), (0, 0), (0, 0)), mode="edge")

    for b in range(bands):
        # 取窗口
        window_slice = padded[b : b + window_length]  # (W, H, Width)
        # 广播乘积与加权求和
        smoothed[b] = np.tensordot(kernel, window_slice, axes=(0, 0))

    return smoothed


def extract_roi_endmember(
    source: Path,
    roi_polygon: list[tuple[float, float]],
    *,
    roi_space: str = "pixel",
    bands: list[int] | None = None,
) -> dict[str, Any]:
    """从 ROI 多边形像元内提取多波段纯净端元光谱曲线及统计特征。"""
    gdal = _gdal()
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"无法打开栅格源: {source}")

    try:
        w, h, count = int(ds.RasterXSize), int(ds.RasterYSize), int(ds.RasterCount)
        gt = ds.GetGeoTransform()
        target_bands = bands or list(range(1, count + 1))

        # 转换 ROI 顶点为像素坐标
        pts = np.asarray(roi_polygon, dtype=np.float64)
        if roi_space == "map" and gt:
            # 仿射逆变换：map -> pixel
            det = gt[1] * gt[5] - gt[2] * gt[4]
            if abs(det) > 1e-12:
                col = (gt[5] * (pts[:, 0] - gt[0]) - gt[2] * (pts[:, 1] - gt[3])) / det
                row = (-gt[4] * (pts[:, 0] - gt[0]) + gt[1] * (pts[:, 1] - gt[3])) / det
                pts = np.column_stack([col, row])

        # 光栅化 ROI 多边形为布尔掩膜
        min_x, max_x = (
            max(0, int(math.floor(pts[:, 0].min()))),
            min(w - 1, int(math.ceil(pts[:, 0].max()))),
        )
        min_y, max_y = (
            max(0, int(math.floor(pts[:, 1].min()))),
            min(h - 1, int(math.ceil(pts[:, 1].max()))),
        )

        if max_x < min_x or max_y < min_y:
            raise ValueError("ROI 在当前栅格有效外包盒内无相交像元")

        box_w = max_x - min_x + 1
        box_h = max_y - min_y + 1

        # 点在多边形内简易光栅化
        from matplotlib.path import Path as MplPath  # type: ignore[import-not-found]

        poly_path = MplPath(pts)
        grid_y, grid_x = np.mgrid[min_y : max_y + 1, min_x : max_x + 1]
        grid_pts = np.column_stack([grid_x.ravel(), grid_y.ravel()])
        inside = poly_path.contains_points(grid_pts).reshape((box_h, box_w))

        if not inside.any():
            raise ValueError("ROI 多边形内部不包含有效像元中心")

        mean_spectrum: list[float] = []
        std_spectrum: list[float] = []

        for b_idx in target_bands:
            band = ds.GetRasterBand(b_idx)
            block = band.ReadAsArray(min_x, min_y, box_w, box_h)
            if block is None:
                continue
            b_arr = np.asarray(block, dtype=np.float64)
            nd = band.GetNoDataValue()
            valid = np.isfinite(b_arr) & inside
            if nd is not None:
                valid &= b_arr != float(nd)

            pixels = b_arr[valid]
            if pixels.size:
                mean_spectrum.append(float(pixels.mean()))
                std_spectrum.append(float(pixels.std()))
            else:
                mean_spectrum.append(0.0)
                std_spectrum.append(0.0)

        return {
            "source": str(source),
            "pixel_count": int(inside.sum()),
            "bands": target_bands,
            "mean_spectrum": mean_spectrum,
            "std_spectrum": std_spectrum,
        }
    finally:
        ds = None


def run_sam(
    out_path: Path,
    source: Path,
    reference_spectrum: list[float],
    *,
    angle_unit: str = "radians",
    max_angle: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """执行光谱角制图（Spectral Angle Mapper / SAM）。

    计算全图各像元与参考端元光谱的广义夹角，输出单波段 Float32 弧度/度栅格。
    """
    gdal = _gdal()
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"无法打开输入栅格: {source}")

    try:
        w, h, count = int(ds.RasterXSize), int(ds.RasterYSize), int(ds.RasterCount)
        gt = ds.GetGeoTransform()
        proj = ds.GetProjectionRef()

        ref_s = np.asarray(reference_spectrum, dtype=np.float64)
        if ref_s.size != count:
            raise ValueError(f"参考光谱波段数 ({ref_s.size}) 与影像波段数 ({count}) 不一致")

        ref_norm = float(np.linalg.norm(ref_s))
        if ref_norm <= 1e-12:
            raise ValueError("参考端元光谱模长为 0，无法计算光谱角")

        _progress(callback, 0.20, "整幅读取多光谱/高光谱像元立方体")
        bands_data = []
        valid_mask = np.ones((h, w), dtype=bool)

        for b in range(1, count + 1):
            band = ds.GetRasterBand(b)
            arr = band.ReadAsArray()
            if arr is None:
                raise RuntimeError(f"读取波段 {b} 失败")
            b_data = np.asarray(arr, dtype=np.float64)
            nd = band.GetNoDataValue()
            if nd is not None:
                valid_mask &= b_data != float(nd)
            valid_mask &= np.isfinite(b_data)
            bands_data.append(b_data)

        cube = np.stack(bands_data, axis=0)  # (B, H, W)

        _progress(callback, 0.50, "向量化计算多波段光谱角夹角余弦")
        # 点积: sum_b (X_b * S_b)
        dot_product = np.tensordot(ref_s, cube, axes=(0, 0))  # (H, W)
        # 像元光谱模长: sqrt(sum_b X_b^2)
        pixel_norm = np.sqrt(np.sum(cube**2, axis=0))  # (H, W)

        # 避免除以零
        denom = pixel_norm * ref_norm
        valid_calc = valid_mask & (denom > 1e-12)

        cos_theta = np.zeros((h, w), dtype=np.float64)
        cos_theta[valid_calc] = dot_product[valid_calc] / denom[valid_calc]
        # 严格截断至 [-1.0, 1.0] 防止反余弦浮点越界 NaN
        cos_theta = np.clip(cos_theta, -1.0, 1.0)

        # 光谱角计算 (弧度)
        angle_arr = np.full((h, w), dst_nodata, dtype=np.float32)
        angles_rad = np.arccos(cos_theta[valid_calc])

        if angle_unit == "degrees":
            angle_arr[valid_calc] = np.degrees(angles_rad).astype(np.float32)
        else:
            angle_arr[valid_calc] = angles_rad.astype(np.float32)

        # 若指定了 max_angle 阈值过滤，将超过阈值的像元设为 NoData
        if max_angle is not None:
            angle_arr[angle_arr > float(max_angle)] = float(dst_nodata)

        _progress(callback, 0.85, f"写出光谱角成果栅格（{out_format}）")
        driver = gdal.GetDriverByName(out_format)
        if driver is None:
            raise RuntimeError(f"不可用输出驱动: {out_format}")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_ds = driver.Create(str(out_path), w, h, 1, gdal.GDT_Float32)
        if out_ds is None:
            raise RuntimeError(f"创建输出栅格失败: {out_path}")

        try:
            if gt:
                out_ds.SetGeoTransform(gt)
            if proj:
                out_ds.SetProjection(proj)
            out_b = out_ds.GetRasterBand(1)
            out_b.WriteArray(angle_arr)
            out_b.SetNoDataValue(float(dst_nodata))
            out_b.FlushCache()
        finally:
            out_ds = None

        # 写入 sidecar 审计记录
        sidecar_path = out_path.with_name(out_path.name + ".sam.json")
        matched_count = int((angle_arr != dst_nodata).sum())
        audit_payload = {
            "type": "spectra_spectral_angle_mapper",
            "schema_version": "1.0",
            "source": str(source),
            "reference_spectrum": [float(x) for x in reference_spectrum],
            "angle_unit": angle_unit,
            "max_angle": max_angle,
            "total_pixels": w * h,
            "matched_pixels": matched_count,
            "mean_angle": float(angle_arr[angle_arr != dst_nodata].mean())
            if matched_count
            else None,
            "min_angle": float(angle_arr[angle_arr != dst_nodata].min()) if matched_count else None,
        }
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(audit_payload, f, indent=2, ensure_ascii=False)

        _progress(callback, 1.0, "光谱角制图完成")
        return {
            "out_path": str(out_path),
            "sidecar_path": str(sidecar_path),
            "matched_pixels": matched_count,
            "mean_angle": audit_payload["mean_angle"],
        }
    finally:
        ds = None
