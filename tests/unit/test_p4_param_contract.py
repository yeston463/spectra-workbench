"""P4-04 单元测试：共享参数契约、能力集合与编目的一致性。

契约是 GUI 与 Worker 的**同一份规则**；这里守住「参数被识别 ≠ 能力会消费」
以及「新增能力必须在 adapter / wiring / registry 三处同时到位」两条红线。
"""

from __future__ import annotations

import pytest

from spectra_workbench.domain.capability_wiring import (
    CONNECTED_CAPABILITIES,
    MULTI_INPUT_CAPABILITIES,
    is_multi_input,
)
from spectra_workbench.processing.raster_params import (
    ParameterError,
    canonical_capability_id,
    normalize_raster_params,
    supported_params_for,
)

pytestmark = [pytest.mark.unit]


# ------------------------------------------------------------------ 参数契约


def test_calc_param_matrix_and_zero_nodata() -> None:
    supported = supported_params_for("gdal:calc")
    assert supported is not None
    assert {"calc", "srcNodata", "dstNodata", "outputType"} <= set(supported)
    norm = normalize_raster_params(
        {"calc": " (A-B)/(A+B) ", "dstNodata": 0, "outputType": "Float32", "format": "GTiff"},
        capability_id="gdal:calc",
    )
    assert norm["calc"] == "(A-B)/(A+B)"
    assert norm["dstNodata"] == 0.0, "零值 NoData 必须保留，不能被 truthiness 丢掉"
    assert norm["outputType"] == "Float32"


@pytest.mark.parametrize(
    "raw",
    [
        {"calc": "A+1", "xRes": 1.0},  # 能力不消费几何参数
        {"calc": "   "},  # 空表达式
        {"calc": 42},  # 非字符串
        {"calc": "A+" * 300},  # 过长
    ],
)
def test_calc_invalid_params_are_rejected(raw: dict) -> None:
    with pytest.raises(ParameterError):
        normalize_raster_params(raw, capability_id="gdal:calc")


def test_statistics_param_matrix_and_aliases() -> None:
    supported = supported_params_for("gdal:statistics")
    assert supported is not None
    assert {
        "statisticsMode",
        "histogramBins",
        "maxSamples",
        "fullLimit",
        "bandList",
        "srcNodata",
    } <= set(supported)
    norm = normalize_raster_params(
        {"mode": "FULL", "bins": 32, "max_samples": 1000, "full_limit": 50000, "bandList": [1, 2]},
        capability_id="gdal:statistics",
    )
    assert norm["statisticsMode"] == "full"
    assert norm["histogramBins"] == 32
    assert norm["maxSamples"] == 1000 and norm["fullLimit"] == 50000
    assert norm["bandList"] == [1, 2]


@pytest.mark.parametrize(
    "raw",
    [
        {"statisticsMode": "quick"},
        {"histogramBins": 1},
        {"histogramBins": 5000},
        {"maxSamples": 0},
        {"fullLimit": -1},
        {"calc": "A+1"},  # 统计能力不消费表达式
        {"xRes": 1.0},
    ],
)
def test_statistics_invalid_params_are_rejected(raw: dict) -> None:
    with pytest.raises(ParameterError):
        normalize_raster_params(raw, capability_id="gdal:statistics")


def test_capability_aliases_resolve() -> None:
    assert canonical_capability_id("gdal_calc.py") == "gdal:calc"
    assert canonical_capability_id("gdal raster calc") == "gdal:calc"
    assert canonical_capability_id("gdal:histogram") == "gdal:statistics"
    assert canonical_capability_id("gdalinfo -stats") == "gdal:statistics"


# ------------------------------------------------------------------ 三处一致


def test_adapter_wiring_and_registry_are_all_updated() -> None:
    from spectra_workbench.providers.gdal_provider.adapter import (
        INPLACE_CAPABILITIES,
        P402_CAPABILITIES,
        GDALProviderAdapter,
    )

    for cid in ("gdal:calc", "gdal:statistics"):
        assert cid in GDALProviderAdapter.SUPPORTED_CAPABILITIES
        assert cid in CONNECTED_CAPABILITIES
    assert "gdal:calc" in P402_CAPABILITIES, "表达式走多输入列表与既有事务提交"
    assert "gdal:statistics" not in P402_CAPABILITIES, "统计报告不是栅格，不走 GDAL 重开校验"
    assert "gdal:calc" not in INPLACE_CAPABILITIES
    assert is_multi_input("gdal:calc") and "gdal:calc" in MULTI_INPUT_CAPABILITIES
    assert not is_multi_input("gdal:statistics")
    assert set(MULTI_INPUT_CAPABILITIES) <= set(CONNECTED_CAPABILITIES)


def test_registry_entries_have_form_entrypoints_and_artifact_output() -> None:
    from spectra_workbench.registry.catalog import get_default_registry

    reg = get_default_registry()
    for cid in ("gdal:calc", "gdal:statistics"):
        cap = reg.get(cid)
        assert cap is not None, f"{cid} 未编目"
        assert any(str(e.kind).endswith("form") and e.available for e in cap.entrypoints)
        assert cap.state.availability == "ready"
    stats = reg.get("gdal:statistics")
    assert stats is not None
    assert str(stats.outputs[0].data_type) == "artifact"
    assert [p.key for p in stats.parameters][:4] == [
        "statisticsMode",
        "histogramBins",
        "maxSamples",
        "fullLimit",
    ]
