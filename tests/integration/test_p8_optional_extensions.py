"""P8-05 集成与契约验收测试：可选引擎扩展登记。

验收标准（对齐 docs/plan/01_完整开发方案.md §1.2, §10.1, §10.2 与 02_里程碑与验收.md P8-05）：
1. Xarray/Rioxarray/Dask、scikit-learn 监督分类、PDAL 点云、GeoPandas 批量矢量、
   外部物理大气校正引擎（6S/Py6S/MODTRAN-FLAASH）、外部开源 GIS 引擎（WhiteboxTools/SAGA/GRASS）
   六类可选扩展全部完成结构化登记。
2. 每项扩展明确声明：能力范围（scope）、不承诺边界（non_goals）、平台核心替代方案（core_alternatives）、
   依赖与版本约束（requirements）、规划入口（planned_entrypoints）与当前缺口（gaps/limitations）。
3. 严禁作为已实现功能宣传：
   - implemented 必须为 False；
   - presence 必须为 missing，binding 必须为 none，availability 必须为 missing_dependency 或 blocked；
   - 所有 entrypoint.available 必须为 False；
   - 严禁出现在 CONNECTED_CAPABILITIES、discovered、callable、platform_executable 或 verified 清单中。
4. 契约模型防伪守卫：任何尝试将未实现扩展构造为 implemented=True、availability=ready 或 available=True 的行为立即抛出 ValueError。
5. 执行器与 UI 守卫：向 Plugin/GDAL/Rasterio Adapter 提交可选扩展任务均被明确拒绝并给出依赖与缺口说明；
   GUI 工具箱归入「可选引擎扩展登记 (未实现/待接入)」专有分组，参数面板展示依赖/入口/缺口并严格禁用提交按钮。
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from PySide6.QtWidgets import QApplication

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.domain.capability_wiring import CONNECTED_CAPABILITIES
from spectra_workbench.domain.models import (
    Availability,
    Binding,
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    Entrypoint,
    OutputPlan,
    Presence,
    Requirement,
    TaskSpec,
    Validation,
)
from spectra_workbench.plugins import (
    OPTIONAL_EXTENSION_IDS,
    OptionalExtensionSpec,
    PluginProviderAdapter,
    audit_optional_extensions,
    build_optional_extension_descriptors,
    get_optional_extension_spec,
    list_optional_extension_specs,
)
from spectra_workbench.providers.gdal_provider.adapter import GDALProviderAdapter
from spectra_workbench.providers.rasterio_provider.adapter import RasterioProviderAdapter
from spectra_workbench.registry.catalog import get_default_registry
from spectra_workbench.ui.forms.param_form import CapabilityFormWidget
from spectra_workbench.ui.panels.toolbox_panel import ToolboxPanel

pytestmark = [pytest.mark.integration]

EXPECTED_EXTENSION_IDS = {
    "ext:xarray_dask",
    "ext:sklearn_classify",
    "ext:pdal_pointcloud",
    "ext:geopandas_vector",
    "ext:external_atmospheric",
    "ext:external_gis",
}


@pytest.fixture(scope="module")
def project_root() -> Path:
    return find_project_root()


@pytest.fixture(scope="module")
def capability_schema(project_root: Path) -> dict:
    schema_path = project_root / "contracts" / "capability.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_all_required_optional_extensions_registered_and_schema_valid(
    capability_schema: dict,
) -> None:
    """六类可选扩展全部登记，且转换后的 CapabilityDescriptor 100% 通过 JSON Schema 契约校验。"""
    assert set(OPTIONAL_EXTENSION_IDS) == EXPECTED_EXTENSION_IDS
    specs = list_optional_extension_specs()
    assert len(specs) == 6

    descriptors = build_optional_extension_descriptors()
    assert len(descriptors) == 6

    reg = get_default_registry()
    for spec, desc in zip(specs, descriptors, strict=True):
        # 1. JSON Schema 严格校验
        dumped = desc.to_contract_dict()
        jsonschema.validate(dumped, capability_schema)

        # 2. 全局注册表可查
        in_reg = reg.get(spec.extension_id)
        assert in_reg is not None, f"全局注册表未收录可选扩展: {spec.extension_id}"
        assert in_reg.capability_id == spec.extension_id
        assert in_reg.engine.provider_id == "extension"

        # 3. 范围、依赖、入口、缺口四要素齐备
        assert len(spec.scope.strip()) >= 20
        assert len(spec.non_goals) >= 1
        assert len(spec.core_alternatives) >= 1
        assert len(spec.requirements) >= 1
        assert len(spec.planned_entrypoints) >= 1
        assert len(spec.gaps) >= 1
        assert len(in_reg.limitations) >= len(spec.gaps)


def test_optional_extensions_never_advertised_as_implemented() -> None:
    """防虚标守卫：未实现的可选扩展绝不出现在已发现、可调用、平台可执行或已接通集合中。"""
    audit = audit_optional_extensions()
    assert audit["compliant"] is True
    assert audit["extension_count"] == 6
    assert audit["implemented_count"] == 0
    assert audit["falsely_advertised"] == []
    assert audit["incomplete_specs"] == []

    reg = get_default_registry()
    discovered_ids = {c.capability_id for c in reg.discovered()}
    callable_ids = {c.capability_id for c in reg.callable()}
    exec_ids = {c.capability_id for c in reg.platform_executable()}
    verified_ids = {c.capability_id for c in reg.verified()}
    missing_ids = {c.capability_id for c in reg.missing()}

    for ext_id in EXPECTED_EXTENSION_IDS:
        assert ext_id not in CONNECTED_CAPABILITIES, f"{ext_id} 严禁混入 GUI 已接通集合"
        assert ext_id not in discovered_ids, f"{ext_id} 严禁标为 discovered"
        assert ext_id not in callable_ids, f"{ext_id} 严禁标为 callable"
        assert ext_id not in exec_ids, f"{ext_id} 严禁标为 platform_executable"
        assert ext_id not in verified_ids, f"{ext_id} 严禁标为 verified"
        assert ext_id in missing_ids, f"{ext_id} 必须保留在 missing 缺口清单中"

        cap = reg.get(ext_id)
        assert cap is not None
        assert cap.state.presence == Presence.missing
        assert cap.state.binding == Binding.none
        assert cap.state.validation == Validation.not_run
        assert cap.state.availability in (
            Availability.missing_dependency,
            Availability.blocked,
        )
        assert all(not ep.available for ep in cap.entrypoints)
        ext_spec = get_optional_extension_spec(ext_id)
        assert ext_spec is not None
        assert ext_spec.implemented is False


def test_specific_extension_scopes_and_dependencies() -> None:
    """逐项核对 Xarray/Dask、scikit-learn、PDAL、GeoPandas、外部大气校正与外部 GIS 引擎的具体依赖与边界。"""
    # 1. Xarray / Rioxarray / Dask
    xarray_spec = get_optional_extension_spec("ext:xarray_dask")
    assert xarray_spec is not None
    req_names = {r.name for r in xarray_spec.requirements}
    assert {"xarray", "rioxarray", "dask"}.issubset(req_names)
    assert any("gdal:mdim_slice" in alt for alt in xarray_spec.core_alternatives)

    # 2. scikit-learn 监督分类
    sk_spec = get_optional_extension_spec("ext:sklearn_classify")
    assert sk_spec is not None
    sk_reqs = {r.name for r in sk_spec.requirements}
    assert "scikit-learn" in sk_reqs
    assert "混淆矩阵" in sk_spec.scope
    assert any("ENVI" in ng for ng in sk_spec.non_goals)

    # 3. PDAL 点云
    pdal_spec = get_optional_extension_spec("ext:pdal_pointcloud")
    assert pdal_spec is not None
    pdal_reqs = {r.name for r in pdal_spec.requirements}
    assert "pdal" in pdal_reqs
    assert "LAS/LAZ" in pdal_spec.scope

    # 4. GeoPandas 批量矢量
    gpd_spec = get_optional_extension_spec("ext:geopandas_vector")
    assert gpd_spec is not None
    gpd_reqs = {r.name for r in gpd_spec.requirements}
    assert {"geopandas", "shapely", "pyproj"}.issubset(gpd_reqs)
    assert any("ogr:vectortranslate" in alt for alt in gpd_spec.core_alternatives)

    # 5. 外部辐射传输大气校正 (6S / FLAASH)
    atm_spec = get_optional_extension_spec("ext:external_atmospheric")
    assert atm_spec is not None
    assert atm_spec.availability == Availability.blocked
    assert any("FLAASH" in ng for ng in atm_spec.non_goals)
    assert any("gdal:calibrate" in alt for alt in atm_spec.core_alternatives)

    # 6. 外部 GIS 引擎 (WhiteboxTools / SAGA / GRASS)
    gis_spec = get_optional_extension_spec("ext:external_gis")
    assert gis_spec is not None
    assert "WhiteboxTools" in gis_spec.name
    assert any("gdal:dem" in alt for alt in gis_spec.core_alternatives)


def test_model_validator_rejects_false_advertising() -> None:
    """模型校验器守卫：任何试图将未实现扩展标为 implemented=True、ready 或 available=True 的构造均被拒绝。"""
    base_kwargs = dict(
        extension_id="ext:fake_test",
        name="测试扩展",
        native_name="fake",
        tier="extension",
        category=["extension"],
        scope="测试范围说明",
        non_goals=["不作为已实现宣传"],
        core_alternatives=["核心替代"],
        planned_worker="plugin",
        version_constraint="fake>=1.0",
        documentation_url="https://example.com",
        requirements=[Requirement(kind="package", name="fake", reason="测试依赖")],
        planned_entrypoints=[Entrypoint(kind="specialized", target="fake:run", available=False)],
        availability=Availability.missing_dependency,
        gaps=["尚未实现"],
        implemented=False,
    )

    # 1. implemented=True 被拒绝
    with pytest.raises(ValueError, match="禁止将 implemented 设为 True"):
        OptionalExtensionSpec(**{**base_kwargs, "implemented": True})

    # 2. availability=ready 被拒绝
    with pytest.raises(ValueError, match="禁止声明 availability=ready"):
        OptionalExtensionSpec(**{**base_kwargs, "availability": Availability.ready})

    # 3. entrypoint.available=True 被拒绝
    with pytest.raises(ValueError, match="禁止声明 available=True 的入口"):
        OptionalExtensionSpec(
            **{
                **base_kwargs,
                "planned_entrypoints": [
                    Entrypoint(kind="specialized", target="fake:run", available=True)
                ],
            }
        )


def test_adapters_cleanly_reject_optional_extension_tasks(
    project_root: Path, tmp_path: Path
) -> None:
    """运行期守卫：向 Plugin / GDAL / Rasterio Adapter 提交未实现的可选扩展任务均被明确拦截。"""
    plugin_adapter = PluginProviderAdapter(plugins_dir=tmp_path / "empty_plugins")
    gdal_adapter = GDALProviderAdapter()
    rio_adapter = RasterioProviderAdapter()

    in_dat = project_root / "tests" / "fixtures" / "data" / "a01_ramp_16x16.dat"
    out_tif = tmp_path / "ext_out.tif"

    for ext_id in sorted(EXPECTED_EXTENSION_IDS):
        spec = TaskSpec(
            task_id=f"reject-{ext_id.replace(':', '-')}",
            capability_id=ext_id,
            descriptor_revision="1.0",
            engine=EngineConstraint(
                provider_id="plugin",
                runtime_id="plugin",
                expected_version="1.0",
            ),
            project_root=str(project_root),
            workspace=str(tmp_path),
            inputs={
                "input": DatasetRef(
                    dataset_id="in",
                    kind=DatasetKind.raster,
                    uri=in_dat.as_uri(),
                )
            },
            outputs=[
                OutputPlan(
                    key="output",
                    kind=DatasetKind.raster,
                    uri=out_tif.as_uri(),
                )
            ],
            parameters={},
        )

        # Plugin Adapter 返回专有错误码 optional_extension_not_implemented 及依赖/缺口说明
        p_val = plugin_adapter.validate(spec)
        assert p_val["valid"] is False
        assert p_val["code"] == "optional_extension_not_implemented"
        assert any("尚未实现" in e and "所需依赖" in e for e in p_val["errors"])

        # GDAL / Rasterio Adapter 同样拒绝
        g_val = gdal_adapter.validate(spec)
        assert g_val["valid"] is False
        r_val = rio_adapter.validate(spec)
        assert r_val["valid"] is False


def test_ui_toolbox_and_form_display_extensions_as_unimplemented(
    qapp: QApplication,
) -> None:
    """UI 守卫：工具箱可按『扩展引擎』筛选全部 6 项扩展，表单展示依赖/入口/缺口且严格禁用提交。"""
    panel = ToolboxPanel()
    panel.cmb_provider.setCurrentText("扩展引擎")
    qapp.processEvents()

    assert panel.tree.topLevelItemCount() == 1
    top_node = panel.tree.topLevelItem(0)
    assert top_node is not None
    assert "可选引擎扩展登记" in top_node.text(0)
    assert top_node.childCount() == 6

    reg = get_default_registry()
    form = CapabilityFormWidget()

    for ext_id in sorted(EXPECTED_EXTENSION_IDS):
        cap = reg.get(ext_id)
        assert cap is not None
        form.load_capability(cap)
        assert not form.btn_submit.isEnabled()
        assert not form.lbl_unsupported_banner.isHidden()
        banner = form.lbl_unsupported_banner.text()
        assert "禁止提交" in banner
        assert "依赖要求:" in banner
        assert "规划入口:" in banner
        assert "未实现" in banner

    panel.cmb_provider.setCurrentText("全部引擎")
    qapp.processEvents()
