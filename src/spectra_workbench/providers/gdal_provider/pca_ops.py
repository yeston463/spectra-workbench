"""P6-03 PCA 主成分变换与逆变换（numpy.linalg.eigh）。

数学定义（对应验收「PCA 完整重建与降维误差符合定义」）
------------------------------------------------------
- 输入数据 ``X`` 形状为 ``(n_bands, n_pixels)``，只用有效像元（所有波段
  NoData/非有限值均排除）参与统计；
- **协方差模式**：``mean = mean(X)``，``C = (X-mean)(X-mean)^T / n_valid``
  （总体协方差，ddof=0）；特征分解 ``C = V diag(λ) V^T``，按 λ 降序排列；
- **相关矩阵模式**：先按波段标准化 ``Z = (X-mean)/std``（std 为总体标准差，
  ddof=0），再对 ``Z`` 做同样的协方差特征分解；``std=0`` 的波段明确拒绝；
- 前 k 个主成分：``Y_k = V_k^T (X-mean)``（相关模式为 ``V_k^T Z``）；
- 逆变换：``X_hat = V_k Y_k + mean``（相关模式为 ``V_k Y_k * std + mean``）；
- **完整重建**（k=n）误差应为浮点噪声量级；**降维误差**定义为
  ``RMSE = sqrt(mean((X - X_hat)^2))``（对有效像元与全部波段取平均），
  ``relative_RMSE = RMSE / RMS(X)``；同时报告被丢弃特征值之和
  ``discarded_variance = sum(λ[k:])``，供解释与核对；
- 特征向量符号不参与误差判据；为可复现性，统一约定每个主成分中绝对值最大的
  载荷为正（``SIGN_CONVENTION``）。

边界（如实记录）
----------------
- 需要整幅读入内存（PCA 统计量必须覆盖全图；分块统计可以作为后续优化，
  但绝不允许对每块独立做 PCA 后冒充全图结果）；
- 输出恒为单波段/多波段 Float32；无效像元输出 NoData；
- 逆变换依赖正变换写出的 ``<成果名>.pca.json`` 变换文件；
- 复数输入明确拒绝（输出是实数 Float32，不能静默丢弃虚部）。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "PCAError",
    "PCA_TRANSFORM_SCHEMA",
    "PCA_TRANSFORM_TYPE",
    "SIGN_CONVENTION",
    "fit_pca",
    "load_transform",
    "project_pca",
    "reconstruct_pca",
    "reconstruction_metrics",
    "run_pca_forward",
    "run_pca_inverse",
]

PCA_TRANSFORM_TYPE = "spectra_pca_transform"
PCA_TRANSFORM_SCHEMA = "1.0"
SIGN_CONVENTION = "largest absolute loading positive"
PCA_MATRICES = ("covariance", "correlation")


class PCAError(ValueError):
    """PCA 参数、输入或变换文件不合法。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（PCA 阶段）")


def _normalize_matrix(matrix: str) -> str:
    normalized = str(matrix or "covariance").strip().lower()
    if normalized not in PCA_MATRICES:
        raise PCAError(
            f"未知 PCA 矩阵类型 {matrix!r}；可用：{', '.join(PCA_MATRICES)}"
        )
    return normalized


def _fix_signs(eigenvectors: np.ndarray) -> np.ndarray:
    """逐主成分把绝对值最大的载荷翻成正号，保证结果可复现。"""
    fixed = eigenvectors.copy()
    for row in range(fixed.shape[0]):
        idx = int(np.argmax(np.abs(fixed[row])))
        if fixed[row, idx] < 0:
            fixed[row] = -fixed[row]
    return fixed


