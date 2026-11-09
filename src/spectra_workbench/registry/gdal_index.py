"""GDAL 3.13.3 算法、传统 CLI 命令、驱动与 Python API 索引编目（P2-02）。

契约保证：
1. 目标版本固定为 GDAL 3.13.3（Release 2026-08-13, 'Iowa City'）。
2. 保留官方原始描述、分类与原生标识符。
3. 建立新旧别名双向关联（例如 gdal_translate <-> gdal:translate, ogr2ogr <-> ogr:vectortranslate）。
4. 缺失或未装配能力（如 gdal_auth、HDF5 驱动、废弃脚本 rgb2pct）严格保留为 missing / unsupported 状态，严禁静默删除。
5. 公共 API 对象严密分类，排除私有符号；SWIG C 扩展无法反射签名的接口（(*args)）附带详尽说明。
6. 本模块纯元数据与静态契约，导入时不加载 osgeo 原生动态库，安全服务于 UI 进程。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spectra_workbench.domain.models import (
    Availability,
    Binding,
    CapabilityDescriptor,
    CapabilityEngineSpec,
    CapabilityKind,
    CapabilitySource,
    CapabilityState,
    DatasetKind,
    Entrypoint,
    ParameterSpec,
    PortSpec,
    Presence,
    Requirement,
    Validation,
    ValidationEvidence,
)
from spectra_workbench.processing.raster_params import (
    CALIBRATE_MODES,
    DIFF_MODES,
    FUSION_METHODS,
    FVC_ENDMEMBER_MODES,
    GCP_TRANSFORMS,
    LOCAL_RESAMPLES,
)
from spectra_workbench.processing.tasseledcap_registry import sensor_keys

GDAL_VERSION = "3.13.3"
GDAL_DOC_BASE = "https://gdal.org/en/3.13.3/"
BASELINE_ID = "gdal-3.13.3"

# 新旧命令与别名双向映射表（包含经典命令与统一 CLI 叶子命令别名）
GDAL_ALIASES: dict[str, str] = {
    # 经典别名
    "gdal_translate": "gdal:translate",
    "gdalwarp": "gdal:warp",
    "gdaldem": "gdal:dem",
    "gdal_grid": "gdal:grid",
    "gdalbuildvrt": "gdal:buildvrt",
    "gdal_rasterize": "gdal:rasterize",
    "gdal_contour": "gdal:contour",
    "gdal_polygonize": "gdal:polygonize",
    "gdal_polygonize.py": "gdal:polygonize",
    "gdal_sieve": "gdal:sieve",
    "gdal_sieve.py": "gdal:sieve",
    "gdal_fillnodata": "gdal:fillnodata",
    "gdal_fillnodata.py": "gdal:fillnodata",
    "nearblack": "gdal:nearblack",
    "gdal_viewshed": "gdal:viewshed",
    "gdal_calc": "gdal:calc",
    "gdal_calc.py": "gdal:calc",
    "gdal_merge": "gdal:merge",
    "gdal_merge.py": "gdal:merge",
    "gdal_proximity": "gdal:proximity",
    "gdal_proximity.py": "gdal:proximity",
    "gdal_retile": "gdal:retile",
    "gdal_retile.py": "gdal:retile",
    "gdal_pansharpen": "gdal:pansharpen",
    "gdal_pansharpen.py": "gdal:pansharpen",
    "ogr2ogr": "ogr:vectortranslate",
    # 统一 CLI 叶子命令别名映射
    "gdal raster convert": "gdal:translate",
    "gdal raster warp": "gdal:warp",
    "gdal raster info": "gdal:cli:gdalinfo",
    "gdal raster dem": "gdal:dem",
    "gdal raster build-vrt": "gdal:buildvrt",
    "gdal raster rasterize": "gdal:rasterize",
    "gdal raster polygonize": "gdal:polygonize",
    "gdal raster calc": "gdal:calc",
    "gdal raster merge": "gdal:merge",
    "gdal raster proximity": "gdal:proximity",
    "gdal raster sieve": "gdal:sieve",
    "gdal raster fill-nodata": "gdal:fillnodata",
    "gdal raster retile": "gdal:retile",
    "gdal raster viewshed": "gdal:viewshed",
    "gdal raster near-black": "gdal:nearblack",
    "gdal raster clean-borders": "gdal:nearblack",
    "gdal raster create-overviews": "gdal:cli:gdaladdo",
    "gdal raster rgb-to-palette": "gdal:cli:rgb2pct",
    "gdal vector convert": "ogr:vectortranslate",
    "gdal vector info": "gdal:cli:ogrinfo",
    "gdal vector tile-index": "gdal:cli:ogrtindex",
    "gdal vector linear-referencing": "gdal:cli:ogrlineref",
}


def _load_resource_json(filename: str) -> dict[str, Any] | None:
    """尝试从 resources/ 或 docs/progress/runs/ 载入静态元数据 JSON。"""
    res_candidates = [
        Path(__file__).resolve().parent.parent / "resources" / filename,
        Path(__file__).resolve().parent.parent.parent.parent / "docs" / "progress" / "runs" / filename,
        Path("src/spectra_workbench/resources") / filename,
    ]
    for c in res_candidates:
        if c.exists():
            try:
                return json.loads(c.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
    return None


def _engine_spec() -> CapabilityEngineSpec:
    return CapabilityEngineSpec(
        provider_id="gdal",
        version_constraint=">=3.13,<4",
        runtime_id="gdal-qgis",
        resolved_version=GDAL_VERSION,
    )


def _source_spec(doc_path: str) -> CapabilitySource:
    url = f"{GDAL_DOC_BASE}{doc_path.lstrip('/')}"
    return CapabilitySource(
        documentation_url=url,
        baseline_id=BASELINE_ID,
        upstream_version=GDAL_VERSION,
        snapshot_sha256=None,  # 由基准组装器统一计算回填
        verified_date="2026-11-04",
    )


def build_gdal_algorithms() -> list[CapabilityDescriptor]:
    """GDAL 结构化算法清单（23 项）。

    其中前 18 项是 GDAL **原生独立工具/算法**；
    `gdal:stack`、`gdal:bandmanage`、`gdal:statistics` 是**平台基于原生 API
    的组合实现**（分别由 `BuildVRT(separate=True)`、`Translate(bandList=...)`
    与 numpy 统计/GDAL 原生统计对照组合而来）；`gdal:calc` 是**平台受限 AST
    求值器**（不调用上游 `gdal_calc.py` 的 eval 语义）。这些能力与原生工具性质
    不同，此处在清单中并列但语义已标注，以免基线对账时把「平台组合能力」
    误当作原生算法。
    """
    return [
        CapabilityDescriptor(
            capability_id="gdal:translate",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 栅格格式转换",
            native_name="gdal.Translate",
            category=["conversion", "raster"],
            description="在不同栅格格式间转换影像，支持子集截取、尺寸重缩放、格式创建选项与重采样算法。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_translate.html"),
            parameters=[
                ParameterSpec(
                    key="format",
                    native_name="format",
                    label="输出驱动格式",
                    type="string",
                    required=False,
                    default="GTiff",
                    choices=["GTiff", "ENVI", "COG", "PNG", "JPEG", "netCDF"],
                    help="目标栅格驱动名称",
                ),
                ParameterSpec(
                    key="outputType",
                    native_name="outputType",
                    label="输出数据类型",
                    type="string",
                    required=False,
                    choices=["Byte", "UInt16", "Int16", "UInt32", "Int32", "Float32", "Float64"],
                ),
                ParameterSpec(
                    key="resampleAlg",
                    native_name="resampleAlg",
                    label="重采样算法",
                    type="string",
                    required=False,
                    default="nearest",
                    choices=["nearest", "bilinear", "cubic", "cubicspline", "lanczos", "average", "mode"],
                ),
                ParameterSpec(key="srcWin", native_name="srcWin", label="像素裁剪窗口 [xoff, yoff, xsize, ysize]", type="list", required=False),
                ParameterSpec(key="projWin", native_name="projWin", label="投影坐标裁剪窗口 [ulx, uly, lrx, lry]", type="list", required=False),
                ParameterSpec(key="outputBounds", native_name="outputBounds", label="输出边界范围 [minx, miny, maxx, maxy]", type="list", required=False),
                ParameterSpec(key="xRes", native_name="xRes", label="输出像元宽度", type="number", required=False),
                ParameterSpec(key="yRes", native_name="yRes", label="输出像元高度", type="number", required=False),
                ParameterSpec(key="width", native_name="width", label="输出宽度 (像元)", type="integer", required=False),
                ParameterSpec(key="height", native_name="height", label="输出高度 (像元)", type="integer", required=False),
                ParameterSpec(key="noData", native_name="noData", label="指定无效值 (NoData)", type="number", required=False),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="输入源影像")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="输出目标影像")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 osgeo.gdal C++ 核心引擎与栅格驱动")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:translate", available=True, notes="GUI 动态配置表单"),
                Entrypoint(kind="cli", target="gdal_translate.exe", available=True, notes="标准 CLI 别名"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.smoke_passed,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[],
            validation_evidence=[
                ValidationEvidence(
                    case_id="smoke_translate_envi_to_gtiff",
                    status="passed",
                    runtime_id="gdal-3.13.3",
                    report_uri="docs/progress/runs/env_report.json",
                )
            ],
        ),
        CapabilityDescriptor(
            capability_id="gdal:warp",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 栅格投影重采样与校正",
            native_name="gdal.Warp",
            category=["warp", "raster", "projection"],
            description="栅格影像重投影、镶嵌、多图层对齐、裁剪到边界或几何掩膜以及重采样处理。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdalwarp.html"),
            parameters=[
                ParameterSpec(key="dstSRS", native_name="dstSRS", label="目标空间参考 (CRS)", type="crs", required=False),
                ParameterSpec(key="srcSRS", native_name="srcSRS", label="源空间参考 (CRS)", type="crs", required=False),
                ParameterSpec(
                    key="resampleAlg",
                    native_name="resampleAlg",
                    label="重采样方式",
                    type="string",
                    required=False,
                    default="near",
                    choices=["near", "bilinear", "cubic", "cubicspline", "lanczos", "average", "mode", "max", "min"],
                ),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="目标无效值 (NoData)", type="number", required=False),
                ParameterSpec(key="srcWin", native_name="srcWin", label="源像素裁剪窗口 [xoff, yoff, xsize, ysize]", type="list", required=False),
                ParameterSpec(key="projWin", native_name="projWin", label="源投影裁剪窗口 [ulx, uly, lrx, lry]", type="list", required=False),
                ParameterSpec(key="outputBounds", native_name="outputBounds", label="目标输出范围 [minx, miny, maxx, maxy]", type="list", required=False),
                ParameterSpec(key="xRes", native_name="xRes", label="输出像元宽度", type="number", required=False),
                ParameterSpec(key="yRes", native_name="yRes", label="输出像元高度", type="number", required=False),
                ParameterSpec(key="width", native_name="width", label="输出宽度 (像元)", type="integer", required=False),
                ParameterSpec(key="height", native_name="height", label="输出高度 (像元)", type="integer", required=False),
                ParameterSpec(key="outputType", native_name="outputType", label="输出数据类型", type="string", required=False),
                ParameterSpec(key="targetAlignedPixels", native_name="targetAlignedPixels", label="像元网格对齐 (-tap)", type="boolean", required=False, default=False),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, multiple=True, description="输入源影像列表")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="重投影成果栅格")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 PROJ 库与 GDAL Warp API")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:warp", available=True),
                Entrypoint(kind="cli", target="gdalwarp.exe", available=True, notes="标准 CLI 别名"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.smoke_passed,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[],
            validation_evidence=[
                ValidationEvidence(
                    case_id="smoke_warp_utm_to_webmercator_3857",
                    status="passed",
                    runtime_id="gdal-3.13.3",
                    report_uri="docs/progress/runs/env_report.json",
                )
            ],
        ),
        CapabilityDescriptor(
            capability_id="gdal:dem",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL DEM 数字高程地形分析",
            native_name="gdal.Dem",
            category=["dem", "terrain", "raster"],
            description="从高程栅格生成山体阴影、坡度、坡向、彩色地貌、地形粗糙度、TRI 与 TPI。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdaldem.html"),
            parameters=[
                ParameterSpec(
                    key="processing",
                    native_name="processing",
                    label="分析模式",
                    type="string",
                    required=True,
                    choices=["hillshade", "slope", "aspect", "TRI", "TPI", "roughness"],
                ),
                ParameterSpec(key="zFactor", native_name="zFactor", label="高程缩放比例 (zFactor)", type="number", required=False, default=1.0),
                ParameterSpec(key="scale", native_name="scale", label="垂直比例因数 (scale)", type="number", required=False, default=1.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="输入 DEM 影像")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="地形分析成果图")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 GDAL DEM 处理核心")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:dem", available=True, notes="平台适配器已接入（P5-02）"),
                Entrypoint(kind="cli", target="gdaldem.exe", available=True, notes="标准 CLI 别名"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "支持 hillshade/slope/aspect/TRI/TPI/roughness；color-relief 需颜色表，当前未接入",
                "输出为 Float32 栅格，PNG/JPEG 等不支持的格式会在写盘前拒绝",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:grid",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 离散点栅格插值",
            native_name="gdal.Grid",
            category=["interpolation", "grid", "raster"],
            description="将离散散点矢量数据或点集通过反距离加权 (IDW)、最近邻、滑动平均或线性插值构建连续栅格。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_grid.html"),
            parameters=[
                ParameterSpec(
                    key="algorithm",
                    native_name="algorithm",
                    label="插值算法",
                    type="string",
                    required=False,
                    default="invdist",
                    choices=["invdist", "invdistnn", "average", "nearest", "linear"],
                ),
                ParameterSpec(key="zField", native_name="zfield", label="高程字段名（可选，默认读几何 Z）", type="string", required=False),
                ParameterSpec(key="width", native_name="width", label="输出宽度（像元）", type="integer", required=True),
                ParameterSpec(key="height", native_name="height", label="输出高度（像元）", type="integer", required=True),
                ParameterSpec(key="outputBounds", native_name="te", label="输出范围 minx,miny,maxx,maxy", type="list", required=False),
                ParameterSpec(key="outputType", native_name="outputType", label="输出数据类型", type="string", required=False, default="Float64"),
                ParameterSpec(key="noData", native_name="noData", label="输出无效值 (NoData)", type="number", required=False),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.vector, required=True, description="包含三维或属性值的输入点图层")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="插值生成的规则栅格")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 GDAL Grid API")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:grid", available=True, notes="平台适配器已接入（P5-02）"),
                Entrypoint(kind="cli", target="gdal_grid.exe", available=True, notes="标准 CLI 别名"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "必须给出 width/height；未指定 outputBounds 时使用点图层 extent",
                "默认 invdist；linear 在规则平面上可按解析值验证，输出 dtype 默认 Float64",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:buildvrt",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 构建虚拟栅格 (VRT)",
            native_name="gdal.BuildVRT",
            category=["vrt", "mosaic", "raster"],
            description="根据多个输入栅格快速构建虚拟镶嵌或多波段堆叠 XML，无物理像素写入延迟与冗余占用。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdalbuildvrt.html"),
            parameters=[
                ParameterSpec(key="overlapPriority", native_name="overlapPriority", label="重叠区优先级 (last/first)", type="string", required=False, default="last", choices=["last", "first"]),
                ParameterSpec(key="resolution", native_name="resolution", label="分辨率策略", type="string", required=False, default="average", choices=["first", "estimate", "average", "highest", "lowest", "same"]),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源无效值覆盖 (srcNodata)", type="number", required=False),
                ParameterSpec(key="vrtNodata", native_name="VRTNodata", label="VRT 虚拟无效值 (vrtNodata)", type="number", required=False),
                ParameterSpec(key="separate", native_name="separate", label="按独立波段堆叠", type="boolean", required=False, default=False),
                ParameterSpec(key="relativePaths", native_name="relativePaths", label="使用相对源路径", type="boolean", required=False, default=True),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, multiple=True, description="输入源文件列表")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="生成的 .vrt 虚拟头文件")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 VRT 驱动支持")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:buildvrt", available=True, notes="平台适配器已接入（P4-02）"),
                Entrypoint(kind="cli", target="gdalbuildvrt.exe", available=True, notes="标准 CLI 别名"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=["仅构建 VRT 描述、不写物理像元；源引用默认相对最终目录", "混合 CRS 明确拒绝；非 separate 要求源波段结构一致；源依赖只读，不参与 sidecar 删除"],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:stack",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 波段堆叠 (Band Stack)",
            native_name="gdal.BuildVRT(separate=True)",
            category=["stack", "band", "raster"],
            description="把多个栅格的波段按输入顺序堆叠为多波段成果；每个源的所有波段依次进入。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdalbuildvrt.html"),
            parameters=[
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData", type="number", required=False),
                ParameterSpec(key="vrtNodata", native_name="VRTNodata", label="VRT NoData", type="number", required=False),
                ParameterSpec(key="xRes", native_name="xRes", label="输出像元宽", type="number", required=False),
                ParameterSpec(key="yRes", native_name="yRes", label="输出像元高", type="number", required=False),
                ParameterSpec(key="outputBounds", native_name="outputBounds", label="输出边界范围 minx,miny,maxx,maxy", type="list", required=False),
                ParameterSpec(key="outputType", native_name="outputType", label="输出数据类型", type="string", required=False),
            ],
            inputs=[PortSpec(key="inputs", data_type=DatasetKind.raster, required=True, multiple=True, description="按堆叠顺序排列的源文件列表")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="多波段成果栅格")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 VRT 与 Translate 支持")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:stack", available=True, notes="平台适配器已接入（P4-02）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=["separate=True 时每个源的所有波段依次进入，波段数不必相同", "输出格式受 S4 矩阵限制（PNG/JPEG 不支持 NoData/mask；JPEG 仅 Byte 1/3 波段）"],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:bandmanage",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 波段管理 (Band Management)",
            native_name="gdal.Translate(bandList=...)",
            category=["band", "raster"],
            description="选择并按给定顺序重排波段（1-based），可用于抽取子集或调整波段次序。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_translate.html"),
            parameters=[
                ParameterSpec(key="bandList", native_name="bandList", label="波段顺序 (1-based)", type="list", required=False),
                ParameterSpec(key="outputType", native_name="outputType", label="输出数据类型", type="string", required=False),
            ],
            inputs=[PortSpec(key="inputs", data_type=DatasetKind.raster, required=True, multiple=False, description="单个输入栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="重排后的成果栅格")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 Translate 的 -b 支持")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:bandmanage", available=True, notes="平台适配器已接入（P4-02）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=["波段号从 1 开始；输出顺序严格按给定列表", "输出格式受 S4 矩阵限制；ENVI/JPEG 等不支持的 alpha/波段组合会提前拒绝"],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:editmeta",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 元数据编辑 (Metadata Edit)",
            native_name="gdal.SetNoDataValue",
            category=["metadata", "raster"],
            description="就地更新数据集的 NoData 元数据；ENVI 头文件按「读-合并-写」保留未知字段。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_edit.html"),
            parameters=[
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="NoData 值", type="number", required=True),
            ],
            inputs=[PortSpec(key="inputs", data_type=DatasetKind.raster, required=True, multiple=False, description="待更新的数据集（输出=输入，in-place）")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="更新后的同一数据集")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需更新模式打开")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:editmeta", available=True, notes="平台适配器已接入（P4-03）"),
                Entrypoint(kind="cli", target="gdal_edit.py", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="modifies_dataset",
            cancellation="cooperative",
            limitations=[
                "in-place 更新会覆盖源文件（须显式允许覆盖）",
                "GDAL 重写 ENVI 头会丢未知字段；平台用读-合并-写保留",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:overview",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 概览构建 (Build Overviews)",
            native_name="gdal.BuildOverviews",
            category=["overview", "raster"],
            description="按给定倍率构建金字塔概览；不改主影像尺寸契约，ENVI 产生外部 .ovr sidecar。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdaladdo.html"),
            parameters=[
                ParameterSpec(key="resampleAlg", native_name="resampleAlg", label="重采样方式", type="string", required=False, default="NEAREST"),
                ParameterSpec(key="levels", native_name="levels", label="倍率列表（严格递增，每个≥2）", type="list", required=True),
                ParameterSpec(key="overviewCompress", native_name="COMPRESS_OVERVIEW", label="概览压缩", type="string", required=False),
            ],
            inputs=[PortSpec(key="inputs", data_type=DatasetKind.raster, required=True, multiple=False, description="待构建概览的数据集（输出=输入，in-place）")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="构建概览后的同一数据集")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 BuildOverviews 支持")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:overview", available=True, notes="平台适配器已接入（P4-03）"),
                Entrypoint(kind="cli", target="gdaladdo.exe", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="modifies_dataset",
            cancellation="cooperative",
            limitations=["in-place 更新会覆盖源文件（须显式允许覆盖）"],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:rasterize",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 矢量要素栅格化",
            native_name="gdal.Rasterize",
            category=["conversion", "rasterize", "vector"],
            description="将矢量要素（多边形、折线、点）按照固定数值或属性字段烧录到已有或新建栅格中。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_rasterize.html"),
            parameters=[
                ParameterSpec(key="attribute", native_name="attribute", label="烧录属性字段名", type="string", required=False),
                ParameterSpec(key="burnValues", native_name="burnValues", label="固定烧录像元值列表", type="list", required=False),
                ParameterSpec(key="allTouched", native_name="allTouched", label="触碰全部像元 (AllTouched)", type="boolean", required=False, default=False),
                ParameterSpec(key="xRes", native_name="xRes", label="输出像元宽", type="number", required=False),
                ParameterSpec(key="yRes", native_name="yRes", label="输出像元高", type="number", required=False),
                ParameterSpec(key="width", native_name="width", label="输出宽度 (像元)", type="integer", required=False),
                ParameterSpec(key="height", native_name="height", label="输出高度 (像元)", type="integer", required=False),
                ParameterSpec(key="outputBounds", native_name="te", label="输出范围 minx,miny,maxx,maxy", type="list", required=False),
                ParameterSpec(key="outputType", native_name="outputType", label="输出数据类型", type="string", required=False, default="Float64"),
                ParameterSpec(key="noData", native_name="noData", label="输出无效值 (NoData)", type="number", required=False),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.vector, required=True, description="输入待栅格化矢量")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="烧录成果栅格")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 GDAL Rasterize API")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:rasterize", available=True, notes="平台适配器已接入（P5-02）"),
                Entrypoint(kind="cli", target="gdal_rasterize.exe", available=True, notes="标准 CLI 别名"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "需要 xRes/yRes 或 width/height；未指定输出范围时使用矢量图层 extent",
                "burnValues 与 attribute 互斥；输出 dtype 默认 Float64，PNG/JPEG 需显式 Byte/UInt16 并通过格式矩阵",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:contour",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 栅格等值线生成",
            native_name="gdal.ContourGenerate",
            category=["vector", "terrain", "contour"],
            description="从单波段数值栅格生成等间距或指定高程的矢量等值线折线/多边形要素。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_contour.html"),
            parameters=[
                ParameterSpec(key="interval", native_name="interval", label="等值线步长", type="number", required=False, default=10.0),
                ParameterSpec(key="offset", native_name="offset", label="基础偏移零点", type="number", required=False, default=0.0),
                ParameterSpec(key="fixedLevels", native_name="fixedLevels", label="固定等级序列（完全替代 interval）", type="list", required=False),
                ParameterSpec(key="band", native_name="band", label="输入波段（1-based，默认 1）", type="integer", required=False, default=1),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="输入单波段高程栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.vector, required=True, description="生成的等值线矢量图层")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 GDAL Contour 生成核心")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:contour", available=True, notes="平台适配器已接入（P5-02）"),
                Entrypoint(kind="cli", target="gdal_contour.exe", available=True),
                Entrypoint(kind="script", target="osgeo.gdal.ContourGenerate", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "矢量成果支持 GPKG / GeoJSON 单文件与 ESRI Shapefile 多文件 bundle（.shp/.shx/.dbf/.prj/.cpg）",
                "band 默认 1；遵循输入 NoData 标签（GDAL useNoData 语义）",
                "interval 与 fixedLevels 二选一：fixedLevels 非空时完全替代等差序列（GDAL 3.13.3 实测）",
                "输出为 3D Line String（wkbLineString25D），字段为 ID 与 ELEV",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:polygonize",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 栅格连通域多边形化",
            native_name="gdal.Polygonize",
            category=["conversion", "vectorize", "raster"],
            description="将单波段离散分类栅格或掩膜中的同值相邻像元转换为矢量多边形。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_polygonize.html"),
            parameters=[
                ParameterSpec(key="fieldName", native_name="fieldName", label="像元值保存字段名", type="string", required=False, default="DN"),
                ParameterSpec(key="connectedness", native_name="connectedness", label="连通性 (4邻域/8邻域)", type="integer", required=False, default=4, choices=[4, 8]),
                ParameterSpec(key="band", native_name="band", label="输入波段（1-based，默认 1）", type="integer", required=False, default=1),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="输入离散分类栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.vector, required=True, description="生成的多边形矢量图层")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 GDAL Polygonize API")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:polygonize", available=True, notes="平台适配器已接入（P5-02）"),
                Entrypoint(kind="cli", target="gdal_polygonize.exe", available=True),
                Entrypoint(kind="script", target="osgeo.gdal.Polygonize", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "矢量成果支持 GPKG / GeoJSON 单文件与 ESRI Shapefile 多文件 bundle（.shp/.shx/.dbf/.prj/.cpg）",
                "band 默认 1；mask 语义遵循 GDAL GetMaskBand（自动遵循 NoData/alpha/mask）",
                "连通性支持 4/8 邻域（8CONNECTED）；像元值存入 fieldName 字段（默认 DN）",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:sieve",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 孤立杂斑滤除 (Sieve Filter)",
            native_name="gdal.SieveFilter",
            category=["filter", "cleanup", "raster"],
            description="消除分类栅格中面积小于阈值的小斑块，并将其合并到最大的相邻多边形中。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_sieve.html"),
            parameters=[
                ParameterSpec(key="threshold", native_name="threshold", label="最小保留像元斑块尺寸", type="integer", required=True, default=4),
                ParameterSpec(key="connectedness", native_name="connectedness", label="邻域连通度 (4 或 8)", type="integer", required=False, default=4, choices=[4, 8]),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="输入分类斑块栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="杂斑滤除后栅格")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 GDAL SieveFilter 算法")],
            entrypoints=[
                Entrypoint(kind="cli", target="gdal_sieve.exe", available=True),
                Entrypoint(kind="script", target="osgeo.gdal.SieveFilter", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:fillnodata",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 无效值插值填补 (FillNodata)",
            native_name="gdal.FillNodata",
            category=["interpolation", "repair", "raster"],
            description="通过向内逆距离权重加权插值填补栅格影像中的 NoData 缺口或无效斑点。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_fillnodata.html"),
            parameters=[
                ParameterSpec(key="maxDistance", native_name="maxDistance", label="最大填补搜索距离 (像元)", type="integer", required=False, default=10),
                ParameterSpec(key="smoothIterations", native_name="smoothIterations", label="平滑迭代次数", type="integer", required=False, default=0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="包含 NoData 缺口的栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="缺口修复成果影像")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 GDAL FillNodata 算法")],
            entrypoints=[
                Entrypoint(kind="cli", target="gdal_fillnodata.exe", available=True),
                Entrypoint(kind="script", target="osgeo.gdal.FillNodata", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:nearblack",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 接近黑白边界整洁处理 (Nearblack)",
            native_name="gdal.Nearblack",
            category=["filter", "cleanup", "raster"],
            description="将斜置航摄或遥感拼接边缘近黑或近白的压缩伪影修正为严格精确的单一背景值或 NoData。",
            engine=_engine_spec(),
            source=_source_spec("programs/nearblack.html"),
            parameters=[
                ParameterSpec(key="near", native_name="near", label="接近阈值范围", type="integer", required=False, default=15),
                ParameterSpec(key="white", native_name="white", label="搜索白色而非黑色边界", type="boolean", required=False, default=False),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="边缘含杂散压缩伪影影像")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="黑白边界清洁影像")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 nearblack 原生算法")],
            entrypoints=[
                Entrypoint(kind="cli", target="nearblack.exe", available=True),
                Entrypoint(kind="script", target="osgeo.gdal.Nearblack", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:viewshed",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL DEM 视域通视分析 (Viewshed)",
            native_name="gdal.ViewshedGenerate",
            category=["dem", "terrain", "analysis"],
            description="基于观察者位置坐标与高度，在 DEM 地形上计算视线通视可视范围掩膜。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_viewshed.html"),
            parameters=[
                ParameterSpec(key="ox", native_name="ox", label="观察者 X 坐标", type="number", required=True),
                ParameterSpec(key="oy", native_name="oy", label="观察者 Y 坐标", type="number", required=True),
                ParameterSpec(key="observerHeight", native_name="observerHeight", label="观察点附加高程 (米)", type="number", required=False, default=2.0),
                ParameterSpec(key="targetHeight", native_name="targetHeight", label="目标点附加高程 (米)", type="number", required=False, default=0.0),
                ParameterSpec(key="maxDistance", native_name="maxDistance", label="最大通视分析半径", type="number", required=False, default=10000.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="输入 DEM 栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="通视范围二值掩膜图")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 gdal_viewshed 原生模块")],
            entrypoints=[
                Entrypoint(kind="cli", target="gdal_viewshed.exe", available=True),
                Entrypoint(kind="script", target="osgeo.gdal.ViewshedGenerate", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:calc",
            descriptor_revision="1.1",
            kind=CapabilityKind.algorithm,
            name="栅格表达式计算器 (受限 AST)",
            native_name="gdal_calc.py / 平台受限求值器",
            category=["calc", "algebra", "raster"],
            description=(
                "对 1..26 幅影像按 A/B/C… 顺序做像元级代数计算。"
                "平台不用 eval：表达式经 AST 白名单解析后由 numpy 求值，"
                "统一 float32 提升防整数回绕，除零等数学无效像元计入输出 NoData。"
            ),
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_calc.html"),
            parameters=[
                ParameterSpec(key="calc", native_name="calc", label="受限表达式", type="expression", required=True, help="例如: (A-B)/(A+B)；支持 + - * / // % ** 与 abs/min/max/sqrt/exp/log/log10"),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="输入 NoData（留空取各源标签）", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData（默认 -9999）", type="number", required=False),
                ParameterSpec(key="outputType", native_name="outputType", label="输出数据类型", type="string", required=False, default="Float32"),
            ],
            inputs=[
                PortSpec(key="inputs", data_type=DatasetKind.raster, required=True, multiple=True, description="按顺序对应 A/B/C… 的输入栅格列表（允许 1 个）"),
            ],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="单波段表达式成果图")],
            requirements=[
                Requirement(kind="package", name="GDAL", reason="需 GDAL 读写栅格"),
                Requirement(kind="package", name="numpy", reason="需 numpy 向量计算库"),
            ],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:calc", available=True, notes="平台适配器已接入（P4-04）"),
                Entrypoint(kind="cli", target="gdal_calc.exe", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "安全边界：AST 白名单，不接受属性访问/下标/lambda/任意调用，不执行用户代码",
                "每个输入只取第 1 波段；多波段源请先用波段管理抽取",
                "输入尺寸/仿射/投影不一致时明确拒绝，不做静默重采样",
                "整型输出若结果越界或含小数会被拒绝（防二次回绕与静默截断）",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:statistics",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="直方图与基础统计报告",
            native_name="gdal.Band.ComputeStatistics / GetHistogram",
            category=["statistics", "histogram", "raster"],
            description=(
                "按 sampled（步长网格，明确标注估计）或 full（全量）模式统计各波段，"
                "排除 NoData/NaN/Inf，输出含直方图与 GDAL 原生近似/精确对照的 JSON 报告。"
            ),
            engine=_engine_spec(),
            source=_source_spec("programs/gdalinfo.html"),
            parameters=[
                ParameterSpec(key="statisticsMode", native_name="mode", label="统计模式", type="string", required=False, default="sampled", choices=["sampled", "full"]),
                ParameterSpec(key="histogramBins", native_name="bins", label="直方图分箱数", type="integer", required=False, default=256),
                ParameterSpec(key="maxSamples", native_name="max_samples", label="抽样预算（像元）", type="integer", required=False, default=1000000),
                ParameterSpec(key="fullLimit", native_name="full_limit", label="全量模式像元上限", type="integer", required=False, default=50000000),
                ParameterSpec(key="bandList", native_name="bandList", label="统计波段（1-based，留空=全部）", type="list", required=False),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="NoData 覆盖值（留空取波段标签）", type="number", required=False),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, multiple=False, description="待统计的栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.artifact, required=True, description="JSON 统计报告（不加载为栅格图层）")],
            requirements=[
                Requirement(kind="package", name="GDAL", reason="需 GDAL 读取波段与原生统计"),
                Requirement(kind="package", name="numpy", reason="需 numpy 计算分位数与直方图"),
            ],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:statistics", available=True, notes="平台适配器已接入（P4-04）"),
                Entrypoint(kind="cli", target="gdalinfo.exe -stats", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "full 模式超过 fullLimit 时明确拒绝，不静默降级为抽样",
                "报告为 JSON artifact，不作为栅格图层加载到地图视图",
                "为避免写 .aux.xml 副作用，原生统计对照期间临时禁用 PAM",
                "超 2000 万像元时跳过 GDAL 原生精确统计对照并在报告中注明",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:merge",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 栅格重叠镶嵌拼图 (gdal_merge)",
            native_name="gdal_merge.py",
            category=["mosaic", "raster"],
            description="将多幅同投影空间相邻或重叠的栅格影像物理镶嵌融合为单幅大尺寸栅格。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_merge.html"),
            parameters=[
                ParameterSpec(key="overlapPriority", native_name="overlapPriority", label="重叠区优先级 (last/first)", type="string", required=False, default="last", choices=["last", "first"]),
                ParameterSpec(key="resolution", native_name="resolution", label="分辨率策略", type="string", required=False, default="average", choices=["first", "estimate", "average", "highest", "lowest", "same"]),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源无效值 (srcNodata)", type="number", required=False),
                ParameterSpec(key="vrtNodata", native_name="VRTNodata", label="中间 VRT 无效值 (vrtNodata)", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="目标输出无效值 (dstNodata)", type="number", required=False),
                ParameterSpec(key="xRes", native_name="xRes", label="输出像元宽", type="number", required=False),
                ParameterSpec(key="yRes", native_name="yRes", label="输出像元高", type="number", required=False),
                ParameterSpec(key="outputBounds", native_name="outputBounds", label="输出边界范围 minx,miny,maxx,maxy", type="list", required=False),
                ParameterSpec(key="outputType", native_name="outputType", label="输出数据类型", type="string", required=False),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, multiple=True, description="输入相邻待拼图像列表")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="镶嵌合并成果影像")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 VRT 与 Translate 支持")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:merge", available=True, notes="平台适配器已接入（P4-02）"),
                Entrypoint(kind="cli", target="gdal_merge.exe", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=["本机无 gdal_merge.py，平台实现走「VRT + Translate」路径；不兼容输入会被明确拒绝", "输出格式受 S4 矩阵限制；旧 sidecar 事务删除、输入依赖受保护"],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:proximity",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 邻近距离图生成 (gdal_proximity)",
            native_name="gdal_proximity.py",
            category=["distance", "analysis", "raster"],
            description="计算影像中每个像元距离目标要素/目标像元值的欧氏几何距离图或欧氏欧拉邻近图。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_proximity.html"),
            parameters=[
                ParameterSpec(key="values", native_name="values", label="目标目标像元值", type="list", required=False, default=[1]),
                ParameterSpec(key="distunits", native_name="distunits", label="距离单位", type="string", required=False, default="PIXEL", choices=["PIXEL", "GEO"]),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="输入二值或目标特征图")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="欧氏邻近距离图")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 gdal_proximity 脚本")],
            entrypoints=[Entrypoint(kind="cli", target="gdal_proximity.exe", available=True)],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="process_termination",
            limitations=[],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:retile",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="GDAL 规则网格分块瓦片生成 (gdal_retile)",
            native_name="gdal_retile.py",
            category=["tile", "pyramid", "raster"],
            description="将大尺寸栅格影像按设定行列尺寸裁切分割为规则瓦片集，并可选构建多级金字塔结构。",
            engine=_engine_spec(),
            source=_source_spec("programs/gdal_retile.html"),
            parameters=[
                ParameterSpec(key="tileWidth", native_name="tileWidth", label="瓦片宽度 (像素)", type="integer", required=False, default=256),
                ParameterSpec(key="tileHeight", native_name="tileHeight", label="瓦片高度 (像素)", type="integer", required=False, default=256),
                ParameterSpec(key="levels", native_name="levels", label="金字塔级数", type="integer", required=False, default=1),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, multiple=True, description="输入源大图")],
            outputs=[PortSpec(key="output_dir", data_type=DatasetKind.artifact, required=True, description="输出切片文件目录")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 gdal_retile 脚本")],
            entrypoints=[Entrypoint(kind="cli", target="gdal_retile.exe", available=True)],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.cli,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="process_termination",
            limitations=[],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:pansharpen",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台影像融合（Brovey / HSV）",
            native_name="spectra_workbench.fusion_ops（numpy）",
            category=["fusion", "pansharpen", "raster"],
            description="单波段 Pan + 3 波段 MS：Brovey 比值融合或 HSV 强度替换；输出 3 波段 Float32，sidecar 记录公式/波段/量程/适用范围。",
            engine=_engine_spec(),
            source=_source_spec("platform/pansharpen"),
            parameters=[
                ParameterSpec(key="msFile", native_name="msFile", label="多光谱影像文件", type="string", required=True),
                ParameterSpec(key="msBands", native_name="msBands", label="3 个 MS 波段号（R,G,B）", type="list", required=True),
                ParameterSpec(key="fusionMethod", native_name="fusionMethod", label="融合方法（brovey / hsv）", type="string", required=False, default="brovey", choices=list(FUSION_METHODS)),
                ParameterSpec(key="fusionResample", native_name="fusionResample", label="MS 重采样（nearest / bilinear）", type="string", required=False, default="bilinear", choices=list(LOCAL_RESAMPLES)),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="单波段全色影像（north-up 网格）")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="3 波段 Float32 融合成果")],
            requirements=[Requirement(kind="package", name="numpy", reason="Brovey/HSV 逐像元融合")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:pansharpen", available=True, notes="平台适配器已接入（P6-04）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "仅 Brovey 与 HSV；NNDiffuse / Gram-Schmidt / PCA 融合未实现，不提供入口",
                "要求 Pan 单波段、MS 3 波段、两者同 CRS、Pan 为 north-up；MS 会重采样到 Pan 网格",
                "HSV 假设 Float32 输入近似 [0,1] 反射率或 Byte 量程；适用范围写入 sidecar",
                "输出恒为 3 波段 Float32 + NoData；公式与 msBands 映射写入 sidecar",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="ogr:vectortranslate",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="OGR 矢量格式转换与空间 SQL (ogr2ogr)",
            native_name="gdal.VectorTranslate",
            category=["vector", "conversion", "sql"],
            description="在各种矢量格式之间转换、重投影、执行空间过滤与 SQL 属性查询。",
            engine=_engine_spec(),
            source=_source_spec("programs/ogr2ogr.html"),
            parameters=[
                ParameterSpec(key="format", native_name="format", label="目标矢量驱动", type="string", required=False, default="GPKG", choices=["GPKG", "ESRI Shapefile", "GeoJSON", "FlatGeobuf"]),
                ParameterSpec(key="sql", native_name="sql", label="SQL 过滤查询语句", type="sql", required=False),
                ParameterSpec(key="dstSRS", native_name="dstSRS", label="目标空间参考 (CRS)", type="crs", required=False),
                ParameterSpec(key="spat", native_name="spat", label="空间过滤框 minx,miny,maxx,maxy（源 CRS 单位）", type="list", required=False),
                ParameterSpec(key="where", native_name="where", label="属性过滤子句 (WHERE)", type="string", required=False),
                ParameterSpec(key="select", native_name="select", label="保留字段列表", type="list", required=False),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.vector, required=True, description="输入源矢量图层")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.vector, required=True, description="输出目标矢量图层")],
            requirements=[Requirement(kind="package", name="GDAL", reason="需 OGR 矢量引擎与矢量驱动")],
            entrypoints=[
                Entrypoint(kind="form", target="ogr:vectortranslate", available=True, notes="平台适配器已接入（P5-04）"),
                Entrypoint(kind="cli", target="ogr2ogr.exe", available=True, notes="标准 CLI 别名"),
                Entrypoint(kind="script", target="osgeo.gdal.VectorTranslate", available=True),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "矢量成果支持 GPKG / GeoJSON / ESRI Shapefile / FlatGeobuf",
                "dstSRS 重投影要求源图层有空间参考；无 SRS 时明确拒绝（GDAL 3.13.3 实测：此场景 ogr2ogr 只改 SRS 标签不换算坐标）",
                "输出几何坐标轴序为传统 GIS 顺序（EPSG:4326 → X=经度, Y=纬度）；spat 过滤框按源图层 CRS 解释（投影 CRS 单位为米）",
                "SQL 为 OGR SQL 方言（SQLStatement）；where/select 在 SQL 之外叠加时作用于源图层",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:sindex",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台光谱指数（NDVI/NDWI/NBR/SAVI）",
            native_name="spectra_workbench.index_ops（numpy）",
            category=["spectral", "index", "raster"],
            description="按波段元数据（scale/offset）换算物理量后计算光谱指数，无效区（NoData/分母为零/非有限值）进输出 NoData。",
            engine=_engine_spec(),
            source=_source_spec("platform/sindex"),
            parameters=[
                ParameterSpec(key="sindex", native_name="index", label="光谱指数", type="string", required=True, choices=["ndvi", "ndwi", "nbr", "savi"]),
                ParameterSpec(key="bandA", native_name="bandA", label="波段 A（ndvi/nbr/savi=NIR，ndwi=Green）", type="integer", required=True),
                ParameterSpec(key="bandB", native_name="bandB", label="波段 B（ndvi/savi=Red，ndwi=NIR，nbr=SWIR）", type="integer", required=True),
                ParameterSpec(key="saviL", native_name="saviL", label="SAVI 土壤调节因子 L（仅 savi）", type="number", required=False, default=0.5),
                ParameterSpec(key="usePhysical", native_name="usePhysical", label="使用波段元数据 scale/offset 换算物理量", type="boolean", required=False, default=True),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="多波段输入栅格（A/B 波段取自同一数据集）")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="单波段 Float32 指数成果")],
            requirements=[Requirement(kind="package", name="numpy", reason="指数逐像元 numpy 求值（gdal worker 运行时自带）")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:sindex", available=True, notes="平台适配器已接入（P6-01）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "输出恒为单波段 Float32，NoData 默认 -9999；PNG/JPEG 因不支持 NoData 在写盘前拒绝",
                "物理量 = 原值 × scale + offset（仅当波段元数据提供非平凡 scale/offset）；usePhysical=false 时用原始像元值",
                "波段角色随指数：ndvi A=NIR/B=Red，ndwi A=Green/B=NIR，nbr A=NIR/B=SWIR，savi 同 ndvi 加 L（0≤L≤1）",
                "架构边界：方案规划的 science 独立 worker 待 sw-science 环境补 pydantic/栅格 IO 后建立，本期在 gdal worker 内以 numpy 实现",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:fftfilter",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台 FFT 频域滤波与去条纹",
            native_name="spectra_workbench.fft_ops（numpy.fft）",
            category=["fft", "filter", "raster"],
            description="单波段 FFT2 → 厄米对称频域抑制掩膜 → 逆 FFT2；掩膜 sidecar 记录抑制 bin 以复现成果。",
            engine=_engine_spec(),
            source=_source_spec("platform/fftfilter"),
            parameters=[
                ParameterSpec(key="fftBand", native_name="fftBand", label="处理波段（1-based）", type="integer", required=True),
                ParameterSpec(key="suppressBins", native_name="suppressBins", label="抑制频率 bin [fy,fx] 列表（负数按模归一化，自动补对称伙伴；空=恒等往返）", type="list", required=False),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖（缺省读波段标签）", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="单波段输入栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="单波段 Float32 滤波成果")],
            requirements=[Requirement(kind="package", name="numpy", reason="FFT 逐像元 numpy.fft 求值（gdal worker 运行时自带）")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:fftfilter", available=True, notes="平台适配器已接入（P6-02）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "输出恒为单波段 Float32；掩膜 sidecar 为 <成果名>.fft_mask.json（含抑制 bin 与对称伙伴清单，可复现）",
                "源波段 NoData 像元以均值填充参与 FFT、逆变换后回填输出 NoData——大空洞场景物理意义有限",
                "实数输入的掩膜自动补厄米对称伙伴，保证逆变换为实数（虚部幅度超容差即报实现缺陷）",
                "抑制 bin 允许负数（fftfreq 语义），按模归一化到 [0,H)×[0,W)",
                "仅接受实数波段；复数波段明确拒绝（不静默丢弃虚部）",
                "FFT 需整幅读入内存（频域算法不可按窗口分块），超大影像的分块 FFT 归后续轮次",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:pca",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台 PCA 主成分变换与逆变换",
            native_name="spectra_workbench.pca_ops（numpy.linalg.eigh）",
            category=["pca", "transform", "raster"],
            description="多波段栅格 PCA：均值/协方差或相关矩阵/特征向量/主成分顺序/解释方差；正变换写可复现变换 sidecar，逆变换按 sidecar 重建。",
            engine=_engine_spec(),
            source=_source_spec("platform/pca"),
            parameters=[
                ParameterSpec(key="pcaMode", native_name="pcaMode", label="模式（forward / inverse）", type="string", required=False, default="forward", choices=["forward", "inverse"]),
                ParameterSpec(key="pcaComponents", native_name="pcaComponents", label="主成分数（缺省=全部波段）", type="integer", required=False),
                ParameterSpec(key="pcaMatrix", native_name="pcaMatrix", label="矩阵类型（covariance / correlation）", type="string", required=False, default="covariance", choices=["covariance", "correlation"]),
                ParameterSpec(key="pcaTransform", native_name="pcaTransform", label="PCA 变换文件（inverse 必填，正变换写出的 .pca.json）", type="string", required=False),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖（缺省读各波段标签）", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="多波段实数栅格（forward）或主成分栅格（inverse）")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="多波段 Float32 主成分/重建成果")],
            requirements=[Requirement(kind="package", name="numpy", reason="PCA 特征分解 numpy.linalg.eigh（gdal worker 运行时自带）")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:pca", available=True, notes="平台适配器已接入（P6-03）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "PCA 统计量必须覆盖全图；当前整幅读入，分块统计/流式实现归后续轮次（绝不能对每块独立 PCA 冒充全图）",
                "输出恒为 Float32；无效像元输出 NoData；正变换 sidecar 为 <成果名>.pca.json，逆变换 sidecar 为 <成果名>.pca_inverse.json",
                "完整重建误差应为浮点噪声量级；降维误差按 reconstruction_rmse / relative_rmse 与 discarded_variance 报告，定义见 pca_ops 文档",
                "特征向量符号不参与误差判据；为可复现统一约定每个主成分绝对值最大的载荷为正",
                "逆变换必须使用同一 sidecar（含 SHA256）；相关矩阵模式要求各波段方差非零",
                "仅接受实数波段；复数输入明确拒绝",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:tasseledcap",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台缨帽变换（Landsat 5/7/8）",
            native_name="spectra_workbench.tasseledcap_ops（numpy 矩阵乘）",
            category=["tasseledcap", "transform", "raster"],
            description="按显式传感器与 6 个输入波段号执行缨帽亮度/绿度/湿度变换；系数、产品级别与来源写入成果元数据与 sidecar。",
            engine=_engine_spec(),
            source=_source_spec("platform/tasseledcap"),
            parameters=[
                ParameterSpec(key="tcSensor", native_name="tcSensor", label="传感器（系数来源锁定）", type="string", required=True, choices=list(sensor_keys())),
                ParameterSpec(key="tcBands", native_name="tcBands", label="6 个输入波段号，顺序 Blue,Green,Red,NIR,SWIR1,SWIR2", type="list", required=True),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖（缺省读各波段标签）", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="含至少 6 个实数波段的栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="3 波段 Float32：brightness/greenness/wetness")],
            requirements=[Requirement(kind="package", name="numpy", reason="缨帽系数矩阵乘（gdal worker 运行时自带）")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:tasseledcap", available=True, notes="平台适配器已接入（P6-03）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "仅登记 Landsat 5 TM（Crist 1985 reflectance factor）、Landsat 7 ETM+（Huang et al. 2002 at-satellite reflectance）、Landsat 8 OLI（Baig et al. 2014 at-satellite reflectance）",
                "调用方必须显式给出 tcBands 的 6 个波段号（Blue,Green,Red,NIR,SWIR1,SWIR2）；不再猜波段或回退到其它传感器",
                "系数按产品级别使用；输入若不是对应反射率产品，结果只具相对意义——sidecar 如实记录 product_level 与来源",
                "输出恒为 3 波段 Float32 + NoData；sidecar 为 <成果名>.tasseledcap.json，含完整系数矩阵、来源与输入波段",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:gcpregister",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台 GCP 配准（仿射/二次多项式）",
            native_name="spectra_workbench.gcp_ops（numpy 最小二乘 + 自定义采样）",
            category=["gcp", "registration", "raster"],
            description="用控制点 JSON 拟合源像素→参考地图坐标的仿射/二次多项式，独立检查点分列残差，再按参考网格反变换重采样。",
            engine=_engine_spec(),
            source=_source_spec("platform/gcpregister"),
            parameters=[
                ParameterSpec(key="gcpTransform", native_name="gcpTransform", label="变换（affine / polynomial2）", type="string", required=False, default="affine", choices=list(GCP_TRANSFORMS)),
                ParameterSpec(key="gcpFile", native_name="gcpFile", label="GCP JSON 文件", type="string", required=True),
                ParameterSpec(key="referenceFile", native_name="referenceFile", label="参考影像（输出网格）", type="string", required=True),
                ParameterSpec(key="gcpResample", native_name="gcpResample", label="重采样（nearest / bilinear）", type="string", required=False, default="bilinear", choices=list(LOCAL_RESAMPLES)),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="源影像（可为未正确地理参考的原始像素）")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="参考网格上的 Float32 配准成果")],
            requirements=[Requirement(kind="package", name="numpy", reason="GCP 最小二乘与反变换采样")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:gcpregister", available=True, notes="平台适配器已接入（P6-04）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "当前仅 affine（1 阶）与 polynomial2（2 阶）；TPS 未实现，不提供入口",
                "GCP 的 x/y 必须与参考影像同一 CRS；源影像可为 raw（无正确 GT）",
                "控制点参与拟合，check 点只用于独立检查，绝不混入拟合",
                "输出网格严格取参考影像；自定义反变换整幅读入源影像，大影像分块归后续",
                "输出恒为 Float32；越界或邻域含 NoData 的像元输出 NoData",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:rasterdiff",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台栅格差值（difference / absolute）",
            native_name="spectra_workbench.diff_ops（numpy）",
            category=["difference", "statistics", "raster"],
            description="对同网格两景逐波段计算带符号差或绝对差，并输出逐波段 min/max/mean/RMSE 统计 sidecar。",
            engine=_engine_spec(),
            source=_source_spec("platform/rasterdiff"),
            parameters=[
                ParameterSpec(key="diffMode", native_name="diffMode", label="差值模式（difference / absolute）", type="string", required=False, default="difference", choices=list(DIFF_MODES)),
                ParameterSpec(key="referenceFile", native_name="referenceFile", label="参考影像 B", type="string", required=True),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="输入影像 A")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="Float32 差值成果（波段数与输入一致）")],
            requirements=[Requirement(kind="package", name="numpy", reason="逐像元差值/统计")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:rasterdiff", available=True, notes="平台适配器已接入（P6-04）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "两景必须同宽高、同波段数、同 GeoTransform、同 CRS；不一致直接拒绝，绝不隐式重采样/重投影",
                "输出恒为 Float32；NoData/非有限值逐波段排除",
                "统计单位为输入像元值差；sidecar 记录每波段 n_valid/min/max/mean/RMSE/mean_abs",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:mask",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台二值掩膜（波段阈值）",
            native_name="spectra_workbench.mask_ops（numpy 比较）",
            category=["mask", "threshold", "raster"],
            description="按波段闭区间阈值 / 有效像元生成 1/0 二值 Byte 掩膜；不设 NoData，0/1 语义显式，sidecar 记录阈值与计数。",
            engine=_engine_spec(),
            source=_source_spec("platform/mask"),
            parameters=[
                ParameterSpec(key="maskBand", native_name="maskBand", label="输入波段（1-based）", type="integer", required=False, default=1),
                ParameterSpec(key="maskLow", native_name="maskLow", label="掩膜下限（闭区间，可空）", type="number", required=False),
                ParameterSpec(key="maskHigh", native_name="maskHigh", label="掩膜上限（闭区间，可空）", type="number", required=False),
                ParameterSpec(key="maskInvert", native_name="maskInvert", label="取反掩膜", type="boolean", required=False, default=False),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖", type="number", required=False),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="任意实数栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="Byte 二值掩膜：1=真，0=假，未设 NoData")],
            requirements=[Requirement(kind="package", name="numpy", reason="逐像元阈值比较")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:mask", available=True, notes="平台适配器已接入（P6-05）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "输出恒为单波段 Byte：1=掩膜真，0=假，显式不设 NoData（避免 0 语义歧义）",
                "阈值语义为闭区间 low<=value<=high；只给单边时为单边比较，两个都不给时为全体有效像元",
                "sidecar 为 <成果名>.mask.json；可直接作为 gdal:fvc 的 maskFile 使用",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:fvc",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台 FVC 植被覆盖度（分位端元/显式端元 + 分级）",
            native_name="spectra_workbench.fvc_ops（numpy）",
            category=["fvc", "classification", "statistics"],
            description="输入 NDVI：按分位端元（缺省 2%/98%）或显式端元计算 FVC，分级区间 `[0,e1)/[e1,e2)/...` 输出第 2 波段类别号，sidecar 含端元来源/像元面积/分级统计与越界质量标记。",
            engine=_engine_spec(),
            source=_source_spec("platform/fvc"),
            parameters=[
                ParameterSpec(key="fvcBand", native_name="fvcBand", label="NDVI 波段（1-based）", type="integer", required=False, default=1),
                ParameterSpec(key="endmemberMode", native_name="endmemberMode", label="端元模式（percentile / values）", type="string", required=False, default="percentile", choices=list(FVC_ENDMEMBER_MODES)),
                ParameterSpec(key="lowPercentile", native_name="lowPercentile", label="低分位（%）", type="number", required=False, default=2.0),
                ParameterSpec(key="highPercentile", native_name="highPercentile", label="高分位（%）", type="number", required=False, default=98.0),
                ParameterSpec(key="fvcSoil", native_name="fvcSoil", label="土壤端元 NDVI（显式值模式必填）", type="number", required=False),
                ParameterSpec(key="fvcVeg", native_name="fvcVeg", label="植被端元 NDVI（显式值模式必填）", type="number", required=False),
                ParameterSpec(key="fvcClassEdges", native_name="fvcClassEdges", label="分级边界（严格递增，(0,1) 内；缺省 0.25/0.5/0.75）", type="list", required=False),
                ParameterSpec(key="maskFile", native_name="maskFile", label="掩膜文件（单波段【1=有效】，可由 gdal:mask 生成）", type="string", required=False),
                ParameterSpec(key="crop", native_name="crop", label="是否夹到 [0,1]（越界质量标记始终在 sidecar）", type="boolean", required=False, default=True),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="NDVI 栅格（可由 gdal:sindex 生成）")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="单波段 Float32 FVC（分级类别栅格 <成果名>.fvc_class.tif 随事务提交）")],
            requirements=[Requirement(kind="package", name="numpy", reason="分位数端元与分级统计")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:fvc", available=True, notes="平台适配器已接入（P6-05）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "端元模式 percentile / values 二选一；values 需显式 fvcSoil 与 fvcVeg 且 soil<veg",
                "越界像元以 n_below_soil/n_above_veg 质量标记如实报告；crop=true 才夹到 [0,1]",
                "分级区间 [0,e1)/[e1,e2)/…/[ek,1)；类别波段 Byte，NoData=0 表示无效区策略",
                "sidecar 为 <成果名>.fvc.json；记录端元来源、百分位、像元面积、统计与有效像元数",
                "仅接受实数 NDVI 波段；复数输入明确拒绝",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:calibrate",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台辐射定标（gain/offset / TOA 反射率）",
            native_name="spectra_workbench.radiometric_ops（numpy）",
            category=["radiometric", "calibration", "raster"],
            description="按显式 gain/offset 或 TOA 反射率公式（π·L·d²/(ESUN·cos θz)）逐波段定标；sidecar 记录公式/单位/sun 几何，便于独立核算。",
            engine=_engine_spec(),
            source=_source_spec("platform/calibrate"),
            parameters=[
                ParameterSpec(key="calibrateMode", native_name="calibrateMode", label="定标模式（physical / toa_reflectance）", type="string", required=False, default="physical", choices=list(CALIBRATE_MODES)),
                ParameterSpec(key="calibrateGains", native_name="calibrateGains", label="每波段 gain（physical 必填，数量=波段数）", type="list", required=False),
                ParameterSpec(key="calibrateOffsets", native_name="calibrateOffsets", label="每波段 offset（physical 可选，数量=波段数）", type="list", required=False),
                ParameterSpec(key="calibrateEsun", native_name="calibrateEsun", label="每波段 ESUN（toa_reflectance 必填）", type="list", required=False),
                ParameterSpec(key="sunZenAngle", native_name="sunZenAngle", label="太阳天顶角（°，toa_reflectance 必填）", type="number", required=False),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="DN/辐亮度栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="Float32 定标成果（波段数与输入一致）")],
            requirements=[Requirement(kind="package", name="numpy", reason="逐波段线性/TOA 公式")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:calibrate", available=True, notes="平台适配器已接入（P6-06）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "实现的是显式 gain/offset 定标与 TOA 反射率公式，不是 FLAASH；FLAASH 登记 platform:flaash_external（blocked）",
                "calibrateMode=physical 必须给出 calibrateGains；toa_reflectance 必须给出 calibrateEsun 与 sunZenAngle（波段数一致）",
                "热红外亮温（BT，K）不属于本能力输出——BT 与反射率/辐亮度是不同物理量，未实现",
                "单位光谱类型在 sidecar 明确标注（radiance_or_reflectance_factor / toa_reflectance）",
                "sidecar 为 <成果名>.calibrate.json",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:dos1",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台 DOS1 大气校正（Chavez 1988 暗目标减法）",
            native_name="spectra_workbench.dos_ops（numpy）",
            category=["atmospheric", "dos1", "raster"],
            description="Chavez 1988 DOS1 暗目标减法：ρ=π·(L−L_1haze)·d²/(ESUN·cos θz)，用户显式提供 hazeDN/ESUN/太阳天顶角，sidecar 独立核算暗像元项。",
            engine=_engine_spec(),
            source=_source_spec("platform/dos1"),
            parameters=[
                ParameterSpec(key="dosBand", native_name="dosBand", label="处理波段（1-based）", type="integer", required=False, default=1),
                ParameterSpec(key="hazeDN", native_name="hazeDN", label="暗对象 DN（直方图低尾）", type="number", required=True),
                ParameterSpec(key="dosEsun", native_name="dosEsun", label="该波段 ESUN（光谱太阳辐照度）", type="number", required=True),
                ParameterSpec(key="sunZenAngle", native_name="sunZenAngle", label="太阳天顶角（°）", type="number", required=True),
                ParameterSpec(key="dosGain", native_name="dosGain", label="线性 gain", type="number", required=False, default=1.0),
                ParameterSpec(key="dosOffset", native_name="dosOffset", label="线性 offset", type="number", required=False, default=0.0),
                ParameterSpec(key="earthSunDistance", native_name="earthSunDistance", label="日地距离 d（无量纲）", type="number", required=False, default=1.0),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="DN（或已转为辐亮度）单波段栅格")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="单波段 Float32 TOA 反射率")],
            requirements=[Requirement(kind="package", name="numpy", reason="逐像元 DOS1 公式")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:dos1", available=True, notes="平台适配器已接入（P6-06）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "仅 DOS1；更复杂的 DOS2/DOS3、6S 或 FLAASH 未实现且不提供入口",
                "hazeDN 由用户显式提供（来自直方图低尾），不做启发式直方图识别",
                "不替代 FLAASH：FLAASH 为外部引擎适配，登记 platform:flaash_external（blocked）",
                "sidecar 为 <成果名>.dos1.json；引用 Chavez 1988 原文",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:cloudreplace",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="平台云掩膜驱动的多时相替换",
            native_name="spectra_workbench.cloudreplace_ops（numpy）",
            category=["cloud", "multitemporal", "raster"],
            description="基于掩膜的跨时相替换：晴像素取 primary、云像素取 secondary，掩膜可膨胀；sidecar 记录每像元来源（primary/secondary time），绝不误称『恢复原时刻真实地表』。",
            engine=_engine_spec(),
            source=_source_spec("platform/cloudreplace"),
            parameters=[
                ParameterSpec(key="secondaryFile", native_name="secondaryFile", label="二期影像（云下源）", type="string", required=True),
                ParameterSpec(key="cloudMaskFile", native_name="cloudMaskFile", label="云掩膜文件（单波段，1=云）", type="string", required=True),
                ParameterSpec(key="bandList", native_name="bandList", label="参与替换的波段列表（缺省=全部）", type="list", required=False),
                ParameterSpec(key="primaryTime", native_name="primaryTime", label="一期时间戳（sidecar 记录）", type="string", required=False),
                ParameterSpec(key="secondaryTime", native_name="secondaryTime", label="二期时间戳（sidecar 记录）", type="string", required=False),
                ParameterSpec(key="maskDilatePixels", native_name="maskDilatePixels", label="掩膜膨胀像素（0=不膨胀）", type="integer", required=False, default=0),
                ParameterSpec(key="srcNodata", native_name="srcNodata", label="源 NoData 覆盖", type="number", required=False),
                ParameterSpec(key="dstNodata", native_name="dstNodata", label="输出 NoData", type="number", required=False, default=-9999.0),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="一期影像（可能带云）")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="Float32 去云成果；输出波段数 = bandList 长度")],
            requirements=[Requirement(kind="package", name="numpy", reason="掩膜膨胀与跨时相替换")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:cloudreplace", available=True, notes="平台适配器已接入（P6-06）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "只替换现有掩膜，不做云/云影自动识别；maskFile 可来自 gdal:mask 或外部产品",
                "两时相影像必须同尺寸/同波段数/同 GeoTransform/同 CRS",
                "输出的云像素值来自二期，绝不声称『恢复原时刻真实地表』；时相写入 sidecar",
                "sidecar 为 <成果名>.cloudreplace.json；含每像元来源规则",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="gdal:mdim_slice",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="多维数组二维切片（netCDF/HDF → GTiff）",
            native_name="spectra_workbench.mdim_ops（GDAL multidim API）",
            category=["multidimensional", "netcdf", "slice"],
            description="netCDF/HDF 经 GDAL multidim 模型读取：非地理维（time/depth/band 等）全部显式钉死，剩余两维按原序组成 rows/cols 输出二维 GTiff；像元为原值，units/scale/offset/NoData 记入 sidecar 与输出带元数据；无 SpatialRef 的数组不带 CRS（不编造地理参考）。",
            engine=_engine_spec(),
            source=_source_spec("platform/mdim_slice"),
            parameters=[
                ParameterSpec(key="array", native_name="array", label="多维数组名", type="string", required=True),
                ParameterSpec(key="dimSelect", native_name="dimSelect", label="钉死维（dim=index，除两保留维外全部）", type="string", required=True),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="多维容器（netCDF/HDF，经 GDAL multidim）")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="二维切片 GTiff（像元原值；sidecar <成果名>.mdim_slice.json 记录选择/单位/scale/offset/坐标/CRS 状态）")],
            requirements=[Requirement(kind="driver", name="netCDF/HDF5", reason="多维容器读写（GDAL multidim）")],
            entrypoints=[
                Entrypoint(kind="form", target="gdal:mdim_slice", available=True, notes="平台适配器已接入（P8-01）"),
            ],
            state=CapabilityState(
                presence=Presence.present,
                binding=Binding.form,
                validation=Validation.not_run,
                availability=Availability.ready,
            ),
            effects="new_dataset",
            cancellation="cooperative",
            limitations=[
                "像元语义为 raw 原值：物理值 = raw×scale+offset，由调用方决定是否换算（绝不用一个布尔混两件事）",
                "无 SpatialRef 的数组输出不带 CRS/GeoTransform，sidecar 标 georeferenced=false；y/x 坐标数组值如实入 sidecar",
                "复数/布尔/对象数组不支持二维切片（显式拒绝）；rank<3 拒绝",
                "本运行时 HDF5 writer 缺失：容器验收以 netCDF/CF 结构为准，HDF5 文件待驱动补齐",
                "netCDF 创建受 P3-02 定档限制：中文/空格路径不可写且只能 chdir+相对文件名（读取不受限）",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="platform:flaash_external",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="FLAASH 大气校正（外部引擎适配）",
            native_name="外部引擎（不简化公式）",
            category=["atmospheric", "flaash", "external", "blocked"],
            description="FLAASH 只能通过外部引擎适配完成，绝不以简化公式宣称实现；当前未编写适配器，无表单入口。",
            engine=_engine_spec(),
            source=_source_spec("platform/flaash_external"),
            parameters=[],
            inputs=[],
            outputs=[],
            requirements=[Requirement(kind="package", name="外部引擎", reason="FLAASH 由外部引擎适配，平台内不模拟")],
            entrypoints=[
                Entrypoint(kind="form", target="platform:flaash_external", available=False, notes="外部引擎适配待落实，没有 form 入口"),
            ],
            state=CapabilityState(
                presence=Presence.missing,
                binding=Binding.none,
                validation=Validation.not_run,
                availability=Availability.blocked,
            ),
            effects="mixed",
            cancellation="unsupported",
            limitations=[
                "FLAASH 由外部引擎提供，绝不通过简化公式宣称实现",
                "当前仅登记为 blocked，无 adapter 执行器、无表单入口",
                "后续通过 P8 插件协议接入",
            ],
            validation_evidence=[],
        ),
        CapabilityDescriptor(
            capability_id="platform:agriculture_count",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="农业计数（直径/尺度/阈值/重叠 + 人工标注对照）",
            native_name="方法未确定（待实现）",
            category=["agriculture", "detection", "blocked"],
            description="农业计数：保留直径、尺度、阈值、重叠参数，方法与人工标注对照仍需落实；当前状态为待实现。",
            engine=_engine_spec(),
            source=_source_spec("platform/agriculture_count"),
            parameters=[],
            inputs=[],
            outputs=[],
            requirements=[Requirement(kind="package", name="未确定", reason="方法未确定，暂无依赖")],
            entrypoints=[
                Entrypoint(kind="form", target="platform:agriculture_count", available=False, notes="方法未落实，无 form 入口"),
            ],
            state=CapabilityState(
                presence=Presence.missing,
                binding=Binding.none,
                validation=Validation.not_run,
                availability=Availability.blocked,
            ),
            effects="mixed",
            cancellation="unsupported",
            limitations=[
                "方法未确定（需要保留直径/尺度/阈值/重叠 4 项参数与人工标注对照）",
                "当前登记为 blocked，没有 adapter 执行器也没有 form 入口",
                "未与人工标注对照的算法不可验证，保持待实现状态",
            ],
            validation_evidence=[],
        ),
    ]


def build_gdal_cli_programs() -> list[CapabilityDescriptor]:
    """GDAL 传统 CLI 工具清单（包含现存与显式保留的缺失工具）。"""
    programs: list[dict[str, Any]] = [
        {"name": "gdalinfo", "doc": "programs/gdalinfo.html", "desc": "栅格数据集详细信息、驱动、波段、元数据与空间范围诊断查看。"},
        {"name": "ogrinfo", "doc": "programs/ogrinfo.html", "desc": "矢量数据源、图层模式、属性表结构与空间要素信息检查。"},
        {"name": "gdal_translate", "doc": "programs/gdal_translate.html", "desc": "经典栅格格式转换与抽样命令行实用工具。"},
        {"name": "gdalwarp", "doc": "programs/gdalwarp.html", "desc": "经典栅格影像空间重投影与镶嵌命令行工具。"},
        {"name": "gdaldem", "doc": "programs/gdaldem.html", "desc": "经典 DEM 数字高程模型地形分析命令行工具。"},
        {"name": "gdalbuildvrt", "doc": "programs/gdalbuildvrt.html", "desc": "构建虚拟栅格 VRT 的经典命令行工具。"},
        {"name": "gdal_contour", "doc": "programs/gdal_contour.html", "desc": "栅格等值线提取 CLI 工具。"},
        {"name": "gdal_rasterize", "doc": "programs/gdal_rasterize.html", "desc": "矢量图层栅格化烧录 CLI 工具。"},
        {"name": "gdal_grid", "doc": "programs/gdal_grid.html", "desc": "离散点插值生成规则栅格 CLI 工具。"},
        {"name": "gdalmanage", "doc": "programs/gdalmanage.html", "desc": "管理文件系统中的 GDAL 栅格数据集（识别、删除、重命名、拷贝）。"},
        {"name": "gdallocationinfo", "doc": "programs/gdallocationinfo.html", "desc": "查询指定空间坐标或像素坐标处像元实际数值。"},
        {"name": "gdalmdiminfo", "doc": "programs/gdalmdiminfo.html", "desc": "检查多维栅格数组（netCDF / HDF / Zarr）维度、变量与属性。"},
        {"name": "gdalmdimtranslate", "doc": "programs/gdalmdimtranslate.html", "desc": "多维栅格数组切片、变量提取与格式转换。"},
        {"name": "gdalsrsinfo", "doc": "programs/gdalsrsinfo.html", "desc": "空间参考 (SRS/CRS) 解析与多种格式（WKT, PROJ, EPSG）相互转换。"},
        {"name": "gdaltransform", "doc": "programs/gdaltransform.html", "desc": "坐标空间投影点转换与批量测试工具。"},
        {"name": "gdaladdo", "doc": "programs/gdaladdo.html", "desc": "为栅格文件构建或更新多层概览视图 (Overviews / Pyramids)。"},
        {"name": "nearblack", "doc": "programs/nearblack.html", "desc": "黑白压缩伪影边界修复命令行工具。"},
        {"name": "ogr2ogr", "doc": "programs/ogr2ogr.html", "desc": "矢量要素转换与空间 SQL 经典命令行工具。"},
        {"name": "ogrlineref", "doc": "programs/ogrlineref.html", "desc": "矢量线性参考 (Linear Referencing) 计算工具。"},
        {"name": "ogrtindex", "doc": "programs/ogrtindex.html", "desc": "构建矢量瓦片索引图层工具。"},
        {"name": "gdal_calc", "doc": "programs/gdal_calc.html", "desc": "栅格代数计算 Python CLI 脚本。"},
        {"name": "gdal_merge", "doc": "programs/gdal_merge.html", "desc": "栅格物理镶嵌拼图 Python CLI 脚本。"},
        {"name": "gdal_polygonize", "doc": "programs/gdal_polygonize.html", "desc": "栅格多边形矢量化 Python CLI 脚本。"},
        {"name": "gdal_proximity", "doc": "programs/gdal_proximity.html", "desc": "像元欧氏距离分析 Python CLI 脚本。"},
        {"name": "gdal_retile", "doc": "programs/gdal_retile.html", "desc": "网格瓦片金字塔切块 Python CLI 脚本。"},
        {"name": "gdal_sieve", "doc": "programs/gdal_sieve.html", "desc": "分类杂斑滤除 Python CLI 脚本。"},
        {"name": "gdal_fillnodata", "doc": "programs/gdal_fillnodata.html", "desc": "无效值插值填补 Python CLI 脚本。"},
        {"name": "gdal_edit", "doc": "programs/gdal_edit.html", "desc": "在位修改现有数据集的空间参考、仿射变换矩阵或 NoData 标记。"},
        {"name": "gdal2tiles", "doc": "programs/gdal2tiles.html", "desc": "生成 TMS / XYZ 标准网络瓦片集与 HTML 预览页。"},
        {"name": "gdal2xyz", "doc": "programs/gdal2xyz.html", "desc": "将栅格转存为三列 ASCII 坐标与数值文本 (X, Y, Z)。"},
        {"name": "gdalcompare", "doc": "programs/gdalcompare.html", "desc": "对比两幅栅格的像素数值、元数据与空间参数差异。"},
        {"name": "gdalenhance", "doc": "programs/gdalenhance.html", "desc": "简易栅格色彩均衡与对比度拉伸工具。"},
        {
            "name": "rgb2pct",
            "doc": "programs/rgb2pct.html",
            "desc": "将 24 位 RGB 影像降采样为 8 位伪彩色带调色板影像（提供 osgeo_utils 模块与新 CLI 替代）。",
            "module": "osgeo_utils.rgb2pct",
            "alt": "可由 gdal raster rgb-to-palette 替代",
        },
        {
            "name": "pct2rgb",
            "doc": "programs/pct2rgb.html",
            "desc": "将 8 位伪彩色影像扩展转换为 24 位 RGB 影像（提供 osgeo_utils 模块与 gdal_translate -expand 替代）。",
            "module": "osgeo_utils.pct2rgb",
            "alt": "可由 gdal_translate -expand rgb/rgba 替代",
        },
    ]

    result: list[CapabilityDescriptor] = []
    for p in programs:
        p_name = str(p["name"])
        target_cmd = f"python -m {p['module']}" if "module" in p else p_name
        notes = str(p["alt"]) if "alt" in p else None
        limitations: list[str] = []
        if "module" in p:
            if "alt" in p:
                limitations.append(str(p["alt"]))
            limitations.append(f"底层通过 {p['module']} 模块提供")
        result.append(
            CapabilityDescriptor(
                capability_id=f"gdal:cli:{p_name}",
                descriptor_revision="1.0",
                kind=CapabilityKind.cli,
                name=f"GDAL CLI: {p_name}",
                native_name=p_name,
                category=["cli", "gdal", "utility"],
                description=str(p["desc"]),
                engine=_engine_spec(),
                source=_source_spec(str(p["doc"])),
                entrypoints=[Entrypoint(kind="cli", target=target_cmd, available=True, notes=notes)],
                state=CapabilityState(
                    presence=Presence.present,
                    binding=Binding.cli,
                    validation=Validation.not_run,
                    availability=Availability.ready,
                ),
                effects="read_only" if "info" in p_name or "compare" in p_name else "new_dataset",
                cancellation="process_termination",
                limitations=limitations,
                validation_evidence=[],
            )
        )

    # 显式保留缺失能力（验收标准：不存在的命令保持 missing 状态，不静默删除）
    missing_programs = [
        {
            "name": "gdal_auth",
            "doc": "programs/gdal.html",
            "desc": "GDAL 身份凭据与 OAuth2 授权管理命令行工具。",
            "reason": "Windows QGIS 运行时未编译该独立命令；凭据在当前版本需通过环境变量或配置文件提供",
        },
    ]

    for mp in missing_programs:
        mp_name = str(mp["name"])
        result.append(
            CapabilityDescriptor(
                capability_id=f"gdal:cli:{mp_name}",
                descriptor_revision="1.0",
                kind=CapabilityKind.cli,
                name=f"GDAL CLI (缺失): {mp_name}",
                native_name=mp_name,
                category=["cli", "gdal", "missing"],
                description=str(mp["desc"]),
                engine=_engine_spec(),
                source=_source_spec(str(mp["doc"])),
                entrypoints=[Entrypoint(kind="cli", target=mp_name, available=False, notes=str(mp["reason"]))],
                state=CapabilityState(
                    presence=Presence.missing,
                    binding=Binding.none,
                    validation=Validation.not_run,
                    availability=Availability.unsupported,
                ),
                effects="mixed",
                cancellation="unsupported",
                limitations=[str(mp["reason"])],
                validation_evidence=[],
            )
        )

    return result


def build_gdal_drivers() -> list[CapabilityDescriptor]:
    """GDAL 全量驱动清单（包含本机探测的 205 个已注册驱动及缺失驱动 HDF5）。"""
    drivers_json = _load_resource_json("gdal_runtime_drivers.json")
    result: list[CapabilityDescriptor] = []

    if drivers_json and "drivers" in drivers_json:
        for d in drivers_json["drivers"]:
            s_name = str(d["short_name"])
            l_name = str(d["long_name"])
            is_raster = bool(d.get("is_raster", True))
            is_vector = bool(d.get("is_vector", False))
            can_create = bool(d.get("can_create", False))
            can_copy = bool(d.get("can_createcopy", False))

            cat = ["driver", "gdal"]
            if is_raster:
                cat.append("raster")
            if is_vector:
                cat.append("vector")

            is_smoke = s_name in ("GTiff", "ENVI")

            result.append(
                CapabilityDescriptor(
                    capability_id=f"gdal:driver:{s_name}",
                    descriptor_revision="1.0",
                    kind=CapabilityKind.driver,
                    name=f"GDAL Driver: {s_name}",
                    native_name=s_name,
                    category=cat,
                    description=f"GDAL 格式驱动: {l_name} ({'栅格' if is_raster else ''} {'矢量' if is_vector else ''})".strip(),
                    engine=_engine_spec(),
                    source=_source_spec(f"drivers/{'raster' if is_raster else 'vector'}/{s_name.lower()}.html"),
                    entrypoints=[Entrypoint(kind="documentation", target=f"driver:{s_name}", available=True)],
                    state=CapabilityState(
                        presence=Presence.present,
                        binding=Binding.indexed,
                        validation=Validation.smoke_passed if is_smoke else Validation.not_run,
                        availability=Availability.ready,
                    ),
                    effects="read_only" if not (can_create or can_copy) else "new_dataset",
                    cancellation="unsupported",
                    limitations=[],
                    validation_evidence=[
                        ValidationEvidence(
                            case_id=f"smoke_driver_{s_name.lower()}",
                            status="passed",
                            runtime_id="gdal-3.13.3",
                            report_uri="docs/progress/runs/env_report.json",
                        )
                    ] if is_smoke else [],
                )
            )

    # 显式保留缺失的 HDF5 驱动（契约要求：缺失驱动保留为 missing / missing_dependency）
    result.append(
        CapabilityDescriptor(
            capability_id="gdal:driver:HDF5",
            descriptor_revision="1.0",
            kind=CapabilityKind.driver,
            name="GDAL Driver (缺失): HDF5",
            native_name="HDF5",
            category=["driver", "gdal", "raster", "missing"],
            description="GDAL 格式驱动: Hierarchical Data Format Release 5 (栅格)",
            engine=_engine_spec(),
            source=_source_spec("drivers/raster/hdf5.html"),
            entrypoints=[Entrypoint(kind="documentation", target="driver:HDF5", available=False, notes="当前 Windows QGIS GDAL 运行时未链接 HDF5 动态库")],
            state=CapabilityState(
                presence=Presence.missing,
                binding=Binding.none,
                validation=Validation.not_run,
                availability=Availability.missing_dependency,
            ),
            effects="read_only",
            cancellation="unsupported",
            limitations=["当前 Windows QGIS GDAL 运行时未链接 HDF5 动态库；部分 HDF5 文件可通过 netCDF 驱动尝试读取"],
            validation_evidence=[],
        )
    )

    return result


def build_gdal_structured_cli_commands() -> list[CapabilityDescriptor]:
    """递归采集官方统一 CLI (gdal --json-usage) 119 个叶子命令路径与参数。"""
    usage_json = _load_resource_json("gdal_json_usage.json")
    if not usage_json or "leaves" not in usage_json:
        return []

    result: list[CapabilityDescriptor] = []
    for leaf in usage_json["leaves"]:
        full_path = leaf.get("full_path", [])
        cmd = leaf.get("cmd", "gdal")
        desc = leaf.get("description", "")
        url = leaf.get("url", f"{GDAL_DOC_BASE}programs/index.html")

        # 构造规则 capability_id，如 gdal:leaf:raster:convert
        cid_suffix = ":".join(p.replace("-", "_") for p in full_path[1:])
        capability_id = f"gdal:leaf:{cid_suffix}"

        group = full_path[1] if len(full_path) > 1 else "general"

        params: list[ParameterSpec] = []
        for arg in leaf.get("input_arguments", []):
            arg_name = arg.get("name", "")
            if not arg_name:
                continue
            raw_type = arg.get("type", "string")
            type_mapping = {
                "boolean": "boolean",
                "integer": "integer",
                "float": "number",
                "string": "string",
                "string_list": "list",
                "dataset": "dataset",
                "geometry": "string",
            }
            spec_type = type_mapping.get(raw_type, "string")
            params.append(
                ParameterSpec(
                    key=arg_name.replace("-", "_"),
                    native_name=arg_name,
                    label=arg_name,
                    type=spec_type,
                    required=bool(arg.get("required", False)),
                    help=arg.get("description", ""),
                )
            )

        result.append(
            CapabilityDescriptor(
                capability_id=capability_id,
                descriptor_revision="1.0",
                kind=CapabilityKind.cli,
                name=f"GDAL 统一 CLI: {cmd}",
                native_name=cmd,
                category=["cli", "gdal", group],
                description=desc or f"GDAL 统一命令行工具: {cmd}",
                engine=_engine_spec(),
                source=CapabilitySource(
                    documentation_url=url,
                    baseline_id=BASELINE_ID,
                    upstream_version=GDAL_VERSION,
                    snapshot_sha256=None,
                    verified_date="2026-11-04",
                ),
                parameters=params,
                entrypoints=[Entrypoint(kind="cli", target=cmd, available=True)],
                state=CapabilityState(
                    presence=Presence.present,
                    binding=Binding.cli,
                    validation=Validation.not_run,
                    availability=Availability.ready,
                ),
                effects="read_only" if any(w in cmd for w in ("info", "check", "summary")) else "new_dataset",
                cancellation="process_termination",
                limitations=[],
                validation_evidence=[],
            )
        )

    return result


def build_gdal_python_api_index() -> list[CapabilityDescriptor]:
    """GDAL/OGR/OSR 公共 Python API 对象与函数索引（P2-03 GDAL 部分）。

    公共分类严格筛选，排除私有 `_` 符号；
    针对 SWIG C 扩展绑定无法反射签名的情况附注明确说明。
    """
    api_items: list[dict[str, Any]] = [
        {"id": "gdal:api:Dataset", "kind": CapabilityKind.class_, "name": "osgeo.gdal.Dataset", "category": ["api", "gdal", "class"], "desc": "栅格数据集核心公开类，提供栅格波段、仿射矩阵、投影、RPC、GCP 与元数据读写。"},
        {"id": "gdal:api:Band", "kind": CapabilityKind.class_, "name": "osgeo.gdal.Band", "category": ["api", "gdal", "class"], "desc": "单波段数据核心类，提供像元数据分块读写 (ReadAsArray)、NoData、统计、色彩表与概览访问。"},
        {"id": "gdal:api:Driver", "kind": CapabilityKind.class_, "name": "osgeo.gdal.Driver", "category": ["api", "gdal", "class"], "desc": "格式驱动句柄类，用于创建、删除、复制数据集与查询格式能力。"},
        {"id": "gdal:api:ColorTable", "kind": CapabilityKind.class_, "name": "osgeo.gdal.ColorTable", "category": ["api", "gdal", "class"], "desc": "伪彩色调色板类，管理波段色彩映射表 (Color Entries)。"},
        {"id": "gdal:api:RasterAttributeTable", "kind": CapabilityKind.class_, "name": "osgeo.gdal.RasterAttributeTable", "category": ["api", "gdal", "class"], "desc": "栅格属性表 (RAT)，管理像元分类的伴生属性。"},
        {"id": "gdal:api:Open", "kind": CapabilityKind.function, "name": "osgeo.gdal.Open", "category": ["api", "gdal", "function"], "desc": "打开只读或可写栅格数据集。", "swig_note": "SWIG C 扩展函数，inspect.signature 返回 (*args)；文档签名: Open(path, eAccess=GA_ReadOnly) -> Dataset"},
        {"id": "gdal:api:OpenEx", "kind": CapabilityKind.function, "name": "osgeo.gdal.OpenEx", "category": ["api", "gdal", "function"], "desc": "高级打开接口，支持限定驱动、只读/更新标志与打开选项 open_options。", "swig_note": "SWIG C 扩展函数；文档签名: OpenEx(utf8_path, nOpenFlags=0, allowed_drivers=None, open_options=None, sibling_files=None) -> Dataset"},
        {"id": "gdal:api:Translate", "kind": CapabilityKind.function, "name": "osgeo.gdal.Translate", "category": ["api", "gdal", "function"], "desc": "Python 包装版栅格格式转换接口。", "swig_note": "签名: Translate(destName, srcDS, **kwargs) -> Dataset"},
        {"id": "gdal:api:Warp", "kind": CapabilityKind.function, "name": "osgeo.gdal.Warp", "category": ["api", "gdal", "function"], "desc": "Python 包装版栅格投影重采样接口。", "swig_note": "签名: Warp(destNameOrDestDS, srcDSOrSrcDSTab, **kwargs) -> Dataset"},
        {"id": "gdal:api:Dem", "kind": CapabilityKind.function, "name": "osgeo.gdal.Dem", "category": ["api", "gdal", "function"], "desc": "Python 包装版 DEM 地形分析接口。", "swig_note": "签名: Dem(destName, srcDS, processing, **kwargs) -> Dataset"},
        {"id": "gdal:api:Grid", "kind": CapabilityKind.function, "name": "osgeo.gdal.Grid", "category": ["api", "gdal", "function"], "desc": "Python 包装版离散点插值网格化接口。", "swig_note": "签名: Grid(destName, srcDS, **kwargs) -> Dataset"},
        {"id": "gdal:api:BuildVRT", "kind": CapabilityKind.function, "name": "osgeo.gdal.BuildVRT", "category": ["api", "gdal", "function"], "desc": "Python 包装版虚拟栅格 VRT 生成接口。", "swig_note": "签名: BuildVRT(destName, srcDS, **kwargs) -> Dataset"},
        {"id": "gdal:api:Rasterize", "kind": CapabilityKind.function, "name": "osgeo.gdal.Rasterize", "category": ["api", "gdal", "function"], "desc": "Python 包装版矢量要素栅格化接口。", "swig_note": "签名: Rasterize(destNameOrDestDS, srcDS, **kwargs) -> Dataset"},
        {"id": "gdal:api:ContourGenerate", "kind": CapabilityKind.function, "name": "osgeo.gdal.ContourGenerate", "category": ["api", "gdal", "function"], "desc": "从单波段生成矢量等值线折线。", "swig_note": "SWIG 函数；签名: ContourGenerate(src_band, dfContourInterval, dfContourBase, fixedLevels, useNoData, noDataValue, dst_layer, idField, elevField, callback=None)"},
        {"id": "gdal:api:Polygonize", "kind": CapabilityKind.function, "name": "osgeo.gdal.Polygonize", "category": ["api", "gdal", "function"], "desc": "单波段离散栅格矢量化为多边形图层。", "swig_note": "SWIG 函数；签名: Polygonize(src_band, mask_band, out_layer, pixValField, papszOptions=[], callback=None)"},
        {"id": "gdal:api:InvGeoTransform", "kind": CapabilityKind.function, "name": "osgeo.gdal.InvGeoTransform", "category": ["api", "gdal", "function"], "desc": "计算 6 参数仿射变换矩阵的逆矩阵。", "swig_note": "SWIG 函数；签名: InvGeoTransform(gt_in) -> (success, gt_out)"},
        {"id": "gdal:api:ApplyGeoTransform", "kind": CapabilityKind.function, "name": "osgeo.gdal.ApplyGeoTransform", "category": ["api", "gdal", "function"], "desc": "应用仿射变换矩阵将像素坐标 (pixel, line) 计算为地理空间坐标 (x, y)。", "swig_note": "SWIG 函数；签名: ApplyGeoTransform(gt, x, y) -> (geo_x, geo_y)"},
        {"id": "ogr:api:DataSource", "kind": CapabilityKind.class_, "name": "osgeo.ogr.DataSource", "category": ["api", "ogr", "class"], "desc": "OGR 矢量数据源核心类，提供矢量图层枚举、创建、执行 SQL 查询。"},
        {"id": "ogr:api:Layer", "kind": CapabilityKind.class_, "name": "osgeo.ogr.Layer", "category": ["api", "ogr", "class"], "desc": "OGR 矢量图层类，提供空间要素迭代、空间过滤器 (SetSpatialFilter) 与属性过滤器。"},
        {"id": "ogr:api:Feature", "kind": CapabilityKind.class_, "name": "osgeo.ogr.Feature", "category": ["api", "ogr", "class"], "desc": "单条矢量要素类，包含几何图形与字段属性字典。"},
        {"id": "ogr:api:Geometry", "kind": CapabilityKind.class_, "name": "osgeo.ogr.Geometry", "category": ["api", "ogr", "class"], "desc": "OGC 标准几何体类（点、折线、多边形、多部件）。"},
        {"id": "ogr:api:Open", "kind": CapabilityKind.function, "name": "osgeo.ogr.Open", "category": ["api", "ogr", "function"], "desc": "打开已有 OGR 矢量数据源。", "swig_note": "SWIG 函数；签名: Open(utf8_path, update=0) -> DataSource"},
        {"id": "osr:api:SpatialReference", "kind": CapabilityKind.class_, "name": "osgeo.osr.SpatialReference", "category": ["api", "osr", "class"], "desc": "OGRSpatialReference 坐标参考系核心类，支持 EPSG、WKT、PROJ 导入导出与轴顺序管理。"},
        {"id": "osr:api:CoordinateTransformation", "kind": CapabilityKind.class_, "name": "osgeo.osr.CoordinateTransformation", "category": ["api", "osr", "class"], "desc": "OGRCoordinateTransformation 坐标变换核心类，执行两套空间参考间的精确点投影转换。"},
    ]

    result: list[CapabilityDescriptor] = []
    for item in api_items:
        notes = []
        if "swig_note" in item:
            notes.append(str(item["swig_note"]))

        result.append(
            CapabilityDescriptor(
                capability_id=str(item["id"]),
                descriptor_revision="1.0",
                kind=item["kind"],
                name=str(item["name"]),
                native_name=str(item["name"]),
                category=list(item["category"]),
                description=str(item["desc"]),
                engine=_engine_spec(),
                source=_source_spec("api/python/index.html"),
                entrypoints=[Entrypoint(kind="script", target=str(item["name"]), available=True, notes=notes[0] if notes else None)],
                state=CapabilityState(
                    presence=Presence.present,
                    binding=Binding.script,
                    validation=Validation.not_run,
                    availability=Availability.ready,
                ),
                effects="read_only" if "class" in item["category"] or "info" in item["id"] or "Transform" in item["id"] else "mixed",
                cancellation="unsupported",
                limitations=notes,
                validation_evidence=[],
            )
        )
    return result


def build_gdal_baseline() -> list[CapabilityDescriptor]:
    """汇总构建完整的 GDAL 3.13.3 公开能力基准条目清单。"""
    all_items: list[CapabilityDescriptor] = []
    all_items.extend(build_gdal_algorithms())
    all_items.extend(build_gdal_cli_programs())
    all_items.extend(build_gdal_drivers())
    all_items.extend(build_gdal_structured_cli_commands())
    all_items.extend(build_gdal_python_api_index())
    return all_items
