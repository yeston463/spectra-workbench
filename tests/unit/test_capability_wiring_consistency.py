"""守护「GUI 已接通声明」与「provider 实际支持集」的一致性。

背景：`domain/capability_wiring.py` 是纯 Python 声明（GUI 可安全导入），
而 provider adapter 顶层 `from osgeo import gdal`，GUI 不得导入它。
因此两者的一致性**无法靠共用常量保证**，只能由测试守护。

若二者漂移，会出现两类具体故障：
- 声明里有、adapter 没有 → GUI 允许提交，worker 报 `unsupported_capability`（假成功入口）；
- adapter 有、声明里没有 → 能力已可执行但 GUI 禁止提交（用户用不到）。

同时守护 registry 编目：能力若未在 registry 登记，工具箱里根本看不到，
即使 adapter 支持也只是「隐形的能力」。
"""

from __future__ import annotations

import pytest

from spectra_workbench.domain.capability_wiring import (
    CONNECTED_CAPABILITIES,
    MULTI_INPUT_CAPABILITIES,
)

pytestmark = [pytest.mark.unit]


def _gdal_supported() -> set[str]:
    from spectra_workbench.providers.gdal_provider.adapter import GDALProviderAdapter

    return set(GDALProviderAdapter.SUPPORTED_CAPABILITIES)


def _rasterio_supported() -> set[str]:
    from spectra_workbench.providers.rasterio_provider.adapter import RasterioProviderAdapter

    return set(RasterioProviderAdapter.SUPPORTED_CAPABILITIES)


def test_declared_capabilities_are_actually_supported() -> None:
    """正向：GUI 声明的每个能力都必须在 provider 实际支持集里。

    否则用户能点提交但 worker 会报 unsupported_capability（假成功入口）。
    """
    declared = set(CONNECTED_CAPABILITIES)
    actual = _gdal_supported() | _rasterio_supported()
    only_declared = sorted(declared - actual)
    assert not only_declared, "GUI 声明了 provider 不支持的能力（会提交后失败）：" + repr(
        only_declared
    )


def test_form_capable_provider_support_is_declared() -> None:
    """反向：provider 支持且 registry 提供**可用 form 入口**的能力，GUI 必须声明。

    否则能力已可执行、注册表也有表单入口，用户却在工具箱里点不到。
    CLI 类能力（如 rio:cli:*）只有 CLI 入口、不走表单通道，故不在此列。
    """
    from spectra_workbench.registry.catalog import get_default_registry

    registry = get_default_registry()
    declared = set(CONNECTED_CAPABILITIES)
    actual = _gdal_supported() | _rasterio_supported()

    missing: list[str] = []
    for cid in sorted(actual - declared):
        cap = registry.get(cid)
        if cap is None:
            # provider 的兼容别名，registry 无独立条目：不要求出现在表单集合
            continue
        has_form = any(str(e.kind).endswith("form") and e.available for e in cap.entrypoints)
        if has_form:
            missing.append(cid)
    assert not missing, "这些能力有可用的 form 入口但 GUI 未声明（用户用不到）：" + repr(missing)


def test_multi_input_set_is_subset_of_connected() -> None:
    """多输入能力必须是已接通能力的子集。"""
    assert set(MULTI_INPUT_CAPABILITIES) <= set(CONNECTED_CAPABILITIES)


def test_all_connected_capabilities_are_indexed() -> None:
    """每个已接通能力都必须在 registry 编目，否则工具箱里看不到。"""
    from spectra_workbench.registry.catalog import get_default_registry

    registry = get_default_registry()
    missing = sorted(cid for cid in CONNECTED_CAPABILITIES if registry.get(cid) is None)
    assert not missing, f"以下已接通能力未在 registry 编目（工具箱不可见）：{missing}"


def test_connected_capabilities_have_available_form_entrypoint() -> None:
    """已接通能力必须有可用的 form 入口，否则 GUI 无法选中提交。"""
    from spectra_workbench.registry.catalog import get_default_registry

    registry = get_default_registry()
    problems: list[str] = []
    for cid in sorted(CONNECTED_CAPABILITIES):
        cap = registry.get(cid)
        if cap is None:
            continue
        forms = [e for e in cap.entrypoints if str(e.kind).endswith("form")]
        if not forms:
            problems.append(f"{cid}: 无 form 入口")
        elif not any(e.available for e in forms):
            problems.append(f"{cid}: form 入口不可用")
        elif cap.state.availability != "ready":
            problems.append(f"{cid}: availability={cap.state.availability}（应为 ready）")
    assert not problems, "已接通能力的注册状态不一致：\n" + "\n".join(problems)


def test_p402_capabilities_have_parameter_matrices() -> None:
    """P4-02 四项能力在共享契约里必须有参数支持矩阵。"""
    from spectra_workbench.processing.raster_params import supported_params_for

    for cid in ("gdal:buildvrt", "gdal:merge", "gdal:stack", "gdal:bandmanage"):
        assert supported_params_for(cid) is not None, f"{cid} 缺少参数支持矩阵"


def test_output_format_control_scope_matches_adapter_contract() -> None:
    """输出格式选择器只出现在真实消费 ``format`` 的能力上。

    - buildvrt 只写 VRT，不能提供 GTiff/ENVI 等其它格式；
    - statistics 固定输出 JSON artifact；
    - merge/stack/bandmanage 由适配器消费 ``format``，必须可选择。
    """
    from spectra_workbench.domain.capability_wiring import (
        FORMAT_CAPABILITIES,
        supports_output_format,
    )

    assert not supports_output_format("gdal:buildvrt")
    assert not supports_output_format("gdal:statistics")
    for cid in ("gdal:merge", "gdal:stack", "gdal:bandmanage"):
        assert supports_output_format(cid), f"{cid} 应允许选择输出格式"
    assert set(FORMAT_CAPABILITIES) <= set(CONNECTED_CAPABILITIES)
