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
from spectra_workbench.providers.base import (
    ExecutionContext,
    OutputConflictError,
    ProviderAdapter,
    resolve_dataset_path,
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


_GDAL_OUTPUT_TYPES = {
    "byte": "Byte",
    "uint8": "Byte",
    "ubyte": "Byte",
    "int8": "Int8",
    "uint16": "UInt16",
    "int16": "Int16",
    "uint32": "UInt32",
    "int32": "Int32",
    "float32": "Float32",
    "float64": "Float64",
    "cfloat32": "CFloat32",
    "cfloat64": "CFloat64",
}


def _first_param(spec_params: dict[str, Any], *names: str) -> Any:
    """按给定顺序取第一个存在的参数值（兼容 GDAL CLI 风格与 snake_case）。"""
    for name in names:
        if name in spec_params and spec_params[name] is not None:
            return spec_params[name]
    return None


def _as_float_list(value: Any, name: str, expected: int) -> list[float]:
    """把参数解析为定长浮点列表；非法输入给出明确错误。"""
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"参数 {name} 必须是长度 {expected} 的数值列表，实际为 {type(value).__name__}")
    if len(value) != expected:
        raise ValueError(f"参数 {name} 必须有 {expected} 个元素，实际 {len(value)} 个")
    try:
        return [float(v) for v in value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数 {name} 含非数值元素：{value!r}") from exc


def _as_int_list(value: Any, name: str, expected: int) -> list[int]:
    """把参数解析为定长整型列表；非法输入给出明确错误。"""
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"参数 {name} 必须是长度 {expected} 的整数列表，实际为 {type(value).__name__}")
    if len(value) != expected:
        raise ValueError(f"参数 {name} 必须有 {expected} 个元素，实际 {len(value)} 个")
    try:
        return [int(v) for v in value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数 {name} 含非整型元素：{value!r}") from exc


def _resolve_output_type(value: Any) -> Any:
    """把输出数据类型名解析为 GDAL 数据类型常量。

    未知类型名必须报错（不得静默回退到源类型），否则「dtype 按明确参数验收」无从谈起。
    """
    if value is None:
        return None
    key = str(value).strip().lower()
    if key in _GDAL_OUTPUT_TYPES:
        return getattr(gdal, f"GDT_{_GDAL_OUTPUT_TYPES[key]}")
    # 允许直接传 GDAL 常量名（如 "GDT_Float32"）
    if key.startswith("gdt_"):
        const = getattr(gdal, key.upper(), None)
        if const is not None:
            return const
    raise ValueError(
        f"未知的输出数据类型 {value!r}；可用：{sorted(_GDAL_OUTPUT_TYPES)}"
    )


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

    SUPPORTED_CAPABILITIES = {"gdal:translate", "gdal_translate", "gdal:warp", "gdalwarp"}

    def validate(self, spec: TaskSpec) -> dict[str, Any]:
        errors: list[str] = []
        code: str = "validation_failed"
        if spec.capability_id not in self.SUPPORTED_CAPABILITIES:
            return {
                "valid": False,
                "code": "unsupported_capability",
                "errors": [f"GDAL 引擎尚未接入该能力执行器: '{spec.capability_id}'（当前已支持: gdal:translate, gdal:warp）"],
            }

        if "input" not in spec.inputs:
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
            if "input" in spec.inputs:
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

        # 2.1 同源冲突防范
        if in_path.resolve() == target_path.resolve():
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

        resample_str = spec.parameters.get("resample_alg", "nearest")
        resample_map = {
            "nearest": gdal.GRA_NearestNeighbour,
            "bilinear": gdal.GRA_Bilinear,
            "cubic": gdal.GRA_Cubic,
            "average": gdal.GRA_Average,
        }
        resample_alg = resample_map.get(resample_str, gdal.GRA_NearestNeighbour)

        creation_options: list[str] = []
        if isinstance(out_plan.creation_options, dict):
            for k, v in out_plan.creation_options.items():
                creation_options.append(f"{k}={v}")

        # 4. 执行算法（按 capability_id 明确分派 Translate 或 Warp）
        is_warp = spec.capability_id in ("gdal:warp", "gdalwarp")
        dst_srs = (
            spec.parameters.get("dstSRS")
            or spec.parameters.get("dst_srs")
            or spec.parameters.get("dst_crs")
        )
        src_srs = spec.parameters.get("srcSRS") or spec.parameters.get("src_srs")
        dst_nodata = spec.parameters.get("dstNodata") or spec.parameters.get("dst_nodata")

        # P4-01：裁剪 / 重采样 / 输出网格参数装配
        # 参数非法时必须显式报错（不得静默忽略），否则「按明确参数验收」无从谈起。
        params = spec.parameters
        try:
            src_win = _first_param(params, "srcWin", "src_win", "window")
            proj_win = _first_param(params, "projWin", "proj_win")
            out_bounds = _first_param(params, "outputBounds", "output_bounds")
            x_res = _first_param(params, "xRes", "x_res", "x_resolution")
            y_res = _first_param(params, "yRes", "y_res", "y_resolution")
            out_type = _first_param(params, "outputType", "output_type", "dtype")
            out_width = _first_param(params, "width", "out_width")
            out_height = _first_param(params, "height", "out_height")
            translate_nodata = _first_param(params, "noData", "no_data", "nodata")
            target_aligned = _first_param(params, "targetAlignedPixels", "target_aligned_pixels")

            src_win_list = (
                _as_int_list(src_win, "srcWin", 4) if src_win is not None else None
            )
            proj_win_list = (
                _as_float_list(proj_win, "projWin", 4) if proj_win is not None else None
            )
            out_bounds_list = (
                _as_float_list(out_bounds, "outputBounds", 4) if out_bounds is not None else None
            )
            output_type = _resolve_output_type(out_type)
            x_res_f = float(x_res) if x_res is not None else None
            y_res_f = float(y_res) if y_res is not None else None
            if x_res_f is not None and x_res_f <= 0:
                raise ValueError(f"参数 xRes 必须为正数，实际 {x_res_f}")
            if y_res_f is not None and y_res_f <= 0:
                raise ValueError(f"参数 yRes 必须为正数，实际 {y_res_f}")
            out_w_i = int(out_width) if out_width is not None else None
            out_h_i = int(out_height) if out_height is not None else None
            if out_w_i is not None and out_w_i <= 0:
                raise ValueError(f"参数 width 必须为正数，实际 {out_w_i}")
            if out_h_i is not None and out_h_i <= 0:
                raise ValueError(f"参数 height 必须为正数，实际 {out_h_i}")
            translate_nodata_f = (
                float(translate_nodata) if translate_nodata is not None else None
            )
        except (TypeError, ValueError) as exc:
            context.log("ERROR", f"Invalid processing parameters: {exc}")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "invalid_parameters", "message": str(exc)},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        try:
            if is_warp:
                warp_kwargs: dict[str, Any] = {
                    "format": out_format,
                    "dstSRS": dst_srs,
                    "srcSRS": src_srs,
                    "dstNodata": dst_nodata,
                    "resampleAlg": resample_alg,
                    "creationOptions": creation_options,
                    "callback": _gdal_cb,
                }
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
