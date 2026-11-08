"""P6-04 GCP 配准：控制点拟合、独立检查点、仿射/二次多项式重采样。

设计
----
- GCP 文件为 JSON：``points`` 每项含 ``pixel``/``line``（源影像 0-based 像素坐标，
  允许小数）与 ``x``/``y``（参考影像 CRS 的地图坐标），``role`` 为
  ``control``（参与拟合）或 ``check``（独立检查点，不参与拟合）；
- 正变换：源像素 → 地图坐标，用控制点做最小二乘（affine=1 阶，polynomial2=2 阶）；
- 反变换：地图坐标 → 源像素，单独用控制点拟合（保证重采样可直接求逆，
  同时报告反变换拟合/检查点残差）；
- 输出网格 = 参考影像网格；逐输出像元中心取地图坐标 → 反变换到源像素 →
  nearest/bilinear 采样；越界或邻域含 NoData 的像元输出 NoData；
- 残差报告把「拟合残差」与「独立检查点误差」分开，单位分别为地图单位和源像素；
- 当前实现整幅读入源影像（自定义重采样），大影像分块/多线程归后续；绝不
  用控制点误差冒充独立检查点误差。

GCP JSON 结构::

    {
      "type": "spectra_gcp_set",
      "schema_version": "1.0",
      "srs": "EPSG:32650",
      "points": [
        {"id": "c1", "pixel": 0.5, "line": 0.5, "x": 500000.0, "y": 3000000.0,
         "role": "control"},
        {"id": "k1", "pixel": 9.5, "line": 9.5, "x": 500095.0, "y": 2999905.0,
         "role": "check"}
      ]
    }
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

__all__ = [
    "GCPError",
    "GCP_TRANSFORMS",
    "apply_polynomial",
    "fit_polynomial",
    "load_gcp_set",
    "run_gcp_registration",
]

GCP_TRANSFORMS: tuple[str, ...] = ("affine", "polynomial2")
_GCP_ORDER = {"affine": 1, "polynomial2": 2}
_RESAMPLE_ALGS = ("nearest", "bilinear")


class GCPError(ValueError):
    """GCP 文件、拟合或参数错误。"""


def _gdal() -> Any:
    from osgeo import gdal  # type: ignore[import-not-found]

    gdal.UseExceptions()
    return gdal


def _progress(callback: Callable[..., Any] | None, ratio: float, message: str) -> None:
    if callback is None:
        return
    if callback(ratio, message, None) == 0:
        raise RuntimeError("任务已取消（GCP 配准阶段）")


def _finite(value: Any, name: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise GCPError(f"{name} 必须是数值，实际 {value!r}") from exc
    if not math.isfinite(v):
        raise GCPError(f"{name} 必须是有限值，实际 {value!r}")
    return v


def load_gcp_set(path: Path) -> dict[str, Any]:
    """读取并校验 GCP JSON；返回控制点/检查点与声明 SRS。"""
    if not path.exists():
        raise GCPError(f"GCP 文件不存在: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GCPError(f"无法读取 GCP 文件 {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("points"), list):
        raise GCPError("GCP 文件必须包含 points 列表")
    controls: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(payload["points"]):
        if not isinstance(raw, dict):
            raise GCPError(f"points[{index}] 必须是对象")
        gid = str(raw.get("id") or f"p{index}")
        if gid in seen_ids:
            raise GCPError(f"GCP id 重复: {gid}")
        seen_ids.add(gid)
        point: dict[str, Any] = {
            "id": gid,
            "pixel": _finite(raw.get("pixel"), f"{gid}.pixel"),
            "line": _finite(raw.get("line"), f"{gid}.line"),
            "x": _finite(raw.get("x"), f"{gid}.x"),
            "y": _finite(raw.get("y"), f"{gid}.y"),
        }
        role = str(raw.get("role") or "control").strip().lower()
        if role in ("control", "controls", "gcp"):
            controls.append(point)
        elif role in ("check", "checkpoint", "checks"):
            checks.append(point)
        else:
            raise GCPError(f"{gid}.role 必须是 control 或 check，实际 {role!r}")
    if not controls:
        raise GCPError("GCP 文件缺少 control 点")
    return {
        "srs": str(payload.get("srs") or "").strip(),
        "controls": controls,
        "checks": checks,
    }


def _basis(u: np.ndarray, v: np.ndarray, order: int) -> np.ndarray:
    u_flat = np.asarray(u, dtype=np.float64).ravel()
    v_flat = np.asarray(v, dtype=np.float64).ravel()
    if order == 1:
        return np.stack([np.ones_like(u_flat), u_flat, v_flat], axis=1)
    if order == 2:
        return np.stack(
            [
                np.ones_like(u_flat),
                u_flat,
                v_flat,
                u_flat * u_flat,
                u_flat * v_flat,
                v_flat * v_flat,
            ],
            axis=1,
        )
    raise GCPError(f"不支持的 GCP 多项式阶数: {order}")


def fit_polynomial(
    src_u: np.ndarray,
    src_v: np.ndarray,
    dst_x: np.ndarray,
    dst_y: np.ndarray,
    *,
    order: int,
) -> dict[str, Any]:
    """用最小二乘拟合 (src_u, src_v) → (dst_x, dst_y)。"""
    u = np.asarray(src_u, dtype=np.float64).ravel()
    v = np.asarray(src_v, dtype=np.float64).ravel()
    x = np.asarray(dst_x, dtype=np.float64).ravel()
    y = np.asarray(dst_y, dtype=np.float64).ravel()
    if not (u.shape == v.shape == x.shape == y.shape):
        raise GCPError("拟合输入数组长度不一致")
    min_points = 3 if order == 1 else 6
    if u.size < min_points:
        raise GCPError(
            f"{order} 阶 GCP 拟合至少需要 {min_points} 个控制点，实际 {u.size} 个"
        )
    matrix = _basis(u, v, order)
    coef_x, *_ = np.linalg.lstsq(matrix, x, rcond=None)
    coef_y, *_ = np.linalg.lstsq(matrix, y, rcond=None)
    residual_x = matrix @ coef_x - x
    residual_y = matrix @ coef_y - y
    dist = np.hypot(residual_x, residual_y)
    return {
        "order": int(order),
        "coef_x": coef_x,
        "coef_y": coef_y,
        "rmse": float(np.sqrt(np.mean(dist * dist))),
        "max_error": float(np.max(dist)),
        "rmse_x": float(np.sqrt(np.mean(residual_x * residual_x))),
        "rmse_y": float(np.sqrt(np.mean(residual_y * residual_y))),
        "points": int(u.size),
    }


def apply_polynomial(
    coef_x: np.ndarray,
    coef_y: np.ndarray,
    u: np.ndarray,
    v: np.ndarray,
    *,
    order: int,
) -> tuple[np.ndarray, np.ndarray]:
    """按拟合系数计算多项式值。"""
    u_arr = np.asarray(u, dtype=np.float64)
    v_arr = np.asarray(v, dtype=np.float64)
    shape = u_arr.shape
    matrix = _basis(u_arr, v_arr, order)
    pred_x = (matrix @ np.asarray(coef_x, dtype=np.float64)).reshape(shape)
    pred_y = (matrix @ np.asarray(coef_y, dtype=np.float64)).reshape(shape)
    return pred_x, pred_y


def _residuals_for_points(
    coef_x: np.ndarray,
    coef_y: np.ndarray,
    points: list[dict[str, Any]],
    *,
    src_key_u: str,
    src_key_v: str,
    dst_key_x: str,
    dst_key_y: str,
    order: int,
) -> dict[str, float]:
    if not points:
        return {
            "points": 0,
            "rmse": 0.0,
            "max_error": 0.0,
            "rmse_x": 0.0,
            "rmse_y": 0.0,
        }
    u = np.asarray([p[src_key_u] for p in points], dtype=np.float64)
    v = np.asarray([p[src_key_v] for p in points], dtype=np.float64)
    x = np.asarray([p[dst_key_x] for p in points], dtype=np.float64)
    y = np.asarray([p[dst_key_y] for p in points], dtype=np.float64)
    pred_x, pred_y = apply_polynomial(coef_x, coef_y, u, v, order=order)
    dx = pred_x - x
    dy = pred_y - y
    dist = np.hypot(dx, dy)
    return {
        "points": int(len(points)),
        "rmse": float(np.sqrt(np.mean(dist * dist))),
        "max_error": float(np.max(dist)),
        "rmse_x": float(np.sqrt(np.mean(dx * dx))),
        "rmse_y": float(np.sqrt(np.mean(dy * dy))),
    }


def _sample(
    band: np.ndarray,
    sx: np.ndarray,
    sy: np.ndarray,
    *,
    nodata: float | None,
    alg: str,
) -> tuple[np.ndarray, np.ndarray]:
    """按源像素坐标采样；返回 (值, 有效掩膜)。严格拒绝邻域含 NoData。"""
    height, width = band.shape
    if alg == "nearest":
        ix = np.floor(sx).astype(np.int64)
        iy = np.floor(sy).astype(np.int64)
        valid = (ix >= 0) & (iy >= 0) & (ix < width) & (iy < height)
        values = np.full(sx.shape, np.nan, dtype=np.float64)
        if valid.any():
            sampled = band[iy[valid], ix[valid]]
            values[valid] = sampled
    elif alg == "bilinear":
        x = sx - 0.5
        y = sy - 0.5
        x0 = np.floor(x).astype(np.int64)
        y0 = np.floor(y).astype(np.int64)
        dx = x - x0
        dy = y - y0
        valid = (
            (x0 >= 0)
            & (y0 >= 0)
            & (x0 + 1 < width)
            & (y0 + 1 < height)
            & np.isfinite(sx)
            & np.isfinite(sy)
        )
        values = np.full(sx.shape, np.nan, dtype=np.float64)
        if valid.any():
            x0v = x0[valid]
            y0v = y0[valid]
            dxv = dx[valid]
            dyv = dy[valid]
            v00 = band[y0v, x0v]
            v10 = band[y0v, x0v + 1]
            v01 = band[y0v + 1, x0v]
            v11 = band[y0v + 1, x0v + 1]
            sampled = (
                v00 * (1.0 - dxv) * (1.0 - dyv)
                + v10 * dxv * (1.0 - dyv)
                + v01 * (1.0 - dxv) * dyv
                + v11 * dxv * dyv
            )
            values[valid] = sampled
    else:
        raise GCPError(f"不支持的 GCP 重采样算法 {alg!r}；可用：nearest, bilinear")
    valid &= np.isfinite(values)
    if nodata is not None:
        valid &= values != nodata
    return values, valid


def run_gcp_registration(
    out_path: Path,
    source: Path,
    reference: Path,
    gcp_path: Path,
    *,
    transform: str = "affine",
    resample_alg: str = "bilinear",
    src_nodata: float | None = None,
    dst_nodata: float = -9999.0,
    out_format: str = "GTiff",
    callback: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """用 GCP 控制点把源影像重采样到参考影像网格。"""
    gdal = _gdal()
    transform_name = str(transform or "affine").strip().lower()
    if transform_name not in GCP_TRANSFORMS:
        raise GCPError(
            f"未知 GCP 变换 {transform!r}；可用：{', '.join(GCP_TRANSFORMS)}"
        )
    order = _GCP_ORDER[transform_name]
    alg = str(resample_alg or "bilinear").strip().lower()
    if alg not in _RESAMPLE_ALGS:
        raise GCPError(f"不支持的 GCP 重采样算法 {resample_alg!r}")
    if not math.isfinite(dst_nodata):
        raise GCPError(f"输出 NoData 必须为有限值，实际 {dst_nodata}")
    if not source.exists():
        raise GCPError(f"源影像不存在: {source}")
    if not reference.exists():
        raise GCPError(f"参考影像不存在: {reference}")
    gcp = load_gcp_set(gcp_path)

    _progress(callback, 0.05, "打开参考影像")
    ref_ds = gdal.Open(str(reference), gdal.GA_ReadOnly)
    if ref_ds is None:
        raise GCPError(f"无法打开参考影像: {reference}")
    try:
        ref_w = int(ref_ds.RasterXSize)
        ref_h = int(ref_ds.RasterYSize)
        ref_gt = tuple(float(v) for v in (ref_ds.GetGeoTransform() or ()))
        ref_proj = ref_ds.GetProjectionRef() or ""
        ref_srs = ref_ds.GetSpatialRef()
    finally:
        ref_ds = None
    if len(ref_gt) != 6:
        raise GCPError("参考影像缺少有效 GeoTransform")

    _progress(callback, 0.15, "打开源影像")
    src_ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if src_ds is None:
        raise GCPError(f"无法打开源影像: {source}")
    try:
        src_w = int(src_ds.RasterXSize)
        src_h = int(src_ds.RasterYSize)
        src_bands = int(src_ds.RasterCount)
        if src_bands < 1:
            raise GCPError("源影像没有波段")
        src_proj = src_ds.GetProjectionRef() or ""
        # GCP 的 x/y 必须与参考影像同一 CRS；源影像的 CRS 仅用于记录。
        if gcp["srs"] and ref_srs is not None:
            declared = gcp["srs"].strip()
            ref_auth = ref_srs.GetAuthorityCode(None)
            if ref_auth and declared.upper().replace("EPSG:", "").strip() != ref_auth:
                raise GCPError(
                    f"GCP 声明 SRS={declared!r} 与参考影像 EPSG:{ref_auth} 不一致"
                )
    finally:
        src_ds = None

    controls = gcp["controls"]
    checks = gcp["checks"]
    for point in controls + checks:
        if not (0.0 <= point["pixel"] <= float(src_w) and 0.0 <= point["line"] <= float(src_h)):
            raise GCPError(
                f"GCP {point['id']} 的像素坐标 ({point['pixel']}, {point['line']}) "
                f"超出源影像范围 {src_w}×{src_h}"
            )
    px_c = np.asarray([p["pixel"] for p in controls], dtype=np.float64)
    py_c = np.asarray([p["line"] for p in controls], dtype=np.float64)
    x_c = np.asarray([p["x"] for p in controls], dtype=np.float64)
    y_c = np.asarray([p["y"] for p in controls], dtype=np.float64)

    _progress(callback, 0.25, f"拟合 {transform_name} 正/反变换")
    forward = fit_polynomial(px_c, py_c, x_c, y_c, order=order)
    backward = fit_polynomial(x_c, y_c, px_c, py_c, order=order)
    forward_check = _residuals_for_points(
        forward["coef_x"],
        forward["coef_y"],
        checks,
        src_key_u="pixel",
        src_key_v="line",
        dst_key_x="x",
        dst_key_y="y",
        order=order,
    )
    backward_check = _residuals_for_points(
        backward["coef_x"],
        backward["coef_y"],
        checks,
        src_key_u="x",
        src_key_v="y",
        dst_key_x="pixel",
        dst_key_y="line",
        order=order,
    )

    # 输出像元中心地图坐标 → 反变换源像素坐标
    yy, xx = np.mgrid[0:ref_h, 0:ref_w]
    map_x = ref_gt[0] + ref_gt[1] * (xx + 0.5) + ref_gt[2] * (yy + 0.5)
    map_y = ref_gt[3] + ref_gt[4] * (xx + 0.5) + ref_gt[5] * (yy + 0.5)
    src_px, src_py = apply_polynomial(
        backward["coef_x"],
        backward["coef_y"],
        map_x,
        map_y,
        order=order,
    )

    _progress(callback, 0.45, f"读取源影像 {src_bands} 个波段")
    src_ds = gdal.Open(str(source), gdal.GA_ReadOnly)
    if src_ds is None:
        raise GCPError(f"无法打开源影像: {source}")
    bands: list[np.ndarray] = []
    labels: list[float | None] = []
    try:
        for band_index in range(1, src_bands + 1):
            band = src_ds.GetRasterBand(band_index)
            if gdal.DataTypeIsComplex(band.DataType):
                raise GCPError(
                    f"GCP 配准仅支持实数波段，波段 {band_index} 是复数类型"
                )
            raw_obj = band.ReadAsArray()
            if raw_obj is None:
                raise GCPError(f"无法读取源波段 {band_index}")
            bands.append(np.asarray(raw_obj, dtype=np.float64))
            label = band.GetNoDataValue()
            labels.append(float(label) if label is not None else None)
    finally:
        src_ds = None

    _progress(callback, 0.65, f"重采样到参考网格（{alg}）")
    outputs: list[np.ndarray] = []
    valid_all = np.ones((ref_h, ref_w), dtype=bool)
    for band, label in zip(bands, labels, strict=True):
        nodata = float(src_nodata) if src_nodata is not None else label
        values, valid = _sample(band, src_px, src_py, nodata=nodata, alg=alg)
        valid_all &= valid
        outputs.append(values)

    _progress(callback, 0.85, f"写出成果（{out_format} / Float32，{src_bands} 波段）")
    driver = gdal.GetDriverByName(out_format)
    if driver is None:
        raise GCPError(f"不可用输出驱动：{out_format}")
    out_ds = driver.Create(str(out_path), ref_w, ref_h, src_bands, gdal.GDT_Float32)
    if out_ds is None:
        raise GCPError(f"创建输出失败（驱动 {out_format}）: {out_path}")
    try:
        out_ds.SetGeoTransform(ref_gt)
        out_ds.SetProjection(ref_proj)
        out_ds.SetMetadataItem("GCP_REGISTRATION", transform_name)
        out_ds.SetMetadataItem("GCP_ORDER", str(order))
        out_ds.SetMetadataItem(
            "GCP_CHECKPOINT_RMSE_MAP", f"{forward_check['rmse']:.12g}"
        )
        out_ds.SetMetadataItem(
            "GCP_CHECKPOINT_RMSE_PIXEL", f"{backward_check['rmse']:.12g}"
        )
        for idx, values in enumerate(outputs):
            band_out = out_ds.GetRasterBand(idx + 1)
            arr = np.where(valid_all, values, dst_nodata).astype(np.float32)
            band_out.WriteArray(arr)
            band_out.SetNoDataValue(float(dst_nodata))
            band_out.SetDescription(f"registered_B{idx + 1}")
            band_out.FlushCache()
    finally:
        out_ds = None

    basis_affine = ["1", "pixel", "line"]
    basis_quad = ["1", "pixel", "line", "pixel^2", "pixel*line", "line^2"]
    payload = {
        "type": "spectra_gcp_registration",
        "schema_version": "1.0",
        "created_by": "spectra-workbench P6-04",
        "source": str(source),
        "reference": str(reference),
        "gcp_file": str(gcp_path),
        "gcp_srs": gcp["srs"],
        "reference_srs": ref_srs.ExportToWkt() if ref_srs is not None else "",
        "source_srs": src_proj,
        "transform": transform_name,
        "order": order,
        "basis": basis_affine if order == 1 else basis_quad,
        "control_points": len(controls),
        "check_points": len(checks),
        "forward_coefficients": {
            "x": [float(v) for v in forward["coef_x"]],
            "y": [float(v) for v in forward["coef_y"]],
        },
        "backward_coefficients": {
            "pixel": [float(v) for v in backward["coef_x"]],
            "line": [float(v) for v in backward["coef_y"]],
        },
        "forward_control_residual": {
            "rmse": forward["rmse"],
            "max_error": forward["max_error"],
            "rmse_x": forward["rmse_x"],
            "rmse_y": forward["rmse_y"],
            "points": forward["points"],
        },
        "forward_checkpoint_error": forward_check,
        "backward_control_residual": {
            "rmse": backward["rmse"],
            "max_error": backward["max_error"],
            "rmse_x": backward["rmse_x"],
            "rmse_y": backward["rmse_y"],
            "points": backward["points"],
        },
        "backward_checkpoint_error": backward_check,
        "resample": alg,
        "resampler": "spectra gcp backward polynomial + nearest/bilinear sampling",
        "output_grid": {
            "width": ref_w,
            "height": ref_h,
            "geotransform": [float(v) for v in ref_gt],
        },
        "n_valid": int(valid_all.sum()),
        "src_nodata": src_nodata,
        "dst_nodata": float(dst_nodata),
    }
    sidecar = out_path.with_name(out_path.name + ".gcpregistration.json")
    sidecar.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, allow_nan=False),
        encoding="utf-8",
    )
    _progress(callback, 1.0, "完成")
    return {
        "transform": transform_name,
        "order": order,
        "control_points": len(controls),
        "check_points": len(checks),
        "forward_control_rmse": forward["rmse"],
        "forward_checkpoint_rmse": forward_check["rmse"],
        "backward_control_rmse": backward["rmse"],
        "backward_checkpoint_rmse": backward_check["rmse"],
        "n_valid": int(valid_all.sum()),
        "sidecar": str(sidecar),
    }
