"""可选引擎扩展登记表（P8-05）。

契约与红线：
1. 明确登记 Xarray/Rioxarray/Dask、scikit-learn、PDAL、GeoPandas、外部大气校正引擎
   （6S/Py6S/MODTRAN-FLAASH）、外部开源 GIS 引擎（WhiteboxTools/SAGA/GRASS）六类可选扩展的：
   - 能力范围（scope）与不承诺边界
   - 依赖项与版本约束（requirements）
   - 规划执行环境与入口（planned_worker / planned_entrypoints）
   - 当前缺口与替代边界（gaps / limitations）
2. 严禁把未实现的可选扩展标为已实现（implemented=False, presence=missing,
   binding=none, availability=missing_dependency 或 blocked, entrypoint.available=False）。
3. 进程隔离：纯静态元数据与契约模型，不导入任何原生地理或科学计算扩展包。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from spectra_workbench.domain.models import (
    Availability,
    Binding,
    CapabilityDescriptor,
    CapabilityEngineSpec,
    CapabilityKind,
    CapabilitySource,
    CapabilityState,
    ContractModel,
    DatasetKind,
    Entrypoint,
    PortSpec,
    Presence,
    Requirement,
    Validation,
)

EXTENSION_BASELINE_ID = "optional-extensions-1.0"
EXTENSION_VERIFIED_DATE = "2026-11-09"


class OptionalExtensionSpec(ContractModel):
    """单项可选引擎扩展登记规格（P8-05）。"""

    extension_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    native_name: str = Field(min_length=1)
    tier: str = Field(min_length=1)
    category: list[str] = Field(min_length=1)
    scope: str = Field(min_length=1)
    non_goals: list[str] = Field(min_length=1)
    core_alternatives: list[str] = Field(min_length=1)
    planned_worker: str = Field(min_length=1)
    version_constraint: str = Field(min_length=1)
    documentation_url: str = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=1)
    planned_entrypoints: list[Entrypoint] = Field(min_length=1)
    inputs: list[PortSpec] = Field(default_factory=list)
    outputs: list[PortSpec] = Field(default_factory=list)
    availability: Availability = Availability.missing_dependency
    gaps: list[str] = Field(min_length=1)
    implemented: bool = False

    @model_validator(mode="after")
    def _enforce_not_falsely_implemented(self) -> OptionalExtensionSpec:
        if self.implemented:
            raise ValueError(f"可选扩展 {self.extension_id} 尚未实现，禁止将 implemented 设为 True")
        if self.availability == Availability.ready:
            raise ValueError(f"未实现的可选扩展 {self.extension_id} 禁止声明 availability=ready")
        if any(ep.available for ep in self.planned_entrypoints):
            raise ValueError(f"未实现的可选扩展 {self.extension_id} 禁止声明 available=True 的入口")
        if not self.gaps:
            raise ValueError(f"可选扩展 {self.extension_id} 必须提供明确的缺口说明")
        return self

    def to_capability_descriptor(self) -> CapabilityDescriptor:
        """转换为注册表统一 CapabilityDescriptor，纳入全局审计分母与缺口对账。"""
        limitations = list(self.gaps)
        for alt in self.core_alternatives:
            alt_note = f"平台核心替代边界：{alt}"
            if alt_note not in limitations:
                limitations.append(alt_note)
        for ng in self.non_goals:
            ng_note = f"不承诺边界：{ng}"
            if ng_note not in limitations:
                limitations.append(ng_note)

        return CapabilityDescriptor(
            capability_id=self.extension_id,
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name=self.name,
            native_name=self.native_name,
            category=list(self.category),
            description=self.scope,
            engine=CapabilityEngineSpec(
                provider_id="extension",
                version_constraint=self.version_constraint,
                runtime_id=self.planned_worker,
                resolved_version=None,
            ),
            source=CapabilitySource(
                documentation_url=self.documentation_url,
                baseline_id=EXTENSION_BASELINE_ID,
                upstream_version=self.version_constraint,
                snapshot_sha256=None,
                verified_date=EXTENSION_VERIFIED_DATE,
            ),
            parameters=[],
            inputs=list(self.inputs),
            outputs=list(self.outputs),
            requirements=list(self.requirements),
            entrypoints=list(self.planned_entrypoints),
            state=CapabilityState(
                presence=Presence.missing,
                binding=Binding.none,
                validation=Validation.not_run,
                availability=self.availability,
            ),
            effects="new_dataset",
            cancellation="unsupported",
            limitations=limitations,
            validation_evidence=[],
        )


OPTIONAL_EXTENSION_SPECS: tuple[OptionalExtensionSpec, ...] = (
    OptionalExtensionSpec(
        extension_id="ext:xarray_dask",
        name="Xarray / Rioxarray / Dask 多维分块延迟计算扩展（未实现）",
        native_name="xarray + rioxarray + dask.array",
        tier="extension",
        category=["extension", "multidimensional", "xarray", "dask", "missing"],
        scope=(
            "面向超大规模 NetCDF/Zarr/HDF 多维时空立方体的延迟加载（Lazy Loading）、"
            "Dask Array 分块有向无环图（DAG）并行调度、跨时间维逐像元统计聚合与时空重采样。"
        ),
        non_goals=[
            "当前版本不内置 Dask 分布式集群调度器，也不将 Xarray/Dask 作为已实现功能宣传",
            "不在 GUI 进程导入 xarray、rioxarray 或 dask",
        ],
        core_alternatives=[
            "多维容器二维切片已由 GDAL multidim API 核心能力 gdal:mdim_slice 实现并验证（P8-01）",
        ],
        planned_worker="plugin",
        version_constraint="xarray>=2024.1,rioxarray>=0.15,dask>=2024.1",
        documentation_url="https://docs.xarray.dev/en/stable/",
        requirements=[
            Requirement(
                kind="package",
                name="xarray",
                constraint=">=2024.1",
                reason="多维命名坐标数组（DataArray / Dataset）建模与 CF 元数据解析",
            ),
            Requirement(
                kind="package",
                name="rioxarray",
                constraint=">=0.15",
                reason="Xarray 与 Rasterio CRS/Affine 空间访问器桥接",
            ),
            Requirement(
                kind="package",
                name="dask",
                constraint=">=2024.1",
                reason="超内存多维数组分块延迟计算与并行图调度",
            ),
            Requirement(
                kind="package",
                name="netCDF4",
                constraint=">=1.6",
                reason="NetCDF4/HDF5 底层分块流式读取后端",
                optional=True,
            ),
        ],
        planned_entrypoints=[
            Entrypoint(
                kind="specialized",
                target="spectra_workbench.extensions.xarray_dask:run_xarray_dask_cube",
                available=False,
                notes="规划通过 P8-04 插件协议在独立科学/插件 Worker 中接入，当前未实现",
            ),
            Entrypoint(
                kind="script",
                target="xarray.open_dataset",
                available=False,
                notes="待目标 Worker 环境安装 xarray/rioxarray/dask 后开放脚本会话",
            ),
        ],
        inputs=[
            PortSpec(
                key="input",
                data_type=DatasetKind.array,
                required=True,
                description="NetCDF / Zarr / HDF 多维数据集",
            )
        ],
        outputs=[
            PortSpec(
                key="output",
                data_type=DatasetKind.raster,
                required=True,
                description="多维聚合或切片输出栅格/数组",
            )
        ],
        availability=Availability.missing_dependency,
        gaps=[
            "未实现：当前受管环境未安装 xarray、rioxarray、dask 依赖，也未接入 Dask 分块图计算执行器，严禁作为已实现功能宣传",
            "需要独立插件环境解析 win-64 锁文件并通过 P8-04 插件 manifest 注册算子后方可启用",
        ],
        implemented=False,
    ),
    OptionalExtensionSpec(
        extension_id="ext:sklearn_classify",
        name="scikit-learn 监督式机器学习分类与精度评价扩展（未实现）",
        native_name="sklearn.ensemble / sklearn.svm / confusion_matrix",
        tier="extension",
        category=["extension", "classification", "machine_learning", "sklearn", "missing"],
        scope=(
            "基于训练 ROI 与地物类别标签的监督式分类插件（随机森林 RandomForest、支持向量机 SVM、"
            "最大似然近似等明确命名算法）、空间上独立的训练/验证样本划分，以及混淆矩阵"
            "（总体精度 OA、Kappa 系数、制图精度 PA、用户精度 UA）独立精度评价报告。"
        ),
        non_goals=[
            "不声称等价实现 ENVI 全部传统分类器或深度学习分割框架",
            "严禁将训练集重代入精度当作独立验证精度报告",
        ],
        core_alternatives=[
            "单波段阈值/区间二值掩膜分类已由 gdal:mask 实现，二分像元植被覆盖度及分级已由 gdal:fvc 实现（P6-05）",
        ],
        planned_worker="plugin",
        version_constraint="scikit-learn>=1.4,joblib>=1.3",
        documentation_url="https://scikit-learn.org/stable/supervised_learning.html",
        requirements=[
            Requirement(
                kind="package",
                name="scikit-learn",
                constraint=">=1.4",
                reason="提供 RandomForestClassifier、SVC 等监督分类器及 confusion_matrix / cohen_kappa_score",
            ),
            Requirement(
                kind="package",
                name="joblib",
                constraint=">=1.3",
                reason="训练模型持久化与分块预测并行化",
            ),
        ],
        planned_entrypoints=[
            Entrypoint(
                kind="form",
                target="ext:sklearn_classify",
                available=False,
                notes="规划通过 P8-04 插件协议提供训练 ROI 选择、独立验证集划分与分类表单，当前未实现",
            ),
            Entrypoint(
                kind="specialized",
                target="spectra_workbench.extensions.sklearn_classify:run_supervised_classify",
                available=False,
                notes="待插件 Worker 安装 scikit-learn 并完成空间独立样本划分契约后启用",
            ),
        ],
        inputs=[
            PortSpec(
                key="input",
                data_type=DatasetKind.raster,
                required=True,
                description="多波段待分类特征栅格",
            ),
            PortSpec(
                key="training_rois",
                data_type=DatasetKind.vector,
                required=True,
                description="带类别标签的训练与独立验证 ROI 矢量",
            ),
        ],
        outputs=[
            PortSpec(
                key="output",
                data_type=DatasetKind.raster,
                required=True,
                description="分类编码栅格成果",
            ),
            PortSpec(
                key="confusion_matrix",
                data_type=DatasetKind.artifact,
                required=True,
                description="独立验证集混淆矩阵与 OA/Kappa 精度报告 JSON",
            ),
        ],
        availability=Availability.missing_dependency,
        gaps=[
            "未实现：当前受管环境未安装 scikit-learn，未实现训练/验证 ROI 空间独立划分、分类器训练及混淆矩阵报告，严禁作为已实现功能宣传",
            "后续通过 P8-04 插件机制以明确算法名（如 RandomForest / SVM）独立交付",
        ],
        implemented=False,
    ),
    OptionalExtensionSpec(
        extension_id="ext:pdal_pointcloud",
        name="PDAL 点云数据处理与三维地形派生扩展（未实现）",
        native_name="PDAL (Point Data Abstraction Library)",
        tier="extension",
        category=["extension", "pointcloud", "pdal", "lidar", "missing"],
        scope=(
            "LAS/LAZ/COPC 机载激光雷达点云读写、PDAL Pipeline JSON 流式滤波"
            "（SMRF/PMF 地面点分类、统计离群点去噪）、高程归一化（Height Above Ground）"
            "及点云栅格化生成 DSM/DTM/CHM。"
        ),
        non_goals=[
            "当前版本不提供三维点云体渲染视图或实时三维地球画布（列为平台远期扩展）",
        ],
        core_alternatives=[
            "二维规则栅格 DEM 地形分析已由 gdal:dem 实现，离散矢量高程点插值栅格化已由 gdal:grid 实现（P5-02）",
        ],
        planned_worker="plugin",
        version_constraint="pdal>=2.6,python-pdal>=3.2",
        documentation_url="https://pdal.io/en/stable/",
        requirements=[
            Requirement(
                kind="native_extension",
                name="pdal",
                constraint=">=2.6",
                reason="PDAL C++ 点云流式处理核心库与 pdal pipeline CLI",
            ),
            Requirement(
                kind="package",
                name="python-pdal",
                constraint=">=3.2",
                reason="Python 与 PDAL Pipeline 数组桥接绑定",
                optional=True,
            ),
        ],
        planned_entrypoints=[
            Entrypoint(
                kind="cli",
                target="pdal",
                available=False,
                notes="本机受管运行时未捆绑 pdal.exe 与点云驱动",
            ),
            Entrypoint(
                kind="specialized",
                target="spectra_workbench.extensions.pdal_pointcloud:run_pdal_pipeline",
                available=False,
                notes="规划通过插件 Worker 调用独立 PDAL 运行时执行管线，当前未实现",
            ),
        ],
        inputs=[
            PortSpec(
                key="input",
                data_type=DatasetKind.any,
                required=True,
                description="LAS / LAZ / COPC 点云数据集",
            )
        ],
        outputs=[
            PortSpec(
                key="output",
                data_type=DatasetKind.raster,
                required=True,
                description="滤波后点云或派生 DSM/DTM 栅格",
            )
        ],
        availability=Availability.missing_dependency,
        gaps=[
            "未实现：当前受管运行时未安装 PDAL 原生库与 CLI，点云读写、地面滤波与三维点云视图均未实现，严禁作为已实现功能宣传",
            "需在独立 conda-forge 点云环境分发 PDAL 二进制并通过插件协议接入",
        ],
        implemented=False,
    ),
    OptionalExtensionSpec(
        extension_id="ext:geopandas_vector",
        name="GeoPandas 批量矢量表格与空间连接分析扩展（未实现）",
        native_name="geopandas + shapely + pyproj + pyogrio",
        tier="spatial_optional",
        category=["extension", "vector", "geopandas", "spatial_analysis", "missing"],
        scope=(
            "基于 GeoDataFrame 的批量矢量空间连接（sjoin / sjoin_nearest）、"
            "多图层拓扑叠加（overlay: union / intersection / difference / symmetric_difference）、"
            "批量缓冲区融合（buffer + dissolve）与复杂属性透视统计。"
        ),
        non_goals=[
            "绝不将 geopandas、fiona 或 pyogrio 导入 GUI 主进程（严格遵守进程隔离红线）",
        ],
        core_alternatives=[
            "矢量格式转换、重投影、属性 WHERE 过滤、空间 BBOX 裁剪及 OGR SQL 查询已由核心能力 ogr:vectortranslate 实现（P5-04）",
        ],
        planned_worker="plugin",
        version_constraint="geopandas>=1.0,shapely>=2.0,pyproj>=3.6,pyogrio>=0.9",
        documentation_url="https://geopandas.org/en/stable/",
        requirements=[
            Requirement(
                kind="package",
                name="geopandas",
                constraint=">=1.0",
                reason="提供 GeoDataFrame 批量空间叠加、连接与表格统计",
            ),
            Requirement(
                kind="package",
                name="pyogrio",
                constraint=">=0.9",
                reason="基于 GDAL/OGR 的高性能矢量化批量读写后端",
            ),
            Requirement(
                kind="package",
                name="shapely",
                constraint=">=2.0",
                reason="GEOS 平面几何谓词与拓扑运算",
            ),
            Requirement(
                kind="package",
                name="pyproj",
                constraint=">=3.6",
                reason="PROJ 坐标参考系定义与批量坐标变换",
            ),
        ],
        planned_entrypoints=[
            Entrypoint(
                kind="specialized",
                target="spectra_workbench.extensions.geopandas_vector:run_geopandas_overlay",
                available=False,
                notes="规划作为表格/批量矢量可选插件在独立 Worker 中运行，当前未实现",
            ),
            Entrypoint(
                kind="script",
                target="geopandas.read_file",
                available=False,
                notes="待独立插件环境安装 geopandas 后在脚本会话中开放",
            ),
        ],
        inputs=[
            PortSpec(
                key="input",
                data_type=DatasetKind.vector,
                required=True,
                description="主输入矢量图层",
            ),
            PortSpec(
                key="overlay",
                data_type=DatasetKind.vector,
                required=False,
                description="叠加或空间连接辅助矢量图层",
            ),
        ],
        outputs=[
            PortSpec(
                key="output",
                data_type=DatasetKind.vector,
                required=True,
                description="批量空间分析结果矢量或统计表",
            )
        ],
        availability=Availability.missing_dependency,
        gaps=[
            "未实现：GeoPandas 在开发方案中定位为表格/批量矢量可选插件，当前未预装且未接入表单执行器，严禁作为已实现功能宣传",
            "核心矢量处理请使用已验证的 ogr:vectortranslate；批量 GeoDataFrame 插件待独立环境锁文件完成后接入",
        ],
        implemented=False,
    ),
    OptionalExtensionSpec(
        extension_id="ext:external_atmospheric",
        name="外部物理辐射传输大气校正引擎扩展 (6S / Py6S / MODTRAN-FLAASH)（未实现）",
        native_name="6S / Py6S / ENVI FLAASH (External Radiative Transfer)",
        tier="extension",
        category=["extension", "atmospheric", "radiometric", "external", "blocked"],
        scope=(
            "通过桥接外部物理辐射传输模型（Second Simulation of a Satellite Signal in the Solar Spectrum 6S、"
            "Py6S 或商业授权 MODTRAN/FLAASH 引擎）执行气溶胶反演、水汽吸收校正与地表反射率精确反演。"
        ),
        non_goals=[
            "绝不以简化经验公式或暗目标减法（DOS1）冒充 FLAASH 或 6S 物理辐射传输模型已实现",
            "不捆绑受商业许可限制的 MODTRAN / ENVI FLAASH 专有二进制程序",
        ],
        core_alternatives=[
            "显式 gain/offset 辐射定标与 TOA 表观反射率已由 gdal:calibrate 实现，Chavez 1988 DOS1 暗目标减法已由 gdal:dos1 实现（P6-06）",
            "平台内同时保留 platform:flaash_external 阻断占位项以防误用",
        ],
        planned_worker="plugin",
        version_constraint="Py6S>=1.9,sixs>=1.1",
        documentation_url="https://py6s.readthedocs.io/en/latest/",
        requirements=[
            Requirement(
                kind="service",
                name="sixs / MODTRAN / FLAASH 外部引擎",
                reason="物理大气辐射传输查找表（LUT）计算需外部编译的 6S 或授权 MODTRAN 可执行程序",
            ),
            Requirement(
                kind="package",
                name="Py6S",
                constraint=">=1.9",
                reason="6S 辐射传输模型 Python 参数封装与输出解析接口",
                optional=True,
            ),
        ],
        planned_entrypoints=[
            Entrypoint(
                kind="specialized",
                target="spectra_workbench.extensions.external_atmospheric:run_radiative_transfer",
                available=False,
                notes="外部辐射传输引擎桥接插件入口，需用户配置合规的外部引擎路径，当前未实现",
            ),
        ],
        inputs=[
            PortSpec(
                key="input",
                data_type=DatasetKind.raster,
                required=True,
                description="已完成辐射定标的辐亮度或 TOA 栅格",
            )
        ],
        outputs=[
            PortSpec(
                key="output",
                data_type=DatasetKind.raster,
                required=True,
                description="物理大气校正地表反射率栅格",
            )
        ],
        availability=Availability.blocked,
        gaps=[
            "未实现：缺少外部 6S 二进制或商业授权 MODTRAN/FLAASH 引擎及插件适配器，严禁以经验公式冒充已实现",
            "当前仅作为可选外部校正引擎登记范围、依赖与入口契约；平台内置可用方法为 gdal:calibrate 与 gdal:dos1",
        ],
        implemented=False,
    ),
    OptionalExtensionSpec(
        extension_id="ext:external_gis",
        name="外部开源 GIS 引擎桥接扩展 (WhiteboxTools / SAGA / GRASS)（未实现）",
        native_name="WhiteboxTools / SAGA GIS / GRASS GIS CLI Bridge",
        tier="extension",
        category=["extension", "gis", "hydrology", "external", "missing"],
        scope=(
            "桥接外部专业 GIS 命令行引擎（WhiteboxTools、SAGA GIS saga_cmd、GRASS GIS），"
            "扩展水文填洼（Breach/Fill Depressions）、D8/D-Infinity 流向与汇流累积量、"
            "流域盆地提取及专业地貌形态学分析工具。"
        ),
        non_goals=[
            "不在核心安装包内强制捆绑数百 MB 的 SAGA / GRASS 完整桌面运行时",
        ],
        core_alternatives=[
            "基础 DEM 坡度、坡向、山体阴影、TPI、TRI、粗糙度已由 gdal:dem 实现，等值线与视域分析已由 gdal:contour / gdal:viewshed 提供",
        ],
        planned_worker="plugin",
        version_constraint="whitebox>=2.3",
        documentation_url="https://www.whiteboxgeo.com/manual/wbt_book/intro.html",
        requirements=[
            Requirement(
                kind="native_extension",
                name="whitebox_tools / saga_cmd / grass",
                constraint=">=2.3",
                reason="需外部独立编译的 WhiteboxTools 或 SAGA/GRASS 命令行可执行程序",
            ),
            Requirement(
                kind="package",
                name="whitebox",
                constraint=">=2.3",
                reason="WhiteboxTools Python 命令行包装接口",
                optional=True,
            ),
        ],
        planned_entrypoints=[
            Entrypoint(
                kind="cli",
                target="whitebox_tools",
                available=False,
                notes="未随核心包分发 whitebox_tools / saga_cmd 外部可执行程序",
            ),
            Entrypoint(
                kind="specialized",
                target="spectra_workbench.extensions.external_gis:run_external_gis_cli",
                available=False,
                notes="规划通过 P8-04 插件协议桥接外部 GIS CLI，当前未实现",
            ),
        ],
        inputs=[
            PortSpec(
                key="input",
                data_type=DatasetKind.raster,
                required=True,
                description="输入 DEM 或水文栅格",
            )
        ],
        outputs=[
            PortSpec(
                key="output",
                data_type=DatasetKind.raster,
                required=True,
                description="外部 GIS 水文/地貌分析输出栅格",
            )
        ],
        availability=Availability.missing_dependency,
        gaps=[
            "未实现：未捆绑 WhiteboxTools / SAGA / GRASS 外部运行时，也未编写 CLI 桥接插件，严禁作为已实现功能宣传",
            "后续由用户按需配置外部 GIS 引擎路径并通过插件 manifest 接入",
        ],
        implemented=False,
    ),
)

OPTIONAL_EXTENSION_IDS: frozenset[str] = frozenset(
    spec.extension_id for spec in OPTIONAL_EXTENSION_SPECS
)

_SPEC_BY_ID: dict[str, OptionalExtensionSpec] = {
    spec.extension_id: spec for spec in OPTIONAL_EXTENSION_SPECS
}


def get_optional_extension_spec(extension_id: str) -> OptionalExtensionSpec | None:
    """按能力/扩展 ID 查询可选引擎扩展登记规格。"""
    return _SPEC_BY_ID.get(extension_id)


def list_optional_extension_specs() -> list[OptionalExtensionSpec]:
    """返回全部已登记的可选引擎扩展规格（按 extension_id 排序）。"""
    return [_SPEC_BY_ID[k] for k in sorted(_SPEC_BY_ID.keys())]


def build_optional_extension_descriptors() -> list[CapabilityDescriptor]:
    """构建可选引擎扩展的 CapabilityDescriptor 列表，供全局注册表登记与审计。"""
    return [spec.to_capability_descriptor() for spec in list_optional_extension_specs()]


def audit_optional_extensions() -> dict[str, Any]:
    """对全部可选引擎扩展执行合规审计（P8-05）。

    核验要点：
    1. 每项扩展均明确声明范围（scope）、依赖（requirements）、规划入口（planned_entrypoints）与缺口（gaps）；
    2. 绝无任何未实现扩展被标为 implemented=True、presence=present、availability=ready 或 entrypoint.available=True；
    3. 每项扩展均有明确的平台核心替代边界（core_alternatives）与不承诺边界（non_goals）。
    """
    specs = list_optional_extension_specs()
    descriptors = [s.to_capability_descriptor() for s in specs]

    falsely_advertised: list[str] = []
    incomplete_specs: list[str] = []
    items: list[dict[str, Any]] = []

    for spec, desc in zip(specs, descriptors, strict=True):
        if (
            spec.implemented
            or desc.state.presence != Presence.missing
            or desc.state.binding != Binding.none
            or desc.state.availability == Availability.ready
            or any(ep.available for ep in desc.entrypoints)
        ):
            falsely_advertised.append(spec.extension_id)

        if (
            not spec.scope.strip()
            or not spec.requirements
            or not spec.planned_entrypoints
            or not spec.gaps
            or not spec.core_alternatives
            or not spec.non_goals
        ):
            incomplete_specs.append(spec.extension_id)

        items.append(
            {
                "extension_id": spec.extension_id,
                "name": spec.name,
                "native_name": spec.native_name,
                "tier": spec.tier,
                "planned_worker": spec.planned_worker,
                "version_constraint": spec.version_constraint,
                "scope": spec.scope,
                "non_goals": list(spec.non_goals),
                "core_alternatives": list(spec.core_alternatives),
                "requirements": [r.to_contract_dict() for r in spec.requirements],
                "planned_entrypoints": [ep.to_contract_dict() for ep in spec.planned_entrypoints],
                "gaps": list(spec.gaps),
                "presence": desc.state.presence.value,
                "binding": desc.state.binding.value,
                "availability": desc.state.availability.value,
                "implemented": spec.implemented,
            }
        )

    return {
        "baseline_id": EXTENSION_BASELINE_ID,
        "verified_date": EXTENSION_VERIFIED_DATE,
        "extension_count": len(specs),
        "implemented_count": sum(1 for s in specs if s.implemented),
        "falsely_advertised": falsely_advertised,
        "incomplete_specs": incomplete_specs,
        "compliant": not falsely_advertised and not incomplete_specs and len(specs) >= 6,
        "extensions": items,
    }
