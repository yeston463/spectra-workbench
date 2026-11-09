"""P9-01 集成与契约验收测试：基准全量对账与参数审计。

验收标准（对齐 docs/plan/01_完整开发方案.md §1.1, §6.1~§6.3 与 02_里程碑与验收.md P9-01）：
1. 公开目标清单全部有四维状态（presence/binding/validation/availability）；
   没有静默排除或只用运行时子集当分母（baseline_registration.ratio == 1.0, stateless_capability_ids == []）。
2. 按 CapabilityKind（algorithm / cli / driver / class / function）分开统计，
   绝不把符号总数或驱动总数当作算法总数，也不把「有 Python 编辑器」说成所有 API 均逐项测试通过。
3. 分开统计五项核心指标：基准登记率、可调用率、图形面板覆盖率（仅以公开 algorithm 为分母）、
   验收样例覆盖率、格式/驱动覆盖率。
4. 全量参数契约审计（parameter_audit）：
   - 全部 24 项已接通图形面板能力在 CapabilityDescriptor.parameters 与 supported_params_for 之间零漂移；
   - 全部 468 项能力、2000+ 项 ParameterSpec 通过类型、非空、choices/default 一致性与无重复键审计；
   - 参数别名映射表零冲突。
5. 运行时发现与静态基准对账：GDAL / Rasterio / Plugin Adapter 运行时枚举与全局注册表完全闭环。
"""

from __future__ import annotations

import pytest

from spectra_workbench.domain.capability_wiring import CONNECTED_CAPABILITIES
from spectra_workbench.providers.gdal_provider.adapter import GDALProviderAdapter
from spectra_workbench.providers.rasterio_provider.adapter import RasterioProviderAdapter
from spectra_workbench.registry import (
    audit_by_kind,
    audit_five_coverage_metrics,
    audit_parameters,
    build_full_audit_report,
    get_default_registry,
)

pytestmark = [pytest.mark.integration]


def test_p9_01_full_baseline_reconciliation_and_no_silent_exclusion() -> None:
    """公开目标清单全部有状态，无静默排除，不以运行时子集充当分母。"""
    reg = get_default_registry()
    report = reg.full_audit_report()

    assert report["p9_01_compliant"] is True
    assert report["stateless_capability_ids"] == []

    rec = report["baseline_reconciliation"]
    assert rec["complete"] is True
    assert rec["baseline_count"] == 443
    assert rec["registry_count"] == 468
    assert rec["missing_from_registry"] == []
    assert rec["unexplained_gaps"] == []
    assert rec["gap_count"] == 13

    # 验证全部 13 项缺口保留在分母中且每项均有非空限制说明
    expected_gaps = {
        "ext:external_atmospheric",
        "ext:external_gis",
        "ext:geopandas_vector",
        "ext:pdal_pointcloud",
        "ext:sklearn_classify",
        "ext:xarray_dask",
        "gdal:cli:gdal_auth",
        "gdal:driver:HDF5",
        "platform:agriculture_count",
        "platform:flaash_external",
        "rio:cli:cogeo",
        "rio:cli:color",
        "rio:cli:mbtiles",
    }
    actual_gaps = {item["capability_id"] for item in rec["missing_or_blocked"]}
    assert actual_gaps == expected_gaps
    for item in rec["missing_or_blocked"]:
        assert len(item["limitations"]) >= 1, f"{item['capability_id']} 缺少缺口说明"


def test_p9_01_by_kind_separation_prevents_symbol_inflation() -> None:
    """函数、类、CLI、驱动与算法分别建立索引与统计，绝不把符号或驱动总数当作算法总数。"""
    reg = get_default_registry()
    by_kind = audit_by_kind(reg)

    assert set(by_kind.keys()) == {"algorithm", "cli", "function", "class", "driver"}
    total_across_kinds = sum(v["total_count"] for v in by_kind.values())
    assert total_across_kinds == 468

    # 算法总数（38 项 GDAL/Rasterio/平台 + 6 项可选扩展 = 44 项）远小于总条目 468，严禁混同
    algo_stats = by_kind["algorithm"]
    assert algo_stats["total_count"] == 44
    assert algo_stats["discovered_count"] == 36
    assert algo_stats["form_connected_count"] == 35
    assert algo_stats["missing_or_blocked_count"] == 8  # 2 platform blocked + 6 ext

    # CLI 工具单独统计（178 项，其中 174 项现存，4 项缺失）
    cli_stats = by_kind["cli"]
    assert cli_stats["total_count"] == 178
    assert cli_stats["discovered_count"] == 174
    assert cli_stats["missing_or_blocked_count"] == 4

    # 驱动单独统计（205 现存 + 1 缺失 HDF5 = 206）
    drv_stats = by_kind["driver"]
    assert drv_stats["total_count"] == 206
    assert drv_stats["discovered_count"] == 205
    assert drv_stats["form_connected_count"] == 0
    assert drv_stats["missing_or_blocked_count"] == 1

    # Python API 类与函数单独统计（13 个类 + 27 个函数 = 40 个符号，form_connected 均为 0）
    assert by_kind["class"]["total_count"] == 13
    assert by_kind["class"]["form_connected_count"] == 0
    assert by_kind["function"]["total_count"] == 27
    assert by_kind["function"]["form_connected_count"] == 0


