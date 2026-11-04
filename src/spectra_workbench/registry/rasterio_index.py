"""Rasterio 1.5.2 Python API 与 rio CLI 编目（P2-03）。

契约保证：
1. 目标版本固定为 Rasterio 1.5.2（基于 GDAL 3.12.2 底层轮子）。
2. 公开分类覆盖：读写 I/O、重投影与重采样 (warp)、矢量掩膜 (mask)、特征与几何 (features)、窗口 (windows) 与内存文件。
3. 严禁私有符号盲扫：仅编目公开模块公开符号，排除以 `_` 开头的私有实现。
4. 全量收录官方全部 21 项 rio 核心子命令（rio info, rio convert, rio warp, rio merge, rio clip, rio stack, rio calc 等）。
5. 缺失或可选插件命令（如 rio-mbtiles, rio-color, rio-cogeo）明确保留为 missing / missing_dependency 状态，不静默删除。
6. 纯静态元数据模型，导入时不触发 native rasterio 动态库，安全服务于 UI 进程。
"""

from __future__ import annotations

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

RASTERIO_VERSION = "1.5.2"
RASTERIO_DOC_BASE = "https://rasterio.readthedocs.io/en/1.5.2/"
BASELINE_ID = "rasterio-1.5.2"

RASTERIO_ALIASES: dict[str, str] = {
    "rio_info": "rio:cli:info",
    "rio_bounds": "rio:cli:bounds",
    "rio_convert": "rio:cli:convert",
    "rio:convert": "rio:cli:convert",
    "rasterio:convert": "rio:cli:convert",
    "rio_warp": "rio:cli:warp",
    "rio_merge": "rio:cli:merge",
    "rio_mask": "rio:cli:mask",
    "rio_clip": "rio:cli:clip",
    "rio_stack": "rio:cli:stack",
    "rio_calc": "rio:cli:calc",
    "rio_shapes": "rio:cli:shapes",
    "rio_rasterize": "rio:cli:rasterize",
    "rio_overview": "rio:cli:overview",
    "rio_sample": "rio:cli:sample",
    "rio_blocks": "rio:cli:blocks",
    "rio_create": "rio:cli:create",
    "rio_edit_info": "rio:cli:edit-info",
}


def _engine_spec() -> CapabilityEngineSpec:
    return CapabilityEngineSpec(
        provider_id="rasterio",
        version_constraint=">=1.5,<2",
        runtime_id="sw-rasterio",
        resolved_version=RASTERIO_VERSION,
    )


def _source_spec(doc_path: str) -> CapabilitySource:
    url = f"{RASTERIO_DOC_BASE}{doc_path.lstrip('/')}"
    return CapabilitySource(
        documentation_url=url,
        baseline_id=BASELINE_ID,
        upstream_version=RASTERIO_VERSION,
        snapshot_sha256=None,  # 由基准组装器统一计算回填
        verified_date="2026-11-04",
    )


def build_rasterio_algorithms() -> list[CapabilityDescriptor]:
    """Rasterio 平台算法清单（包含已支持的格式转换与分块拷贝）。"""
    return [
        CapabilityDescriptor(
            capability_id="rasterio:convert",
            descriptor_revision="1.0",
            kind=CapabilityKind.algorithm,
            name="Rasterio 栅格格式转换与分块拷贝",
            native_name="rasterio.open",
            category=["conversion", "raster", "rasterio"],
            description="在不同栅格格式间按分块窗口转换与拷贝影像，支持设置创建选项与分块读取，防止大内存占用。",
            engine=_engine_spec(),
            source=_source_spec("topics/reading.html"),
            parameters=[
                ParameterSpec(
                    key="driver",
                    native_name="driver",
                    label="目标栅格驱动格式",
                    type="string",
                    required=False,
                    default="GTiff",
                    choices=["GTiff", "PNG", "JPEG"],
                    help="目标栅格驱动名称",
                ),
            ],
            inputs=[PortSpec(key="input", data_type=DatasetKind.raster, required=True, description="输入源影像")],
            outputs=[PortSpec(key="output", data_type=DatasetKind.raster, required=True, description="输出目标影像")],
            requirements=[Requirement(kind="package", name="rasterio", reason="需 rasterio 核心库与底层 GDAL 动态库")],
            entrypoints=[
                Entrypoint(kind="form", target="rasterio:convert", available=True, notes="平台分块转换执行器"),
                Entrypoint(kind="cli", target="rio convert", available=True, notes="标准 rio CLI 别名"),
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
                    case_id="smoke_rasterio_convert",
                    status="passed",
                    runtime_id="sw-rasterio",
                    report_uri="docs/progress/runs/env_report.json",
                )
            ],
        )
    ]


