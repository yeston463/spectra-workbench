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

        try:
            if is_warp:
                warp_opts = gdal.WarpOptions(
                    format=out_format,
                    dstSRS=dst_srs,
                    srcSRS=src_srs,
                    dstNodata=dst_nodata,
                    resampleAlg=resample_alg,
                    creationOptions=creation_options,
                    callback=_gdal_cb,
                )
                ds = gdal.Warp(str(staged_path), str(in_path), options=warp_opts)
            else:
                opts = gdal.TranslateOptions(
                    format=out_format,
                    resampleAlg=resample_alg,
                    creationOptions=creation_options,
                    callback=_gdal_cb,
                )
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
            gt = ds.GetGeoTransform()
            geotransform = [float(x) for x in gt] if gt else None
            bounds = None
            if geotransform:
                minx = geotransform[0]
                maxx = minx + geotransform[1] * w + geotransform[2] * h
                maxy = geotransform[3]
                miny = maxy + geotransform[4] * w + geotransform[5] * h
                bounds = [min(minx, maxx), min(miny, maxy), max(minx, maxx), max(miny, maxy)]

            crs_epsg: int | None = None
            crs_wkt: str | None = None
            srs = ds.GetSpatialRef()
            if srs is not None:
                code_str = srs.GetAuthorityCode(None)
                if code_str and str(code_str).isdigit():
                    crs_epsg = int(code_str)
                crs_wkt = srs.ExportToWkt()

            bands_info: list[dict[str, Any]] = []
            for b_idx in range(1, count + 1):
                b = ds.GetRasterBand(b_idx)
                dt_name = gdal.GetDataTypeName(b.DataType)
                nd = b.GetNoDataValue()
                ci = gdal.GetColorInterpretationName(b.GetColorInterpretation())
                bands_info.append({
                    "band": b_idx,
                    "dtype": dt_name,
                    "nodata": float(nd) if nd is not None else None,
                    "color_interp": ci,
                    "overviews": int(b.GetOverviewCount()),
                })

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
                "bounds": bounds,
                "bands": bands_info,
            }
        finally:
            del ds

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
    ) -> dict[str, Any]:
        """后台分块读取局部窗口并抽样（禁止整幅读入）。"""
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
