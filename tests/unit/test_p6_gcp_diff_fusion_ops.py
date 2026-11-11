"""P6-04 GCP 拟合、差值、Brovey/HSV 的纯数学/契约测试。"""

from __future__ import annotations

import json

import numpy as np
import pytest

from spectra_workbench.processing.raster_params import (
    ParameterError,
    normalize_raster_params,
    supported_params_for,
)
from spectra_workbench.providers.gdal_provider.diff_ops import DiffError, compute_difference
from spectra_workbench.providers.gdal_provider.fusion_ops import (
    brovey_fusion,
    hsv_fusion,
)
from spectra_workbench.providers.gdal_provider.gcp_ops import (
    GCPError,
    apply_polynomial,
    fit_polynomial,
    load_gcp_set,
)

pytestmark = [pytest.mark.unit]


def _affine_points(n: int = 4) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    gx, gy = np.meshgrid(np.linspace(0.5, 19.5, n), np.linspace(0.5, 14.5, n))
    px = gx.ravel()
    py = gy.ravel()
    x = 100.0 + 2.0 * px + 0.1 * py
    y = 200.0 - 0.2 * px + 3.0 * py
    return px, py, x, y


def test_affine_fit_recovers_known_coefficients() -> None:
    px, py, x, y = _affine_points()
    fit_x = fit_polynomial(px, py, x, x, order=1)  # 仅验证 x
    assert fit_x["coef_x"] == pytest.approx([100.0, 2.0, 0.1], abs=1e-10)
    assert fit_x["rmse"] <= 1e-10

    fit = fit_polynomial(px, py, x, y, order=1)
    assert fit["coef_x"] == pytest.approx([100.0, 2.0, 0.1], abs=1e-10)
    assert fit["coef_y"] == pytest.approx([200.0, -0.2, 3.0], abs=1e-10)
    assert fit["rmse"] <= 1e-10


def test_backward_affine_is_exact_inverse() -> None:
    px, py, x, y = _affine_points()
    backward = fit_polynomial(x, y, px, py, order=1)
    assert backward["rmse"] <= 1e-10
    restored_px, restored_py = apply_polynomial(
        backward["coef_x"], backward["coef_y"], x, y, order=1
    )
    assert np.allclose(restored_px, px, atol=1e-10)
    assert np.allclose(restored_py, py, atol=1e-10)


def test_quadratic_transform_needs_polynomial2_and_reports_independent_error() -> None:
    px = np.linspace(0.5, 19.5, 30)
    py = np.linspace(0.5, 19.5, 30)
    x = 10.0 + 1.1 * px + 0.05 * py + 0.002 * px * px + 0.001 * px * py
    y = 5.0 - 0.1 * px + 0.9 * py + 0.001 * py * py

    affine = fit_polynomial(px[:20], py[:20], x[:20], y[:20], order=1)
    quad = fit_polynomial(px[:20], py[:20], x[:20], y[:20], order=2)
    assert quad["rmse"] <= 1e-10
    # 用独立的 21..30 点检查 affine 的误差应明显大于二次多项式。
    check_x, check_y = apply_polynomial(
        affine["coef_x"], affine["coef_y"], px[20:], py[20:], order=1
    )
    affine_err = float(np.sqrt(np.mean((check_x - x[20:]) ** 2 + (check_y - y[20:]) ** 2)))
    check_x2, check_y2 = apply_polynomial(quad["coef_x"], quad["coef_y"], px[20:], py[20:], order=2)
    quad_err = float(np.sqrt(np.mean((check_x2 - x[20:]) ** 2 + (check_y2 - y[20:]) ** 2)))
    assert affine_err > 0.01
    assert quad_err <= 1e-10


def test_fit_rejects_too_few_points() -> None:
    with pytest.raises(GCPError):
        fit_polynomial([1.0, 2.0], [1.0, 2.0], [3.0, 4.0], [3.0, 4.0], order=1)
    with pytest.raises(GCPError):
        fit_polynomial([1.0] * 5, [1.0] * 5, [3.0] * 5, [3.0] * 5, order=2)


