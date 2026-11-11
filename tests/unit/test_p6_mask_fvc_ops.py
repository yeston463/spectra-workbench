"""P6-05 二值掩膜与 FVC 的纯数学/契约测试。"""

from __future__ import annotations

import numpy as np
import pytest

from spectra_workbench.processing.raster_params import (
    ParameterError,
    normalize_raster_params,
    supported_params_for,
)
from spectra_workbench.providers.gdal_provider.fvc_ops import (
    FVCError,
    compute_fvc,
    validate_class_edges,
)
from spectra_workbench.providers.gdal_provider.mask_ops import (
    MaskError,
    compute_mask,
)

pytestmark = [pytest.mark.unit]


def _ndvi_plane() -> np.ndarray:
    y, x = np.mgrid[0:4, 0:5]
    return 0.1 + 0.02 * x + 0.01 * y


def test_mask_thresholds_and_invert() -> None:
    values = np.array([[0.0, 0.2, 0.5, 0.9, float("nan")], [0.1, 0.3, 0.45, 0.7, 0.95]])
    valid = np.isfinite(values)
    valid &= values != 0.0  # 0.0 视为源 NoData 标签
    mask = compute_mask(values, valid, low=0.2, high=0.5, invert=False)
    assert mask.tolist() == [
        [False, True, True, False, False],
        [False, True, True, False, False],
    ]

    inverted = compute_mask(values, valid, low=0.2, high=0.5, invert=True)
    assert inverted.tolist() == [
        [False, False, False, True, False],
        [True, False, False, True, True],
    ]

    all_valid = compute_mask(values, valid, low=None, high=None, invert=False)
    assert all_valid.tolist() == [
        [False, True, True, True, False],
        [True, True, True, True, True],
    ]

    low_only = compute_mask(values, valid, low=0.0, high=None, invert=False)
    assert bool(low_only[0][0]) is False  # NoData 标签被排除

    with pytest.raises(MaskError):
        compute_mask(values, valid, low=0.5, high=0.2, invert=False)


def test_fvc_formula_and_out_of_range_counts() -> None:
    ndvi = _ndvi_plane()
    fvc, mask, counts = compute_fvc(ndvi, 0.15, 0.55, mask=None)
    assert mask.all()
    assert counts["n_below_soil"] > 0
    assert counts["n_above_veg"] == 0
    expected = (ndvi - 0.15) / (0.55 - 0.15)
    assert np.allclose(fvc, expected, atol=1e-12)

    veg_only = np.full((2, 2), 0.6)
    fvc2, _mask2, counts2 = compute_fvc(veg_only, 0.15, 0.55, mask=None)
    assert counts2["n_below_soil"] == 0
    assert counts2["n_above_veg"] == 4  # (0.6-0.15)/0.4 = 1.125 > 1
    assert np.allclose(fvc2, 1.125, atol=1e-12)

    with pytest.raises(FVCError, match="soil < veg"):
        compute_fvc(ndvi, 0.55, 0.15, mask=None)
    with pytest.raises(FVCError):
        compute_fvc(ndvi, float("nan"), 0.55, mask=None)


def test_fvc_class_edges_validation() -> None:
    assert validate_class_edges([0.3, 0.6]) == [0.3, 0.6]
    bad_lists: list[list[float]] = [
        [],
        [0.0],
        [1.0],
        [-0.1],
        [float("nan")],
        [0.5, 0.5],
        [0.6, 0.3],
    ]
    for edges in bad_lists:
        with pytest.raises(FVCError):
            validate_class_edges(edges)


def test_p6_05_param_contracts() -> None:
    supported = supported_params_for("gdal:mask")
    assert supported is not None
    assert {"maskBand", "maskLow", "maskHigh", "maskInvert"} <= set(supported)

    supported = supported_params_for("gdal:fvc")
    assert supported is not None
    assert {
        "fvcBand",
        "endmemberMode",
        "lowPercentile",
        "highPercentile",
        "fvcSoil",
        "fvcVeg",
        "fvcClassEdges",
        "maskFile",
        "crop",
        "srcNodata",
        "dstNodata",
    } <= set(supported)

    norm = normalize_raster_params(
        {
            "mask_band": 2,
            "mask_low": 0.2,
            "mask_high": 0.8,
            "mask_invert": True,
        },
        capability_id="gdal:mask",
    )
    assert norm["maskBand"] == 2
    assert norm["maskLow"] == 0.2
    assert norm["maskHigh"] == 0.8
    assert norm["maskInvert"] is True

    fvc = normalize_raster_params(
        {
            "fvc_band": 1,
            "endmember_mode": "VALUES",
            "fvc_soil": 0.15,
            "fvc_veg": 0.55,
            "fvc_class_edges": [0.3, 0.6],
            "mask_file": "mask.tif",
            "crop": False,
            "dstNodata": 0,
        },
        capability_id="gdal:fvc",
    )
    assert fvc["endmemberMode"] == "values"
    assert fvc["fvcSoil"] == 0.15
    assert fvc["fvcClassEdges"] == [0.3, 0.6]
    assert fvc["maskFile"] == "mask.tif"
    assert fvc["crop"] is False
    assert fvc["dstNodata"] == 0.0

    for raw in (
        {"maskBand": 0},
        {"maskLow": 0.9, "maskHigh": 0.2},
        {"maskInvert": "yes"},
        {"endmemberMode": "kmeans"},
        {"lowPercentile": 98, "highPercentile": 2},
        {"fvcClassEdges": []},
        {"fvcClassEdges": [0.5, 0.4]},
    ):
        with pytest.raises(ParameterError):
            normalize_raster_params(raw, capability_id="gdal:fvc")