def build_rio_cli_commands() -> list[CapabilityDescriptor]:
    """收录官方 21 项 rio 核心子命令与缺失插件。"""
    rio_commands: list[dict[str, Any]] = [
        {"name": "info", "desc": "以结构化 JSON 或纯文本输出栅格数据集元数据、尺寸、驱动与波段摘要。"},
        {"name": "bounds", "desc": "计算并输出栅格文件的地理空间外包框 (Bounding Box) 坐标或 GeoJSON 多边形。"},
        {"name": "convert", "desc": "在不同栅格格式间转换、改变数据类型或应用缩放因数。"},
        {"name": "warp", "desc": "利用 GDAL warp 核心在不同坐标系间重投影栅格影像。"},
        {"name": "merge", "desc": "将多幅空间重叠或相邻的栅格影像拼合/镶嵌为单幅马赛克。"},
        {"name": "mask", "desc": "利用 GeoJSON 矢量多边形对栅格进行空间裁剪掩膜。"},
        {"name": "clip", "desc": "按给定的地理经纬度或投影外包矩形快速裁切栅格窗口。"},
        {"name": "stack", "desc": "将多个单波段或多波段栅格影像堆叠为多波段合成成果。"},
        {"name": "calc", "desc": "通过数学表达式对栅格像元数组进行按行运算（例如计算 NDVI）。"},
        {"name": "shapes", "desc": "提取栅格同值连通域并输出为 GeoJSON 多边形要素集。"},
        {"name": "rasterize", "desc": "将 GeoJSON 矢量要素烧录成栅格数据。"},
        {"name": "blocks", "desc": "以 GeoJSON 多边形流形式输出栅格文件的底层物理瓦片块结构布局。"},
        {"name": "create", "desc": "依据指定尺寸、CRS 与变换矩阵创建空白栅格模板。"},
        {"name": "edit-info", "desc": "就地编辑现有栅格文件的元数据、NODATA 值、标签或坐标变换矩阵。"},
        {"name": "env", "desc": "打印当前进程的 GDAL/Rasterio 环境变量与底层配置选项。"},
        {"name": "gcps", "desc": "读取或写入栅格关联的地面控制点 (Ground Control Points)。"},
        {"name": "insp", "desc": "打开交互式 Python 解释器终端并预载入目标数据集对象。"},
        {"name": "overview", "desc": "为已有栅格文件建立或重建内部减采样金字塔概览。"},
        {"name": "rm", "desc": "删除指定栅格数据集及其相关联的辅助文件。"},
        {"name": "sample", "desc": "根据传入的空间坐标点序列抽样提取各波段像元值。"},
        {"name": "transform", "desc": "在像元行/列坐标与地理坐标系坐标之间相互换算。"},
    ]

    result: list[CapabilityDescriptor] = []
    for cmd in rio_commands:
        cmd_name = str(cmd["name"])
        result.append(
            CapabilityDescriptor(
                capability_id=f"rio:cli:{cmd_name}",
                descriptor_revision="1.0",
                kind=CapabilityKind.cli,
                name=f"rio {cmd_name}",
                native_name=f"rio {cmd_name}",
                category=["cli", "rasterio", "rio"],
                description=str(cmd["desc"]),
                engine=_engine_spec(),
                source=_source_spec("cli.html"),
                entrypoints=[Entrypoint(kind="cli", target=f"rio {cmd_name}", available=True)],
                state=CapabilityState(
                    presence=Presence.present,
                    binding=Binding.cli,
                    validation=Validation.not_run,
                    availability=Availability.ready,
                ),
                effects="read_only" if cmd_name in ("info", "bounds", "insp", "env", "sample", "blocks") else "new_dataset",
                cancellation="process_termination",
                limitations=[],
                validation_evidence=[],
            )
        )

    # 显式保留缺失能力（可选插件）
    missing_plugins = [
        {
            "name": "mbtiles",
            "desc": "将栅格导出为 MBTiles 切片包（需独立安装 rio-mbtiles 插件）。",
            "reason": "sw-rasterio 环境未安装可选插件 rio-mbtiles",
        },
        {
            "name": "color",
            "desc": "色彩饱和度、曲线与色调调整插件（需独立安装 rio-color）。",
            "reason": "sw-rasterio 环境未安装可选插件 rio-color",
        },
        {
            "name": "cogeo",
            "desc": "生成 Cloud-Optimized GeoTIFF 工具（需独立安装 rio-cogeo）。",
            "reason": "sw-rasterio 环境未安装可选插件 rio-cogeo；可通过 GDAL COG 驱动直接输出",
        },
    ]

    for mp in missing_plugins:
        mp_name = str(mp["name"])
        result.append(
            CapabilityDescriptor(
                capability_id=f"rio:cli:{mp_name}",
                descriptor_revision="1.0",
                kind=CapabilityKind.cli,
                name=f"rio {mp_name} (缺失插件)",
                native_name=f"rio {mp_name}",
                category=["cli", "rasterio", "missing_plugin"],
                description=str(mp["desc"]),
                engine=_engine_spec(),
                source=_source_spec("cli.html"),
                entrypoints=[Entrypoint(kind="cli", target=f"rio {mp_name}", available=False, notes=str(mp["reason"]))],
                state=CapabilityState(
                    presence=Presence.missing,
                    binding=Binding.none,
                    validation=Validation.not_run,
                    availability=Availability.missing_dependency,
                ),
                effects="mixed",
                cancellation="unsupported",
                limitations=[str(mp["reason"])],
                validation_evidence=[],
            )
        )

    return result