def test_load_gcp_set_roles_and_validation(tmp_path) -> None:
    path = tmp_path / "gcps.json"
    path.write_text(
        json.dumps(
            {
                "type": "spectra_gcp_set",
                "schema_version": "1.0",
                "srs": "EPSG:32650",
                "points": [
                    {"id": "c1", "pixel": 0, "line": 0, "x": 1, "y": 1, "role": "control"},
                    {"id": "c2", "pixel": 1, "line": 0, "x": 2, "y": 1, "role": "control"},
                    {"id": "c3", "pixel": 0, "line": 1, "x": 1, "y": 2, "role": "control"},
                    {"id": "k1", "pixel": 2, "line": 2, "x": 3, "y": 3, "role": "check"},
                ],
            }
        ),
        encoding="utf-8",
    )
    loaded = load_gcp_set(path)
    assert loaded["srs"] == "EPSG:32650"
    assert len(loaded["controls"]) == 3
    assert len(loaded["checks"]) == 1

    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "points": [
                    {"id": "c1", "pixel": 0, "line": 0, "x": 1, "y": 1},
                    {"id": "c1", "pixel": 1, "line": 0, "x": 2, "y": 1},
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GCPError, match="重复"):
        load_gcp_set(bad)


def test_compute_difference_modes() -> None:
    a = np.array([[1.0, 5.0], [3.0, 9.0]])
    b = np.array([[0.5, 7.0], [3.0, 4.0]])
    assert np.allclose(compute_difference(a, b, "difference"), a - b)
    assert np.allclose(compute_difference(a, b, "absolute"), np.abs(a - b))
    with pytest.raises(DiffError):
        compute_difference(a, b, "ratio")


def test_brovey_formula_and_zero_sum() -> None:
    ms = np.array([[[1.0]], [[2.0]], [[3.0]]])
    pan = np.array([[6.0]])
    out, valid = brovey_fusion(ms, pan, ms_scale=1.0, pan_scale=1.0)
    assert np.allclose(out[:, 0, 0], [1.0, 2.0, 3.0])
    assert valid.all()

    out2, valid2 = brovey_fusion(ms, np.array([[3.0]]), ms_scale=1.0, pan_scale=1.0)
    assert np.allclose(out2[:, 0, 0], [0.5, 1.0, 1.5])

    zero = np.zeros((3, 1, 1))
    _out3, valid3 = brovey_fusion(zero, pan, ms_scale=1.0, pan_scale=1.0)
    assert not valid3.any()


def test_hsv_preserves_hue_saturation_and_replaces_value() -> None:
    ms = np.array([[[0.2]], [[0.4]], [[0.6]]])
    out, valid = hsv_fusion(ms, np.array([[0.6]]), ms_scale=1.0, pan_scale=1.0)
    assert valid.all()
    assert np.allclose(out[:, 0, 0], [0.2, 0.4, 0.6], atol=1e-12)

    out2, _ = hsv_fusion(ms, np.array([[1.0]]), ms_scale=1.0, pan_scale=1.0)
    assert np.max(out2[:, 0, 0]) == pytest.approx(1.0)
    # 色相/饱和度不因 V 替换而改变：最大通道为 1，最小通道按原比例。
    assert np.all(out2[:, 0, 0] >= 0.0)
    assert np.all(out2[:, 0, 0] <= 1.0)


def test_p6_04_param_contracts() -> None:
    assert supported_params_for("gdal:gcpregister") is not None
    assert supported_params_for("gdal:rasterdiff") is not None
    assert supported_params_for("gdal:pansharpen") is not None

    gcp = normalize_raster_params(
        {
            "gcp_transform": "polynomial2",
            "gcp_file": "g.json",
            "reference_file": "ref.tif",
            "gcp_resample": "nearest",
            "dstNodata": 0,
        },
        capability_id="gdal:gcpregister",
    )
    assert gcp["gcpTransform"] == "polynomial2"
    assert gcp["gcpResample"] == "nearest"
    assert gcp["dstNodata"] == 0.0

    diff = normalize_raster_params(
        {"diff_mode": "absolute", "reference_file": "b.tif"},
        capability_id="gdal:rasterdiff",
    )
    assert diff["diffMode"] == "absolute"

    fusion = normalize_raster_params(
        {
            "ms_file": "ms.tif",
            "ms_bands": [3, 2, 1],
            "fusion_method": "HSV",
            "fusion_resample": "nearest",
        },
        capability_id="gdal:pansharpen",
    )
    assert fusion["msBands"] == [3, 2, 1]
    assert fusion["fusionMethod"] == "hsv"

    for raw in (
        {"gcpTransform": "tps"},
        {"gcpFile": "   ", "referenceFile": "ref.tif"},
        {"diffMode": "ratio", "referenceFile": "b.tif"},
        {"msFile": "ms.tif", "msBands": [1, 2]},
        {"msFile": "ms.tif", "msBands": [1, 1, 2]},
        {"msFile": "ms.tif", "msBands": [1, 2, 3], "fusionMethod": "nndiffuse"},
    ):
        with pytest.raises(ParameterError):
            normalize_raster_params(raw, capability_id="gdal:pansharpen")
