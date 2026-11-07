"""GDAL 引擎 Provider 适配器实现（P1-03）。

仅在 GDAL 独立 worker 进程中导入 osgeo。
实现栅格格式转换、波段处理与能力发现，内置专有 staging 事务与闭合校验。
严格遵循输出安全语义：
1. 相对路径严格按 TaskSpec.project_root 解析，不受 worker 进程 cwd 影响。
2. 严格核验 OutputPlan.overwrite 约束，拒绝覆盖已有成果，防范输入输出同源冲突。
3. 多文件数据集（如 ENVI 头文件 .hdr、辅助文件 .aux.xml）作为统一整体提交与回滚。
4. 提交前后均进行完整性校验（staging 重开校验与 final target 最终重开校验）。
"""

from __future__ import annotations

import contextlib
import json
import math
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from osgeo import gdal, osr  # type: ignore[import-not-found]

    gdal.UseExceptions()
    GDAL_AVAILABLE = True
except ImportError:
    GDAL_AVAILABLE = False
    gdal = None
    osr = None

from spectra_workbench.domain.capability_wiring import VECTOR_OUTPUT_CAPABILITIES
from spectra_workbench.domain.models import (
    Availability,
    CapabilityDescriptor,
    Presence,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.processing.output_formats import (
    VECTOR_OUTPUT_FORMATS,
    normalize_output_format,
    shapefile_sidecar_paths,
    validate_output_format,
    validate_vector_output_format,
)
from spectra_workbench.processing.raster_params import (
    ParameterError,
    normalize_raster_params,
)
from spectra_workbench.providers.base import (
    ExecutionContext,
    OutputConflictError,
    ProviderAdapter,
    resolve_dataset_path,
)
from spectra_workbench.providers.gdal_provider.calc_ops import run_calc
from spectra_workbench.providers.gdal_provider.expression_ops import ExpressionError
from spectra_workbench.providers.gdal_provider.metadata_ops import (
    apply_nodata,
    build_overviews,
    restore_missing_hdr_fields,
    snapshot_hdr_lines,
)
from spectra_workbench.providers.gdal_provider.mosaic_ops import (
    IncompatibleSourcesError,
    NodataConflictError,
    SourceUsageError,
    assert_vrt_sources,
    build_vrt,
    check_source_compatibility,
    compare_rasters,
    inspect_raster,
    manage_bands,
    merge_sources,
    set_vrt_source_paths,
    stack_sources,
)
from spectra_workbench.providers.gdal_provider.sidecars import (
    SidecarOwnershipError,
    collect_protected_inputs,
    ensure_no_stale_sidecars,
)
from spectra_workbench.providers.gdal_provider.statistics_ops import (
    compute_statistics,
    write_statistics_report,
)
from spectra_workbench.providers.gdal_provider.vector_ops import inspect_vector
from spectra_workbench.registry.gdal_index import (
    GDAL_VERSION,
    build_gdal_baseline,
)


def _resolve_raster_input(path: Path) -> Path:
    """处理 ENVI 格式：若传入 .hdr 文件，转换为 GDAL 期望读取的对应 .dat/.img 数据文件。"""
    if path.suffix.lower() == ".hdr":
        for ext in (".dat", ".img"):
            candidate = path.with_suffix(ext)
            if candidate.exists():
                return candidate
    return path


#: P4-02 能力集合：这些能力统一走多输入提取（inputs["inputs"] 列表）。
P402_CAPABILITIES: frozenset[str] = frozenset(
    {
        "gdal:buildvrt",
        "gdalbuildvrt",
        "gdal:merge",
        "gdal_merge",
        "gdal:stack",
        "gdal:bandmanage",
        # --- P4-03（同为列表形式输入；in-place 语义见 INPLACE_CAPABILITIES）---
        "gdal:editmeta",
        "gdal:overview",
        # --- P4-04（同为列表形式输入；表达式计算输出栅格）---
        "gdal:calc",
    }
)

#: P4-03 in-place 能力：直接更新源文件（输出=输入），staging 走「复制-改-提交」。
INPLACE_CAPABILITIES: frozenset[str] = frozenset({"gdal:editmeta", "gdal:overview"})

#: P4-04 统计报告能力：输出 JSON artifact（不是栅格图层），单输入。
P404_STATISTICS_CAPABILITY = "gdal:statistics"


def _extract_raster_inputs(spec: Any) -> list[Path]:
    """按任务协议提取**多**个栅格输入源。

    约定：
    - `spec.inputs["inputs"]` 为 `list[DatasetRef]`（P4-02 多输入能力的标准形式）；
    - `spec.inputs["input"]` 若是列表同样按多输入处理（兼容写法）。

    路径解析规则与单输入路径保持一致（基于 project_root，ENVI .hdr 自动转 .dat）。
    """
    inputs = getattr(spec, "inputs", {}) or {}
    raw_list: list[Any] = []
    multi = inputs.get("inputs")
    if isinstance(multi, (list, tuple)):
        raw_list.extend(multi)
    single = inputs.get("input")
    if isinstance(single, (list, tuple)):
        raw_list.extend(single)
    elif single is not None:
        raw_list.append(single)

    paths: list[Path] = []
    for ref in raw_list:
        raw_uri = ref.uri if hasattr(ref, "uri") else str(ref)
        paths.append(
            _resolve_raster_input(resolve_dataset_path(raw_uri, spec.project_root))
        )
    return paths


def _affine_bounds(
    geotransform: list[float] | tuple[float, ...] | None,
    width: int,
    height: int,
) -> list[float] | None:
    """按四个像元外边界角点计算仿射范围，返回 [minx, miny, maxx, maxy]。

    GeoTransform 为 (originX, pixelWidth, rowRotation, originY, colRotation, pixelHeight)，
    像元 (col, row) 的**外边界**角点坐标为：
        x = originX + col * pixelWidth + row * rowRotation
        y = originY + col * colRotation + row * pixelHeight

    四个外边界角点为 (0,0)、(width,0)、(width,height)、(0,height)。
    只取 (0,0) 与 (width,height) 两个对角点会在旋转/倾斜仿射下漏掉另外两角，
    导致范围偏小——因此这里必须遍历全部四角。
    """
    if not geotransform or len(geotransform) < 6:
        return None
    ox, px, row_rot, oy, col_rot, py = (float(v) for v in geotransform[:6])
    w = float(width)
    h = float(height)
    corners = [
        (ox + col * px + row * row_rot, oy + col * col_rot + row * py)
        for col, row in ((0.0, 0.0), (w, 0.0), (w, h), (0.0, h))
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return [min(xs), min(ys), max(xs), max(ys)]


def _vector_bounds(src_ds: Any) -> tuple[float, float, float, float] | None:
    """Union all vector layer extents as (minx, miny, maxx, maxy).

    OGR returns (minX, maxX, minY, maxY); GDAL Rasterize/Grid expect
    (minX, minY, maxX, maxY). Missing/invalid extents return None.
    """
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    try:
        for layer_idx in range(src_ds.GetLayerCount()):
            layer = src_ds.GetLayer(layer_idx)
            if layer is None:
                continue
            extent = layer.GetExtent()
            if not extent:
                continue
            e_min_x, e_max_x, e_min_y, e_max_y = extent
            min_x = min(min_x, float(e_min_x))
            min_y = min(min_y, float(e_min_y))
            max_x = max(max_x, float(e_max_x))
            max_y = max(max_y, float(e_max_y))
    except Exception:
        return None
    if not all(math.isfinite(v) for v in (min_x, min_y, max_x, max_y)):
        return None
    return (min_x, min_y, max_x, max_y)


#: 源窗口预裁剪的像元数预算上限（单波段等价像元）。
#: 预裁剪走惰性 VRT（不物化像元），此上限用于防御「窗口过大导致下游 Warp
#: 在目标范围上仍需展开巨量数据」的情形：超限时明确拒绝，而不是默默跑巨量作业。
MAX_SOURCE_WINDOW_PIXELS = 64_000_000


def _resample_gdal_constants() -> dict[str, Any]:
    """canonical 重采样算法名 -> GDAL 的 GRA_* 常量（**惰性构造**）。

    刻意不在模块级构造：adapter 必须能在**没有 osgeo 的 GUI 进程**中被 import
    （顶层 `from osgeo import gdal` 是 try/except 保护的，失败时 gdal 为 None）。
    模块级访问 `gdal.GRA_*` 会在 GUI 进程抛 AttributeError，
    使 GUI 侧单元测试在收集阶段就失败。这里改为按需构造——
    只有真正执行任务（worker 进程）时才会访问 GDAL 常量。

    名字集合由 processing.raster_params 统一维护（契约的唯一来源），
    这里只负责把名字翻译成 GDAL 常量。
    """
    return {
        "nearest": gdal.GRA_NearestNeighbour,
        "bilinear": gdal.GRA_Bilinear,
        "cubic": gdal.GRA_Cubic,
        "cubic_spline": gdal.GRA_CubicSpline,
        "lanczos": gdal.GRA_Lanczos,
        "average": gdal.GRA_Average,
        "mode": gdal.GRA_Mode,
        "max": gdal.GRA_Max,
        "min": gdal.GRA_Min,
        "median": gdal.GRA_Med,
        "q1": gdal.GRA_Q1,
        "q3": gdal.GRA_Q3,
    }


class GDALProviderAdapter(ProviderAdapter):
    """GDAL 算法与格式转换适配器。"""

    @property
    def provider_id(self) -> str:
        return "gdal"

    def runtime_info(self) -> dict[str, Any]:
        if not GDAL_AVAILABLE or gdal is None or osr is None:
            return {"available": False, "error": "osgeo module not importable in current environment"}

        return {
            "available": True,
            "provider_id": "gdal",
            "gdal_version": gdal.__version__,
            "driver_count": gdal.GetDriverCount(),
            "has_proj": hasattr(osr, "GetPROJVersionMajor"),
        }

    def discover(self) -> list[CapabilityDescriptor]:
        baseline = build_gdal_baseline()
        version_str = gdal.__version__ if GDAL_AVAILABLE and gdal is not None else GDAL_VERSION
        for c in baseline:
            c.engine.resolved_version = version_str
            if not GDAL_AVAILABLE:
                c.state.presence = Presence.missing
                c.state.availability = Availability.missing_dependency
        return baseline

    SUPPORTED_CAPABILITIES = {
        "gdal:translate",
        "gdal_translate",
        "gdal:warp",
        "gdalwarp",
        # --- P4-02：拼接 / VRT / 堆叠 / 波段管理 ---
        "gdal:buildvrt",
        "gdalbuildvrt",
        "gdal:merge",
        "gdal_merge",
        "gdal:stack",
        "gdal:bandmanage",
        # --- P4-03：元数据编辑 / 概览构建（in-place）---
        "gdal:editmeta",
        "gdal:overview",
        # --- P5-02：地形分析 / 矢量栅格化 / 散点插值 ---
        "gdal:dem",
        "gdal:rasterize",
        "gdal:grid",
        # --- P5-02：栅格矢量化 / 等值线（成果为矢量）---
        "gdal:polygonize",
        "gdal:contour",
        # --- P5-04：矢量处理 / SQL / 坐标转换（成果为矢量）---
        "ogr:vectortranslate",
        # --- P4-04：受限表达式计算 / 直方图与基础统计 ---
        "gdal:calc",
        P404_STATISTICS_CAPABILITY,
    }

    #: 需要多个输入源的能力（至少 2 个）。
    #: gdal:calc 允许**恰好 1 个**输入（如 A*2），故不列入此集合，
    #: 但仍走 inputs["inputs"] 列表形式（见 P402_CAPABILITIES 与 validate 专属分支）。
    MULTI_INPUT_CAPABILITIES = {
        "gdal:buildvrt",
        "gdalbuildvrt",
        "gdal:merge",
        "gdal_merge",
        "gdal:stack",
    }

    def validate(self, spec: TaskSpec) -> dict[str, Any]:
        errors: list[str] = []
        code: str = "validation_failed"
        if spec.capability_id not in self.SUPPORTED_CAPABILITIES:
            return {
                "valid": False,
                "code": "unsupported_capability",
                "errors": [f"GDAL 引擎尚未接入该能力执行器: '{spec.capability_id}'（当前已支持: gdal:translate, gdal:warp）"],
            }

        is_multi = spec.capability_id in self.MULTI_INPUT_CAPABILITIES
        if spec.capability_id in INPLACE_CAPABILITIES:
            # P4-03 in-place：恰好 1 个输入，且输出=输入是意图本身（同源豁免）。
            # target 在本分支内自行解析（此处先于 validate 后段的 target 定义）。
            srcs = _extract_raster_inputs(spec)
            inplace_target = (
                resolve_dataset_path(spec.outputs[0].uri, spec.project_root)
                if spec.outputs
                else None
            )
            if len(srcs) != 1:
                errors.append(
                    f"能力 {spec.capability_id} 需要恰好 1 个输入源，实际 {len(srcs)} 个"
                )
                code = "input_not_found"
            elif inplace_target is None:
                errors.append("TaskSpec outputs 必须声明至少一个输出计划")
                code = "invalid_output_plan"
            else:
                if not srcs[0].exists():
                    errors.append(f"输入文件不存在: {srcs[0]}")
                    code = "input_not_found"
                if srcs[0].resolve() != inplace_target.resolve():
                    errors.append(
                        f"in-place 能力要求输出与输入为同一文件："
                        f"输入 {srcs[0]}，输出 {inplace_target}"
                    )
                    code = "invalid_output_plan"
        elif spec.capability_id == "gdal:bandmanage":
            # 波段管理：恰好 1 个输入源，但同样用 inputs["inputs"] 列表形式
            srcs = _extract_raster_inputs(spec)
            if len(srcs) != 1:
                errors.append(
                    f"能力 {spec.capability_id} 需要恰好 1 个输入源，实际 {len(srcs)} 个"
                )
                code = "input_not_found"
            else:
                for in_path in srcs:
                    if not in_path.exists():
                        errors.append(f"输入文件不存在: {in_path}")
                        code = "input_not_found"
        elif spec.capability_id == "gdal:calc":
            # 表达式计算：1..N 个输入（A/B/C…），允许单源表达式（如 A*2）
            srcs = _extract_raster_inputs(spec)
            if not srcs:
                errors.append("能力 gdal:calc 至少需要 1 个输入源")
                code = "input_not_found"
            elif len(srcs) > 26:
                errors.append(f"能力 gdal:calc 最多支持 26 个输入源，实际 {len(srcs)} 个")
                code = "input_not_found"
            else:
                for in_path in srcs:
                    if not in_path.exists():
                        errors.append(f"输入文件不存在: {in_path}")
                        code = "input_not_found"
        elif is_multi:
            # 多输入能力：inputs["inputs"] 为列表（兼容 inputs["input"] 为列表）
            srcs = _extract_raster_inputs(spec)
            if len(srcs) < 2:
                errors.append(
                    f"能力 {spec.capability_id} 需要至少 2 个输入源，"
                    f"实际提供 {len(srcs)} 个（请在 inputs['inputs'] 中给出列表）"
                )
                code = "input_not_found"
            else:
                for in_path in srcs:
                    if not in_path.exists():
                        errors.append(f"输入文件不存在: {in_path}")
                        code = "input_not_found"
        elif "input" not in spec.inputs:
            errors.append("TaskSpec inputs 缺少必需的 'input' 端口")
            code = "input_not_found"
        else:
            in_ref = spec.inputs["input"]
            raw_uri = in_ref.uri if hasattr(in_ref, "uri") else str(in_ref)
            in_path = _resolve_raster_input(resolve_dataset_path(raw_uri, spec.project_root))
            if not in_path.exists():
                errors.append(f"输入文件不存在: {in_path}")
                code = "input_not_found"

        if not spec.outputs:
            errors.append("TaskSpec outputs 必须声明至少一个输出计划")
            code = "invalid_output_plan"
        else:
            out_plan = spec.outputs[0]
            target_path = resolve_dataset_path(out_plan.uri, spec.project_root)
            if spec.capability_id in INPLACE_CAPABILITIES:
                pass  # in-place：输出=输入是意图本身，同源检查已在专属分支完成
            elif is_multi or spec.capability_id in ("gdal:bandmanage", "gdal:calc"):
                for in_p in _extract_raster_inputs(spec):
                    if in_p.resolve() == target_path.resolve():
                        errors.append(f"输入与输出目标指向同一文件: {target_path}")
                        code = "input_output_conflict"
            elif "input" in spec.inputs:
                in_ref = spec.inputs["input"]
                raw_in_uri = in_ref.uri if hasattr(in_ref, "uri") else str(in_ref)
                in_p = _resolve_raster_input(resolve_dataset_path(raw_in_uri, spec.project_root))
                if in_p.resolve() == target_path.resolve():
                    errors.append(f"输入与输出目标指向同一文件: {target_path}")
                    code = "input_output_conflict"

        return {"valid": len(errors) == 0, "code": code, "errors": errors}

    def estimate(self, spec: TaskSpec) -> dict[str, Any]:
        return {
            "estimated_ram_mib": 128,
            "estimated_disk_bytes": 1024 * 1024 * 5,
            "estimated_duration_seconds": 1.0,
        }

    def execute(self, spec: TaskSpec, context: ExecutionContext) -> TaskResult:
        if not GDAL_AVAILABLE or gdal is None:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "gdal_missing", "message": "GDAL is not available in current worker process"},
            )

        # 0. 准入与路由核验：非支持能力直接报错，禁止回退到 Translate
        val = self.validate(spec)
        if not val["valid"]:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "unsupported_capability", "message": "; ".join(val["errors"])},
                duration_seconds=0.0,
            )

        start_time = time.monotonic()
        context.log("INFO", f"GDAL Worker initializing task {spec.task_id} with GDAL {gdal.__version__}")
        context.progress(0.05, "initializing", "Verifying inputs and parameters")

        # 1. 严格解析输入路径（基于 project_root）
        is_p402 = spec.capability_id in P402_CAPABILITIES
        if is_p402:
            # P4-02 能力：输入来自 inputs["inputs"] 列表（多输入），
            # 由 P4-02 分支自行解析与校验，这里不强制要求单输入端口。
            in_path = None
        else:
            in_ref = spec.inputs["input"]
            raw_in_uri = in_ref.uri if hasattr(in_ref, "uri") else str(in_ref)
            in_path = _resolve_raster_input(resolve_dataset_path(raw_in_uri, spec.project_root))
            if not in_path.exists():
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={"code": "input_not_found", "message": f"Input raster file does not exist: {in_path}"},
                )

        # 2. 严格解析目标输出路径（基于 project_root）
        out_plan = spec.outputs[0]
        target_path = resolve_dataset_path(out_plan.uri, spec.project_root)
        out_format = spec.parameters.get("format", out_plan.driver or "GTiff")
        overwrite = out_plan.overwrite

        # 2.1 同源冲突防范（P4-02 的多输入同源检查在 validate 中统一处理）
        if in_path is not None and in_path.resolve() == target_path.resolve():
            context.log("ERROR", f"Input and output target point to the same file: {target_path}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "input_output_conflict", "message": f"Input and output target point to the same file: {target_path}"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # 2.2 overwrite=false 目标冲突检查（不修改原目标）
        if target_path.exists() and not overwrite:
            context.log("ERROR", f"Target file already exists and overwrite=false: {target_path}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "output_already_exists", "message": f"Target file already exists and overwrite=false: {target_path}"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        if out_format.upper() == "ENVI" and target_path.with_suffix(".hdr").exists() and not overwrite:
            context.log("ERROR", f"Target ENVI header already exists and overwrite=false: {target_path.with_suffix('.hdr')}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "output_already_exists", "message": f"Target header file already exists and overwrite=false: {target_path.with_suffix('.hdr')}"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # 2.2b P4-02 输出格式与旧 sidecar 预检（在任何 staging 写入之前）
        if spec.capability_id in (
            "gdal:buildvrt",
            "gdalbuildvrt",
            "gdal:merge",
            "gdal_merge",
            "gdal:stack",
            "gdal:bandmanage",
        ):
            format_explicit = bool(
                str(spec.parameters.get("format") or "").strip()
            )
            fmt_err = self._validate_p402_output_format(
                spec.capability_id, out_format, format_explicit=format_explicit
            )
            if fmt_err is not None:
                context.log("ERROR", f"不支持的输出格式: {fmt_err}")
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={"code": "unsupported_output_format", "message": fmt_err},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            stale_err = self._stale_header_conflict(target_path, out_format)
            if stale_err is not None:
                context.log("ERROR", f"目标存在可能配错新数据的旧头文件: {stale_err}")
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={"code": "stale_header_conflict", "message": stale_err},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )

        # 2.3 构造专有 staging 路径
        staging_filename = f"staging_{spec.task_id}_{target_path.name}"
        staged_path = context.create_staging_path(staging_filename)

        context.log("DEBUG", f"Input: {in_path} -> Staged: {staged_path} -> Final: {target_path}")
        context.check_cancelled()

        # 3. 构造 GDAL 进度与取消回调
        def _gdal_cb(df_complete: float, psz_message: str | None, p_data: Any) -> int:
            if context.is_cancelled.is_set():
                return 0
            ratio = max(0.0, min(1.0, float(df_complete)))
            phase_msg = psz_message or f"{int(ratio * 100)}%"
            context.progress(ratio, "processing", phase_msg)
            return 1

        # 参数统一由契约模块归一化与校验（GUI 与 Worker 共用同一套规则）：
        # 未知算法不���退、别名冲突不择一、整数参数不截断小数、零值按 None 判断。
        try:
            norm = normalize_raster_params(
                spec.parameters,
                capability_id=spec.capability_id,
            )
        except ParameterError as exc:
            context.log("ERROR", f"Invalid processing parameters: {exc}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "invalid_parameters", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        creation_options: list[str] = []
        if isinstance(out_plan.creation_options, dict):
            for k, v in out_plan.creation_options.items():
                creation_options.append(f"{k}={v}")

        # 4. 执行算法（按 capability_id 明确分派）
        #    P4-02：拼接 / VRT / 堆叠 / 波段管理走独立分支（多输入），
        #    复用既有事务（staging → 重开校验 → commit → 登记）。
        if (
            not is_p402
            and in_path is not None
            and spec.capability_id != P404_STATISTICS_CAPABILITY
            and spec.capability_id not in ("gdal:rasterize", "gdal:grid")
            and spec.capability_id not in VECTOR_OUTPUT_CAPABILITIES
        ):
            format_violations = self._general_format_violations(in_path, norm, out_format)
            if format_violations:
                context.log("ERROR", f"输出格式组合不受支持: {format_violations}")
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={
                        "code": "unsupported_format_combination",
                        "message": "；".join(format_violations),
                    },
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )

        p402_result = self._execute_p402(
            spec,
            context,
            norm=norm,
            out_format=out_format,
            target_path=target_path,
            staged_path=staged_path,
            start_time=start_time,
        )
        if p402_result is not None:
            return p402_result

        # P4-04 统计：产出 JSON 报告（artifact），不写栅格、不需要 GDAL 重开校验。
        if spec.capability_id == P404_STATISTICS_CAPABILITY:
            return self._execute_p404_statistics(
                spec,
                context,
                norm=norm,
                in_path=in_path,
                target_path=target_path,
                staged_path=staged_path,
                start_time=start_time,
            )

        # P5-02 矢量成果（polygonize / contour）：成果是 OGR 数据集，
        # 建层/写要素/OGR 重开校验与栅格完全不同，走独立分支。
        # 矢量成果能力是单输入能力，validate() 已保证 input 端口存在，
        # 因此此处 in_path 必已解析（is_p402 分支才允许为 None）。
        if spec.capability_id in VECTOR_OUTPUT_CAPABILITIES and in_path is not None:
            return self._execute_vector_output(
                spec,
                context,
                norm=norm,
                in_path=in_path,
                out_format=out_format,
                target_path=target_path,
                staged_path=staged_path,
                start_time=start_time,
                callback=_gdal_cb,
            )

        is_warp = spec.capability_id in ("gdal:warp", "gdalwarp")
        is_dem = spec.capability_id == "gdal:dem"
        is_rasterize = spec.capability_id == "gdal:rasterize"
        is_grid = spec.capability_id == "gdal:grid"

        if is_grid:
            grid_bands = 1
            grid_dtype = str(norm.get("outputType") or "Float64")
            grid_violations = validate_output_format(
                out_format,
                dtype=grid_dtype,
                bands=grid_bands,
                has_nodata=norm.get("noData") is not None,
                has_alpha=False,
            )
            if grid_violations:
                context.log(
                    "ERROR",
                    f"Grid 输出格式不受支持: {grid_violations}",
                )
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={
                        "code": "unsupported_format_combination",
                        "message": "；".join(grid_violations),
                    },
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            if not norm.get("width") or not norm.get("height"):
                message = "gdal:grid 需要 width/height 以确定输出网格"
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={"code": "invalid_parameters", "message": message},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )

        if is_rasterize:
            rasterize_bands = len(norm.get("burnValues") or []) or 1
            rasterize_dtype = str(norm.get("outputType") or "Float64")
            rasterize_violations = validate_output_format(
                out_format,
                dtype=rasterize_dtype,
                bands=rasterize_bands,
                has_nodata=norm.get("noData") is not None,
                has_alpha=False,
            )
            if rasterize_violations:
                context.log(
                    "ERROR",
                    f"Rasterize 输出格式不受支持: {rasterize_violations}",
                )
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={
                        "code": "unsupported_format_combination",
                        "message": "；".join(rasterize_violations),
                    },
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            if all(
                norm.get(key) is None
                for key in ("xRes", "yRes", "width", "height")
            ):
                message = "gdal:rasterize 需要 xRes/yRes 或 width/height 以确定输出网格"
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={"code": "invalid_parameters", "message": message},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )

        # DEM 地形分析输出固定为 Float32 单波段；输入可能是 Byte/UInt16，
        # 不能只按源 dtype 判断格式矩阵，否则 PNG/JPEG 会在提交后才失败。
        if is_dem:
            dem_format_violations = validate_output_format(
                out_format,
                dtype="Float32",
                bands=1,
                has_nodata=False,
                has_alpha=False,
            )
            if dem_format_violations:
                context.log(
                    "ERROR",
                    f"DEM 输出格式不受支持: {dem_format_violations}",
                )
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={
                        "code": "unsupported_format_combination",
                        "message": "；".join(dem_format_violations),
                    },
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )

        # 重采样算法：契约已保证是受支持的 canonical 名，这里只做名字 -> GDAL 常量映射
        resample_alg = _resample_gdal_constants()[norm.get("resampleAlg", "nearest")]

        dst_srs = norm.get("dstSRS")
        src_srs = norm.get("srcSRS")
        # 零值合法：dstNodata=0 是有效请求，不能被 or 丢弃
        dst_nodata = norm.get("dstNodata")

        src_win_list = norm.get("srcWin")
        proj_win_list = norm.get("projWin")
        out_bounds_list = norm.get("outputBounds")
        x_res_f = norm.get("xRes")
        y_res_f = norm.get("yRes")
        output_type_name = norm.get("outputType")
        output_type = getattr(gdal, f"GDT_{output_type_name}") if output_type_name else None
        out_w_i = norm.get("width")
        out_h_i = norm.get("height")
        translate_nodata_f = norm.get("noData")
        target_aligned = norm.get("targetAlignedPixels")

        # Warp 下的源窗口裁剪：源像元窗口与目标 CRS 的 outputBounds 语义不同
        # （前者按源像元索引，后者按目标 CRS 的轴对齐范围），不得混为一谈。
        if is_warp and (src_win_list is not None or proj_win_list is not None):
            if out_bounds_list is not None:
                context.log(
                    "ERROR",
                    "参数冲突：Warp 下 srcWin/projWin（源窗口）与 outputBounds（目标范围）"
                    "语义不同，不能同时指定",
                )
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={
                        "code": "invalid_parameters",
                        "message": (
                            "Warp 下不能同时指定 srcWin/projWin 与 outputBounds："
                            "前者是源像元/源地理窗口，后者是目标 CRS 的轴对齐范围"
                        ),
                    },
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            if src_win_list is not None and proj_win_list is not None:
                context.log("ERROR", "参数冲突：srcWin 与 projWin 不能同时指定")
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={
                        "code": "invalid_parameters",
                        "message": "srcWin 与 projWin 不能同时指定（两者都是源窗口语义）",
                    },
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )

        try:
            if is_grid:
                src_ds = gdal.OpenEx(str(in_path), gdal.OF_VECTOR)
                if src_ds is None:
                    raise RuntimeError(f"无法作为矢量打开输入: {in_path}")
                try:
                    grid_kwargs: dict[str, Any] = {
                        "creationOptions": creation_options,
                        "callback": _gdal_cb,
                        "width": int(norm["width"]),
                        "height": int(norm["height"]),
                        "algorithm": str(norm.get("algorithm") or "invdist"),
                    }
                    if norm.get("zField"):
                        grid_kwargs["zfield"] = str(norm["zField"])
                    if norm.get("outputBounds") is not None:
                        grid_kwargs["outputBounds"] = [
                            float(v) for v in norm["outputBounds"]
                        ]
                    else:
                        bounds = _vector_bounds(src_ds)
                        if bounds is not None:
                            grid_kwargs["outputBounds"] = bounds
                    if norm.get("outputType") is not None:
                        grid_kwargs["outputType"] = getattr(
                            gdal, f"GDT_{norm['outputType']}"
                        )
                    if norm.get("noData") is not None:
                        grid_kwargs["noData"] = float(norm["noData"])
                    if out_format.upper() == "GTIFF":
                        grid_kwargs["format"] = out_format
                        ds = gdal.Grid(
                            str(staged_path),
                            src_ds,
                            options=gdal.GridOptions(**grid_kwargs),
                        )
                    else:
                        raw_path = staged_path.with_name(
                            f"{staged_path.stem}_raw.tif"
                        )
                        grid_kwargs["format"] = "GTiff"
                        raw_ds = gdal.Grid(
                            str(raw_path),
                            src_ds,
                            options=gdal.GridOptions(**grid_kwargs),
                        )
                        if raw_ds is None:
                            raise RuntimeError("Grid 临时栅格创建失败")
                        del raw_ds
                        try:
                            ds = gdal.Translate(
                                str(staged_path),
                                str(raw_path),
                                options=gdal.TranslateOptions(
                                    format=out_format,
                                    creationOptions=creation_options,
                                    callback=_gdal_cb,
                                ),
                            )
                        finally:
                            raw_path.unlink(missing_ok=True)
                finally:
                    src_ds = None
            elif is_rasterize:
                burn_values = norm.get("burnValues")
                attribute = norm.get("attribute")
                if not burn_values and not attribute:
                    raise ValueError("gdal:rasterize 需要 burnValues 或 attribute")
                src_ds = gdal.OpenEx(str(in_path), gdal.OF_VECTOR)
                if src_ds is None:
                    raise RuntimeError(f"无法作为矢量打开输入: {in_path}")
                try:
                    rasterize_kwargs: dict[str, Any] = {
                        "creationOptions": creation_options,
                        "callback": _gdal_cb,
                    }
                    if burn_values:
                        rasterize_kwargs["burnValues"] = [float(v) for v in burn_values]
                    if attribute:
                        rasterize_kwargs["attribute"] = str(attribute)
                    if norm.get("allTouched") is not None:
                        rasterize_kwargs["allTouched"] = bool(norm["allTouched"])
                    if norm.get("xRes") is not None:
                        rasterize_kwargs["xRes"] = float(norm["xRes"])
                    if norm.get("yRes") is not None:
                        rasterize_kwargs["yRes"] = float(norm["yRes"])
                    if norm.get("width") is not None:
                        rasterize_kwargs["width"] = int(norm["width"])
                    if norm.get("height") is not None:
                        rasterize_kwargs["height"] = int(norm["height"])
                    if norm.get("outputBounds") is not None:
                        rasterize_kwargs["outputBounds"] = [
                            float(v) for v in norm["outputBounds"]
                        ]
                    if norm.get("outputType") is not None:
                        rasterize_kwargs["outputType"] = getattr(
                            gdal, f"GDT_{norm['outputType']}"
                        )
                    if norm.get("noData") is not None:
                        rasterize_kwargs["noData"] = float(norm["noData"])
                    if "outputBounds" not in rasterize_kwargs:
                        bounds = _vector_bounds(src_ds)
                        if bounds is not None:
                            rasterize_kwargs["outputBounds"] = bounds
                    if out_format.upper() == "GTIFF":
                        rasterize_kwargs["format"] = out_format
                        ds = gdal.Rasterize(
                            str(staged_path),
                            src_ds,
                            options=gdal.RasterizeOptions(**rasterize_kwargs),
                        )
                    else:
                        # Rasterize can only create drivers with DCAP_CREATE;
                        # PNG/JPEG/COG are CreateCopy-only. Rasterize to a
                        # temporary GTiff and translate to the requested format.
                        raw_path = staged_path.with_name(
                            f"{staged_path.stem}_raw.tif"
                        )
                        rasterize_kwargs["format"] = "GTiff"
                        raw_ds = gdal.Rasterize(
                            str(raw_path),
                            src_ds,
                            options=gdal.RasterizeOptions(**rasterize_kwargs),
                        )
                        if raw_ds is None:
                            raise RuntimeError("Rasterize 临时栅格创建失败")
                        del raw_ds
                        try:
                            ds = gdal.Translate(
                                str(staged_path),
                                str(raw_path),
                                options=gdal.TranslateOptions(
                                    format=out_format,
                                    creationOptions=creation_options,
                                    callback=_gdal_cb,
                                ),
                            )
                        finally:
                            raw_path.unlink(missing_ok=True)
                finally:
                    src_ds = None
            elif is_dem:
                dem_processing = norm.get("processing")
                if not dem_processing:
                    raise ValueError("gdal:dem 需要 processing 参数")
                dem_kwargs: dict[str, Any] = {
                    "format": out_format,
                    "creationOptions": creation_options,
                    "callback": _gdal_cb,
                }
                if norm.get("zFactor") is not None:
                    dem_kwargs["zFactor"] = float(norm["zFactor"])
                if norm.get("scale") is not None:
                    dem_kwargs["scale"] = float(norm["scale"])
                ds = gdal.DEMProcessing(
                    str(staged_path),
                    str(in_path),
                    str(dem_processing),
                    options=gdal.DEMProcessingOptions(**dem_kwargs),
                )
            elif is_warp:
                warp_kwargs: dict[str, Any] = {
                    "format": out_format,
                    "resampleAlg": resample_alg,
                    "creationOptions": creation_options,
                    "callback": _gdal_cb,
                }
                if dst_srs is not None:
                    warp_kwargs["dstSRS"] = dst_srs
                if src_srs is not None:
                    warp_kwargs["srcSRS"] = src_srs
                if dst_nodata is not None:
                    warp_kwargs["dstNodata"] = dst_nodata
                if out_bounds_list is not None:
                    warp_kwargs["outputBounds"] = out_bounds_list
                if x_res_f is not None:
                    warp_kwargs["xRes"] = x_res_f
                if y_res_f is not None:
                    warp_kwargs["yRes"] = y_res_f
                if output_type is not None:
                    warp_kwargs["outputType"] = output_type
                if out_w_i is not None:
                    warp_kwargs["width"] = out_w_i
                if out_h_i is not None:
                    warp_kwargs["height"] = out_h_i
                if target_aligned is not None:
                    # 仅 Warp 支持 targetAlignedPixels（实测 Translate 不接受该关键字）
                    warp_kwargs["targetAlignedPixels"] = bool(target_aligned)

                if src_win_list is not None or proj_win_list is not None:
                    # 两步语义：先按源窗口裁剪（源像元索引 / 源地理范围），
                    # 再对裁剪结果做重投影。源窗口与目标 CRS 的 outputBounds 不可混用。
                    #
                    # 预裁剪用**惰性 VRT**而不是 MEM：
                    # MEM 会物化整个裁剪区的像元（内存随窗口面积×波段×类型增长，无上限），
                    # 违反项目「有界大影像处理」要求；VRT 只生成 XML 描述
                    # （实测 8x9 窗口仅 1.2KB），裁剪在读取时生效，语义与 MEM 逐点一致。
                    if src_win_list is not None:
                        window_pixels = src_win_list[2] * src_win_list[3]
                    else:
                        # projWin 为源地理范围，像元数需读源尺寸估算
                        window_pixels = 0
                    if window_pixels > MAX_SOURCE_WINDOW_PIXELS:
                        message = (
                            f"源窗口过大（{window_pixels} 像元 > 上限 "
                            f"{MAX_SOURCE_WINDOW_PIXELS}）：已拒绝，避免巨量内存作业"
                        )
                        context.log("ERROR", message)
                        return TaskResult(
                            task_id=spec.task_id,
                            attempt=spec.attempt,
                            status=TaskStatus.failed,
                            error={"code": "window_too_large", "message": message},
                            duration_seconds=round(time.monotonic() - start_time, 4),
                        )
                    crop_kwargs: dict[str, Any] = {
                        "format": "VRT",
                        # 预裁剪阶段也接入取消回调：此前未传，取消无法在此阶段生效
                        "callback": _gdal_cb,
                    }
                    if src_win_list is not None:
                        crop_kwargs["srcWin"] = src_win_list
                    if proj_win_list is not None:
                        crop_kwargs["projWin"] = proj_win_list
                    cropped = gdal.Translate(
                        "", str(in_path), options=gdal.TranslateOptions(**crop_kwargs)
                    )
                    if cropped is None:
                        raise RuntimeError("源窗口裁剪失败")
                    try:
                        warp_opts = gdal.WarpOptions(**warp_kwargs)
                        ds = gdal.Warp(str(staged_path), cropped, options=warp_opts)
                    finally:
                        # VRT 在内存中，无落盘中间成果；显式释放确保不留引用
                        cropped = None
                else:
                    warp_opts = gdal.WarpOptions(**warp_kwargs)
                    ds = gdal.Warp(str(staged_path), str(in_path), options=warp_opts)
            else:
                translate_kwargs: dict[str, Any] = {
                    "format": out_format,
                    "resampleAlg": resample_alg,
                    "creationOptions": creation_options,
                    "callback": _gdal_cb,
                }
                if src_win_list is not None:
                    translate_kwargs["srcWin"] = src_win_list
                if proj_win_list is not None:
                    translate_kwargs["projWin"] = proj_win_list
                if out_bounds_list is not None:
                    translate_kwargs["outputBounds"] = out_bounds_list
                if x_res_f is not None:
                    translate_kwargs["xRes"] = x_res_f
                if y_res_f is not None:
                    translate_kwargs["yRes"] = y_res_f
                if output_type is not None:
                    translate_kwargs["outputType"] = output_type
                if translate_nodata_f is not None:
                    translate_kwargs["noData"] = translate_nodata_f
                if out_w_i is not None:
                    translate_kwargs["width"] = out_w_i
                if out_h_i is not None:
                    translate_kwargs["height"] = out_h_i
                opts = gdal.TranslateOptions(**translate_kwargs)
                ds = gdal.Translate(str(staged_path), str(in_path), options=opts)
        except Exception as exc:
            if context.is_cancelled.is_set():
                context.log("WARNING", f"Task {spec.task_id} was cancelled during computation")
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.cancelled,
                    error={"code": "task_cancelled", "message": "Operation cancelled by user"},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            op_name = (
                "Grid"
                if is_grid
                else "Rasterize"
                if is_rasterize
                else "DEMProcessing"
                if is_dem
                else "Warp"
                if is_warp
                else "Translate"
            )
            context.log("ERROR", f"GDAL {op_name} failed: {exc}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": f"{op_name.lower()}_error", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        if context.is_cancelled.is_set() or ds is None:
            if ds is not None:
                del ds
            if staged_path.exists():
                staged_path.unlink()
            if context.is_cancelled.is_set():
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.cancelled,
                    error={"code": "task_cancelled", "message": "Operation cancelled by user"},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            op_name = (
                "Grid"
                if is_grid
                else "Rasterize"
                if is_rasterize
                else "DEMProcessing"
                if is_dem
                else "Warp"
                if is_warp
                else "Translate"
            )
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": f"{op_name.lower()}_returned_none", "message": gdal.GetLastErrorMsg() or f"{op_name} failed"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # 5. 获取全部伴生 sidecar 文件（如 ENVI .hdr、.aux.xml），并关闭句柄刷新到磁盘
        raw_files = ds.GetFileList() or []
        staged_files: list[Path] = [Path(f).resolve() for f in raw_files]
        if staged_path.resolve() not in staged_files:
            staged_files.insert(0, staged_path.resolve())
        # 在 Windows 上显式关闭 dataset 释放文件锁
        del ds

        # 6. 重新打开并进行 staging 成果完整性验证
        context.progress(0.9, "verifying", "Re-opening staged dataset for validation")
        try:
            verify_ds = gdal.Open(str(staged_path), gdal.GA_ReadOnly)
            if verify_ds is None:
                raise RuntimeError(f"Unable to reopen staged file {staged_path}")
            w = verify_ds.RasterXSize
            h = verify_ds.RasterYSize
            bands = verify_ds.RasterCount
            if is_warp and dst_srs:
                srs = verify_ds.GetSpatialRef()
                if srs is None:
                    raise RuntimeError("Warp 成果未包含有效空间参考 (SpatialRef is None)")
                auth_code = srs.GetAuthorityCode(None)
                clean_expected = str(dst_srs).upper().replace("EPSG:", "").strip()
                if auth_code and auth_code != clean_expected:
                    raise RuntimeError(f"Warp 成果空间参考不匹配：期望 {clean_expected}，实际 {auth_code}")
            del verify_ds
        except Exception as verify_err:
            context.log("ERROR", f"Verification of staged output failed: {verify_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "verification_failed", "message": str(verify_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # 7. 构造数据集全量文件提交对（主文件 + 全部 sidecar）
        commit_pairs: list[tuple[Path, Path]] = []
        for sf in staged_files:
            if sf == staged_path.resolve():
                tf = target_path
            elif sf.suffix.lower() == ".hdr":
                tf = target_path.with_suffix(".hdr")
            elif sf.name.endswith(".aux.xml"):
                tf = target_path.parent / f"{target_path.name}.aux.xml"
            elif sf.name.startswith(staged_path.name):
                tail = sf.name[len(staged_path.name):]
                tf = target_path.parent / f"{target_path.name}{tail}"
            else:
                tf = target_path.parent / sf.name
            commit_pairs.append((sf, tf))

        # 7.5 sidecar 所有权预检：输入碰撞拒绝；stale 交给提交事务备份/删除
        new_targets = [tf for _, tf in commit_pairs]
        protected_inputs = collect_protected_inputs([in_path]) if in_path else set()
        try:
            report = ensure_no_stale_sidecars(
                target_path,
                new_targets,
                protected_inputs=protected_inputs,
                allow_stale_deletion=True,
            )
        except SidecarOwnershipError as sidecar_err:
            context.log("ERROR", f"sidecar 所有权预检失败: {sidecar_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={
                    "code": sidecar_err.code,
                    "message": str(sidecar_err),
                    "details": {
                        "stale": [str(p) for p in sidecar_err.stale],
                        "unknown": [str(p) for p in sidecar_err.unknown],
                        "protected": [str(p) for p in sidecar_err.protected],
                    },
                },
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        new_set = {p.resolve() for p in new_targets}
        stale_sidecars = [p for p in report.owned if p.resolve() not in new_set]
        if stale_sidecars:
            context.log(
                "INFO",
                "旧 sidecar 将随事务备份后删除："
                + ", ".join(p.name for p in stale_sidecars),
            )

        # 8. 事务性提交全部成果文件
        context.progress(0.95, "committing", "Committing staged output and sidecars to destination")
        try:
            committed_files = context.commit_files(
                commit_pairs,
                overwrite=overwrite,
                delete_paths=stale_sidecars,
                protected_paths=protected_inputs,
            )
        except OutputConflictError as conflict_err:
            context.log("ERROR", f"Commit rejected due to existing target: {conflict_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "output_already_exists", "message": str(conflict_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        except Exception as commit_err:
            context.log("ERROR", f"Transaction commit failed: {commit_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "commit_failed", "message": str(commit_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # 9. 最终成果在 target_path 的二次核验（确保移动后 sidecar 依然完备可读）
        try:
            final_verify_ds = gdal.Open(str(target_path), gdal.GA_ReadOnly)
            if final_verify_ds is None:
                raise RuntimeError(f"Committed dataset at {target_path} failed to reopen")
            final_driver = final_verify_ds.GetDriver().ShortName
            del final_verify_ds
        except Exception as final_err:
            context.log("ERROR", f"Final dataset reopen check failed: {final_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "final_reopen_failed", "message": str(final_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        duration = round(time.monotonic() - start_time, 4)
        context.progress(1.0, "completed", "Conversion completed successfully")
        context.log("INFO", f"Task {spec.task_id} completed in {duration}s -> {target_path} ({w}x{h}, {bands} bands)")

        output_record = {
            "key": out_plan.key,
            "uri": target_path.as_uri(),
            "data_type": "raster",
            "driver": final_driver,
            "width": w,
            "height": h,
            "bands": bands,
            "committed_files": [f.as_uri() for f in committed_files],
        }

        return TaskResult(
            task_id=spec.task_id,
            attempt=spec.attempt,
            status=TaskStatus.succeeded,
            outputs=[output_record],
            duration_seconds=duration,
            engine_runtime={
                "provider_id": "gdal",
                "version": gdal.__version__,
            },
        )

    # ------------------------------------------------------------------
    # P4-02 成果完整性辅助
    # ------------------------------------------------------------------
    def _validate_p402_output_format(
        self, capability_id: str, out_format: str, *, format_explicit: bool
    ) -> str | None:
        """校验请求的输出格式能被当前能力真实产出。

        BuildVRT 的物理成果永远是 VRT：如果调用方**显式**要求 GTiff 等其他格式，
        必须拒绝，而不是写出 VRT 后按调用者默认格式登记。未显式给 format 时
        （GUI 的 buildvrt 表单不暴露格式），允许使用实际 VRT 驱动。
        """
        if gdal is None:
            return "GDAL 不可用"
        if capability_id in ("gdal:buildvrt", "gdalbuildvrt"):
            if format_explicit and out_format.upper() != "VRT":
                return (
                    f"gdal:buildvrt 只能产出 VRT，不能按 {out_format!r} 输出；"
                    "需要 GeoTIFF 等物理格式请使用 gdal:merge 或格式转换"
                )
            return None
        if out_format.upper() == "VRT":
            return None
        driver = gdal.GetDriverByName(out_format)
        if driver is None:
            return f"GDAL 未注册输出驱动 {out_format!r}"
        can_create = bool(driver.GetMetadataItem(gdal.DCAP_CREATE))
        can_copy = bool(driver.GetMetadataItem(gdal.DCAP_CREATECOPY))
        if not can_create and not can_copy:
            return f"GDAL 驱动 {out_format!r} 不支持创建/写出数据集"
        return None

    def _stale_header_conflict(self, target_path: Path, out_format: str) -> str | None:
        """阻止新数据覆盖 ENVI 数据文件却遗留旧 .hdr 的歧义目标。

        典型场景：目标 out.dat 旁已有旧 out.hdr，新任务却请求 GTiff（或 VRT）
        写到 out.dat。提交只替换 out.dat，GDAL 可能按旧 .hdr 把新数据识别成
        ENVI，造成「新数据配旧头文件」。这里在写入前明确拒绝，并保留旧文件。
        """
        header = target_path.with_suffix(".hdr")
        if not header.exists() or out_format.upper() == "ENVI":
            return None
        if target_path.suffix.lower() in ("", ".dat", ".img"):
            return (
                f"目标 {target_path.name} 旁存在旧 ENVI 头文件 {header.name}，"
                f"但本次输出格式是 {out_format}；继续会留下「新数据配旧头文件」的歧义。"
                "请改用明确的输出扩展名（如 .tif/.vrt），或先移除旧头文件。"
            )
        return None

    def _collect_staged_bundle(self, staged_path: Path, *, driver: str) -> list[Path]:
        """收集 staging 目录中的主文件与全部伴生成果文件。

        只收集**真实存在于 staging** 的文件，避免把 VRT 引用的源文件误当成成果。
        命名判据覆盖：主文件前缀（.dat.aux.xml/.dat.ovr）、同 stem 不同后缀
        （ENVI .hdr、GTiff .ovr）、以及驱动强制要求的头文件。
        """
        main = staged_path.resolve()
        files: set[Path] = {main}
        parent = staged_path.parent
        if parent.is_dir():
            for p in parent.iterdir():
                if not p.is_file() or p.name == "manifest.json":
                    continue
                resolved = p.resolve()
                if resolved == main:
                    continue
                if p.name.startswith(staged_path.name) or p.stem == staged_path.stem:
                    files.add(resolved)
        if driver.upper() == "ENVI":
            header = staged_path.with_suffix(".hdr")
            if not header.exists():
                raise RuntimeError(
                    f"ENVI 成果缺少主文件同名头文件：{header.name}（拒绝提交不完整成果）"
                )
            files.add(header.resolve())
        # 非 VRT 成果再用驱动自身的 GetFileList 补充；只接受确实位于 staging
        # 的文件，避免把被引用的输入源误当成果。VRT 的 GetFileList 会返回源，
        # 且按 staging 位置解析，不能用于成果收集。
        if driver.upper() != "VRT" and gdal is not None:
            with contextlib.suppress(Exception):
                ds = gdal.Open(str(staged_path), gdal.GA_ReadOnly)
                if ds is not None:
                    try:
                        for raw in ds.GetFileList() or []:
                            p = Path(raw)
                            if p.is_file() and p.parent.resolve() == parent.resolve():
                                files.add(p.resolve())
                    finally:
                        ds = None
        return sorted(files, key=lambda p: (p != main, p.name))

    def _map_bundle_target(self, staged_path: Path, staged_file: Path, target_path: Path) -> Path:
        """把 staging 文件名映射为最终目标文件名（主文件 + sidecar 一致命名）。"""
        main = staged_path.resolve()
        sf = staged_file.resolve()
        if sf == main:
            return target_path
        if sf.suffix.lower() == ".hdr":
            # ENVI 头文件必须与最终主文件同 stem，不能保留 staging 前缀
            return target_path.with_suffix(".hdr")
        if sf.name.startswith(staged_path.name):
            tail = sf.name[len(staged_path.name):]
            return target_path.parent / f"{target_path.name}{tail}"
        if sf.stem == staged_path.stem:
            return target_path.with_suffix(sf.suffix)
        return target_path.parent / sf.name

    def _cleanup_staged_bundle(self, staged_path: Path) -> None:
        """取消/提交前失败时清理 staging 计算文件；保留 manifest 作为审计证据。"""
        parent = staged_path.parent
        if not parent.is_dir():
            return
        for p in list(parent.iterdir()):
            if p.is_file() and p.name != "manifest.json":
                with contextlib.suppress(OSError):
                    p.unlink()

    def _log_engine_warnings(self, context: Any, info: dict[str, Any] | None) -> None:
        """把 GDAL warning 写入任务日志（源集合核验失败时也会随错误返回）。"""
        if not info:
            return
        for warning in info.get("warnings", []) or []:
            context.log("WARNING", f"GDAL: {warning}")

    def _inspect_staged_for_commit(
        self, staged_path: Path, target_path: Path, *, driver: str
    ) -> dict[str, Any]:
        """提交前真实重开 staging 成果并读取必要像元。

        VRT 的相对源引用以**最终目标目录**为基准；因此先复制一份到最终目录再打开，
        不能用 staging 位置打开来冒充最终可读性。
        """
        if driver.upper() != "VRT":
            return inspect_raster(staged_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        verify_path = target_path.parent / f".verify_{uuid.uuid4().hex[:12]}_{target_path.name}"
        try:
            shutil.copy2(staged_path, verify_path)
            return inspect_raster(verify_path)
        finally:
            with contextlib.suppress(OSError):
                verify_path.unlink()

    def _validate_actual_output_format(self, info: dict[str, Any], format_name: str) -> list[str]:
        """Validate a real reopened output against the format support matrix."""
        gdal_mod = gdal
        if gdal_mod is None:
            return ["GDAL 不可用"]
        types = info.get("types") or []
        bands = int(info.get("bands") or 0)
        nodata = info.get("nodata") or []
        color_interps = info.get("color_interpretations") or []
        mask_flags = info.get("mask_flags") or []
        has_nodata = any(v is not None for v in nodata)
        has_alpha = any(int(v) == int(gdal_mod.GCI_AlphaBand) for v in color_interps)
        has_mask = (not has_alpha) and any(
            int(v) & int(gdal_mod.GMF_PER_DATASET) for v in mask_flags
        )
        return validate_output_format(
            format_name,
            dtype=str(types[0]) if types else "",
            bands=bands,
            has_nodata=has_nodata,
            has_alpha=has_alpha,
            has_mask=has_mask,
        )

    def _p402_format_violations(
        self,
        capability_id: str,
        sources: list[Path],
        norm: dict[str, Any],
        out_format: str,
    ) -> list[str]:
        """Preflight output format against source dtype/bands/alpha/NoData."""
        if gdal is None or capability_id in ("gdal:buildvrt", "gdalbuildvrt"):
            return []
        if capability_id in INPLACE_CAPABILITIES:
            return []
        dtype = norm.get("outputType")
        bands = 0
        has_nodata = any(
            norm.get(key) is not None for key in ("srcNodata", "vrtNodata", "dstNodata")
        )
        has_alpha = False
        has_mask = False
        try:
            if capability_id in ("gdal:merge", "gdal_merge"):
                ds0 = gdal.Open(str(sources[0]))
                if ds0 is None:
                    return []
                bands = ds0.RasterCount
                dtype = dtype or gdal.GetDataTypeName(ds0.GetRasterBand(1).DataType)
                ds0 = None
                for source in sources:
                    ds = gdal.Open(str(source))
                    if ds is None:
                        continue
                    for i in range(1, ds.RasterCount + 1):
                        band = ds.GetRasterBand(i)
                        has_nodata = has_nodata or band.GetNoDataValue() is not None
                        has_alpha = has_alpha or int(band.GetColorInterpretation()) == int(
                            gdal.GCI_AlphaBand
                        )
                        has_mask = has_mask or bool(
                            int(band.GetMaskFlags()) & int(gdal.GMF_PER_DATASET)
                        )
                    ds = None
            elif capability_id == "gdal:stack":
                total_bands = 0
                for source in sources:
                    ds = gdal.Open(str(source))
                    if ds is None:
                        continue
                    total_bands += ds.RasterCount
                    if dtype is None:
                        dtype = gdal.GetDataTypeName(ds.GetRasterBand(1).DataType)
                    for i in range(1, ds.RasterCount + 1):
                        band = ds.GetRasterBand(i)
                        has_nodata = has_nodata or band.GetNoDataValue() is not None
                        has_alpha = has_alpha or int(band.GetColorInterpretation()) == int(
                            gdal.GCI_AlphaBand
                        )
                        has_mask = has_mask or bool(
                            int(band.GetMaskFlags()) & int(gdal.GMF_PER_DATASET)
                        )
                    ds = None
                bands = total_bands
            elif capability_id == "gdal:bandmanage":
                ds = gdal.Open(str(sources[0]))
                if ds is None:
                    return []
                band_list = norm.get("bandList")
                selected = (
                    [int(b) for b in band_list]
                    if isinstance(band_list, (list, tuple)) and band_list
                    else list(range(1, ds.RasterCount + 1))
                )
                bands = len(selected)
                first = ds.GetRasterBand(selected[0])
                dtype = dtype or gdal.GetDataTypeName(first.DataType)
                for idx in selected:
                    band = ds.GetRasterBand(int(idx))
                    has_nodata = has_nodata or band.GetNoDataValue() is not None
                    has_alpha = has_alpha or int(band.GetColorInterpretation()) == int(
                        gdal.GCI_AlphaBand
                    )
                    has_mask = has_mask or bool(
                        int(band.GetMaskFlags()) & int(gdal.GMF_PER_DATASET)
                    )
                ds = None
            else:
                return []
        except Exception:
            return []
        if has_alpha:
            # Alpha is a mask channel; do not report it as an independent
            # per-dataset mask (PNG only exposes alpha).
            has_mask = False
        return validate_output_format(
            out_format,
            dtype=str(dtype or ""),
            bands=bands,
            has_nodata=has_nodata,
            has_alpha=has_alpha,
            has_mask=has_mask,
        )

    def _general_format_violations(
        self, in_path: Path, norm: dict[str, Any], out_format: str
    ) -> list[str]:
        if gdal is None:
            return []
        ds = gdal.Open(str(in_path))
        if ds is None:
            return []
        try:
            dtype = norm.get("outputType") or gdal.GetDataTypeName(
                ds.GetRasterBand(1).DataType
            )
            has_nodata = any(
                norm.get(key) is not None for key in ("noData", "dstNodata")
            )
            has_alpha = False
            has_mask = False
            for i in range(1, ds.RasterCount + 1):
                band = ds.GetRasterBand(i)
                has_nodata = has_nodata or band.GetNoDataValue() is not None
                has_alpha = has_alpha or int(band.GetColorInterpretation()) == int(
                    gdal.GCI_AlphaBand
                )
                has_mask = has_mask or bool(
                    int(band.GetMaskFlags()) & int(gdal.GMF_PER_DATASET)
                )
            if has_alpha:
                has_mask = False
            return validate_output_format(
                out_format,
                dtype=str(dtype),
                bands=ds.RasterCount,
                has_nodata=has_nodata,
                has_alpha=has_alpha,
                has_mask=has_mask,
            )
        finally:
            ds = None

    def _execute_p402(
        self,
        spec: Any,
        context: Any,
        *,
        norm: dict[str, Any],
        out_format: str,
        target_path: Path,
        staged_path: Path,
        start_time: float,
    ) -> Any:
        """P4-02 能力（拼接/VRT/堆叠/波段管理）的执行分支。

        非 P4-02 能力返回 None，调用方继续走 Translate/Warp 分支。

        成果提交**完整复用**既有协议：收集全部 sidecar → 提交前真实重开校验
        → 按最终命名映射提交对 → 事务提交 → 提交后最终重开核验。
        输出元数据取自**实际重开**的成果，不采信处理函数返回的 info。
        """
        cid = spec.capability_id
        if cid not in P402_CAPABILITIES:
            return None

        sources = _extract_raster_inputs(spec)
        if not sources:
            context.log("ERROR", "P4-02 能力缺少输入数据集")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "input_not_found", "message": "任务未提供任何输入栅格"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        missing = [str(p) for p in sources if not p.exists()]
        if missing:
            context.log("ERROR", f"输入数据集不存在: {missing}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "input_not_found", "message": f"输入数据集不存在：{missing}"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        is_inplace = cid in INPLACE_CAPABILITIES
        protected_inputs = collect_protected_inputs(sources)
        format_violations = self._p402_format_violations(cid, sources, norm, out_format)
        if format_violations:
            context.log("ERROR", f"输出格式组合不受支持: {format_violations}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={
                    "code": "unsupported_format_combination",
                    "message": "；".join(format_violations),
                },
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        inplace_info: dict[str, Any] = {}
        calc_info: dict[str, Any] = {}
        if is_inplace:
            # in-place 前置校验：输出必须是输入自身；原地改必须显式允许覆盖。
            src = sources[0]
            if src.resolve() != target_path.resolve():
                context.log("ERROR", f"in-place 能力要求输出与输入为同一文件: {src} vs {target_path}")
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={"code": "invalid_output_plan",
                           "message": f"in-place 能力要求输出与输入为同一文件：{src} vs {target_path}"},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            overwrite = bool(spec.outputs[0].overwrite)
            if not overwrite:
                context.log("ERROR", "in-place 更新会覆盖源文件，必须显式 overwrite=true")
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={"code": "output_already_exists",
                           "message": "in-place 更新会覆盖源文件，必须显式 overwrite=true"},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            # 复制源文件集合（主文件 + sidecar）到 staging：改副本、成套提交，
            # 失败不影响源（多文件提交可恢复的验收落点）。
            err = self._stage_source_bundle(src, staged_path, context)
            if err is not None:
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.failed,
                    error={"code": "staging_invalid", "message": err},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            context.log("INFO", f"in-place：源文件集合已复制到 staging（{staged_path.name}）")

        # 输入兼容性预检：GDAL 对不兼容输入会**静默跳过**（混合 CRS 丢源、
        # 非 separate 跳过多波段源），必须先核对并明确拒绝。
        # BuildVRT 也要查：此前只查 merge/stack，导致 buildvrt 混合 CRS 仍 succeeded。
        if not is_inplace and cid in ("gdal:buildvrt", "gdalbuildvrt"):
            separate = bool(norm.get("separate", False))
            compat = check_source_compatibility(
                sources,
                require_same_bands=not separate,
                allow_ungeoreferenced=separate,
            )
        elif not is_inplace and cid in ("gdal:merge", "gdal_merge"):
            compat = check_source_compatibility(sources, require_same_bands=True)
        elif cid == "gdal:stack":
            # separate=True 时每个源的所有波段依次进入，波段数可以不同；
            # 实测无地理参考源在 separate=True 下仍可堆叠（不同于非 separate）。
            compat = check_source_compatibility(
                sources, require_same_bands=False, allow_ungeoreferenced=True
            )
        else:
            compat = {"compatible": True, "reason": None, "details": {}}
        if not compat["compatible"]:
            context.log("ERROR", f"输入源不兼容: {compat['reason']}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "incompatible_sources", "message": str(compat["reason"])},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        out_plan = spec.outputs[0]
        overwrite = out_plan.overwrite
        context.log(
            "INFO",
            f"能力 {cid}：{len(sources)} 个输入源 → {target_path.name}",
        )
        context.progress(0.10, "processing", "执行拼接 / VRT / 堆叠 / 波段管理 / 表达式计算")

        # 取消/进度回调：长计算阶段必须可取消（此前只在计算结束后检查）
        def _progress_cb(ratio: float, message: str, _data: Any = None) -> int:
            with contextlib.suppress(Exception):  # 进度回调异常不应中断处理
                context.progress(0.10 + 0.65 * float(ratio), "processing", message)
            return 0 if context.is_cancelled.is_set() else 1

        try:
            if is_inplace:
                if cid == "gdal:editmeta":
                    nodata = norm.get("srcNodata")
                    if nodata is None:
                        raise ValueError("editmeta 需要 srcNodata 参数（要写入的 NoData 值）")
                    hdr = staged_path.with_suffix(".hdr")
                    snap = snapshot_hdr_lines(hdr)
                    inplace_info.update(apply_nodata(staged_path, nodata))
                    # 读-合并-写：GDAL 重写 .hdr 会丢未知字段（实测），按快照补回
                    restored = restore_missing_hdr_fields(hdr, snap)
                    inplace_info["unknown_fields_restored"] = restored
                    context.log("INFO", f"NoData 已写入（未知字段补回 {restored} 条）")
                else:  # gdal:overview
                    levels = norm.get("levels")
                    if not levels:
                        raise ValueError("overview 需要 levels 参数（如 [2, 4, 8]）")
                    inplace_info.update(
                        build_overviews(
                            staged_path,
                            resampling=str(norm.get("resampleAlg", "NEAREST")),
                            levels=list(levels),
                            compress=norm.get("overviewCompress"),
                            callback=_progress_cb,
                        )
                    )
                    context.log(
                        "INFO",
                        f"概览已构建：{inplace_info.get('overview_count')} 级 "
                        f"（主影像尺寸契约保持 {inplace_info.get('width')}x{inplace_info.get('height')}）",
                    )
            elif cid in ("gdal:buildvrt", "gdalbuildvrt"):
                info = build_vrt(
                    staged_path,
                    sources,
                    overlap_priority=norm.get("overlapPriority", "last"),
                    resolution=norm.get("resolution"),
                    src_nodata=norm.get("srcNodata"),
                    vrt_nodata=norm.get("vrtNodata"),
                    separate=bool(norm.get("separate", False)),
                    callback=_progress_cb,
                )
                self._log_engine_warnings(context, info)
                # VRT 源引用按开关统一处理：
                # 相对模式 → 相对于**最终目标目录**（保证目录整体搬移后仍可打开）；
                # 绝对模式 → 主动改为绝对引用（不能指望引擎一定输出绝对路径）。
                use_relative = bool(norm.get("relativePaths", True))
                handled = set_vrt_source_paths(
                    staged_path, target_path.parent, use_relative=use_relative
                )
                context.log(
                    "INFO",
                    f"VRT 源引用已按最终目录重写（{handled} 条，"
                    f"模式={'相对' if use_relative else '绝对'}）",
                )
            elif cid in ("gdal:merge", "gdal_merge"):
                info = merge_sources(
                    staged_path,
                    sources,
                    overlap_priority=norm.get("overlapPriority", "last"),
                    resolution=norm.get("resolution"),
                    src_nodata=norm.get("srcNodata"),
                    vrt_nodata=norm.get("vrtNodata"),
                    dst_nodata=norm.get("dstNodata"),
                    x_res=norm.get("xRes"),
                    y_res=norm.get("yRes"),
                    output_bounds=norm.get("outputBounds"),
                    output_type=norm.get("outputType"),
                    out_format=out_format,
                    callback=_progress_cb,
                )
                self._log_engine_warnings(context, info)
            elif cid == "gdal:stack":
                info = stack_sources(
                    staged_path,
                    sources,
                    src_nodata=norm.get("srcNodata"),
                    vrt_nodata=norm.get("vrtNodata"),
                    x_res=norm.get("xRes"),
                    y_res=norm.get("yRes"),
                    output_bounds=norm.get("outputBounds"),
                    output_type=norm.get("outputType"),
                    out_format=out_format,
                    callback=_progress_cb,
                )
                self._log_engine_warnings(context, info)
            elif cid == "gdal:calc":
                expression = str(norm.get("calc") or "").strip()
                if not expression:
                    raise ValueError(
                        "gdal:calc 需要 calc 表达式参数（如 '(A-B)/(A+B)'）；"
                        "仅支持白名单运算符/函数与 A/B/C… 波段名"
                    )
                calc_info.update(
                    run_calc(
                        staged_path,
                        sources,
                        expression=expression,
                        src_nodata=norm.get("srcNodata"),
                        dst_nodata=float(norm.get("dstNodata", -9999.0)),
                        output_type=str(norm.get("outputType", "Float32")),
                        out_format=out_format,
                        callback=_progress_cb,
                    )
                )
                context.log(
                    "INFO",
                    f"表达式计算完成：输入无效 {calc_info.get('n_input_invalid')} 像元、"
                    f"数学无效（除零等）{calc_info.get('n_math_invalid')} 像元→输出 NoData",
                )
            else:  # gdal:bandmanage
                info = manage_bands(
                    staged_path,
                    sources[0],
                    band_list=norm.get("bandList"),
                    output_type=norm.get("outputType"),
                    out_format=out_format,
                    callback=_progress_cb,
                )
                self._log_engine_warnings(context, info)
        except ExpressionError as exc:
            # 表达式解析/白名单/输出语义错误：与引擎故障区分，便于用户直接改表达式
            context.log("ERROR", f"表达式被拒绝: {exc}")
            self._cleanup_staged_bundle(staged_path)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "invalid_expression", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        except (SourceUsageError, IncompatibleSourcesError) as exc:
            context.log("ERROR", f"输入源集合不完整: {exc}")
            self._cleanup_staged_bundle(staged_path)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "incompatible_sources", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        except NodataConflictError as exc:
            context.log("ERROR", f"输出 NoData 语义冲突: {exc}")
            self._cleanup_staged_bundle(staged_path)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "nodata_conflict", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        except Exception as exc:
            if context.is_cancelled.is_set():
                context.log("WARNING", f"任务 {spec.task_id} 在处理期间被取消")
                self._cleanup_staged_bundle(staged_path)
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.cancelled,
                    error={"code": "task_cancelled", "message": "Operation cancelled by user"},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            context.log("ERROR", f"P4-02 处理失败: {exc}")
            self._cleanup_staged_bundle(staged_path)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "p402_error", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        if context.is_cancelled.is_set():
            context.log("WARNING", "检测到取消请求，中止 P4-02 任务")
            self._cleanup_staged_bundle(staged_path)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.cancelled,
                error={"code": "task_cancelled", "message": "用户取消任务"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # ---- 成果完整性：收集 staging 内主文件与全部 sidecar ----
        context.progress(0.80, "collecting", "收集成果与伴生文件")
        try:
            ds = gdal.Open(str(staged_path), gdal.GA_ReadOnly)
            if ds is None:
                raise RuntimeError(f"无法重开 staging 成果：{staged_path}")
            driver = ds.GetDriver().ShortName
            del ds
            if not staged_path.exists() or staged_path.stat().st_size == 0:
                raise RuntimeError("staging 成果缺失或为空")

            staged_files = self._collect_staged_bundle(staged_path, driver=driver)
            for sf in staged_files:
                if not sf.exists() or sf.stat().st_size == 0:
                    raise RuntimeError(f"staging 成果文件缺失或为空：{sf}")

            if cid in ("gdal:buildvrt", "gdalbuildvrt"):
                expected_driver = "VRT"
            elif cid in INPLACE_CAPABILITIES or cid == "gdal:calc":
                # 就地更新/表达式计算的输出格式由源或表达式实现决定，
                # 以实际驱动为准；合并/堆叠等新建栅格则必须与请求格式一致。
                expected_driver = driver
            elif out_format.upper() == "COG":
                # GDAL COG 是 GTiff 容器，实际驱动名为 GTiff。
                expected_driver = "GTiff"
            else:
                expected_driver = out_format
            if driver.upper() != expected_driver.upper():
                raise RuntimeError(
                    f"staging 成果驱动不符：期望 {expected_driver}，实际 {driver}"
                )

            if driver.upper() == "ENVI":
                header = staged_path.with_suffix(".hdr")
                if not header.exists():
                    raise RuntimeError(
                        f"ENVI 头文件未与主文件同目录就绪：{header.name}"
                    )

            if driver.upper() == "VRT":
                # VRT 的源引用以**最终目标目录**为基准；在 commit 前按 final_dir
                # 解析并核对每个源存在，不能只凭 staging 位置打开。
                summary = assert_vrt_sources(
                    staged_path,
                    target_path.parent,
                    sources,
                    operation="提交前 VRT 成果",
                )
                context.log(
                    "INFO",
                    f"VRT 源引用按最终目录校验通过（{summary['source_count']} 条，"
                    f"相对 {summary['relative_count']} 条）",
                )

            expected = self._inspect_staged_for_commit(
                staged_path, target_path, driver=driver
            )
            if expected["driver"].upper() != expected_driver.upper():
                raise RuntimeError(
                    f"提交前重开驱动不符：期望 {expected_driver}，实际 {expected['driver']}"
                )
            actual_format_violations = self._validate_actual_output_format(
                expected, expected["driver"]
            )
            if actual_format_violations:
                raise RuntimeError(
                    "实际成果不符合输出格式矩阵："
                    + "；".join(actual_format_violations)
                )
            context.log(
                "INFO",
                f"staging 成果真实重开通过：{expected['width']}x{expected['height']}, "
                f"{expected['bands']} 波段, 驱动 {expected['driver']}, "
                f"样本像元已读取",
            )
        except Exception as verify_err:
            context.log("ERROR", f"staging 成果校验失败: {verify_err}")
            self._cleanup_staged_bundle(staged_path)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "verification_failed", "message": str(verify_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # ---- 构造完整提交对（主文件 + 全部 sidecar，按最终命名映射）----
        commit_pairs: list[tuple[Path, Path]] = []
        seen_targets: dict[Path, Path] = {}
        try:
            for sf in staged_files:
                tf = self._map_bundle_target(staged_path, sf, target_path)
                if tf in seen_targets:
                    raise RuntimeError(
                        f"sidecar 命名映射冲突：{seen_targets[tf].name} 与 {sf.name} "
                        f"都映射到 {tf.name}"
                    )
                seen_targets[tf] = sf
                commit_pairs.append((sf, tf))
        except Exception as map_err:
            context.log("ERROR", f"成果命名映射失败: {map_err}")
            self._cleanup_staged_bundle(staged_path)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "sidecar_mapping_failed", "message": str(map_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # ---- sidecar 所有权预检：输入碰撞拒绝；stale 交给提交事务备份/删除 ----
        new_targets = [tf for _, tf in commit_pairs]
        try:
            report = ensure_no_stale_sidecars(
                target_path,
                new_targets,
                protected_inputs=protected_inputs,
                allow_inplace=is_inplace,
                allow_stale_deletion=True,
            )
        except SidecarOwnershipError as sidecar_err:
            context.log("ERROR", f"sidecar 所有权预检失败: {sidecar_err}")
            self._cleanup_staged_bundle(staged_path)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={
                    "code": sidecar_err.code,
                    "message": str(sidecar_err),
                    "details": {
                        "stale": [str(p) for p in sidecar_err.stale],
                        "unknown": [str(p) for p in sidecar_err.unknown],
                        "protected": [str(p) for p in sidecar_err.protected],
                    },
                },
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        new_set = {p.resolve() for p in new_targets}
        stale_sidecars = [p for p in report.owned if p.resolve() not in new_set]
        if stale_sidecars:
            context.log(
                "INFO",
                "旧 sidecar 将随事务备份后删除："
                + ", ".join(p.name for p in stale_sidecars),
            )

        # ---- 事务提交全部成果文件 ----
        context.progress(0.92, "committing", "提交 staging 成果与伴生文件")
        try:
            committed_files = context.commit_files(
                commit_pairs,
                overwrite=overwrite,
                delete_paths=stale_sidecars,
                protected_paths=protected_inputs,
            )
        except OutputConflictError as conflict_err:
            context.log("ERROR", f"提交被拒（目标已存在）: {conflict_err}")
            self._cleanup_staged_bundle(staged_path)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "output_already_exists", "message": str(conflict_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        except Exception as commit_err:
            # 事务内部已回滚；staging/manifest 保留为可恢复证据，绝不反向回滚已 committed。
            context.log("ERROR", f"事务提交失败（保留 staging/manifest 供恢复）: {commit_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "commit_failed", "message": str(commit_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # ---- 提交后最终核验：真实重开目标文件，核对驱动/尺寸/波段/像元/mask ----
        context.progress(0.97, "verifying_final", "核验已安装目标")
        try:
            actual = inspect_raster(target_path)
            compare_rasters(expected, actual)
            if actual["driver"].upper() != expected_driver.upper():
                raise RuntimeError(
                    f"最终成果驱动不符：期望 {expected_driver}，实际 {actual['driver']}"
                )
            if actual["driver"].upper() == "VRT":
                assert_vrt_sources(
                    target_path,
                    target_path.parent,
                    sources,
                    operation="提交后最终 VRT 成果",
                )
            if actual["driver"].upper() == "ENVI":
                header = target_path.with_suffix(".hdr")
                if not header.exists():
                    raise RuntimeError(f"提交后 ENVI 头文件缺失：{header.name}")
            expected_targets = {tf.resolve() for _, tf in commit_pairs}
            committed_targets = {Path(p).resolve() for p in committed_files}
            missing_committed = expected_targets - committed_targets
            if missing_committed:
                raise RuntimeError(
                    f"提交返回的文件集合不完整，缺少：{[str(p) for p in sorted(missing_committed)]}"
                )
            final_w = actual["width"]
            final_h = actual["height"]
            final_bands = actual["bands"]
            final_driver = actual["driver"]
        except Exception as final_err:
            # 提交决断点已过：不反向回滚已生效事务，只如实报告失败。
            context.log("ERROR", f"最终成果重开核验失败: {final_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "final_reopen_failed", "message": str(final_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        duration = round(time.monotonic() - start_time, 4)
        context.progress(1.0, "completed", "P4-02 处理任务全部完成")
        context.log(
            "INFO",
            f"成果提交成功: {target_path} ({final_w}x{final_h}, {final_bands} 波段, "
            f"驱动 {final_driver})",
        )

        # 输出记录来自**实际重开的最终成果**，不采信处理函数返回的 info。
        output_record: dict[str, Any] = {
            "key": out_plan.key,
            "uri": target_path.as_uri(),
            "driver": final_driver,
            "width": final_w,
            "height": final_h,
            "bands": final_bands,
            "data_type": actual["types"][0] if actual["types"] else None,
            "nodata": actual["nodata"][0] if actual["nodata"] else None,
            "committed_files": [Path(p).as_uri() for p in committed_files],
        }
        if calc_info:
            # 表达式语义证据随成果返回（除零/无效区计数），供验收与日志核对
            output_record["calc"] = {
                "expression": calc_info.get("expression"),
                "band_names": calc_info.get("band_names"),
                "n_input_invalid": calc_info.get("n_input_invalid"),
                "n_math_invalid": calc_info.get("n_math_invalid"),
                "n_output_invalid": calc_info.get("n_output_invalid"),
                "dst_nodata": calc_info.get("out_nodata"),
                "dtype": calc_info.get("dtype"),
            }
        return TaskResult(
            task_id=spec.task_id,
            attempt=spec.attempt,
            status=TaskStatus.succeeded,
            outputs=[output_record],
            duration_seconds=duration,
        )

    def _execute_p404_statistics(
        self,
        spec: Any,
        context: Any,
        *,
        norm: dict[str, Any],
        in_path: Path | None,
        target_path: Path,
        staged_path: Path,
        start_time: float,
    ) -> Any:
        """P4-04 统计报告分支：写 JSON artifact，事务提交后**读回校验**。

        与栅格能力不同，报告不是数据集，因此不做 GDAL 重开；
        但同样走 staging → 目标存在性/覆盖检查 → 成套提交 → 最终读回，
        避免「报成功却没落盘」或「提交了半截文件」。
        """
        out_plan = spec.outputs[0]
        if in_path is None:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "input_not_found", "message": "统计能力缺少输入栅格"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        context.log(
            "INFO",
            f"统计模式={norm.get('statisticsMode', 'sampled')}、"
            f"直方图分箱={norm.get('histogramBins', 256)}、输入={in_path.name}",
        )
        context.progress(0.10, "processing", "计算直方图与基础统计")
        try:
            report = compute_statistics(
                in_path,
                mode=str(norm.get("statisticsMode", "sampled")),
                bins=int(norm.get("histogramBins", 256)),
                max_samples=int(norm.get("maxSamples", 1_000_000)),
                full_limit=int(norm.get("fullLimit", 50_000_000)),
                band_list=norm.get("bandList"),
                nodata=norm.get("srcNodata"),
            )
        except ValueError as exc:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "invalid_parameters", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        except Exception as exc:
            context.log("ERROR", f"统计计算失败: {exc}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "p404_statistics_error", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        if context.is_cancelled.is_set():
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.cancelled,
                error={"code": "task_cancelled", "message": "用户取消任务"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        try:
            write_statistics_report(report, staged_path)
        except Exception as exc:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "staging_invalid", "message": f"统计报告写入失败: {exc}"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        if not staged_path.exists() or staged_path.stat().st_size == 0:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "staging_invalid", "message": "staging 统计报告缺失或为空"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        context.progress(0.92, "committing", "提交统计报告")
        try:
            context.commit_files([(staged_path, target_path)], overwrite=out_plan.overwrite)
        except OutputConflictError as exc:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "output_already_exists", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        except Exception as exc:
            context.log("ERROR", f"统计报告提交失败: {exc}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "commit_failed", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # 最终核验：读回 JSON，确认模式与波段数没有被写坏
        context.progress(0.97, "verifying_final", "读回统计报告")
        try:
            loaded = json.loads(target_path.read_text(encoding="utf-8"))
            if loaded.get("mode") != report["mode"] or len(loaded.get("bands", [])) != len(
                report["bands"]
            ):
                raise RuntimeError("提交后的报告内容与计算结果不一致")
        except Exception as exc:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "final_reopen_failed", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        duration = round(time.monotonic() - start_time, 4)
        context.progress(1.0, "completed", "统计报告生成完成")
        context.log(
            "INFO",
            f"统计报告已提交: {target_path} "
            f"(mode={report['mode']}, source={report['source']}, stride={report['stride']}, "
            f"bands={len(report['bands'])})",
        )
        return TaskResult(
            task_id=spec.task_id,
            attempt=spec.attempt,
            status=TaskStatus.succeeded,
            outputs=[
                {
                    "key": out_plan.key,
                    "uri": target_path.as_uri(),
                    "driver": "JSON",
                    "kind": "artifact",
                    "width": report["width"],
                    "height": report["height"],
                    "bands": len(report["bands"]),
                    "statistics_mode": report["mode"],
                    "statistics_source": report["source"],
                    "histogram_bins": report["histogram_bins"],
                }
            ],
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # P5-02：矢量成果（polygonize / contour）
    # ------------------------------------------------------------------
    def _execute_vector_output(
        self,
        spec: TaskSpec,
        context: ExecutionContext,
        *,
        norm: dict[str, Any],
        in_path: Path,
        out_format: str,
        target_path: Path,
        staged_path: Path,
        start_time: float,
        callback: Any,
    ) -> TaskResult:
        """执行成果为**矢量**（OGR 数据集）的能力（P5-02）。

        与栅格主流程的关键差异（不能复用仅栅格的提交校验）：
        - 输出格式走 OGR 驱动矩阵（GPKG/GeoJSON），显式给非矢量格式必须拒绝；
        - 建数据源/图层/字段后由 ``gdal.Polygonize`` / ``gdal.ContourGenerate``
          写入要素，进度与取消经 GDAL callback 生效；
        - staging 校验与最终核验都用 ``inspect_vector`` 真实重开并逐层比对
          要素数——``gdal.Open`` 读不到矢量图层，绝不复用。
        """
        out_plan = spec.outputs[0]
        overwrite = out_plan.overwrite

        def _fail(code: str, message: str) -> TaskResult:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": code, "message": message},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # 1. 输出格式：显式 format/driver 必须是受支持的 OGR 驱动；未给时默认 GPKG。
        explicit_format = (
            str(spec.parameters.get("format") or "").strip()
            or str(out_plan.driver or "").strip()
        )
        effective_format = explicit_format or "GPKG"
        vector_violations = validate_vector_output_format(effective_format)
        if vector_violations:
            context.log("ERROR", f"矢量输出格式不受支持: {vector_violations}")
            return _fail(
                "unsupported_format_combination", "；".join(vector_violations)
            )
        ogr_driver_name = VECTOR_OUTPUT_FORMATS[
            normalize_output_format(effective_format)
        ]

        # 2. 能力级参数门禁（取值合法性已由 normalize_raster_params 保证）
        is_contour = spec.capability_id == "gdal:contour"
        is_polygonize = spec.capability_id == "gdal:polygonize"
        is_translate = spec.capability_id == "ogr:vectortranslate"
        if is_contour and not norm.get("interval") and not norm.get("fixedLevels"):
            return _fail(
                "invalid_parameters",
                "gdal:contour 需要正数 interval（等值线步长）或非空 fixedLevels（固定等级序列）",
            )

        layer_name = target_path.stem
        op_name = (
            "VectorTranslate"
            if is_translate
            else "ContourGenerate"
            if is_contour
            else "Polygonize"
        )

        # 3. 生成矢量成果（staging 内）
        try:
            # vectortranslate 的输入是矢量，其余两项输入是栅格
            open_flags = gdal.OF_VECTOR if is_translate else gdal.OF_RASTER
            src_ds = gdal.OpenEx(str(in_path), open_flags | gdal.OF_READONLY)
            if src_ds is None:
                kind_text = "矢量" if is_translate else "栅格"
                raise RuntimeError(f"无法作为{kind_text}打开输入: {in_path}")
            try:
                from osgeo import ogr

                if is_translate:
                    # 假重投影防护（GDAL 3.13.3 实测）：源图层无空间参考时，
                    # 即使 dstSRS + reproject=True 也**不会**重投影、只把输出
                    # SRS 标签写成目标 CRS——产出坐标原样、标签错误的假成果，
                    # 必须在写盘前明确拒绝。
                    dst_srs_value = norm.get("dstSRS")
                    if dst_srs_value:
                        for layer_idx in range(src_ds.GetLayerCount()):
                            src_lyr = src_ds.GetLayerByIndex(layer_idx)
                            if src_lyr is not None and src_lyr.GetSpatialRef() is None:
                                return _fail(
                                    "invalid_parameters",
                                    f"图层 {src_lyr.GetName()} 无空间参考，无法重投影到 "
                                    f"{dst_srs_value}；请先为源数据指定 CRS "
                                    "（ogr2ogr 在此场景只改 SRS 标签、不换算坐标）",
                                )
                    translate_kwargs: dict[str, Any] = {
                        "format": ogr_driver_name,
                    }
                    if dst_srs_value:
                        translate_kwargs["dstSRS"] = str(dst_srs_value)
                        # 实测：dstSRS 不配 reproject=True 只改 SRS 标签（假重投影）
                        translate_kwargs["reproject"] = True
                    if norm.get("sql"):
                        translate_kwargs["SQLStatement"] = str(norm["sql"])
                    if norm.get("where"):
                        translate_kwargs["where"] = str(norm["where"])
                    if norm.get("select"):
                        translate_kwargs["selectFields"] = [
                            str(v) for v in norm["select"]
                        ]
                    if norm.get("spat"):
                        # 轴序与单位：spatFilter 按源图层 CRS 解释（投影 CRS 为米）
                        translate_kwargs["spatFilter"] = tuple(
                            float(v) for v in norm["spat"]
                        )
                    vds = gdal.VectorTranslate(
                        str(staged_path),
                        src_ds,
                        options=gdal.VectorTranslateOptions(**translate_kwargs),
                        callback=callback,
                    )
                    if vds is None:
                        raise RuntimeError(
                            gdal.GetLastErrorMsg() or "VectorTranslate 失败"
                        )
                    # 显式释放句柄刷新到磁盘（FlatGeobuf 等驱动延迟落盘）
                    vds = None
                else:
                    band_index = int(norm.get("band") or 1)
                    if band_index < 1 or band_index > src_ds.RasterCount:
                        return _fail(
                            "invalid_parameters",
                            f"band 超出范围：{band_index}（数据集共 {src_ds.RasterCount} 个波段）",
                        )
                    band = src_ds.GetRasterBand(band_index)
                    ods = ogr.GetDriverByName(ogr_driver_name).CreateDataSource(
                        str(staged_path)
                    )
                    if ods is None:
                        raise RuntimeError(
                            f"无法创建矢量输出（驱动 {ogr_driver_name}）: {staged_path}"
                        )
                    try:
                        if is_polygonize:
                            field_name = str(norm.get("fieldName") or "DN")
                            layer = ods.CreateLayer(
                                layer_name, src_ds.GetSpatialRef(), ogr.wkbUnknown
                            )
                            layer.CreateField(
                                ogr.FieldDefn(field_name, ogr.OFTInteger)
                            )
                            connectedness = int(norm.get("connectedness") or 4)
                            options = ["8CONNECTED=YES"] if connectedness == 8 else []
                            # GDAL 3.13.3 实测：options 必须传列表（None 会被拒绝）
                            rc = gdal.Polygonize(
                                band, band.GetMaskBand(), layer, 0, options, callback
                            )
                            if rc != 0:
                                raise RuntimeError(
                                    gdal.GetLastErrorMsg()
                                    or f"Polygonize 失败（rc={rc}）"
                                )
                        else:
                            layer = ods.CreateLayer(
                                layer_name,
                                src_ds.GetSpatialRef(),
                                ogr.wkbLineString25D,
                            )
                            layer.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger64))
                            layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
                            nodata = band.GetNoDataValue()
                            use_no_data = nodata is not None
                            # GDAL 3.13.3 实测：第 4 参是 fixedLevels **序列**
                            # （docstring 的 fixedLevelCount 已被 typemap 折叠），
                            # 非空时**完全替代** interval/base 等差序列（interval=0 也可）；
                            # 传 [] 表示按 interval/base 生成
                            fixed_levels = [
                                float(v) for v in norm.get("fixedLevels") or []
                            ]
                            interval_value = (
                                float(norm["interval"]) if norm.get("interval") else 0.0
                            )
                            rc = gdal.ContourGenerate(
                                band,
                                interval_value,
                                float(norm.get("offset") or 0.0),
                                fixed_levels,
                                bool(use_no_data),
                                float(nodata) if use_no_data else 0.0,
                                layer,
                                0,
                                1,
                                callback,
                            )
                            if rc != 0:
                                raise RuntimeError(
                                    gdal.GetLastErrorMsg()
                                    or f"ContourGenerate 失败（rc={rc}）"
                                )
                    finally:
                        # 显式关闭矢量数据源，Windows 上释放文件锁并刷新到磁盘
                        ods = None
            finally:
                src_ds = None
        except Exception as exc:
            if context.is_cancelled.is_set():
                context.log(
                    "WARNING", f"Task {spec.task_id} was cancelled during {op_name}"
                )
                staged_path.unlink(missing_ok=True)
                return TaskResult(
                    task_id=spec.task_id,
                    attempt=spec.attempt,
                    status=TaskStatus.cancelled,
                    error={"code": "task_cancelled", "message": "Operation cancelled by user"},
                    duration_seconds=round(time.monotonic() - start_time, 4),
                )
            context.log("ERROR", f"GDAL {op_name} failed: {exc}")
            staged_path.unlink(missing_ok=True)
            return _fail(f"{op_name.lower()}_error", str(exc))

        if context.is_cancelled.is_set():
            staged_path.unlink(missing_ok=True)
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.cancelled,
                error={"code": "task_cancelled", "message": "Operation cancelled by user"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # 4. staging 成果 OGR 重开校验（真实读图层与要素，不只是文件存在）
        context.progress(0.9, "verifying", "Re-opening staged vector output for validation")
        try:
            staged_info = inspect_vector(staged_path)
        except Exception as verify_err:
            context.log("ERROR", f"Verification of staged vector output failed: {verify_err}")
            staged_path.unlink(missing_ok=True)
            return _fail("verification_failed", str(verify_err))
        if staged_info["layer_count"] < 1:
            staged_path.unlink(missing_ok=True)
            return _fail("verification_failed", "staging 矢量成果不含任何图层")

        # 5. 构造提交对：GPKG/GeoJSON 是单文件；ESRI Shapefile 是
        #    .shp/.shx/.dbf/.prj(/.cpg) 多文件 bundle（第 53 轮实测清单）。
        if ogr_driver_name == "ESRI Shapefile":
            staged_bundle = shapefile_sidecar_paths(staged_path)
        else:
            staged_bundle = []
        commit_pairs: list[tuple[Path, Path]] = [(staged_path, target_path)]
        for sidecar in staged_bundle:
            commit_pairs.append((sidecar, target_path.with_suffix(sidecar.suffix)))

        # 5.5 sidecar 所有权预检：输入碰撞拒绝；旧成果残留 sidecar 随事务备份后删除
        #（覆盖已有 Shapefile 时旧 .shx/.dbf 与新成果不配对会污染图层，必须清掉）
        new_targets = [tf for _, tf in commit_pairs]
        protected_inputs = collect_protected_inputs([in_path]) if in_path else set()
        try:
            sidecar_report = ensure_no_stale_sidecars(
                target_path,
                new_targets,
                protected_inputs=protected_inputs,
                allow_stale_deletion=True,
            )
        except SidecarOwnershipError as sidecar_err:
            context.log("ERROR", f"sidecar 所有权预检失败: {sidecar_err}")
            return _fail(
                sidecar_err.code,
                str(sidecar_err),
            )
        new_target_set = {p.resolve() for p in new_targets}
        stale_sidecars = [
            p for p in sidecar_report.owned if p.resolve() not in new_target_set
        ]
        if stale_sidecars:
            context.log(
                "INFO",
                "旧 sidecar 将随事务备份后删除："
                + ", ".join(p.name for p in stale_sidecars),
            )

        # 6. 事务提交
        context.progress(0.95, "committing", "Committing staged vector output to destination")
        try:
            committed_files = context.commit_files(
                commit_pairs,
                overwrite=overwrite,
                delete_paths=stale_sidecars,
                protected_paths=protected_inputs,
            )
        except OutputConflictError as conflict_err:
            context.log("ERROR", f"Commit rejected due to existing target: {conflict_err}")
            return _fail("output_already_exists", str(conflict_err))
        except Exception as commit_err:
            context.log("ERROR", f"Transaction commit failed: {commit_err}")
            return _fail("commit_failed", str(commit_err))

        # 7. 最终核验：target 再次 OGR 重开，图层数与要素数必须与 staging 一致
        try:
            final_info = inspect_vector(target_path, sample=False)
        except Exception as final_err:
            context.log("ERROR", f"Final vector reopen check failed: {final_err}")
            return _fail("final_reopen_failed", str(final_err))
        staged_counts = [
            int(layer.get("feature_count") or 0)
            for layer in staged_info["layers"]
            if "feature_count" in layer
        ]
        final_counts = [
            int(layer.get("feature_count") or 0)
            for layer in final_info["layers"]
            if "feature_count" in layer
        ]
        if (
            staged_info["layer_count"] != final_info["layer_count"]
            or sorted(staged_counts) != sorted(final_counts)
        ):
            message = (
                f"提交前后矢量成果不一致：layers {staged_info['layer_count']}→"
                f"{final_info['layer_count']}，features {sorted(staged_counts)}→"
                f"{sorted(final_counts)}"
            )
            context.log("ERROR", message)
            return _fail("final_reopen_failed", message)

        duration = round(time.monotonic() - start_time, 4)
        context.progress(1.0, "completed", "Vector output completed successfully")
        first_layer = (final_info.get("layers") or [{}])[0]
        context.log(
            "INFO",
            f"Task {spec.task_id} completed in {duration}s -> {target_path} "
            f"({final_info.get('driver')}, {final_info['layer_count']} layer(s), "
            f"{first_layer.get('feature_count', 0)} feature(s))",
        )
        output_record = {
            "key": out_plan.key,
            "uri": target_path.as_uri(),
            "data_type": "vector",
            "driver": final_info.get("driver") or ogr_driver_name,
            "layer_count": final_info.get("layer_count", 0),
            "feature_count": first_layer.get("feature_count", 0),
            "geometry_type": first_layer.get("geometry_type"),
            "committed_files": [f.as_uri() for f in committed_files],
        }
        return TaskResult(
            task_id=spec.task_id,
            attempt=spec.attempt,
            status=TaskStatus.succeeded,
            outputs=[output_record],
            duration_seconds=duration,
            engine_runtime={
                "provider_id": "gdal",
                "version": gdal.__version__,
            },
        )

    def _stage_source_bundle(self, source: Path, staged_path: Path, context: Any) -> str | None:
        """把源文件及其 sidecar 复制进 staging（in-place 更新的事务副本）。

        主文件复制为 staged_path；sidecar 按「staged 主文件名 + 相对后缀」命名，
        使既有 commit 映射规则（.hdr / .aux.xml / tail）无需修改即可工作。
        返回 None 表示成功，否则返回错误消息。
        """
        import shutil as _shutil

        if gdal is None:
            return "GDAL 不可用，无法收集源文件集合"
        try:
            ds = gdal.Open(str(source), gdal.GA_ReadOnly)
            raw = ds.GetFileList() if ds is not None else []
            del ds
        except Exception as exc:
            return f"收集源文件集合失败：{exc}"

        files: list[Path] = [Path(f).resolve() for f in raw]
        if source.resolve() not in files:
            files.insert(0, source.resolve())
        files = [f for f in files if f.exists()]

        src_name = source.name
        for f in files:
            if f == source.resolve():
                dst = staged_path
            else:
                if f.name.startswith(src_name):
                    # 同主名扩展形态（如 .dat.aux.xml、.dat.ovr）：保留 tail
                    tail = f.name[len(src_name):]
                    dst = staged_path.parent / f"{staged_path.name}{tail}"
                elif f.stem == source.stem and f.suffix:
                    # 同 stem 不同后缀（ENVI 的 .dat 对 .hdr）：后缀对齐 staged 主名。
                    # 实测教训：ENVI sidecar 是 legacy.hdr（不以 legacy.dat 开头），
                    # 若按原名落到 staging，GDAL 打开 staged 主文件时找不到同名 .hdr
                    # 会报 not recognized as being in a supported file format。
                    dst = staged_path.with_suffix(f.suffix)
                else:
                    dst = staged_path.parent / f.name
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                _shutil.copy2(f, dst)
            except Exception as exc:
                return f"复制 {f.name} 到 staging 失败：{exc}"
        context.log("INFO", f"staging 副本就绪：{len(files)} 个文件")
        return None

    def probe_dataset(self, path: Path | str) -> dict[str, Any]:
        """探测并返回数据集详细元数据（零全量内存加载）。"""
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用，无法探测数据集")
        in_path = _resolve_raster_input(Path(path))
        if not in_path.exists():
            raise FileNotFoundError(f"数据集文件不存在: {in_path}")
        ds = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开栅格数据集: {in_path}")
        try:
            w = int(ds.RasterXSize)
            h = int(ds.RasterYSize)
            count = int(ds.RasterCount)
            driver_name = ds.GetDriver().ShortName if ds.GetDriver() else "Unknown"
            driver_long = ds.GetDriver().LongName if ds.GetDriver() else ""
            if driver_name == "VRT":
                # VRT 元数据可脱离源打开，但像元读取会失败。桌面打开时必须
                # 明确报告失效的相对引用，而不是登记一个看似正常的空图层。
                missing: list[str] = []
                in_resolved = in_path.resolve()
                for raw_source in ds.GetFileList() or []:
                    text = str(raw_source)
                    lowered = text.lower()
                    if lowered.startswith(
                        ("/vsi", "http://", "https://", "s3://", "gs://", "az://", "zip://", "tar://")
                    ):
                        continue
                    source_path = Path(text)
                    try:
                        if source_path.resolve() == in_resolved:
                            continue
                    except OSError:
                        pass
                    if not source_path.exists():
                        missing.append(text)
                if missing:
                    raise FileNotFoundError(
                        "VRT 引用的源文件不存在: " + ", ".join(missing)
                    )
            gt = ds.GetGeoTransform(can_return_null=True)
            geotransform = [float(x) for x in gt] if gt else None
            # 范围按四个像元外边界角点计算，旋转/倾斜仿射下不会漏角
            bounds = _affine_bounds(geotransform, w, h)

            crs_epsg: int | None = None
            crs_wkt: str | None = None
            srs = ds.GetSpatialRef()
            if srs is not None:
                code_str = srs.GetAuthorityCode(None)
                if code_str and str(code_str).isdigit():
                    crs_epsg = int(code_str)
                crs_wkt = srs.ExportToWkt()

            bands_info: list[dict[str, Any]] = []
            overview_levels: list[int] = []
            for b_idx in range(1, count + 1):
                b = ds.GetRasterBand(b_idx)
                dt_name = gdal.GetDataTypeName(b.DataType)
                nd = b.GetNoDataValue()
                ci = gdal.GetColorInterpretationName(b.GetColorInterpretation())
                flags = int(b.GetMaskFlags())
                scale = b.GetScale()
                offset = b.GetOffset()
                unit = b.GetUnitType() or None
                desc = b.GetDescription() or None
                ov_list = []
                for i in range(int(b.GetOverviewCount())):
                    ov = b.GetOverview(i)
                    if ov is not None and ov.XSize:
                        ov_list.append({"level": i, "width": int(ov.XSize), "height": int(ov.YSize)})
                if b_idx == 1:
                    # 层级 = 相对全幅的抽取倍数（2/4/8…），不是概览的像素宽度
                    overview_levels = [int(round(w / x["width"])) for x in ov_list if x["width"]]
                bands_info.append({
                    "band": b_idx,
                    "dtype": dt_name,
                    "nodata": float(nd) if nd is not None else None,
                    "color_interp": ci,
                    "overviews": int(b.GetOverviewCount()),
                    "overview_sizes": ov_list,
                    "mask_flags": flags,
                    "has_alpha": bool(flags & gdal.GMF_ALPHA),
                    "all_valid": bool(flags & gdal.GMF_ALL_VALID),
                    "has_nodata_flag": bool(flags & gdal.GMF_NODATA),
                    "scale": float(scale) if scale is not None else None,
                    "offset": float(offset) if offset is not None else None,
                    "unit": unit,
                    "description": desc,
                })

            # 空间模型：区分 affine / GCP / 无地理参考，避免把默认值当成地理参考
            gcp_count = int(ds.GetGCPCount())
            gcp_crs = None
            if gcp_count:
                gsp = ds.GetGCPSpatialRef()
                gcp_crs = gsp.ExportToWkt() if gsp is not None else None
            spatial_model = "none"
            if geotransform:
                spatial_model = "affine"
            elif gcp_count:
                spatial_model = "gcp"

            return {
                "path": str(in_path),
                "driver": driver_name,
                "driver_long_name": driver_long,
                "width": w,
                "height": h,
                "count": count,
                "dtypes": [b["dtype"] for b in bands_info],
                "nodata": bands_info[0]["nodata"] if bands_info else None,
                "crs_epsg": crs_epsg,
                "crs_wkt": crs_wkt,
                "geotransform": geotransform,
                "has_geotransform": bool(geotransform),
                "spatial_model": spatial_model,
                "gcp_count": gcp_count,
                "gcp_crs_wkt": gcp_crs,
                "bounds": bounds,
                "overview_levels": overview_levels,
                "units": [b["unit"] for b in bands_info],
                "band_names": [b["description"] for b in bands_info],
                "bands": bands_info,
            }
        finally:
            del ds

    # ------------------------------------------------------- P3-02 矢量元数据
    def probe_vector(self, path: Path | str) -> dict[str, Any]:
        """探测矢量数据集的基础元数据（P3-02）。

        只读取结构信息，不做全表扫描：
        - 图层名称、几何类型、CRS、范围；
        - 字段名称 / 类型 / 可空性。

        要素数与范围若驱动不支持或需昂贵扫描，返回 None 并在
        ``feature_count_expensive`` / ``extent_available`` 中如实标注，
        绝不返回编造数值。
        """
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用，无法探测矢量数据集")
        try:
            from osgeo import ogr
        except ImportError as exc:  # pragma: no cover - 引擎环境缺失
            raise RuntimeError(f"OGR 运行时不可用：{exc}") from exc

        in_path = Path(path)
        if not in_path.exists():
            raise FileNotFoundError(f"矢量数据集不存在: {in_path}")

        ds = ogr.Open(str(in_path))
        if ds is None:
            raise RuntimeError(f"无法打开矢量数据集: {in_path}")

        layers: list[dict[str, Any]] = []
        try:
            layer_count = int(ds.GetLayerCount())
            for idx in range(layer_count):
                lyr = ds.GetLayer(idx)
                if lyr is None:
                    continue
                layers.append(self._describe_vector_layer(lyr, idx))
        finally:
            ds = None

        if not layers:
            raise RuntimeError(f"矢量数据集不含任何图层: {in_path}")

        driver_name = ""
        driver_long = ""
        try:
            # ogr.Open 后无法直接取驱动名时，退回用 gdal 打开同名栅格探测（多数驱动同名）
            gdal_ds = gdal.Open(str(in_path))
            if gdal_ds is not None:
                drv = gdal_ds.GetDriver()
                if drv is not None:
                    driver_name = drv.ShortName
                    driver_long = drv.LongName
                gdal_ds = None
        except Exception:
            driver_name = ""

        return {
            "path": str(in_path),
            "kind": "vector",
            "driver": driver_name,
            "driver_long_name": driver_long,
            "layer_count": len(layers),
            "layers": layers,
        }

    @staticmethod
    def _describe_vector_layer(lyr: Any, index: int) -> dict[str, Any]:
        """描述单个矢量图层：名称、几何类型、CRS、范围、字段。"""
        try:
            from osgeo import ogr
        except ImportError:  # pragma: no cover - 引擎环境缺失
            ogr = None

        name = lyr.GetName() or f"layer_{index}"

        geom_type_name = ""
        try:
            if ogr is not None:
                geom_type_name = ogr.GeometryTypeToName(lyr.GetGeomType()) or ""
        except Exception:
            geom_type_name = ""

        crs_epsg: int | None = None
        crs_wkt: str | None = None
        try:
            srs = lyr.GetSpatialRef()
            if srs is not None:
                code = srs.GetAuthorityCode(None)
                if code and str(code).isdigit():
                    crs_epsg = int(code)
                crs_wkt = srs.ExportToWkt()
        except Exception:
            pass

        # 范围：驱动不支持时返回 None，不伪造
        extent: list[float] | None = None
        try:
            ext = lyr.GetExtent(force=0)  # 0 = 允许驱动使用已缓存/快速计算
            if ext is not None:
                vals = [float(v) for v in ext]
                if len(vals) == 4 and all(v == v for v in vals):  # 排除 NaN
                    extent = vals
        except Exception:
            extent = None

        # 要素数：必须显式 force=0。
        # ogr.Layer.GetFeatureCount 的默认形参是 force=1，驱动在计数昂贵时
        # 会为此触发全表扫描——那正是本项要避免的。传 force=0 时驱动只返回
        # 已缓存的廉价计数；取不到就如实标记为「昂贵/未知」，不代为发起扫描。
        feature_count: int | None = None
        feature_count_expensive = False
        try:
            fc = lyr.GetFeatureCount(force=0)
            if fc is not None and int(fc) >= 0:
                feature_count = int(fc)
            else:
                feature_count_expensive = True
        except Exception:
            feature_count = None
            feature_count_expensive = True

        fields: list[dict[str, Any]] = []
        try:
            defn = lyr.GetLayerDefn()
            for i in range(int(defn.GetFieldCount())):
                fdef = defn.GetFieldDefn(i)
                nullable: bool | None = None
                # GDAL 3.13 的 Python 绑定没有 GetNullable()，只有 IsNullable()
                if hasattr(fdef, "IsNullable"):
                    try:
                        nullable = bool(fdef.IsNullable())
                    except Exception:
                        nullable = None
                fields.append(
                    {
                        "name": fdef.GetName(),
                        "type": fdef.GetTypeName(),
                        "type_code": int(fdef.GetType()),
                        "nullable": nullable,
                    }
                )
        except Exception:
            fields = []

        return {
            "index": index,
            "name": name,
            "geometry_type": geom_type_name,
            "crs_epsg": crs_epsg,
            "crs_wkt": crs_wkt,
            "extent": extent,
            "extent_available": extent is not None,
            "feature_count": feature_count,
            "feature_count_expensive": feature_count_expensive,
            "field_count": len(fields),
            "fields": fields,
        }

    # ----------------------------------------------------- P5-03 分页属性表
    #: 单页读取的要素数上限：属性表分页服务的硬上限，
    #: 拒绝「一页拿全表」的调用，从接口上杜绝一次性全读。
    MAX_VECTOR_PAGE_SIZE = 1000

    def read_vector_page(
        self,
        path: Path | str,
        layer_index: int = 0,
        start: int = 0,
        count: int = 100,
    ) -> dict[str, Any]:
        """按范围读取矢量图层的一页要素（P5-03 分页属性表）。

        - **按范围查询**：``SetNextByIndex(start)`` 后只读 ``count`` 个要素，
          本方法绝不迭代全表，也不主动触发全表统计；
        - **廉价计数**：``GetFeatureCount(force=0)``，驱动拿不到廉价计数时
          如实返回 ``feature_count_expensive=True`` 与 ``feature_count=None``，
          绝不代为发起全表扫描；
        - **空几何 / 多部件 / 编码**：逐要素如实标注几何类型、是否空几何与
          部件数；字段值由 GDAL 按数据集声明编码（如 .cpg）解码为 Python str。
        """
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用，无法读取矢量属性")
        try:
            from osgeo import ogr
        except ImportError as exc:  # pragma: no cover - 引擎环境缺失
            raise RuntimeError(f"OGR 运行时不可用：{exc}") from exc

        in_path = Path(path)
        if not in_path.exists():
            raise FileNotFoundError(f"矢量数据集不存在: {in_path}")
        layer_index = int(layer_index)
        start = int(start)
        count = int(count)
        if layer_index < 0:
            raise ValueError(f"layer_index 必须 ≥ 0，实际 {layer_index}")
        if start < 0:
            raise ValueError(f"start 必须 ≥ 0，实际 {start}")
        if count < 1:
            raise ValueError(f"count 必须 ≥ 1，实际 {count}")
        if count > self.MAX_VECTOR_PAGE_SIZE:
            raise ValueError(
                f"单页 count={count} 超过上限 {self.MAX_VECTOR_PAGE_SIZE}："
                "属性表必须分页读取，不允许一次性取全表"
            )

        ds = ogr.Open(str(in_path))
        if ds is None:
            raise RuntimeError(f"无法打开矢量数据集: {in_path}")
        try:
            layer_count = int(ds.GetLayerCount())
            if layer_index >= layer_count:
                raise ValueError(
                    f"layer_index={layer_index} 超出范围（数据集共 {layer_count} 个图层）"
                )
            lyr = ds.GetLayerByIndex(layer_index)
            if lyr is None:
                raise RuntimeError(f"图层 {layer_index} 不可用")
            defn = lyr.GetLayerDefn()
            fields: list[dict[str, Any]] = []
            for f_idx in range(defn.GetFieldCount()):
                fdef = defn.GetFieldDefn(f_idx)
                nullable: bool | None = None
                try:
                    nullable = bool(fdef.IsNullable())
                except Exception:
                    nullable = None
                fields.append(
                    {
                        "name": fdef.GetName(),
                        "type": fdef.GetTypeName(),
                        "type_code": int(fdef.GetType()),
                        "nullable": nullable,
                    }
                )
            field_names = [f["name"] for f in fields]

            # 廉价计数：force=0 只取驱动缓存/头信息，绝不发起全表扫描
            feature_count: int | None = None
            feature_count_expensive = True
            try:
                fc = lyr.GetFeatureCount(force=0)
                if fc is not None and int(fc) >= 0:
                    feature_count = int(fc)
                    feature_count_expensive = False
            except Exception:
                feature_count = None
                feature_count_expensive = True

            features: list[dict[str, Any]] = []
            # SetNextByIndex 语义由驱动保证（GPKG 走 SQL LIMIT/OFFSET，
            # Shapefile 顺序跳转）；这里只保证本方法**读取**的要素数 ≤ count。
            # OGR 返回码 0 = OGRERR_NONE（成功），非 0 才是失败
            if lyr.SetNextByIndex(start) != 0:
                raise RuntimeError(f"无法定位到要素 {start}（SetNextByIndex 失败）")
            for _ in range(count):
                feat = lyr.GetNextFeature()
                if feat is None:
                    break
                geom = feat.GetGeometryRef()
                geom_type = None
                is_empty = True
                part_count = 0
                if geom is not None:
                    geom_type = ogr.GeometryTypeToName(geom.GetGeometryType())
                    is_empty = bool(geom.IsEmpty())
                    part_count = int(geom.GetGeometryCount())
                values: dict[str, Any] = {}
                for name in field_names:
                    values[name] = feat.GetField(name)
                features.append(
                    {
                        "fid": int(feat.GetFID()),
                        "geometry_type": geom_type,
                        "empty_geometry": is_empty,
                        "part_count": part_count,
                        "fields": values,
                    }
                )

            srs = lyr.GetSpatialRef()
            crs_epsg: int | None = None
            if srs is not None:
                code = srs.GetAuthorityCode(None)
                if code and str(code).isdigit():
                    crs_epsg = int(code)

            return {
                "path": str(in_path),
                "layer_index": layer_index,
                "layer_name": lyr.GetName(),
                "layer_count": layer_count,
                "driver": ds.GetDriver().GetName() if ds.GetDriver() else None,
                "crs_epsg": crs_epsg,
                "start": start,
                "requested_count": count,
                "returned_count": len(features),
                "feature_count": feature_count,
                "feature_count_expensive": feature_count_expensive,
                "fields": fields,
                "features": features,
            }
        finally:
            ds = None

    # ----------------------------------------------------- P3-02 子数据集发现
    def list_subdatasets(self, path: Path | str) -> dict[str, Any]:
        """列出容器数据集的子数据集（P3-02）。

        子数据集 URI 由 GDAL 返回，形如 ``NETCDF:"file.nc":Band1``，
        它**不是普通文件路径**：这里原样保留 ``uri``，绝不做 Path 解析或本地
        存在性判断（那会把协议前缀与变量名当成路径的一部分而误判为不存在）。
        """
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用，无法枚举子数据集")
        in_path = Path(path)
        if not in_path.exists():
            raise FileNotFoundError(f"数据集不存在: {in_path}")

        ds = gdal.Open(str(in_path))
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            raw = ds.GetSubDatasets()
        finally:
            ds = None

        entries: list[dict[str, Any]] = []
        for item in raw or []:
            if not item:
                continue
            uri = str(item[0])
            desc = str(item[1]) if len(item) > 1 else ""
            entries.append({"uri": uri, "description": desc})

        return {
            "path": str(in_path),
            "subdataset_count": len(entries),
            "subdatasets": entries,
        }

    def probe_subdataset(self, uri: str) -> dict[str, Any]:
        """探测单个子数据集（P3-02）。

        ``uri`` 是 GDAL 返回的数据源引用（可能是 ``NETCDF:"file.nc":Band1`` 这类
        协议 URI），必须原样交给 GDAL 打开，不能当普通路径处理。
        """
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用，无法探测子数据集")
        if not uri or not str(uri).strip():
            raise ValueError("子数据集 URI 不得为空")

        ds = gdal.Open(str(uri))
        if ds is None:
            raise RuntimeError(f"无法打开子数据集: {uri}")
        try:
            width = int(ds.RasterXSize)
            height = int(ds.RasterYSize)
            count = int(ds.RasterCount)
            driver_name = ds.GetDriver().ShortName if ds.GetDriver() else "Unknown"

            gt = ds.GetGeoTransform(can_return_null=True)
            geotransform = [float(x) for x in gt] if gt else None
            # 范围按四个像元外边界角点计算（与栅格探测复用同一算法）
            bounds = _affine_bounds(geotransform, width, height)

            crs_epsg: int | None = None
            crs_wkt: str | None = None
            srs = ds.GetSpatialRef()
            if srs is not None:
                code = srs.GetAuthorityCode(None)
                if code and str(code).isdigit():
                    crs_epsg = int(code)
                crs_wkt = srs.ExportToWkt()

            bands: list[dict[str, Any]] = []
            for b in range(1, count + 1):
                band = ds.GetRasterBand(b)
                nd = band.GetNoDataValue()
                bands.append(
                    {
                        "band": b,
                        "dtype": gdal.GetDataTypeName(band.DataType),
                        "nodata": float(nd) if nd is not None else None,
                        "description": band.GetDescription() or None,
                    }
                )

            return {
                "uri": str(uri),
                "kind": "subdataset",
                "driver": driver_name,
                "width": width,
                "height": height,
                "count": count,
                "dtypes": [b["dtype"] for b in bands],
                "nodata": bands[0]["nodata"] if bands else None,
                "crs_epsg": crs_epsg,
                "crs_wkt": crs_wkt,
                "geotransform": geotransform,
                "bounds": bounds,
                "bands": bands,
            }
        finally:
            ds = None

    def read_window(
        self,
        path: Path | str,
        x_off: int = 0,
        y_off: int = 0,
        win_w: int = 512,
        win_h: int = 512,
        out_w: int = 512,
        out_h: int = 512,
        bands: list[int] | None = None,
        raw: bool = False,
        overview: str = "auto",
    ) -> dict[str, Any]:
        """后台分块读取局部窗口并抽样（禁止整幅读入）。

        raw=False（默认）：旧行为，返回已 min-max 拉伸的 uint8 显示字节。
        raw=True：返回原始 dtype 数组 + 有效性位图 + 每波段 NoData/scale/offset，
                  拉伸与伪彩由 GUI 侧完成（原值/物理值/显示值分离）。
        """
        if raw:
            return self._read_window_raw(path, x_off, y_off, win_w, win_h, out_w, out_h, bands, overview)

        import base64

        import numpy as np

        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用")
        in_path = _resolve_raster_input(Path(path))
        if not in_path.exists():
            raise FileNotFoundError(f"数据集文件不存在: {in_path}")
        ds = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            total_w = int(ds.RasterXSize)
            total_h = int(ds.RasterYSize)
            total_bands = int(ds.RasterCount)

            # 窗口裁剪保护
            x_off = max(0, min(x_off, total_w - 1))
            y_off = max(0, min(y_off, total_h - 1))
            win_w = max(1, min(win_w, total_w - x_off))
            win_h = max(1, min(win_h, total_h - y_off))
            out_w = max(1, min(out_w, 2048))
            out_h = max(1, min(out_h, 2048))

            target_bands = bands if bands else ([1] if total_bands == 1 else [1, 2, 3][:total_bands])
            band_arrays: list[np.ndarray] = []
            stats: list[dict[str, Any]] = []

            for b_idx in target_bands:
                if b_idx < 1 or b_idx > total_bands:
                    continue
                band_obj = ds.GetRasterBand(b_idx)
                # 使用 GDAL 底层抽样读取指定局部窗口，绝不全量载入
                arr = band_obj.ReadAsArray(
                    x_off, y_off, win_w, win_h, buf_xsize=out_w, buf_ysize=out_h
                )
                if arr is None:
                    arr = np.zeros((out_h, out_w), dtype=np.float32)
                else:
                    arr = arr.astype(np.float32)

                nd = band_obj.GetNoDataValue()
                valid_mask = arr != nd if nd is not None else np.isfinite(arr)

                if np.any(valid_mask):
                    b_min = float(np.min(arr[valid_mask]))
                    b_max = float(np.max(arr[valid_mask]))
                else:
                    b_min, b_max = 0.0, 1.0

                stats.append({"band": b_idx, "min": b_min, "max": b_max})

                # min-max 线性拉伸为 0~255 uint8 供渲染
                if b_max > b_min:
                    stretched = (arr - b_min) / (b_max - b_min) * 255.0
                    stretched = np.clip(stretched, 0, 255).astype(np.uint8)
                else:
                    stretched = np.zeros_like(arr, dtype=np.uint8)
                band_arrays.append(stretched)

            if not band_arrays:
                final_arr = np.zeros((out_h, out_w), dtype=np.uint8)
                is_rgb = False
            elif len(band_arrays) == 1:
                final_arr = band_arrays[0]
                is_rgb = False
            else:
                while len(band_arrays) < 3:
                    band_arrays.append(band_arrays[-1])
                final_arr = np.stack(band_arrays[:3], axis=-1)
                is_rgb = True

            raw_bytes = final_arr.tobytes()
            b64_str = base64.b64encode(raw_bytes).decode("ascii")

            return {
                "x_off": x_off,
                "y_off": y_off,
                "win_w": win_w,
                "win_h": win_h,
                "out_w": out_w,
                "out_h": out_h,
                "is_rgb": is_rgb,
                "bands": target_bands,
                "b64_data": b64_str,
                "stats": stats,
            }
        finally:
            del ds

    # ------------------------------------------------------------------
    # P3-05 坐标查询 / 统计（全部在 worker 内完成，GUI 不碰原生库）
    # ------------------------------------------------------------------

    def _read_window_raw(
        self,
        path: Path | str,
        x_off: int,
        y_off: int,
        win_w: int,
        win_h: int,
        out_w: int,
        out_h: int,
        bands: list[int] | None,
        overview: str = "auto",
    ) -> dict[str, Any]:
        """读取原始像元值 + 有效性位图，并按需选择概览层。

        概览选择是显式的：只有当需要的降采样比 >= 概览比、且概览仍不比目标输出更粗时才使用它，
        并在响应里报告实际使用的概览层，便于核对「用了概览而不是偷偷全量读」。
        """
        import base64

        import numpy as np

        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用")
        in_path = _resolve_raster_input(Path(path))
        if not in_path.exists():
            raise FileNotFoundError(f"数据集文件不存在: {in_path}")
        ds = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            total_w = int(ds.RasterXSize)
            total_h = int(ds.RasterYSize)
            total_bands = int(ds.RasterCount)

            x_off = max(0, min(int(x_off), total_w - 1))
            y_off = max(0, min(int(y_off), total_h - 1))
            win_w = max(1, min(int(win_w), total_w - x_off))
            win_h = max(1, min(int(win_h), total_h - y_off))
            out_w = max(1, min(int(out_w), 2048))
            out_h = max(1, min(int(out_h), 2048))

            target_bands = bands if bands else list(range(1, min(3, total_bands) + 1))
            target_bands = [b for b in target_bands if 1 <= b <= total_bands]
            if not target_bands:
                raise ValueError(f"请求波段 {bands} 全部越界（数据集有 {total_bands} 个波段）")

            # 1) 概览层选择
            needed = max(win_w / out_w, win_h / out_h)
            ov_level = -1
            ov_ratio = 1.0
            base_band = ds.GetRasterBand(1)
            if needed > 1.01 and overview == "auto":
                best: tuple[int, float] | None = None
                for i in range(int(base_band.GetOverviewCount())):
                    ov = base_band.GetOverview(i)
                    if ov is None or not ov.XSize:
                        continue
                    ratio = float(total_w) / float(ov.XSize)
                    if ratio <= needed + 1e-9 and (best is None or ratio > best[1]):
                        best = (i, ratio)
                if best is not None and best[1] > 1.01:
                    ov_level, ov_ratio = best

            inv = 1.0 / ov_ratio
            if ov_level >= 0:
                _ov = base_band.GetOverview(ov_level)
                level_w, level_h = int(_ov.XSize), int(_ov.YSize)
            else:
                level_w, level_h = total_w, total_h
            lx = max(0, min(int(math.floor(x_off * inv)), level_w - 1))
            ly = max(0, min(int(math.floor(y_off * inv)), level_h - 1))
            lw = max(1, min(int(math.ceil(win_w * inv)), level_w - lx))
            lh = max(1, min(int(math.ceil(win_h * inv)), level_h - ly))

            arrays: list[np.ndarray] = []
            valids: list[np.ndarray] = []
            metas: list[dict[str, Any]] = []
            for b_idx in target_bands:
                band = ds.GetRasterBand(b_idx)
                src = band.GetOverview(ov_level) if ov_level >= 0 else band
                if src is None:
                    src = band
                arr = src.ReadAsArray(lx, ly, lw, lh, buf_xsize=out_w, buf_ysize=out_h)
                if arr is None:
                    raise RuntimeError(f"波段 {b_idx} 窗口读取失败")
                arr = np.asarray(arr)

                nd = band.GetNoDataValue()
                flags = int(band.GetMaskFlags())
                valid = np.ones(arr.shape, dtype=bool)
                if nd is not None:
                    valid &= arr != float(nd)
                if np.issubdtype(arr.dtype, np.floating):
                    valid &= np.isfinite(arr)
                try:
                    mb = band.GetMaskBand()
                    msrc = mb.GetOverview(ov_level) if ov_level >= 0 and mb.GetOverviewCount() > ov_level else mb
                    m = msrc.ReadAsArray(lx, ly, lw, lh, buf_xsize=out_w, buf_ysize=out_h)
                    if m is not None:
                        valid &= np.asarray(m) > 0
                except Exception:
                    pass

                scale = band.GetScale()
                offset = band.GetOffset()
                vals = arr[valid]
                arrays.append(arr)
                valids.append(valid.astype(np.uint8))
                metas.append({
                    "band": b_idx,
                    "dtype": gdal.GetDataTypeName(band.DataType),
                    "numpy_dtype": str(arr.dtype),
                    "nodata": float(nd) if nd is not None else None,
                    "scale": float(scale) if scale is not None else None,
                    "offset": float(offset) if offset is not None else None,
                    "mask_flags": flags,
                    "has_alpha": bool(flags & gdal.GMF_ALPHA),
                    "all_valid_mask": bool(flags & gdal.GMF_ALL_VALID),
                    "min": float(vals.min()) if vals.size else None,
                    "max": float(vals.max()) if vals.size else None,
                    "valid_count": int(vals.size),
                    "total_count": int(arr.size),
                })

            stacked = np.stack(arrays, axis=0)
            valid_stack = np.stack(valids, axis=0)
            packed_valid = np.packbits(valid_stack.reshape(-1), axis=None)

            return {
                "raw": True,
                "x_off": x_off,
                "y_off": y_off,
                "win_w": win_w,
                "win_h": win_h,
                "out_w": out_w,
                "out_h": out_h,
                "bands": target_bands,
                "shape": [int(stacked.shape[0]), int(stacked.shape[1]), int(stacked.shape[2])],
                "numpy_dtype": str(stacked.dtype),
                "data_b64": base64.b64encode(np.ascontiguousarray(stacked).tobytes()).decode("ascii"),
                "valid_bits_b64": base64.b64encode(packed_valid.tobytes()).decode("ascii"),
                "valid_bits_count": int(valid_stack.size),
                "band_meta": metas,
                "overview_used": ov_level,
                "overview_ratio": ov_ratio,
                "needed_decimation": needed,
                "is_rgb": len(target_bands) >= 3,
            }
        finally:
            del ds

    @staticmethod
    def _affine_xy(gt: list[float] | tuple[float, ...], x: float, y: float) -> tuple[float, float]:
        return (gt[0] + gt[1] * x + gt[2] * y, gt[3] + gt[4] * x + gt[5] * y)

    @staticmethod
    def _affine_inv(gt: list[float] | tuple[float, ...], mx: float, my: float) -> tuple[float, float]:
        det = gt[1] * gt[5] - gt[2] * gt[4]
        if det == 0:
            raise ValueError("GeoTransform 退化（行列式为 0），无法反算行列号")
        dx, dy = mx - gt[0], my - gt[3]
        return ((gt[5] * dx - gt[2] * dy) / det, (-gt[4] * dx + gt[1] * dy) / det)

    @staticmethod
    def _transform_xy(x: float, y: float, src_wkt: str, dst_wkt: str) -> tuple[float, float]:
        src = osr.SpatialReference()
        src.ImportFromWkt(src_wkt)
        dst = osr.SpatialReference()
        dst.ImportFromWkt(dst_wkt)
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(src, dst)
        nx, ny, *_ = ct.TransformPoint(float(x), float(y))
        return float(nx), float(ny)

    def query_pixel(
        self,
        path: Path | str,
        row: int,
        col: int,
        bands: list[int] | None = None,
        view_crs_wkt: str | None = None,
    ) -> dict[str, Any]:
        """查询单个像元的原值、物理值与有效性；返回像元中心/角点地图坐标。"""
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用")
        in_path = _resolve_raster_input(Path(path))
        ds = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            w, h, count = int(ds.RasterXSize), int(ds.RasterYSize), int(ds.RasterCount)
            row, col = int(row), int(col)
            inside = 0 <= row < h and 0 <= col < w
            gt = ds.GetGeoTransform(can_return_null=True)
            srs = ds.GetSpatialRef()
            crs_wkt = srs.ExportToWkt() if srs is not None else None
            crs_epsg = None
            if srs is not None:
                code = srs.GetAuthorityCode(None)
                crs_epsg = int(code) if code and str(code).isdigit() else None

            center = corner = None
            if gt:
                corner = self._affine_xy(gt, col, row)
                center = self._affine_xy(gt, col + 0.5, row + 0.5)
            if center and view_crs_wkt and crs_wkt and view_crs_wkt != crs_wkt:
                center = self._transform_xy(center[0], center[1], crs_wkt, view_crs_wkt)

            target = bands or list(range(1, count + 1))
            band_values: list[dict[str, Any]] = []
            alpha_idx = None
            for b_idx in range(1, count + 1):
                band = ds.GetRasterBand(b_idx)
                flags = int(band.GetMaskFlags())
                if flags & gdal.GMF_ALPHA:
                    alpha_idx = b_idx
            for b_idx in target:
                if b_idx < 1 or b_idx > count:
                    continue
                band = ds.GetRasterBand(b_idx)
                val = None
                if inside:
                    try:
                        arr = band.ReadAsArray(col, row, 1, 1)
                        if arr is not None and arr.size:
                            val = float(arr[0, 0])
                    except Exception:
                        val = None
                nd = band.GetNoDataValue()
                scale = band.GetScale()
                offset = band.GetOffset()
                physical = None
                if val is not None:
                    physical = val * float(scale) + float(offset) if (scale is not None or offset is not None) else val
                mask_val = None
                if inside:
                    try:
                        mask_band = band.GetMaskBand()
                        m = mask_band.ReadAsArray(col, row, 1, 1)
                        mask_val = int(m[0, 0]) if m is not None and m.size else None
                    except Exception:
                        mask_val = None
                flags_now = int(band.GetMaskFlags())
                status = "valid"
                if not inside:
                    status = "outside"
                elif nd is not None and val is not None and float(nd) == float(val):
                    status = "nodata"
                elif mask_val == 0:
                    # GDAL 把 alpha 波段也当作 mask 带返回，靠标志位区分来源
                    status = "alpha_zero" if (flags_now & gdal.GMF_ALPHA) else "masked"
                elif alpha_idx is not None and b_idx != alpha_idx:
                    try:
                        a = ds.GetRasterBand(alpha_idx).ReadAsArray(col, row, 1, 1)
                        if a is not None and a.size and int(a[0, 0]) == 0:
                            status = "alpha_zero"
                    except Exception:
                        pass
                band_values.append({
                    "band": b_idx,
                    "raw": val,
                    "physical": physical,
                    "nodata": float(nd) if nd is not None else None,
                    "scale": float(scale) if scale is not None else None,
                    "offset": float(offset) if offset is not None else None,
                    "mask": mask_val,
                    "mask_flags": flags_now,
                    "status": status,
                    "is_zero": bool(val == 0.0) if val is not None else False,
                })
            return {
                "path": str(in_path),
                "row": row,
                "col": col,
                "inside": inside,
                "width": w,
                "height": h,
                "band_count": count,
                "pixel_convention": "corner",
                "has_geotransform": bool(gt),
                "map_corner": list(corner) if corner else None,
                "map_center": list(center) if center else None,
                "crs_wkt": crs_wkt,
                "crs_epsg": crs_epsg,
                "geotransform": [float(x) for x in gt] if gt else None,
                "bands": band_values,
            }
        finally:
            del ds

    def map_to_pixel(
        self,
        path: Path | str,
        x: float,
        y: float,
        view_crs_wkt: str | None = None,
        convention: str = "center",
    ) -> dict[str, Any]:
        """地图坐标 -> 行列号。同时给出中心/角点两种约定与是否在图幅内。"""
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用")
        in_path = _resolve_raster_input(Path(path))
        ds = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            w, h = int(ds.RasterXSize), int(ds.RasterYSize)
            gt = ds.GetGeoTransform(can_return_null=True)
            if not gt:
                raise ValueError("数据集没有存储的 GeoTransform（GDAL 默认值不构成地理参考），只能使用像元坐标")
            srs = ds.GetSpatialRef()
            crs_wkt = srs.ExportToWkt() if srs is not None else None
            wx, wy = float(x), float(y)
            if view_crs_wkt and crs_wkt and view_crs_wkt != crs_wkt:
                wx, wy = self._transform_xy(wx, wy, view_crs_wkt, crs_wkt)
            u, v = self._affine_inv(gt, wx, wy)
            # corner 约定（GDAL 默认）：像元 c 覆盖 [c, c+1)，索引 = floor(U)
            # center 约定（CF/netCDF 风格）：像元 c 覆盖 [c-0.5, c+0.5)，索引 = floor(U+0.5)
            col_corner, row_corner = int(math.floor(u)), int(math.floor(v))
            col_center, row_center = int(math.floor(u + 0.5)), int(math.floor(v + 0.5))
            chosen_col = col_center if convention == "center" else col_corner
            chosen_row = row_center if convention == "center" else row_corner
            return {
                "path": str(in_path),
                "view_crs_wkt": view_crs_wkt,
                "dataset_crs_wkt": crs_wkt,
                "map_x": float(x),
                "map_y": float(y),
                "dataset_x": wx,
                "dataset_y": wy,
                "col_float_corner": u,
                "row_float_corner": v,
                "col_corner": col_corner,
                "row_corner": row_corner,
                "col_center": col_center,
                "row_center": row_center,
                "col": chosen_col,
                "row": chosen_row,
                "requested_convention": convention,
                "dataset_pixel_convention": "corner",
                "has_geotransform": True,
                "inside": 0 <= chosen_col < w and 0 <= chosen_row < h,
                "inside_corner": 0 <= col_corner < w and 0 <= row_corner < h,
            }
        finally:
            del ds

    def compute_stats(
        self,
        path: Path | str,
        mode: str = "sampled",
        bands: list[int] | None = None,
        max_samples: int = 1_000_000,
        full_limit: int = 50_000_000,
    ) -> dict[str, Any]:
        """P3 显示拉伸用的按需统计（协议通道）。

        P4-04 起与任务式统计报告共用 ``statistics_ops.compute_statistics``
        这一份实现（抽样/全量语义、NoData 排除、超上限拒绝保持一致），
        这里只裁出协议原有的键，避免双份逻辑漂移；
        同时关闭 GDAL 原生精确统计对照，防止大图预览被全量扫描拖慢。
        """
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用")
        report = compute_statistics(
            _resolve_raster_input(Path(path)),
            mode=mode,
            band_list=bands,
            max_samples=max_samples,
            full_limit=full_limit,
            include_gdal_reference=False,
        )
        keys = (
            "band", "valid_count", "total_count", "min", "max", "mean", "std", "p2", "p98",
        )
        return {
            "path": report["path"],
            "mode": report["mode"],
            "stride": report["stride"],
            "source": report["source"],
            "width": report["width"],
            "height": report["height"],
            "bands": [{k: b[k] for k in keys} for b in report["bands"]],
        }

    def transform_point(
        self, x: float, y: float, src_wkt: str, dst_wkt: str, z: float = 0.0
    ) -> dict[str, Any]:
        """点在两个 CRS 之间换算。GUI 需要显示视图坐标时向 worker 索取。"""
        if not GDAL_AVAILABLE or osr is None:
            raise RuntimeError("GDAL 运行时不可用")
        src = osr.SpatialReference()
        if src.ImportFromWkt(src_wkt) != 0 or not src.GetAttrValue("AUTHORITY", 1):
            raise ValueError("源 CRS WKT 无法解析")
        dst = osr.SpatialReference()
        if dst.ImportFromWkt(dst_wkt) != 0 or not dst.GetAttrValue("AUTHORITY", 1):
            raise ValueError("目标 CRS WKT 无法解析")
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        ct = osr.CoordinateTransformation(src, dst)
        tx, ty, tz = ct.TransformPoint(float(x), float(y), float(z))
        return {
            "src": [float(x), float(y)],
            "dst": [float(tx), float(ty)],
            "src_wkt": src_wkt,
            "dst_wkt": dst_wkt,
        }

    def crs_wkt(self, epsg: int) -> dict[str, Any]:
        """EPSG 代码 -> WKT。GUI 需要视图 CRS 时向 worker 索取，不在界面导入 osr。"""
        if not GDAL_AVAILABLE or osr is None:
            raise RuntimeError("GDAL 运行时不可用")
        srs = osr.SpatialReference()
        code = srs.ImportFromEPSG(int(epsg))
        if code != 0:
            raise ValueError(f"无法从 EPSG:{epsg} 构建空间参考")
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        return {"epsg": int(epsg), "wkt": srs.ExportToWkt(), "name": srs.GetName() or ""}

    def transform_bounds(self, path: Path | str, view_crs_wkt: str) -> dict[str, Any]:
        """把数据集地理范围换算到视图 CRS，用于确定显示网格。"""
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用")
        in_path = _resolve_raster_input(Path(path))
        ds = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            gt = ds.GetGeoTransform(can_return_null=True)
            if not gt:
                raise ValueError("数据集没有地理参考，无法换算到视图 CRS")
            srs = ds.GetSpatialRef()
            if srs is None:
                raise ValueError("数据集缺少 CRS，无法换算到视图 CRS")
            w, h = int(ds.RasterXSize), int(ds.RasterYSize)
            dst = osr.SpatialReference()
            dst.ImportFromWkt(view_crs_wkt)
            src = osr.SpatialReference(srs.ExportToWkt())
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            ct = osr.CoordinateTransformation(src, dst)
            corners = []
            for (x, y) in [
                (0, 0), (w, 0), (w, h), (0, h), (w / 2, 0), (w / 2, h), (0, h / 2), (w, h / 2)
            ]:
                mx, my = self._affine_xy(gt, x, y)
                tx, ty, _ = ct.TransformPoint(mx, my)
                corners.append((float(tx), float(ty)))
            xs = [c[0] for c in corners]
            ys = [c[1] for c in corners]
            return {
                "view_crs_wkt": view_crs_wkt,
                "bounds": [min(xs), min(ys), max(xs), max(ys)],
                "corners": corners,
                "dataset_crs_wkt": srs.ExportToWkt(),
            }
        finally:
            del ds

    def transform_coordinates(
        self,
        path: Path | str,
        points: list[list[float]] | list[tuple[float, float]],
        from_space: str,
        to_space: str,
        src_crs_wkt: str | None = None,
        dst_crs_wkt: str | None = None,
        convention: str = "corner",
    ) -> dict[str, Any]:
        """ROI 坐标在像元与地图坐标系之间转换。

        from_space/to_space 为 "pixel" 或 "map"。
        - pixel -> map: 基于数据集 GeoTransform 计算原生坐标，若需要再重投影到目标 CRS。
        - map -> pixel: 若有必要先从源 CRS 重投影到原生 CRS，再基于逆 GeoTransform 计算像元。
        - map -> map: 纯坐标系重投影。
        - pixel -> pixel: 原样返回。
        """
        if not points:
            return {"points": [], "from_space": from_space, "to_space": to_space}
        if from_space not in ("pixel", "map") or to_space not in ("pixel", "map"):
            raise ValueError(f"未知坐标空间转换：{from_space} -> {to_space}")

        if from_space == "pixel" and to_space == "pixel":
            return {
                "points": [[float(p[0]), float(p[1])] for p in points],
                "from_space": "pixel",
                "to_space": "pixel",
            }

        if not GDAL_AVAILABLE or gdal is None or osr is None:
            raise RuntimeError("GDAL 运行时不可用")

        in_path = _resolve_raster_input(Path(path))
        ds = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            gt = ds.GetGeoTransform(can_return_null=True)
            srs = ds.GetSpatialRef()
            ds_crs_wkt = srs.ExportToWkt() if srs is not None else None

            if from_space == "pixel" and to_space == "map":
                if not gt:
                    raise ValueError("数据集没有地理参考（GeoTransform），无法由像元坐标换算到地图坐标")
                if not ds_crs_wkt:
                    raise ValueError("数据集缺少 CRS，无法换算到地图坐标")
                target_crs_wkt = dst_crs_wkt or ds_crs_wkt
                ct = None
                if target_crs_wkt != ds_crs_wkt:
                    src_srs = osr.SpatialReference(ds_crs_wkt)
                    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    dst_srs = osr.SpatialReference(target_crs_wkt)
                    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    ct = osr.CoordinateTransformation(src_srs, dst_srs)
                out_pts: list[list[float]] = []
                for p in points:
                    col, row = float(p[0]), float(p[1])
                    u, v = (col + 0.5, row + 0.5) if convention == "center" else (col, row)
                    mx, my = self._affine_xy(gt, u, v)
                    if ct is not None:
                        tx, ty, _ = ct.TransformPoint(mx, my)
                        out_pts.append([float(tx), float(ty)])
                    else:
                        out_pts.append([float(mx), float(my)])
                return {
                    "points": out_pts,
                    "from_space": "pixel",
                    "to_space": "map",
                    "crs_wkt": target_crs_wkt,
                }

            elif from_space == "map" and to_space == "pixel":
                if not gt:
                    raise ValueError("数据集没有地理参考（GeoTransform），无法由地图坐标换算到像元坐标")
                if not ds_crs_wkt:
                    raise ValueError("数据集缺少 CRS，无法由地图坐标换算到像元坐标")
                source_crs_wkt = src_crs_wkt or ds_crs_wkt
                ct = None
                if source_crs_wkt != ds_crs_wkt:
                    src_srs = osr.SpatialReference(source_crs_wkt)
                    src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    dst_srs = osr.SpatialReference(ds_crs_wkt)
                    dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    ct = osr.CoordinateTransformation(src_srs, dst_srs)
                out_pts = []
                for p in points:
                    mx, my = float(p[0]), float(p[1])
                    if ct is not None:
                        tx, ty, _ = ct.TransformPoint(mx, my)
                    else:
                        tx, ty = mx, my
                    u, v = self._affine_inv(gt, tx, ty)
                    col, row = (u - 0.5, v - 0.5) if convention == "center" else (u, v)
                    out_pts.append([float(col), float(row)])
                return {
                    "points": out_pts,
                    "from_space": "map",
                    "to_space": "pixel",
                }

            elif from_space == "map" and to_space == "map":
                source_crs_wkt = src_crs_wkt or ds_crs_wkt
                target_crs_wkt = dst_crs_wkt or ds_crs_wkt
                if not source_crs_wkt or not target_crs_wkt or source_crs_wkt == target_crs_wkt:
                    return {
                        "points": [[float(p[0]), float(p[1])] for p in points],
                        "from_space": "map",
                        "to_space": "map",
                        "crs_wkt": target_crs_wkt or source_crs_wkt,
                    }
                src_srs = osr.SpatialReference(source_crs_wkt)
                src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                dst_srs = osr.SpatialReference(target_crs_wkt)
                dst_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                ct = osr.CoordinateTransformation(src_srs, dst_srs)
                out_pts = []
                for p in points:
                    tx, ty, _ = ct.TransformPoint(float(p[0]), float(p[1]))
                    out_pts.append([float(tx), float(ty)])
                return {
                    "points": out_pts,
                    "from_space": "map",
                    "to_space": "map",
                    "crs_wkt": target_crs_wkt,
                }
            raise ValueError(f"不受支持的坐标空间变换：{from_space} -> {to_space}")
        finally:
            del ds

    def read_view_window(
        self,
        path: Path | str,
        view_crs_wkt: str,
        x_min: float,
        y_min: float,
        x_max: float,
        y_max: float,
        out_w: int,
        out_h: int,
        bands: list[int] | None = None,
        resampling: str = "near",
    ) -> dict[str, Any]:
        """把数据集重投影到统一的视图网格后返回原值。

        跨 CRS 图层叠加必须走这里：只按像素位置摆放不同 CRS 的图层是错的。
        """
        import base64

        import numpy as np

        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用")
        if x_max <= x_min or y_max <= y_min:
            raise ValueError(f"视图范围非法：({x_min},{y_min})-({x_max},{y_max})")
        out_w = max(1, min(int(out_w), 2048))
        out_h = max(1, min(int(out_h), 2048))
        in_path = _resolve_raster_input(Path(path))
        src = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if src is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            total_bands = int(src.RasterCount)
            target_bands = [b for b in (bands or list(range(1, min(3, total_bands) + 1))) if 1 <= b <= total_bands]
            if not target_bands:
                raise ValueError(f"请求波段 {bands} 全部越界（数据集有 {total_bands} 个波段）")
            # 该 GDAL 构建的 WarpOptions 未暴露选波段参数，因此重投影全部波段后
            # 按源波段序号取带（顺序一致，不重排）。
            mem_name = (
                f"/vsimem/spectra_view/{in_path.stem}_{int(time.time_ns())}_{out_w}x{out_h}"
            )
            warped = gdal.Warp(
                mem_name,
                src,
                dstSRS=view_crs_wkt,
                outputBounds=[float(x_min), float(y_min), float(x_max), float(y_max)],
                width=out_w,
                height=out_h,
                resampleAlg=resampling,
                format="MEM",
            )
            if warped is None:
                raise RuntimeError("重投影到视图网格失败")
            try:
                arrays = []
                metas = []
                for b_idx in target_bands:
                    band = warped.GetRasterBand(int(b_idx))
                    arr = band.ReadAsArray(0, 0, out_w, out_h)
                    if arr is None:
                        raise RuntimeError(f"重投影后波段 {b_idx} 读取失败")
                    arr = np.asarray(arr)
                    nd = band.GetNoDataValue()
                    src_band = src.GetRasterBand(b_idx)
                    valid = np.isfinite(arr.astype(np.float64))
                    if nd is not None:
                        valid &= arr != float(nd)
                    arrays.append(arr)
                    metas.append({
                        "band": int(b_idx),
                        "dtype": gdal.GetDataTypeName(band.DataType),
                        "numpy_dtype": str(arr.dtype),
                        "nodata": float(nd) if nd is not None else None,
                        "scale": (src_band.GetScale() if src_band.GetScale() is not None else None),
                        "offset": (src_band.GetOffset() if src_band.GetOffset() is not None else None),
                        "mask_flags": int(src_band.GetMaskFlags()),
                        "min": float(arr[valid].min()) if valid.any() else None,
                        "max": float(arr[valid].max()) if valid.any() else None,
                        "valid_count": int(valid.sum()),
                        "total_count": int(arr.size),
                    })
                stacked = np.stack(arrays, axis=0)
                valid_stack = np.stack([np.isfinite(a.astype(np.float64)) for a in arrays], axis=0)
                for i, meta in enumerate(metas):
                    if meta["nodata"] is not None:
                        valid_stack[i] &= stacked[i] != float(meta["nodata"])
                vgt = warped.GetGeoTransform(can_return_null=True)
                packed = np.packbits(valid_stack.reshape(-1), axis=None)
                return {
                    "raw": True,
                    "reprojected": True,
                    "view_crs_wkt": view_crs_wkt,
                    "x_off": float(x_min),
                    "y_off": float(y_min),
                    "win_w": float(x_max) - float(x_min),
                    "win_h": float(y_max) - float(y_min),
                    "out_w": int(out_w),
                    "out_h": int(out_h),
                    "grid": {
                        "x_min": float(x_min),
                        "y_min": float(y_min),
                        "x_max": float(x_max),
                        "y_max": float(y_max),
                        "affine": [float(v) for v in vgt] if vgt else None,
                        "out_w": out_w,
                        "out_h": out_h,
                    },
                    "bands": target_bands,
                    "shape": [int(stacked.shape[0]), out_h, out_w],
                    "numpy_dtype": str(stacked.dtype),
                    "data_b64": base64.b64encode(np.ascontiguousarray(stacked).tobytes()).decode("ascii"),
                    "valid_bits_b64": base64.b64encode(packed.tobytes()).decode("ascii"),
                    "valid_bits_count": int(valid_stack.size),
                    "band_meta": metas,
                    "overview_used": -1,
                    "resampling": resampling,
                    "is_rgb": len(target_bands) >= 3,
                }
            finally:
                del warped
        finally:
            del src

    def dataset_overviews(self, path: Path | str) -> dict[str, Any]:
        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用")
        in_path = _resolve_raster_input(Path(path))
        ds = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            w = int(ds.RasterXSize)
            levels: list[dict[str, Any]] = []
            band = ds.GetRasterBand(1)
            for i in range(int(band.GetOverviewCount())):
                ov = band.GetOverview(i)
                if ov is None:
                    continue
                levels.append({
                    "level": i,
                    "width": int(ov.XSize),
                    "height": int(ov.YSize),
                    "ratio": float(w) / float(ov.XSize) if ov.XSize else 0.0,
                })
            return {"path": str(in_path), "width": w, "height": int(ds.RasterYSize), "overviews": levels}
        finally:
            del ds
