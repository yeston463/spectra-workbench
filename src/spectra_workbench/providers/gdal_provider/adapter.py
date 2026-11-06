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
import math
import time
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

from spectra_workbench.domain.models import (
    Availability,
    CapabilityDescriptor,
    Presence,
    TaskResult,
    TaskSpec,
    TaskStatus,
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
from spectra_workbench.providers.gdal_provider.metadata_ops import (
    apply_nodata,
    build_overviews,
    restore_missing_hdr_fields,
    snapshot_hdr_lines,
)
from spectra_workbench.providers.gdal_provider.mosaic_ops import (
    build_vrt,
    check_source_compatibility,
    manage_bands,
    merge_sources,
    set_vrt_source_paths,
    stack_sources,
)
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
    }
)

#: P4-03 in-place 能力：直接更新源文件（输出=输入），staging 走「复制-改-提交」。
INPLACE_CAPABILITIES: frozenset[str] = frozenset({"gdal:editmeta", "gdal:overview"})


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
    }

    #: 需要多个输入源的能力（至少 2 个）。
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
            elif is_multi or spec.capability_id == "gdal:bandmanage":
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

        is_warp = spec.capability_id in ("gdal:warp", "gdalwarp")

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
            if is_warp:
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
            op_name = "Warp" if is_warp else "Translate"
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
            op_name = "Warp" if is_warp else "Translate"
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

        # 8. 事务性提交全部成果文件
        context.progress(0.95, "committing", "Committing staged output and sidecars to destination")
        try:
            committed_files = context.commit_files(commit_pairs, overwrite=overwrite)
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
            "driver": out_format,
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
        inplace_info: dict[str, Any] = {}
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
        # 非 separate 截断多波段），必须先核对并明确拒绝。
        if not is_inplace and cid in ("gdal:merge", "gdal_merge"):
            compat = check_source_compatibility(sources, require_same_bands=True)
        elif cid == "gdal:stack":
            # separate=True 时每个源的所有波段依次进入，波段数可以不同
            compat = check_source_compatibility(sources, require_same_bands=False)
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
            f"P4-02 能力 {cid}：{len(sources)} 个输入源 → {target_path.name}",
        )
        context.progress(0.10, "processing", "执行拼接 / VRT / 堆叠 / 波段管理")

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
                build_vrt(
                    staged_path,
                    sources,
                    overlap_priority=norm.get("overlapPriority", "last"),
                    resolution=norm.get("resolution"),
                    src_nodata=norm.get("srcNodata"),
                    vrt_nodata=norm.get("vrtNodata"),
                    separate=bool(norm.get("separate", False)),
                    callback=_progress_cb,
                )
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
                merge_sources(
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
            elif cid == "gdal:stack":
                stack_sources(
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
            else:  # gdal:bandmanage
                manage_bands(
                    staged_path,
                    sources[0],
                    band_list=norm.get("bandList"),
                    output_type=norm.get("outputType"),
                    out_format=out_format,
                    callback=_progress_cb,
                )
        except Exception as exc:
            context.log("ERROR", f"P4-02 处理失败: {exc}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "p402_error", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        if context.is_cancelled.is_set():
            context.log("WARNING", "检测到取消请求，中止 P4-02 任务")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.cancelled,
                error={"code": "task_cancelled", "message": "用户取消任务"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # ---- 成果完整性：收集全部 sidecar（ENVI .hdr / .aux.xml 等）----
        context.progress(0.80, "collecting", "收集成果与伴生文件")
        try:
            ds = gdal.Open(str(staged_path), gdal.GA_ReadOnly)
            if ds is None:
                raise RuntimeError(f"无法重开 staging 成果：{staged_path}")
            raw_files = ds.GetFileList() or []
            staged_files: list[Path] = [Path(f).resolve() for f in raw_files]
            if staged_path.resolve() not in staged_files:
                staged_files.insert(0, staged_path.resolve())
            # **排除输入源**：VRT 的 GetFileList() 会把它引用的源文件一并返回，
            # 且这些路径是按 **VRT 当前位置**（staging）解析的，
            # 因此不能用「与输入源同路径」来排除。
            # 稳健判据：**要提交的文件必须真实存在于 staging**——
            # 伴生文件（.hdr/.aux.xml）确实在 staging，而被引用的源不在。
            source_set = {sp.resolve() for sp in sources}
            staged_files = [
                sf
                for sf in staged_files
                if sf.exists() and sf not in source_set
            ]
            if staged_path.resolve() not in staged_files:
                staged_files.insert(0, staged_path.resolve())
            # 提交前真实重开校验：确认 staging 成果确实可读
            # （尺寸/波段等元数据在提交后由**最终重开**给出，不采信此处快照）
            del ds
        except Exception as verify_err:
            context.log("ERROR", f"staging 成果校验失败: {verify_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "verification_failed", "message": str(verify_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        if not staged_path.exists() or staged_path.stat().st_size == 0:
            context.log("ERROR", "staging 成果缺失或为空")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "staging_invalid", "message": "staging 成果缺失或为空"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # ---- 构造完整提交对（主文件 + 全部 sidecar，按最终命名映射）----
        commit_pairs: list[tuple[Path, Path]] = []
        for sf in staged_files:
            if sf == staged_path.resolve():
                tf = target_path
            elif sf.suffix.lower() == ".hdr":
                tf = target_path.with_suffix(".hdr")
            elif sf.name.endswith(".aux.xml"):
                tf = target_path.parent / f"{target_path.name}.aux.xml"
            elif sf.name.startswith(staged_path.name):
                tail = sf.name[len(staged_path.name) :]
                tf = target_path.parent / f"{target_path.name}{tail}"
            else:
                tf = target_path.parent / sf.name
            commit_pairs.append((sf, tf))

        # ---- 事务提交全部成果文件 ----
        context.progress(0.92, "committing", "提交 staging 成果与伴生文件")
        try:
            context.commit_files(commit_pairs, overwrite=overwrite)
        except OutputConflictError as conflict_err:
            context.log("ERROR", f"提交被拒（目标已存在）: {conflict_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "output_already_exists", "message": str(conflict_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )
        except Exception as commit_err:
            context.log("ERROR", f"事务提交失败: {commit_err}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "commit_failed", "message": str(commit_err)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        # ---- 提交后最终核验：真实重开目标文件 ----
        context.progress(0.97, "verifying_final", "核验已安装目标")
        try:
            final_ds = gdal.Open(str(target_path), gdal.GA_ReadOnly)
            if final_ds is None:
                raise RuntimeError(f"提交后的成果无法重开：{target_path}")
            final_w = final_ds.RasterXSize
            final_h = final_ds.RasterYSize
            final_bands = final_ds.RasterCount
            final_driver = final_ds.GetDriver().ShortName
            del final_ds
        except Exception as final_err:
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

        # 输出记录来自**实际重开**的成果
        output_record: dict[str, Any] = {
            "key": out_plan.key,
            "uri": target_path.as_uri(),
            "driver": final_driver,
            "width": final_w,
            "height": final_h,
            "bands": final_bands,
        }
        return TaskResult(
            task_id=spec.task_id,
            attempt=spec.attempt,
            status=TaskStatus.succeeded,
            outputs=[output_record],
            duration_seconds=duration,
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
        """统计。sampled 用步长抽样并明确标注；full 超过像素上限时拒绝而不是偷偷降级。"""
        import numpy as np

        if not GDAL_AVAILABLE or gdal is None:
            raise RuntimeError("GDAL 运行时不可用")
        in_path = _resolve_raster_input(Path(path))
        ds = gdal.Open(str(in_path), gdal.GA_ReadOnly)
        if ds is None:
            raise RuntimeError(f"无法打开数据集: {in_path}")
        try:
            w, h, count = int(ds.RasterXSize), int(ds.RasterYSize), int(ds.RasterCount)
            total = w * h
            if mode == "full" and total > full_limit:
                raise ValueError(
                    f"全量统计被拒绝：{total} 像素超过上限 {full_limit}；请改用 sampled 或分块统计"
                )
            stride = 1
            if mode == "sampled":
                stride = max(1, int(math.ceil(math.sqrt(total / max(1, max_samples)))))
            target = bands or list(range(1, count + 1))
            out_bands = []
            for b_idx in target:
                if b_idx < 1 or b_idx > count:
                    continue
                band = ds.GetRasterBand(b_idx)
                read_w = max(1, (w + stride - 1) // stride)
                read_h = max(1, (h + stride - 1) // stride)
                arr = band.ReadAsArray(0, 0, w, h, buf_xsize=read_w, buf_ysize=read_h)
                if arr is None:
                    continue
                arr = arr.astype(np.float64)
                nd = band.GetNoDataValue()
                valid = np.isfinite(arr)
                if nd is not None:
                    valid &= arr != float(nd)
                vals = arr[valid]
                entry = {
                    "band": b_idx,
                    "valid_count": int(vals.size),
                    "total_count": int(arr.size),
                    "min": float(vals.min()) if vals.size else None,
                    "max": float(vals.max()) if vals.size else None,
                    "mean": float(vals.mean()) if vals.size else None,
                    "std": float(vals.std()) if vals.size else None,
                    "p2": float(np.percentile(vals, 2)) if vals.size else None,
                    "p98": float(np.percentile(vals, 98)) if vals.size else None,
                }
                out_bands.append(entry)
            return {
                "path": str(in_path),
                "mode": mode,
                "stride": stride,
                "source": "full_raster" if (mode == "full" and stride == 1) else "sampled_grid",
                "width": w,
                "height": h,
                "bands": out_bands,
            }
        finally:
            del ds

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
