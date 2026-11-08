"""P2-01 ~ P2-03 能力基准、GDAL/Rasterio 索引与覆盖审计单元测试。

验证目标：
1. P2-01: 目标版本固定为 GDAL 3.13.3 与 Rasterio 1.5.2，来源、版本、SHA-256 哈希完整，
   契约模式校验 100% 通过，随机抽样与官方目录一致。
2. P2-02: GDAL 结构化算法、传统 CLI 命令、驱动检测及 API 索引，新旧别名双向关联，
   缺失命令（gdal_auth、废弃脚本、HDF5 驱动）显式保留为 missing 状态。
3. P2-03: Rasterio 公共 API 分类与 21 项 rio 核心子命令编目，排除私有符号；
   SWIG / C 扩展无法反射签名的接口附带说明。
4. 四维状态独立判定：已发现 (discovered)、可调用 (callable)、已验证 (verified)、缺失 (missing)。
5. 进程隔离：注册表模块导入严禁污染进程导入 osgeo/rasterio/fiona/geopandas。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest

from spectra_workbench.app.doctor import find_project_root
from spectra_workbench.domain.models import (
    Availability,
    Binding,
    CapabilityKind,
    DatasetKind,
    DatasetRef,
    EngineConstraint,
    OutputPlan,
    Presence,
    TaskSpec,
)
from spectra_workbench.providers.gdal_provider.adapter import GDALProviderAdapter
from spectra_workbench.providers.rasterio_provider.adapter import RasterioProviderAdapter
from spectra_workbench.registry.baseline import (
    compute_capabilities_sha256,
    load_baseline_from_file,
)
from spectra_workbench.registry.catalog import (
    get_default_registry,
)
from spectra_workbench.registry.gdal_index import (
    GDAL_ALIASES,
    GDAL_DOC_BASE,
    GDAL_VERSION,
    build_gdal_algorithms,
    build_gdal_drivers,
    build_gdal_python_api_index,
)
from spectra_workbench.registry.rasterio_index import (
    RASTERIO_DOC_BASE,
    RASTERIO_VERSION,
    build_rasterio_python_api,
    build_rio_cli_commands,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def project_root() -> Path:
    return find_project_root()


@pytest.fixture(scope="module")
def capability_schema(project_root: Path) -> dict:
    schema_path = project_root / "contracts" / "capability.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


# ==============================================================================
# P2-01: 目标版本上游基准、SHA-256 哈希与契约一致性测试
# ==============================================================================


def test_baseline_json_files_exist_and_conform_to_schema(
    project_root: Path,
    capability_schema: dict,
):
    """基准文件在 contracts/baselines 与 resources/baselines 均存在且通过 JSON Schema 严格校验。"""
    gdal_json = project_root / "contracts" / "baselines" / "gdal-3.13.3.json"
    rasterio_json = project_root / "contracts" / "baselines" / "rasterio-1.5.2.json"

    assert gdal_json.exists(), f"GDAL 基准文件缺失: {gdal_json}"
    assert rasterio_json.exists(), f"Rasterio 基准文件缺失: {rasterio_json}"

    for path in (gdal_json, rasterio_json):
        meta, descriptors = load_baseline_from_file(path)
        assert meta.capability_count > 0
        assert len(meta.items_sha256) == 64

        for desc in descriptors:
            # 严格核对每个条目的 snapshot_sha256 与基准哈希一致
            assert desc.source.snapshot_sha256 == meta.items_sha256
            # 校验 JSON Schema
            dumped = desc.model_dump(mode="json")
            jsonschema.validate(dumped, capability_schema)


def test_baseline_sha256_cryptographic_integrity(project_root: Path):
    """基准条目哈希不可篡改：任何微小改动都会使哈希重算失败。"""
    gdal_json = project_root / "contracts" / "baselines" / "gdal-3.13.3.json"
    meta, descriptors = load_baseline_from_file(gdal_json)

    # 正常重算哈希完全匹配
    recomputed = compute_capabilities_sha256(descriptors)
    assert recomputed == meta.items_sha256

    # 篡改任意描述符内容
    tampered_descriptors = [d.model_copy(deep=True) for d in descriptors]
    tampered_descriptors[0].description = "非法篡改的描述字符串"
    tampered_hash = compute_capabilities_sha256(tampered_descriptors)
    assert tampered_hash != meta.items_sha256


def test_random_sampling_matches_official_catalog():
    """手工随机抽查项与上游官方目录完全吻合。"""
    reg = get_default_registry()

    # 1. 抽查 GDAL 经典核心工具
    assert reg.has("gdal:translate")
    assert reg.has("gdal:warp")
    assert reg.has("gdal:dem")
    assert reg.has("gdal:grid")
    assert reg.has("gdal:buildvrt")
    assert reg.has("gdal:cli:gdalinfo")
    assert reg.has("ogr:vectortranslate")

    desc_translate = reg.get("gdal:translate")
    assert desc_translate is not None
    assert desc_translate.engine.provider_id == "gdal"
    assert desc_translate.source.upstream_version == GDAL_VERSION
    assert desc_translate.source.documentation_url.startswith(GDAL_DOC_BASE)

    # 2. 抽查 Rasterio 核心接口与 CLI
    assert reg.has("rasterio:api:open")
    assert reg.has("rasterio:api:mask")
    assert reg.has("rasterio:api:reproject")
    assert reg.has("rasterio:api:Window")
    assert reg.has("rio:cli:info")
    assert reg.has("rio:cli:bounds")
    assert reg.has("rio:cli:convert")
    assert reg.has("rio:cli:warp")
    assert reg.has("rio:cli:merge")

    desc_rio_info = reg.get("rio:cli:info")
    assert desc_rio_info is not None
    assert desc_rio_info.engine.provider_id == "rasterio"
    assert desc_rio_info.source.upstream_version == RASTERIO_VERSION
    assert desc_rio_info.source.documentation_url.startswith(RASTERIO_DOC_BASE)


# ==============================================================================
# P2-02: GDAL 结构化算法、CLI、驱动检测与别名关联
# ==============================================================================


def test_gdal_algorithms_count_and_structure():
    """GDAL 结构化算法规格完整（36 项 = 18 项原生 + 18 项平台组合/受限实现）。

    其中 `gdal:stack` / `gdal:bandmanage` / `gdal:statistics` 是平台基于
    原生 API 的组合实现，`gdal:calc` 是平台受限 AST 求值器，
    `gdal:sindex` 是平台光谱指数（numpy），`gdal:fftfilter` 是平台 FFT
    频域滤波（numpy.fft），`gdal:pca`/`gdal:tasseledcap` 是平台矩阵变换，
    `gcpregister`/`rasterdiff`/`pansharpen` 是平台配准/差值/融合实现，
    `mask`/`fvc` 是平台掩膜/植被覆盖度流程，性质与 18 项原生工具不同，
    已在 registry 文档串中标注。
    """
    algorithms = build_gdal_algorithms()
    assert len(algorithms) == 36

    algo_ids = {a.capability_id for a in algorithms}
    expected_ids = {
        "gdal:translate",
        "gdal:warp",
        "gdal:dem",
        "gdal:grid",
        "gdal:buildvrt",
        "gdal:rasterize",
        "gdal:contour",
        "gdal:polygonize",
        "gdal:sieve",
        "gdal:fillnodata",
        "gdal:nearblack",
        "gdal:viewshed",
        "gdal:calc",
        "gdal:merge",
        "gdal:proximity",
        "gdal:retile",
        "gdal:pansharpen",
        "ogr:vectortranslate",
        # 平台组合能力（非 GDAL 原生独立工具）
        "gdal:stack",
        "gdal:bandmanage",
        # P4-03：元数据编辑 / 概览构建（有原生 CLI 工具 gdal_edit.py / gdaladdo）
        "gdal:editmeta",
        "gdal:overview",
        # P4-04：直方图与基础统计（平台组合能力）
        "gdal:statistics",
        # P6-01：光谱指数（平台能力，numpy 求值）
        "gdal:sindex",
        # P6-02：FFT 频域滤波 / 去条纹（平台能力，numpy.fft 求值）
        "gdal:fftfilter",
        # P6-03：PCA 正/逆变换与缨帽变换（平台能力，numpy 矩阵运算）
        "gdal:pca",
        "gdal:tasseledcap",
        # P6-04：GCP 配准 / 栅格差值（pansharpen 既有条目升级为平台融合）
        "gdal:gcpregister",
        "gdal:rasterdiff",
        # P6-05：二值掩膜 / FVC 植被覆盖度
        "gdal:mask",
        "gdal:fvc",
        # P6-06：辐射定标 / DOS1 大气校正 / 多时相去云
        "gdal:calibrate",
        "gdal:dos1",
        "gdal:cloudreplace",
        # P6-06 blocked：FLAASH 外部引擎 / 农业计数（方法未确定）
        "platform:flaash_external",
        "platform:agriculture_count",
    }
    assert algo_ids == expected_ids

    for a in algorithms:
        if a.state.presence != Presence.present:
            # blocked/missing 占位条目（platform:flaash_external 等）无真实输入端口
            continue
        assert a.kind == CapabilityKind.algorithm
        assert len(a.inputs) >= 1
        assert len(a.outputs) >= 1
        assert len(a.entrypoints) >= 1


def test_gdal_aliases_two_way_mapping():
    """新旧命令别名双向关联：支持通过别名或原生 ID 透明查找。"""
    reg = get_default_registry()

    for alias, target_id in GDAL_ALIASES.items():
        assert reg.has(alias), f"别名未登记: {alias}"
        assert reg.has(target_id), f"目标能力 ID 未登记: {target_id}"

        by_alias = reg.get(alias)
        by_id = reg.get(target_id)
        assert by_alias is not None
        assert by_id is not None
        assert by_alias.capability_id == by_id.capability_id == target_id

        # 验证反向别名索引
        aliases = reg.get_aliases(target_id)
        assert alias in aliases


def test_gdal_missing_and_deprecated_tools_retained():
    """缺口保留约束与状态纠正：缺失工具保留，现存 utility 如实反映状态与替代关系。"""
    reg = get_default_registry()

    # 1. 确实缺失的工具
    gdal_auth = reg.get("gdal:cli:gdal_auth")
    assert gdal_auth is not None, "缺失工具未保留: gdal_auth"
    assert gdal_auth.state.presence == Presence.missing
    assert gdal_auth.state.availability == Availability.unsupported
    assert len(gdal_auth.limitations) > 0

    # 2. HDF5 缺失驱动
    hdf5 = reg.get("gdal:driver:HDF5")
    assert hdf5 is not None, "缺失的 HDF5 驱动未保留"
    assert hdf5.state.presence == Presence.missing
    assert hdf5.state.availability == Availability.missing_dependency

    # 3. 历史脚本 rgb2pct 与 pct2rgb 在当前环境的 osgeo_utils 模块中实际存在
    for mod_name, alt_hint in [("rgb2pct", "rgb-to-palette"), ("pct2rgb", "gdal_translate -expand")]:
        cap = reg.get(f"gdal:cli:{mod_name}")
        assert cap is not None, f"历史工具未登记: {mod_name}"
        assert cap.state.presence == Presence.present
        assert cap.state.binding == Binding.cli
        assert cap.state.availability == Availability.ready
        assert any(alt_hint in lim for lim in cap.limitations)


def test_gdal_present_drivers_catalog():
    """驱动探测基准收录全量 205 个已注册驱动与 1 个缺失驱动。"""
    drivers = build_gdal_drivers()
    assert len(drivers) == 206
    driver_map = {d.native_name: d for d in drivers}

    for expected in ("GTiff", "ENVI", "COG", "VRT", "GPKG", "ESRI Shapefile", "netCDF", "HDF4"):
        assert expected in driver_map
        d = driver_map[expected]
        assert d.state.presence == Presence.present
        assert d.state.availability == Availability.ready

    assert "HDF5" in driver_map
    assert driver_map["HDF5"].state.presence == Presence.missing


# ==============================================================================
# P2-03: Rasterio API / rio CLI 编目与 GDAL/OGR/OSR 索引
# ==============================================================================


def test_rio_all_21_commands_cataloged():
    """全量编目官方 21 项 rio 核心子命令。"""
    rio_cmds = build_rio_cli_commands()
    cmd_names = {c.native_name.split()[1] for c in rio_cmds if c.native_name.startswith("rio ")}

    expected_cmds = {
        "blocks", "bounds", "calc", "clip", "convert", "create", "edit-info",
        "env", "gcps", "info", "insp", "mask", "merge", "overview", "rasterize",
        "rm", "sample", "shapes", "stack", "transform", "warp",
    }
    assert expected_cmds.issubset(cmd_names)


def test_rio_missing_plugins_retained():
    """缺口保留约束：未安装的 rio 插件明确保留为 missing 状态。"""
    reg = get_default_registry()

    for plugin in ("cogeo", "color", "mbtiles"):
        cid = f"rio:cli:{plugin}"
        cap = reg.get(cid)
        assert cap is not None, f"缺失插件未保留: {cid}"
        assert cap.state.presence == Presence.missing
        assert cap.state.availability == Availability.missing_dependency


def test_rasterio_api_no_private_symbols_and_classified():
    """公共对象分类；避免私有符号盲扫；不能反射签名的接口有说明。"""
    apis = build_rasterio_python_api()

    for item in apis:
        # 绝无以 _ 开头的私有符号
        parts = item.native_name.split(".")
        for part in parts:
            assert not part.startswith("_"), f"非法私有符号泄露: {item.native_name}"

        # 具备分类与签名说明
        assert len(item.category) >= 2
        assert len(item.limitations) >= 1
        assert "签名" in item.limitations[0]


def test_gdal_swig_interfaces_have_explanatory_notes():
    """GDAL/OGR/OSR Python API 索引：SWIG C 扩展接口附带说明。"""
    gdal_apis = build_gdal_python_api_index()
    api_map = {a.capability_id: a for a in gdal_apis}

    # Open 与 OpenEx 接口
    assert "gdal:api:Open" in api_map
    open_cap = api_map["gdal:api:Open"]
    assert len(open_cap.limitations) >= 1
    assert "SWIG" in open_cap.limitations[0]

    # 空间参考接口
    assert "osr:api:SpatialReference" in api_map
    srs_cap = api_map["osr:api:SpatialReference"]
    assert srs_cap.kind == CapabilityKind.class_


# ==============================================================================
# 四维状态独立判定与审计报告
# ==============================================================================


def test_four_dimensional_state_independence_and_audit():
    """四维状态严格解耦独立，审计报告基准分母非零，包含真实已验证与平台执行统计。"""
    reg = get_default_registry()
    report = reg.audit_report()

    assert report["completeness"] == "partial"
    # 第 32 轮：新增 gdal:stack / gdal:bandmanage 两项平台组合能力编目，
    # 并把 gdal:buildvrt / gdal:merge 的 binding 修正为 cli（有原生 CLI 工具）、
    # 新增两项为 form（仅平台表单入口）。据此：
    #   基线总数 443 -> 445；已发现 438 -> 440；
    #   可调用 229 -> 232（buildvrt/stack/bandmanage 各 +1，merge 恢复 cli 不变）；
    #   平台可执行 3 -> 5。
    # 第 35 轮（P4-04）：新增 gdal:statistics 编目，并把 gdal:calc 补上 form 入口
    # （原条目已计入基线，故只 +1 个新条目）：447 -> 448、442 -> 443、234 -> 235。
    # 第 48 轮（P5-02）：gdal:dem 接入 form 执行器，callable +1、platform_executable +1，
    # 从缺失清单移出，缺失项 8 -> 7；第 49 轮 gdal:rasterize 接入后 7 -> 6；
    # 第 50 轮 gdal:grid 接入后 6 -> 5。
    # 第 51 轮（P5-02）：gdal:polygonize 接入 form 执行器（成果为矢量），
    # platform_executable 8 -> 9（原本 binding=cli 已计入 callable，缺口维持 5 不变）。
    # 第 52 轮（P5-02）：gdal:contour 接入 form 执行器，platform_executable 9 -> 10。
    # 第 55 轮（P5-04）：ogr:vectortranslate 接入 form 执行器，platform_executable 10 -> 11
    # （原本 binding=cli 已计入 callable）。
    # 第 58 轮（P6-01）：新增平台能力 gdal:sindex 编目（基线 448 -> 449）并接入 form
    # 执行器：基线/已发现/可调用/平台可执行 449/444/239/12。
    # 第 59 轮（P6-02）：新增平台能力 gdal:fftfilter 编目并接入 form 执行器：
    # 450/445/240/13。
    # 第 60 轮（P6-03）：新增 gdal:pca 与 gdal:tasseledcap：452/447/242/15。
    # 第 61 轮（P6-04）：新增 gcpregister/rasterdiff 并把既有 pansharpen 升级为
    # form 执行器：454/449/244/18。
    # 第 62 轮（P6-05）：新增 gdal:mask 与 gdal:fvc：456/451/246/20。
    # 第 63 轮（P6-06）：新增 calibrate/dos1/cloudreplace + blocked 的
    # flaash_external/agriculture_count：461/454/249/23、missing 7。
    assert report["total_baseline_count"] == 461
    assert report["discovered"]["count"] == 454
    assert report["callable"]["count"] == 249
    assert report["platform_executable"]["count"] == 23
    # 第 63 轮 blocked 新增 flaash_external 与 agriculture_count：5 -> 7
    assert report["missing"]["count"] == 7
    assert report["verified"]["count"] == 5

    assert report["discovered"]["ratio"] > 0.90
    assert report["callable"]["ratio"] > 0.50

    # 分引擎统计核验
    by_provider = report["by_provider"]
    assert "gdal" in by_provider
    assert "rasterio" in by_provider

    gdal_rep = by_provider["gdal"]
    # 第 32 轮新增 stack/bandmanage；第 34 轮新增 editmeta/overview；
    # 第 35 轮新增 statistics：基线总数 402 -> 407，已发现 400 -> 405。
    # 第 58 轮新增平台能力 gdal:sindex：407 -> 408、405 -> 406。
    # 第 59 轮新增平台能力 gdal:fftfilter：408 -> 409、406 -> 407。
    # 第 60 轮新增 gdal:pca / gdal:tasseledcap：409 -> 411、407 -> 409。
    # 第 61 轮新增 gcpregister/rasterdiff（pansharpen 升级既有条目）：411 -> 413、409 -> 411。
    # 第 62 轮新增 gdal:mask / gdal:fvc：413 -> 415、411 -> 413。
    # 第 63 轮新增 3 个可执行 + 2 个 blocked：415 -> 420、413 -> 416、missing 5。
    # gdal:buildvrt、P5-02 的 gdal:dem/rasterize/grid 由 blocked 改为 ready，
    # GDAL 缺失项 6 -> 5 -> 4 -> 3 -> 2。
    assert gdal_rep["total_baseline_count"] == 420
    assert gdal_rep["discovered"]["count"] == 416
    # ? 63 ? blocked ?? flaash_external ? agriculture_count?2 -> 4
    assert gdal_rep["missing"]["count"] == 4

    rio_rep = by_provider["rasterio"]
    assert rio_rep["total_baseline_count"] == 41
    assert rio_rep["discovered"]["count"] == 38
    assert rio_rep["missing"]["count"] == 3


def test_provider_adapters_discover_baseline():
    """GDAL 与 Rasterio 适配器在各自 worker 内均可发现基准能力。"""
    gdal_adapter = GDALProviderAdapter()
    gdal_caps = gdal_adapter.discover()
    # 第 32/34/35 轮新增编目；第 58 轮 gdal:sindex：407 -> 408；
    # 第 59 轮 gdal:fftfilter：408 -> 409；第 60 轮 pca/tasseledcap：409 -> 411；
    # 第 61 轮 gcpregister/rasterdiff：411 -> 413；第 62 轮 mask/fvc：413 -> 415；
    # 第 63 轮 calibrate/dos1/cloudreplace + 2 blocked：415 -> 420。
    assert len(gdal_caps) == 420

    rio_adapter = RasterioProviderAdapter()
    rio_caps = rio_adapter.discover()
    assert len(rio_caps) == 41


def test_audit_capability_registered_without_handler(project_root: Path):
    """审计测试 1：能力在注册表中存在（如 gdal:retile），但平台 Adapter 尚未接入 Handler。"""
    reg = get_default_registry()
    assert reg.has("gdal:retile"), "gdal:retile 必须存在于目录中以保持完整上游基准"
    retile_cap = reg.get("gdal:retile")
    assert retile_cap is not None
    assert retile_cap.state.availability == Availability.ready

    adapter = GDALProviderAdapter()
    assert "gdal:retile" not in adapter.SUPPORTED_CAPABILITIES
    in_dat = project_root / "tests/fixtures/data/a01_ramp_16x16.dat"
    spec = TaskSpec(
        task_id="audit-unhandled-cap",
        capability_id="gdal:retile",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(project_root),
        workspace=str(project_root / ".staging"),
        inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=(project_root / "retile.tif").as_uri())],
        parameters={},
    )
    val = adapter.validate(spec)
    assert not val["valid"]
    assert any("尚未接入该能力执行器" in err for err in val["errors"])


def test_audit_module_exists_but_platform_unwired(project_root: Path):
    """审计测试 2：模块与 API 在 Python 环境中存在（如 rasterio:api:mask），但平台未接线为 Task 任务。"""
    reg = get_default_registry()
    assert reg.has("rasterio:api:mask")
    mask_cap = reg.get("rasterio:api:mask")
    assert mask_cap is not None
    assert mask_cap.state.binding == Binding.script

    adapter = RasterioProviderAdapter()
    in_dat = project_root / "tests/fixtures/data/a01_ramp_16x16.dat"
    spec = TaskSpec(
        task_id="audit-unwired-api",
        capability_id="rasterio:api:mask",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="rasterio", runtime_id="sw-rasterio", expected_version="1.5.2"),
        project_root=str(project_root),
        workspace=str(project_root / ".staging"),
        inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=in_dat.as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=(project_root / "mask.tif").as_uri())],
        parameters={},
    )
    val = adapter.validate(spec)
    assert not val["valid"]
    assert any("尚未接入该能力执行器" in err for err in val["errors"])


def test_audit_runtime_missing_dependency():
    """审计测试 3：缺失外部运行时或依赖项时，状态准确记录为 missing 与 missing_dependency。"""
    reg = get_default_registry()

    hdf5 = reg.get("gdal:driver:HDF5")
    assert hdf5 is not None
    assert hdf5.state.presence == Presence.missing
    assert hdf5.state.availability == Availability.missing_dependency

    mbtiles = reg.get("rio:cli:mbtiles")
    assert mbtiles is not None
    assert mbtiles.state.presence == Presence.missing
    assert mbtiles.state.availability == Availability.missing_dependency


def test_audit_validation_failure_handling(project_root: Path):
    """审计测试 4：输入文件不存在或同源冲突时，验证准入准确拦截并返回详细错误。"""
    adapter = GDALProviderAdapter()
    non_existent = project_root / "not_existing_file.dat"
    spec = TaskSpec(
        task_id="audit-val-failure",
        capability_id="gdal:translate",
        descriptor_revision="1.0",
        engine=EngineConstraint(provider_id="gdal", runtime_id="gdal-qgis", expected_version="3.13.3"),
        project_root=str(project_root),
        workspace=str(project_root / ".staging"),
        inputs={"input": DatasetRef(dataset_id="in", kind=DatasetKind.raster, uri=non_existent.as_uri())},
        outputs=[OutputPlan(key="output", kind=DatasetKind.raster, uri=(project_root / "out.tif").as_uri())],
        parameters={},
    )
    val = adapter.validate(spec)
    assert not val["valid"]
    assert any("输入文件不存在" in err for err in val["errors"])


def test_ui_isolation_from_geospatial_libs():
    """隔离性守卫：导入 registry 模块绝对不得导入 osgeo/rasterio/fiona/geopandas。"""
    for forbidden in ("osgeo", "rasterio", "fiona", "geopandas"):
        assert forbidden not in sys.modules, f"违背 UI 进程隔离约束：已导入 {forbidden}"