def fit_pca(
    data: np.ndarray,
    *,
    valid: np.ndarray | None = None,
    matrix: str = "covariance",
) -> dict[str, Any]:
    """拟合 PCA，返回均值、标准差、特征值、特征向量与解释方差。

    ``data`` 形状必须为 ``(n_bands, n_pixels)``；``valid`` 为逐像元布尔掩膜。
    返回的 ``eigenvectors`` 形状为 ``(n_bands, n_bands)``，每行一个主成分。
    """
    matrix_name = _normalize_matrix(matrix)
    arr = np.asarray(data, dtype=np.float64)
    if arr.ndim != 2 or arr.shape[0] < 2:
        raise PCAError(f"PCA 需要至少 2 个波段，实际形状 {arr.shape}")
    if valid is None:
        mask = np.ones(arr.shape[1], dtype=bool)
    else:
        mask = np.asarray(valid, dtype=bool)
        if mask.shape != (arr.shape[1],):
            raise PCAError("valid 掩膜必须与像元数一致")
    n_valid = int(mask.sum())
    if n_valid < 2:
        raise PCAError(f"有效像元不足（{n_valid} < 2），无法估计协方差")

    samples = arr[:, mask]
    mean = samples.mean(axis=1)
    centered = samples - mean[:, None]
    std = np.ones(arr.shape[0], dtype=np.float64)
    if matrix_name == "correlation":
        std = centered.std(axis=1, ddof=0)
        zero_std = np.isclose(std, 0.0, atol=1e-15)
        if zero_std.any():
            bad = [str(i + 1) for i in np.flatnonzero(zero_std)]
            raise PCAError(
                "相关矩阵模式要求每个波段方差非零；零方差波段："
                + ", ".join(bad)
            )
        standardized = centered / std[:, None]
        covariance = standardized @ standardized.T / n_valid
    else:
        covariance = centered @ centered.T / n_valid

    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = eigenvalues[order]
    eigenvectors = _fix_signs(eigenvectors[:, order].T)

    total = float(eigenvalues.sum())
    explained = eigenvalues / total if total > 0 else np.zeros_like(eigenvalues)

    return {
        "matrix": matrix_name,
        "band_count": int(arr.shape[0]),
        "n_valid": n_valid,
        "mean": mean,
        "std": std,
        "eigenvalues": eigenvalues,
        "eigenvectors": eigenvectors,
        "explained_variance_ratio": explained,
        "total_variance": total,
    }


def _component_count(fit: dict[str, Any], components: int | None) -> int:
    n_bands = int(fit["band_count"])
    if components is None:
        return n_bands
    try:
        k = int(components)
    except (TypeError, ValueError) as exc:
        raise PCAError(f"components 必须是整数，实际 {components!r}") from exc
    if k < 1 or k > n_bands:
        raise PCAError(f"components 必须在 [1, {n_bands}]，实际 {k}")
    return k


def project_pca(
    data: np.ndarray,
    fit: dict[str, Any],
    *,
    components: int | None = None,
) -> np.ndarray:
    """把 ``(n_bands, n_pixels)`` 数据投影为 ``(k, n_pixels)`` 主成分。"""
    arr = np.asarray(data, dtype=np.float64)
    n_bands = int(fit["band_count"])
    if arr.ndim != 2 or arr.shape[0] != n_bands:
        raise PCAError(f"投影数据形状 {arr.shape} 与变换的 {n_bands} 个波段不匹配")
    k = _component_count(fit, components)
    centered = arr - np.asarray(fit["mean"], dtype=np.float64)[:, None]
    if fit["matrix"] == "correlation":
        centered = centered / np.asarray(fit["std"], dtype=np.float64)[:, None]
    eigenvectors = np.asarray(fit["eigenvectors"], dtype=np.float64)[:k]
    return eigenvectors @ centered


def reconstruct_pca(
    components: np.ndarray,
    fit: dict[str, Any],
    *,
    components_used: int | None = None,
) -> np.ndarray:
    """把 ``(k, n_pixels)`` 主成分逆变换回 ``(n_bands, n_pixels)``。"""
    arr = np.asarray(components, dtype=np.float64)
    k = _component_count(fit, components_used)
    if arr.ndim != 2 or arr.shape[0] != k:
        raise PCAError(f"主成分数据形状 {arr.shape} 与使用成分数 {k} 不匹配")
    eigenvectors = np.asarray(fit["eigenvectors"], dtype=np.float64)[:k]
    reconstructed = eigenvectors.T @ arr
    if fit["matrix"] == "correlation":
        reconstructed = reconstructed * np.asarray(fit["std"], dtype=np.float64)[:, None]
    reconstructed = reconstructed + np.asarray(fit["mean"], dtype=np.float64)[:, None]
    return reconstructed


def reconstruction_metrics(
    data: np.ndarray,
    reconstructed: np.ndarray,
    valid: np.ndarray,
) -> dict[str, float]:
    """按文档定义计算 RMSE 与相对 RMSE（仅有效像元、全部波段）。"""
    arr = np.asarray(data, dtype=np.float64)
    rec = np.asarray(reconstructed, dtype=np.float64)
    mask = np.asarray(valid, dtype=bool)
    if arr.shape != rec.shape or arr.shape[1] != mask.shape[0]:
        raise PCAError("重建误差计算的数据/掩膜形状不一致")
    diff = arr[:, mask] - rec[:, mask]
    rmse = float(np.sqrt(np.mean(diff * diff)))
    rms_input = float(np.sqrt(np.mean(arr[:, mask] * arr[:, mask])))
    relative = rmse / max(rms_input, 1e-12)
    return {"reconstruction_rmse": rmse, "reconstruction_relative_rmse": relative}


