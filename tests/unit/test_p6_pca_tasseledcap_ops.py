"""P6-03 PCA 与缨帽变换的纯数学/契约测试。

覆盖验收中的可独立验证部分：
- PCA 完整重建误差为浮点噪声量级；
- 降维重建误差与 discarded_variance 的数学关系符合定义；
- 协方差/相关矩阵、主成分顺序、符号约定可按契约检查；
- 缨帽系数按传感器登记，来源、产品级别与波段顺序可检查。
"""

from __future__ import annotations

import numpy as np
import pytest

from spectra_workbench.processing.raster_params import (
    ParameterError,
    normalize_raster_params,
    supported_params_for,
)
from spectra_workbench.processing.tasseledcap_registry import (
    TASSELED_CAP_SENSORS,
    get_sensor,
)
from spectra_workbench.providers.gdal_provider.pca_ops import (
    PCAError,
    fit_pca,
    project_pca,
    reconstruct_pca,
    reconstruction_metrics,
)
from spectra_workbench.providers.gdal_provider.tasseledcap_ops import apply_coefficients

pytestmark = [pytest.mark.unit]


def _rank_two_data(n_pixels: int = 40) -> np.ndarray:
    x = np.linspace(-2.0, 2.0, n_pixels)
    band1 = 1.0 + 2.0 * x
    band2 = 3.0 - x
    band3 = 0.5 * band1 + 0.5 * band2  # 精确线性相关，协方差秩为 2
    return np.vstack([band1, band2, band3])


def test_pca_full_reconstruction_is_numerical_noise() -> None:
    data = _rank_two_data()
    fit = fit_pca(data, matrix="covariance")
    projected = project_pca(data, fit)
    reconstructed = reconstruct_pca(projected, fit)

    assert np.allclose(reconstructed, data, atol=1e-10)
    metrics = reconstruction_metrics(data, reconstructed, np.ones(data.shape[1], dtype=bool))
    assert metrics["reconstruction_rmse"] <= 1e-10
    assert metrics["reconstruction_relative_rmse"] <= 1e-10
    assert np.all(np.diff(fit["eigenvalues"]) <= 1e-12), "特征值必须降序"


def test_pca_reduced_error_matches_discarded_variance_definition() -> None:
    data = _rank_two_data()
    fit = fit_pca(data, matrix="covariance")
    k = 1
    projected = project_pca(data, fit, components=k)
    reconstructed = reconstruct_pca(projected, fit, components_used=k)
    metrics = reconstruction_metrics(data, reconstructed, np.ones(data.shape[1], dtype=bool))

    discarded = float(np.asarray(fit["eigenvalues"])[k:].sum())
    # 定义：MSE（对像元×波段取平均）= 丢弃特征值之和 / 波段数。
    expected_mse = discarded / data.shape[0]
    assert metrics["reconstruction_rmse"] ** 2 == pytest.approx(expected_mse, rel=1e-10)
    assert metrics["reconstruction_rmse"] > 0.0
    assert metrics["reconstruction_relative_rmse"] > 0.0


def test_pca_sign_convention_is_deterministic() -> None:
    data = _rank_two_data()
    fit = fit_pca(data, matrix="covariance")
    for row in np.asarray(fit["eigenvectors"]):
        assert row[int(np.argmax(np.abs(row)))] > 0.0


def test_pca_correlation_matrix_reconstructs_and_uses_std() -> None:
    x = np.linspace(-1.0, 1.0, 30)
    data = np.vstack([100.0 * x + 500.0, 0.1 * x - 2.0, -3.0 * x + 7.0])
    fit = fit_pca(data, matrix="correlation")
    assert fit["std"].shape == (3,)
    assert np.all(fit["std"] > 0)
    projected = project_pca(data, fit)
    reconstructed = reconstruct_pca(projected, fit)
    assert np.allclose(reconstructed, data, atol=1e-9)


def test_pca_rejects_zero_variance_or_too_few_valid_pixels() -> None:
    data = np.vstack(
        [np.ones(10), np.arange(10, dtype=np.float64), np.arange(10, dtype=np.float64) * 2]
    )
    with pytest.raises(PCAError):
        fit_pca(data, matrix="correlation")

    with pytest.raises(PCAError):
        fit_pca(data, valid=np.zeros(10, dtype=bool))


@pytest.mark.parametrize(
    "sensor_key",
    ["landsat5_tm", "landsat7_etm", "landsat8_oli"],
)
def test_tasseled_cap_registry_has_source_and_band_order(sensor_key: str) -> None:
    sensor = get_sensor(sensor_key)
    assert sensor.band_order == ("Blue", "Green", "Red", "NIR", "SWIR1", "SWIR2")
    assert sensor.product_level
    assert sensor.source and sensor.source_url.startswith("http")
    assert len(sensor.coefficients) == 3
    assert all(len(row) == 6 for row in sensor.coefficients)
    assert TASSELED_CAP_SENSORS[sensor_key] is sensor