def build_rasterio_python_api() -> list[CapabilityDescriptor]:
    """Rasterio 1.5.2 公开 Python 核心 API 索引（严格分类，排除私有符号）。"""
    api_entries: list[dict[str, Any]] = [
        {
            "id": "rasterio:api:open",
            "kind": CapabilityKind.function,
            "name": "rasterio.open",
            "category": ["api", "rasterio", "io"],
            "desc": "打开栅格数据集句柄（支持只读 'r'、新建 'w' 或内存模式），返回 DatasetReader 或 DatasetWriter。",
            "doc": "api/rasterio.html#rasterio.open",
            "sig": "(fp, mode='r', driver=None, width=None, height=None, count=None, crs=None, transform=None, dtype=None, nodata=None, sharing=False, **kwargs)",
        },
        {
            "id": "rasterio:api:pad",
            "kind": CapabilityKind.function,
            "name": "rasterio.pad",
            "category": ["api", "rasterio", "array"],
            "desc": "沿栅格数组边缘扩展填充指定像元或镜像边缘像元。",
            "doc": "api/rasterio.html#rasterio.pad",
            "sig": "(array, transform, pad_width, mode='edge', **kwargs)",
        },
        {
            "id": "rasterio:api:band",
            "kind": CapabilityKind.function,
            "name": "rasterio.band",
            "category": ["api", "rasterio", "io"],
            "desc": "从数据集句柄获取指定波段索引的轻量级波段代理引用。",
            "doc": "api/rasterio.html#rasterio.band",
            "sig": "(ds, bidx)",
        },
        {
            "id": "rasterio:api:merge",
            "kind": CapabilityKind.function,
            "name": "rasterio.merge.merge",
            "category": ["api", "rasterio", "mosaic"],
            "desc": "将多个 DatasetReader 句柄像素物理镶嵌融合成单个多波段 NumPy 数组与变换矩阵。",
            "doc": "api/rasterio.merge.html#rasterio.merge.merge",
            "sig": "(sources, bounds=None, res=None, nodata=None, precision=None, indexes=None, method='first', target_aligned_pixels=False, dst_path=None, dst_kwds=None)",
        },
        {
            "id": "rasterio:api:mask",
            "kind": CapabilityKind.function,
            "name": "rasterio.mask.mask",
            "category": ["api", "rasterio", "mask"],
            "desc": "利用 GeoJSON 几何多边形对打开的栅格数据集提取掩膜切片与更新后的仿射变换。",
            "doc": "api/rasterio.mask.html#rasterio.mask.mask",
            "sig": "(dataset, shapes, all_touched=False, invert=False, nodata=None, filled=True, crop=False, pad=False, pad_width=0.5, indexes=None)",
        },
        {
            "id": "rasterio:api:reproject",
            "kind": CapabilityKind.function,
            "name": "rasterio.warp.reproject",
            "category": ["api", "rasterio", "warp"],
            "desc": "底层重投影引擎，支持在不同 CRS 栅格数组或内存块之间执行重投影与重采样运算。",
            "doc": "api/rasterio.warp.html#rasterio.warp.reproject",
            "sig": "(source, destination=None, src_transform=None, gcps=None, src_crs=None, src_nodata=None, dst_transform=None, dst_crs=None, dst_nodata=None, dst_resolution=None, resampling=Resampling.nearest, init_dest_nodata=True, warp_mem_limit=0, **kwargs)",
        },
        {
            "id": "rasterio:api:calculate_default_transform",
            "kind": CapabilityKind.function,
            "name": "rasterio.warp.calculate_default_transform",
            "category": ["api", "rasterio", "warp"],
            "desc": "根据源坐标系、目标坐标系与尺寸，预估重投影后的最佳输出网格尺寸与仿射变换矩阵。",
            "doc": "api/rasterio.warp.html#rasterio.warp.calculate_default_transform",
            "sig": "(src_crs, dst_crs, width, height, left=None, bottom=None, right=None, top=None, gcps=None, rpcs=None, resolution=None, dst_width=None, dst_height=None)",
        },
        {
            "id": "rasterio:api:transform_bounds",
            "kind": CapabilityKind.function,
            "name": "rasterio.warp.transform_bounds",
            "category": ["api", "rasterio", "warp"],
            "desc": "在两套不同 CRS 之间精确换算外包矩形经纬度或投影坐标范围。",
            "doc": "api/rasterio.warp.html#rasterio.warp.transform_bounds",
            "sig": "(src_crs, dst_crs, left, bottom, right, top, densify_pts=21)",
        },
        {
            "id": "rasterio:api:rasterize",
            "kind": CapabilityKind.function,
            "name": "rasterio.features.rasterize",
            "category": ["api", "rasterio", "features"],
            "desc": "将几何形状迭代器烧录成指定尺寸的 NumPy 栅格布尔或数值数组。",
            "doc": "api/rasterio.features.html#rasterio.features.rasterize",
            "sig": "(shapes, out_shape=None, fill=0, out=None, transform=IDENTITY, all_touched=False, merge_alg=MergeAlg.replace, default_value=1, dtype=None)",
        },
        {
            "id": "rasterio:api:shapes",
            "kind": CapabilityKind.function,
            "name": "rasterio.features.shapes",
            "category": ["api", "rasterio", "features"],
            "desc": "高效提取栅格图像中的同值连通域，以 (geojson_geometry, value) 生成器迭代返回。",
            "doc": "api/rasterio.features.html#rasterio.features.shapes",
            "sig": "(image, mask=None, connectivity=4, transform=IDENTITY)",
        },
        {
            "id": "rasterio:api:geometry_mask",
            "kind": CapabilityKind.function,
            "name": "rasterio.features.geometry_mask",
            "category": ["api", "rasterio", "features"],
            "desc": "根据几何形状生成布尔掩膜数组（目标内部或外部为 True）。",
            "doc": "api/rasterio.features.html#rasterio.features.geometry_mask",
            "sig": "(geometries, out_shape, transform, all_touched=False, invert=False)",
        },
        {
            "id": "rasterio:api:sieve",
            "kind": CapabilityKind.function,
            "name": "rasterio.features.sieve",
            "category": ["api", "rasterio", "features"],
            "desc": "快速去除单波段二值或分类数组中小于指定像素阈值的孤立噪声小斑点。",
            "doc": "api/rasterio.features.html#rasterio.features.sieve",
            "sig": "(image, size, out=None, mask=None, connectivity=4)",
        },
        {
            "id": "rasterio:api:Window",
            "kind": CapabilityKind.class_,
            "name": "rasterio.windows.Window",
            "category": ["api", "rasterio", "windows"],
            "desc": "栅格局部子窗口规格类（col_off, row_off, width, height），用于大图分块按需读取与流式处理。",
            "doc": "api/rasterio.windows.html#rasterio.windows.Window",
            "sig": "Window(col_off, row_off, width, height)",
        },
        {
            "id": "rasterio:api:from_bounds",
            "kind": CapabilityKind.function,
            "name": "rasterio.windows.from_bounds",
            "category": ["api", "rasterio", "windows"],
            "desc": "根据地理空间范围外包框 (left, bottom, right, top) 计算对应的像素行列窗口 Window。",
            "doc": "api/rasterio.windows.html#rasterio.windows.from_bounds",
            "sig": "(left, bottom, right, top, transform, height=None, width=None, precision=None)",
        },
        {
            "id": "rasterio:api:MemoryFile",
            "kind": CapabilityKind.class_,
            "name": "rasterio.io.MemoryFile",
            "category": ["api", "rasterio", "io", "memory"],
            "desc": "在内存缓冲（/vsimem/）中创建虚拟栅格文件，支持无需磁盘写入的快速内存管线流式处理。",
            "doc": "api/rasterio.io.html#rasterio.io.MemoryFile",
            "sig": "MemoryFile(file_or_bytes=None, filename=None, ext='')",
        },
        {
            "id": "rasterio:api:copy",
            "kind": CapabilityKind.function,
            "name": "rasterio.shutil.copy",
            "category": ["api", "rasterio", "shutil"],
            "desc": "完整拷贝已有栅格数据集及其辅助文件（如 .hdr、.xml、.ovr），支持驱动格式转换与重缩放。",
            "doc": "api/rasterio.shutil.html#rasterio.shutil.copy",
            "sig": "(src, dst, driver=None, strict=True, **creation_options)",
        },
    ]

    result: list[CapabilityDescriptor] = []
    for item in api_entries:
        sig_str = str(item["sig"])
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
                source=_source_spec(str(item["doc"])),
                entrypoints=[Entrypoint(kind="script", target=str(item["name"]), available=True, notes=f"签名: {sig_str}")],
                state=CapabilityState(
                    presence=Presence.present,
                    binding=Binding.script,
                    validation=Validation.not_run,
                    availability=Availability.ready,
                ),
                effects="read_only" if "class" in item["category"] or "windows" in item["category"] else "mixed",
                cancellation="unsupported",
                limitations=[f"接口签名: {sig_str}"],
                validation_evidence=[],
            )
        )
    return result


def build_rasterio_baseline() -> list[CapabilityDescriptor]:
    """汇总构建完整的 Rasterio 1.5.2 公开能力基准条目清单。"""
    all_items: list[CapabilityDescriptor] = []
    all_items.extend(build_rasterio_algorithms())
    all_items.extend(build_rio_cli_commands())
    all_items.extend(build_rasterio_python_api())
    return all_items