def _open_source(source: Path) -> Any:
    gdal = _gdal()
    if not source.exists():
        raise FileNotFoundError(f"输入栅格不存在: {source}")
    ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if ds is None:
        raise PCAError(f"无法打开输入栅格: {source}")
    return ds


def _read_bands(
    ds: Any,
    band_indices: list[int],
    src_nodata: float | None,
) -> tuple[np.ndarray, np.ndarray, list[float | None]]:
    """读取 1-based 波段为 ``(n_bands, n_pixels)``，并构造联合有效掩膜。"""
    gdal = _gdal()
    n_bands = int(ds.RasterCount)
    arrays: list[np.ndarray] = []
    labels: list[float | None] = []
    valid = np.ones(int(ds.RasterXSize) * int(ds.RasterYSize), dtype=bool)
    for band_index in band_indices:
        if band_index < 1 or band_index > n_bands:
            raise PCAError(f"波段 {band_index} 超出范围（数据集共 {n_bands} 个波段）")
        band = ds.GetRasterBand(band_index)
        if gdal.DataTypeIsComplex(band.DataType):
            raise PCAError(
                f"PCA 当前仅支持实数波段，波段 {band_index} 是复数类型 "
                f"({gdal.GetDataTypeName(band.DataType)})"
            )
        raw_obj = band.ReadAsArray()
        if raw_obj is None:
            raise PCAError(f"无法读取波段 {band_index}")
        raw = np.asarray(raw_obj, dtype=np.float64).ravel()
        label = band.GetNoDataValue()
        labels.append(float(label) if label is not None else None)
        nodata = float(src_nodata) if src_nodata is not None else labels[-1]
        band_valid = np.isfinite(raw)
        if nodata is not None:
            band_valid &= raw != nodata
        valid &= band_valid
        arrays.append(raw)
    return np.vstack(arrays), valid, labels


def _transform_payload(
    fit: dict[str, Any],
    *,
    source: Path,
    components: int,
    src_nodata: float | None,
    dst_nodata: float,
    metrics: dict[str, float],
) -> dict[str, Any]:
    eigenvalues = np.asarray(fit["eigenvalues"], dtype=np.float64)
    discarded = float(eigenvalues[components:].sum()) if components < len(eigenvalues) else 0.0
    return {
        "type": PCA_TRANSFORM_TYPE,
        "schema_version": PCA_TRANSFORM_SCHEMA,
        "created_by": "spectra-workbench P6-03",
        "source": str(source),
        "band_count": int(fit["band_count"]),
        "n_valid": int(fit["n_valid"]),
        "matrix": fit["matrix"],
        "components": int(components),
        "mean": [float(v) for v in fit["mean"]],
        "std": [float(v) for v in fit["std"]],
        "eigenvalues": [float(v) for v in eigenvalues],
        "eigenvectors": [
            [float(v) for v in row]
            for row in np.asarray(fit["eigenvectors"], dtype=np.float64)
        ],
        "explained_variance_ratio": [
            float(v) for v in np.asarray(fit["explained_variance_ratio"], dtype=np.float64)
        ],
        "total_variance": float(fit["total_variance"]),
        "discarded_variance": discarded,
        "reconstruction_rmse": float(metrics["reconstruction_rmse"]),
        "reconstruction_relative_rmse": float(metrics["reconstruction_relative_rmse"]),
        "sign_convention": SIGN_CONVENTION,
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )


def run_pca_forward(
    out_path: Path,
    source: Path,
    *,
    components: int | None = None,
    matrix: str = "covariance",
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """全图 PCA 正变换：写主成分栅格与可复现的变换 sidecar。"""
    gdal = _gdal()
    if not math.isfinite(dst_nodata):
        raise PCAError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    _progress(callback, 0.05, "打开输入栅格")
    ds = _open_source(source)
    try:
        n_bands = int(ds.RasterCount)
        if n_bands < 2:
            raise PCAError(f"PCA 需要至少 2 个波段，实际 {n_bands}")
        width, height = int(ds.RasterXSize), int(ds.RasterYSize)
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""
        _progress(callback, 0.15, f"读取 {n_bands} 个波段")
        data, valid, _labels = _read_bands(ds, list(range(1, n_bands + 1)), src_nodata)
    finally:
        ds = None

    _progress(callback, 0.40, f"拟合 PCA（{matrix}）")
    fit = fit_pca(data, valid=valid, matrix=matrix)
    k = _component_count(fit, components)
    _progress(callback, 0.60, f"投影前 {k} 个主成分")
    projected = project_pca(data, fit, components=k)
    reconstruction = reconstruct_pca(projected, fit, components_used=k)
    metrics = reconstruction_metrics(data, reconstruction, valid)

    _progress(callback, 0.80, f"写出成果（{out_format} / Float32，{k} 波段）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise PCAError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), width, height, k, gdal.GDT_Float32)
    if out_ds is None:
        raise PCAError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    eigenvalues = np.asarray(fit["eigenvalues"], dtype=np.float64)
    explained = np.asarray(fit["explained_variance_ratio"], dtype=np.float64)
    try:
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(proj)
        out_ds.SetMetadataItem("PCA_MODE", "forward")
        out_ds.SetMetadataItem("PCA_MATRIX", str(fit["matrix"]))
        out_ds.SetMetadataItem("PCA_COMPONENTS", str(k))
        out_ds.SetMetadataItem("PCA_SIGN_CONVENTION", SIGN_CONVENTION)
        for idx in range(k):
            band_out = out_ds.GetRasterBand(idx + 1)
            values = np.where(valid, projected[idx], dst_nodata).astype(np.float32)
            band_out.WriteArray(values.reshape(height, width))
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.SetDescription(f"PC{idx + 1}")
            band_out.SetMetadataItem("EIGENVALUE", f"{eigenvalues[idx]:.12g}")
            band_out.SetMetadataItem(
                "EXPLAINED_VARIANCE_RATIO", f"{explained[idx]:.12g}"
            )
            band_out.SetMetadataItem("PCA_MATRIX", str(fit["matrix"]))
            band_out.FlushCache()
    finally:
        out_ds = None

    payload = _transform_payload(
        fit,
        source=source,
        components=k,
        src_nodata=src_nodata,
        dst_nodata=dst_nodata,
        metrics=metrics,
    )
    sidecar = out_path.with_name(out_path.name + ".pca.json")
    _write_json(sidecar, payload)
    _progress(callback, 1.0, "完成")
    return {
        "mode": "forward",
        "band_count": int(fit["band_count"]),
        "components": k,
        "matrix": fit["matrix"],
        "n_valid": int(fit["n_valid"]),
        "discarded_variance": payload["discarded_variance"],
        "reconstruction_rmse": metrics["reconstruction_rmse"],
        "reconstruction_relative_rmse": metrics["reconstruction_relative_rmse"],
        "transform_sidecar": str(sidecar),
    }


def load_transform(path: Path) -> dict[str, Any]:
    """读取并校验 PCA 正变换 sidecar；返回含 numpy 数组的变换字典。"""
    if not path.exists():
        raise PCAError(f"PCA 变换文件不存在: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PCAError(f"无法读取 PCA 变换文件 {path}: {exc}") from exc
    if payload.get("type") != PCA_TRANSFORM_TYPE:
        raise PCAError(f"不是 PCA 变换文件（type={payload.get('type')!r}）: {path}")
    if str(payload.get("schema_version")) != PCA_TRANSFORM_SCHEMA:
        raise PCAError(
            f"不支持的 PCA 变换文件版本 {payload.get('schema_version')!r}"
        )
    matrix = _normalize_matrix(str(payload.get("matrix") or "covariance"))
    try:
        mean = np.asarray(payload["mean"], dtype=np.float64)
        std = np.asarray(payload.get("std", np.ones_like(mean)), dtype=np.float64)
        eigenvalues = np.asarray(payload["eigenvalues"], dtype=np.float64)
        eigenvectors = np.asarray(payload["eigenvectors"], dtype=np.float64)
        band_count = int(payload["band_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PCAError(f"PCA 变换文件字段不完整或类型错误: {exc}") from exc
    if band_count < 2:
        raise PCAError(f"PCA 变换文件的 band_count 必须 ≥ 2，实际 {band_count}")
    if mean.shape != (band_count,) or std.shape != (band_count,):
        raise PCAError("PCA 变换文件的 mean/std 长度与 band_count 不一致")
    if eigenvalues.shape != (band_count,):
        raise PCAError("PCA 变换文件的 eigenvalues 长度与 band_count 不一致")
    if eigenvectors.shape != (band_count, band_count):
        raise PCAError("PCA 变换文件的 eigenvectors 必须是 band_count × band_count")
    if matrix == "correlation" and np.isclose(std, 0.0, atol=1e-15).any():
        raise PCAError("相关矩阵模式的 PCA 变换文件含零标准差波段")
    orthogonality = eigenvectors @ eigenvectors.T
    if not np.allclose(orthogonality, np.eye(band_count), atol=1e-8):
        raise PCAError("PCA 变换文件的特征向量不是正交矩阵，文件可能已损坏")
    components = int(payload.get("components") or band_count)
    if components < 1 or components > band_count:
        raise PCAError(f"PCA 变换文件的 components 非法: {components}")
    return {
        "path": path,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "matrix": matrix,
        "band_count": band_count,
        "components": components,
        "mean": mean,
        "std": std,
        "eigenvalues": eigenvalues,
        "eigenvectors": eigenvectors,
        "explained_variance_ratio": np.asarray(
            payload.get("explained_variance_ratio", []), dtype=np.float64
        ),
        "total_variance": float(payload.get("total_variance") or 0.0),
        "discarded_variance": float(payload.get("discarded_variance") or 0.0),
        "source": str(payload.get("source") or ""),
    }


def run_pca_inverse(
    out_path: Path,
    source: Path,
    transform_path: Path,
    *,
    components: int | None = None,
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """PCA 逆变换：读主成分栅格 + 变换 sidecar，重建原始波段。"""
    gdal = _gdal()
    if not math.isfinite(dst_nodata):
        raise PCAError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    _progress(callback, 0.05, "读取 PCA 变换文件")
    fit = load_transform(transform_path)
    _progress(callback, 0.15, "打开主成分栅格")
    ds = _open_source(source)
    try:
        available = int(ds.RasterCount)
        n_bands = int(fit["band_count"])
        if components is None:
            k = min(available, int(fit["components"]))
        else:
            k = int(components)
            if k < 1 or k > min(available, n_bands):
                raise PCAError(
                    f"components 必须在 [1, {min(available, n_bands)}]，实际 {k}"
                )
        width, height = int(ds.RasterXSize), int(ds.RasterYSize)
        gt = tuple(float(v) for v in (ds.GetGeoTransform() or ()))
        proj = ds.GetProjectionRef() or ""
        _progress(callback, 0.30, f"读取 {k} 个主成分")
        comps, valid, _labels = _read_bands(
            ds, list(range(1, k + 1)), src_nodata
        )
    finally:
        ds = None

    _progress(callback, 0.60, f"逆变换 {k} 个主成分")
    reconstructed = reconstruct_pca(comps, fit, components_used=k)

    _progress(callback, 0.80, f"写出重建成果（{out_format} / Float32，{n_bands} 波段）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise PCAError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), width, height, n_bands, gdal.GDT_Float32)
    if out_ds is None:
        raise PCAError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(gt)
        out_ds.SetProjection(proj)
        out_ds.SetMetadataItem("PCA_MODE", "inverse")
        out_ds.SetMetadataItem("PCA_MATRIX", str(fit["matrix"]))
        out_ds.SetMetadataItem("PCA_COMPONENTS", str(k))
        out_ds.SetMetadataItem("PCA_TRANSFORM_SHA256", fit["sha256"])
        for idx in range(n_bands):
            band_out = out_ds.GetRasterBand(idx + 1)
            values = np.where(valid, reconstructed[idx], dst_nodata).astype(np.float32)
            band_out.WriteArray(values.reshape(height, width))
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.SetDescription(f"PCA_recon_B{idx + 1}")
            band_out.FlushCache()
    finally:
        out_ds = None

    payload = {
        "type": "spectra_pca_inverse",
        "schema_version": PCA_TRANSFORM_SCHEMA,
        "created_by": "spectra-workbench P6-03",
        "transform_file": str(transform_path),
        "transform_sha256": fit["sha256"],
        "transform_source": fit["source"],
        "matrix": fit["matrix"],
        "band_count": n_bands,
        "components_used": k,
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
    }
    sidecar = out_path.with_name(out_path.name + ".pca_inverse.json")
    _write_json(sidecar, payload)
    _progress(callback, 1.0, "完成")
    return {
        "mode": "inverse",
        "band_count": n_bands,
        "components_used": k,
        "matrix": fit["matrix"],
        "transform_sha256": fit["sha256"],
        "inverse_sidecar": str(sidecar),
    }