def test_tasseled_cap_published_coefficient_values_are_pinned() -> None:
    # 逐项钉住公开来源的数值，防止后续改表时无感漂移。
    tm = get_sensor("landsat5_tm")
    assert tm.coefficients[0][0] == pytest.approx(0.2043)
    assert tm.coefficients[2][-1] == pytest.approx(-0.6109)

    etm = get_sensor("landsat7_etm")
    assert etm.coefficients[0][0] == pytest.approx(0.3561)
    assert etm.coefficients[2][-1] == pytest.approx(-0.5388)

    oli = get_sensor("landsat8_oli")
    assert oli.coefficients[0][0] == pytest.approx(0.3029)
    assert oli.coefficients[1][3] == pytest.approx(0.7276)
    assert oli.coefficients[2][-1] == pytest.approx(-0.4559)


def test_tasseled_cap_unknown_sensor_is_rejected() -> None:
    with pytest.raises(ValueError):
        get_sensor("sentinel2_msi")


def test_apply_coefficients_matches_linear_combination() -> None:
    sensor = get_sensor("landsat8_oli")
    identity = np.eye(6, dtype=np.float64)
    result = apply_coefficients(identity, np.asarray(sensor.coefficients))
    assert np.allclose(result, np.asarray(sensor.coefficients), atol=1e-15)

    pixel = np.array([[0.1], [0.2], [0.3], [0.4], [0.5], [0.6]])
    expected = np.asarray(sensor.coefficients) @ pixel
    assert np.allclose(apply_coefficients(pixel, np.asarray(sensor.coefficients)), expected)


def test_pca_param_contract_and_zero_values() -> None:
    supported = supported_params_for("gdal:pca")
    assert supported is not None
    assert {
        "pcaMode",
        "pcaComponents",
        "pcaMatrix",
        "pcaTransform",
        "srcNodata",
        "dstNodata",
    } <= set(supported)

    norm = normalize_raster_params(
        {
            "pca_mode": "INVERSE",
            "components": 2,
            "pca_transform": "transform.json",
            "srcNodata": 0,
            "dstNodata": 0,
            "format": "GTiff",
        },
        capability_id="gdal:pca",
    )
    assert norm["pcaMode"] == "inverse"
    assert norm["pcaComponents"] == 2
    assert norm["pcaTransform"] == "transform.json"
    assert norm["srcNodata"] == 0.0
    assert norm["dstNodata"] == 0.0


@pytest.mark.parametrize(
    "raw",
    [
        {"pcaMode": "both"},
        {"pcaMatrix": "covariance_matrix"},
        {"pcaComponents": 0},
        {"pcaComponents": 1.5},
        {"pcaTransform": "   "},
        {"tcSensor": "landsat5_tm"},  # 能力不消费缨帽参数
    ],
)
def test_pca_invalid_params_are_rejected(raw: dict) -> None:
    with pytest.raises(ParameterError):
        normalize_raster_params(raw, capability_id="gdal:pca")


def test_tasseled_cap_param_contract_and_rejections() -> None:
    supported = supported_params_for("gdal:tasseledcap")
    assert supported is not None
    assert {"tcSensor", "tcBands", "srcNodata", "dstNodata"} <= set(supported)

    norm = normalize_raster_params(
        {"tc_sensor": "LANDSAT8_OLI", "tc_bands": [2, 3, 4, 5, 6, 7], "dstNodata": 0},
        capability_id="gdal:tasseledcap",
    )
    assert norm["tcSensor"] == "landsat8_oli"
    assert norm["tcBands"] == [2, 3, 4, 5, 6, 7]
    assert norm["dstNodata"] == 0.0

    for raw in (
        {"tcSensor": "sentinel2_msi", "tcBands": [1, 2, 3, 4, 5, 6]},
        {"tcSensor": "landsat8_oli", "tcBands": [1, 2, 3, 4, 5]},
        {"tcSensor": "landsat8_oli", "tcBands": [1, 1, 2, 3, 4, 5]},
        {"tcSensor": "landsat8_oli", "tcBands": [1, 2, 3, 4, 5, 0]},
        {"pcaMode": "forward", "tcSensor": "landsat8_oli", "tcBands": [1, 2, 3, 4, 5, 6]},
    ):
        with pytest.raises(ParameterError):
            normalize_raster_params(raw, capability_id="gdal:tasseledcap")