def test_p9_01_five_independent_coverage_metrics() -> None:
    """分开统计基准登记率、可调用率、图形面板覆盖率、验收样例覆盖率、格式/驱动覆盖率。"""
    reg = get_default_registry()
    metrics = audit_five_coverage_metrics(reg)

    # 1. 基准登记率：443 / 443 = 100%
    b_reg = metrics["baseline_registration"]
    assert b_reg["baseline_denominator"] == 443
    assert b_reg["registered_in_catalog"] == 443
    assert b_reg["missing_from_catalog"] == 0
    assert b_reg["ratio"] == 1.0
    assert b_reg["runtime_subset_used_as_denominator"] is False
    assert b_reg["silently_excluded_count"] == 0

    # 2. 可调用率
    c_cov = metrics["callable_coverage"]
    assert c_cov["total_registry_denominator"] == 468
    assert c_cov["callable_count"] == 250
    assert c_cov["non_driver_denominator"] == 262
    assert c_cov["callable_non_driver_count"] == 250

    # 3. 图形面板覆盖率：仅以 algorithm 为分母（35 / 44 = 79.55%，在已发现算法中 35 / 36 = 97.22%）
    f_cov = metrics["form_panel_coverage"]
    assert f_cov["total_algorithm_denominator"] == 44
    assert f_cov["present_algorithm_denominator"] == 36
    assert f_cov["connected_form_algorithm_count"] == 35
    assert set(f_cov["connected_capability_ids"]).issubset(CONNECTED_CAPABILITIES)
    assert len(f_cov["unconnected_present_algorithms"]) == 1
    assert set(f_cov["unconnected_present_algorithms"]) == {
        "gdal:retile",
    }

    # 4. 验收样例覆盖率：明确声明不得把有 Python 编辑器说成所有 API 均逐项测试通过
    v_cov = metrics["validation_sample_coverage"]
    assert v_cov["verified_evidence_count"] == 5
    assert v_cov["connected_form_count"] == 35
    assert v_cov["script_editor_claimed_as_all_api_tested"] is False

    # 5. 格式/驱动覆盖率：205 / 206 = 99.51%
    d_cov = metrics["driver_coverage"]
    assert d_cov["total_driver_denominator"] == 206
    assert d_cov["present_driver_count"] == 205
    assert d_cov["missing_driver_count"] == 1
    assert d_cov["missing_driver_ids"] == ["gdal:driver:HDF5"]
    assert d_cov["raster_driver_count"] > 100
    assert d_cov["vector_driver_count"] > 50


def test_p9_01_full_parameter_contract_audit_zero_drift() -> None:
    """全量参数审计：35 项已接通算法（含 4 项旧别名共 39 项）参数零漂移，全部 468 项能力 2000+ 参数无错误。"""
    reg = get_default_registry()
    pa = audit_parameters(reg)

    assert pa["compliant"] is True
    assert pa["total_capabilities_audited"] == 468
    assert pa["total_parameters_audited"] >= 2027
    assert pa["connected_capabilities_audited"] == 39
    assert pa["schema_errors"] == []
    assert pa["duplicate_key_errors"] == []
    assert pa["default_choice_errors"] == []
    assert pa["connected_parameter_drift"] == []
    assert pa["param_alias_collisions"] == []


def test_p9_01_runtime_discovery_vs_registry_denominator() -> None:
    """核对运行时适配器 discover() 与全局注册表：运行时缺失项不会使注册表分母缩水。"""
    reg = get_default_registry()
    gdal_caps = GDALProviderAdapter().discover()
    rio_caps = RasterioProviderAdapter().discover()

    for cap in gdal_caps + rio_caps:
        in_reg = reg.get(cap.capability_id)
        assert in_reg is not None, f"适配器发现的能力未在全局注册表登记: {cap.capability_id}"

    full = build_full_audit_report(reg)
    assert full["total_baseline_count"] == 468
    assert full["p9_01_compliant"] is True
