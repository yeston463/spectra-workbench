"""Rasterio 引擎 Provider 适配器实现（P1-03 / P2-03）。

仅在 Rasterio 独立 worker 进程中导入 rasterio。
实现能力发现、任务校验、资源预估与基础栅格处理执行（如 format convert / bounds / metadata），
内置专有 staging 事务与闭合校验。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

try:
    import rasterio  # type: ignore[import-not-found]
    import rasterio.shutil  # type: ignore[import-not-found]

    RASTERIO_AVAILABLE = True
except ImportError:
    RASTERIO_AVAILABLE = False
    rasterio = None

from spectra_workbench.domain.models import (
    CapabilityDescriptor,
    DatasetRef,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.providers.base import (
    ExecutionContext,
    InputOutputConflictError,
    OutputConflictError,
    ProviderAdapter,
    resolve_dataset_path,
)
from spectra_workbench.registry.rasterio_index import (
    RASTERIO_VERSION,
    build_rasterio_baseline,
)


class RasterioProviderAdapter(ProviderAdapter):
    """Rasterio 适配器，服务于 sw-rasterio 独立进程。"""

    SUPPORTED_CAPABILITIES = {
        "rasterio:convert",
        "rio:cli:convert",
        "rasterio:copy",
        "rasterio:api:copy",
    }

    @property
    def provider_id(self) -> str:
        return "rasterio"

    def runtime_info(self) -> dict[str, Any]:
        if not RASTERIO_AVAILABLE or rasterio is None:
            return {"available": False, "error": "rasterio module not importable in current environment"}

        return {
            "available": True,
            "provider_id": "rasterio",
            "rasterio_version": getattr(rasterio, "__version__", RASTERIO_VERSION),
            "gdal_version": getattr(rasterio, "__gdal_version__", "unknown"),
        }

    def discover(self) -> list[CapabilityDescriptor]:
        """返回当前环境发现的 Rasterio 1.5.2 全部能力条目。"""
        baseline = build_rasterio_baseline()
        if RASTERIO_AVAILABLE and rasterio is not None:
            v = getattr(rasterio, "__version__", RASTERIO_VERSION)
            for c in baseline:
                c.engine.resolved_version = v
        return baseline

    def _extract_input_refs(self, spec: TaskSpec) -> list[DatasetRef]:
        refs: list[DatasetRef] = []
        for val in spec.inputs.values():
            if isinstance(val, list):
                refs.extend(val)
            else:
                refs.append(val)
        return refs

    def validate(self, spec: TaskSpec) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        code: str = "validation_failed"

        if spec.engine.provider_id != self.provider_id:
            return {
                "valid": False,
                "code": "engine_mismatch",
                "errors": [f"Engine provider 不匹配：期望 'rasterio'，实际 '{spec.engine.provider_id}'"],
                "warnings": warnings,
            }

        if spec.capability_id not in self.SUPPORTED_CAPABILITIES:
            return {
                "valid": False,
                "code": "unsupported_capability",
                "errors": [f"Rasterio 引擎尚未接入该能力执行器: '{spec.capability_id}'（当前已支持: rasterio:convert）"],
                "warnings": warnings,
            }

        input_refs = self._extract_input_refs(spec)
        if not input_refs:
            errors.append("TaskSpec 必须包含至少一个输入数据集")
            code = "input_not_found"

        for inp in input_refs:
            p = resolve_dataset_path(inp.uri, spec.project_root)
            if not p.exists():
                errors.append(f"输入数据集路径不存在：{p}")
                code = "input_not_found"

        if not spec.outputs:
            errors.append("TaskSpec 必须包含至少一个输出计划")
            code = "invalid_output_plan"

        for out in spec.outputs:
            p = resolve_dataset_path(out.uri, spec.project_root)
            if p.exists() and not out.overwrite:
                errors.append(f"输出目标路径已存在且未设置 overwrite=True：{p}")
                code = "output_already_exists"

        for inp in input_refs:
            inp_p = resolve_dataset_path(inp.uri, spec.project_root)
            for out in spec.outputs:
                out_p = resolve_dataset_path(out.uri, spec.project_root)
                if inp_p == out_p:
                    errors.append(f"输入与输出目标同源冲突：{inp_p} == {out_p}")
                    code = "input_output_conflict"

        return {
            "valid": len(errors) == 0,
            "code": code,
            "errors": errors,
            "warnings": warnings,
        }

    def estimate(self, spec: TaskSpec) -> dict[str, Any]:
        total_input_bytes = 0
        input_refs = self._extract_input_refs(spec)
        for inp in input_refs:
            p = resolve_dataset_path(inp.uri, spec.project_root)
            if p.exists():
                total_input_bytes += p.stat().st_size

        ram_mib = max(128, int((total_input_bytes * 2) / (1024 * 1024)))
        disk_bytes = int(total_input_bytes * 1.5)
        est_seconds = max(1.0, round(total_input_bytes / (10 * 1024 * 1024), 2))

        return {
            "ram_limit_mib": min(ram_mib, 4096),
            "temp_disk_bytes": max(disk_bytes, 1024 * 1024),
            "estimated_seconds": est_seconds,
        }

    def execute(self, spec: TaskSpec, context: ExecutionContext) -> TaskResult:
        start_time = time.monotonic()
        if not RASTERIO_AVAILABLE or rasterio is None:
            raise RuntimeError("当前运行环境无法导入 rasterio 模块")

        val = self.validate(spec)
        if not val["valid"]:
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "unsupported_capability", "message": "; ".join(val["errors"])},
                duration_seconds=0.0,
            )

        context.progress(0.05, "init", "准备 Rasterio 处理环境与路径解析")
        context.log("INFO", f"开始执行 Rasterio 任务: {spec.task_id} ({spec.capability_id})")

        input_refs = self._extract_input_refs(spec)
        inp_spec = input_refs[0]
        out_plan = spec.outputs[0]

        inp_path = resolve_dataset_path(inp_spec.uri, spec.project_root)
        out_path = resolve_dataset_path(out_plan.uri, spec.project_root)

        if inp_path == out_path:
            raise InputOutputConflictError(f"输入与输出目标同源冲突: {inp_path}")

        if out_path.exists() and not out_plan.overwrite:
            raise OutputConflictError(f"输出目标路径已存在且未设置 overwrite=True: {out_path}")

        staging_out = context.staging_dir / out_path.name
        staging_out.parent.mkdir(parents=True, exist_ok=True)

        context.progress(0.20, "opening", f"打开输入栅格: {inp_path.name}")
        if context.is_cancelled.is_set():
            context.log("WARNING", "检测到取消请求，中止执行")
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.cancelled,
                error={"code": "task_cancelled", "message": "用户取消任务"},
                duration_seconds=round(time.monotonic() - start_time, 4),
            )

        with rasterio.open(str(inp_path)) as src:
            meta = src.meta.copy()
            dst_driver = spec.parameters.get("format", out_plan.driver or "GTiff")
            meta.update(driver=dst_driver)

            context.progress(0.40, "processing", f"向 staging 写入成果栅格（分块流式写入）: {staging_out.name}")
            with rasterio.open(str(staging_out), "w", **meta) as dst:
                for i in range(1, src.count + 1):
                    for _, window in src.block_windows(i):
                        if context.is_cancelled.is_set():
                            context.log("WARNING", "检测到取消请求，中止像素复制")
                            return TaskResult(
                                task_id=spec.task_id,
                                attempt=spec.attempt,
                                status=TaskStatus.cancelled,
                                error={"code": "task_cancelled", "message": "用户取消任务"},
                                duration_seconds=round(time.monotonic() - start_time, 4),
                            )
                        chunk = src.read(i, window=window)
                        dst.write(chunk, i, window=window)
                    ratio = 0.40 + 0.40 * (i / src.count)
                    context.progress(ratio, "writing", f"写入波段 {i}/{src.count}")

        # 校验 staging 成果
        context.progress(0.85, "verifying_staging", "校验 staging 文件完整性")
        with rasterio.open(str(staging_out)) as check_ds:
            if check_ds.width <= 0 or check_ds.height <= 0:
                raise RuntimeError(f"Staging 成果栅格尺寸非法: {check_ds.width}x{check_ds.height}")

        # 提交文件事务
        context.progress(0.90, "committing", "提交 staging 成果至目标路径")
        context.commit_files([(staging_out, out_path)])

        # 终态重开核验
        context.progress(0.95, "verifying_final", "核验已安装目标栅格")
        with rasterio.open(str(out_path)) as final_ds:
            w, h, count = final_ds.width, final_ds.height, final_ds.count

        duration = round(time.monotonic() - start_time, 4)
        context.progress(1.0, "completed", "Rasterio 处理任务全部完成")
        context.log("INFO", f"成果提交成功: {out_path} ({w}x{h}, {count} 波段)")

        output_record: dict[str, Any] = {
            "key": out_plan.key,
            "uri": out_path.as_uri(),
            "data_type": "raster",
            "driver": dst_driver,
            "width": w,
            "height": h,
            "bands": count,
        }

        return TaskResult(
            task_id=spec.task_id,
            attempt=spec.attempt,
            status=TaskStatus.succeeded,
            outputs=[output_record],
            duration_seconds=duration,
            engine_runtime={
                "provider_id": "rasterio",
                "version": getattr(rasterio, "__version__", RASTERIO_VERSION),
            },
        )

    def probe_dataset(self, path: Path | str) -> dict[str, Any]:
        """通过 Rasterio 探测数据集元数据。"""
        from pathlib import Path
        if not RASTERIO_AVAILABLE or rasterio is None:
            raise RuntimeError("Rasterio 运行时不可用")
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {p}")
        with rasterio.open(str(p)) as src:
            bounds = list(src.bounds) if src.bounds else None
            crs_epsg = src.crs.to_epsg() if src.crs else None
            crs_wkt = src.crs.to_wkt() if src.crs else None
            gt = list(src.transform.to_gdal()) if src.transform else None
            bands_info = []
            for b_idx in range(1, src.count + 1):
                bands_info.append({
                    "band": b_idx,
                    "dtype": str(src.dtypes[b_idx - 1]),
                    "nodata": float(src.nodata) if src.nodata is not None else None,
                    "color_interp": str(src.colorinterp[b_idx - 1].name) if src.colorinterp else "undefined",
                    "overviews": len(src.overviews(b_idx)) if hasattr(src, "overviews") else 0,
                })
            return {
                "path": str(p),
                "driver": src.driver,
                "driver_long_name": src.driver,
                "width": src.width,
                "height": src.height,
                "count": src.count,
                "dtypes": [b["dtype"] for b in bands_info],
                "nodata": float(src.nodata) if src.nodata is not None else None,
                "crs_epsg": crs_epsg,
                "crs_wkt": crs_wkt,
                "geotransform": gt,
                "bounds": bounds,
                "bands": bands_info,
            }

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
        """通过 Rasterio 分块窗口读取（仅显示路径；原值窗口由 GDAL provider 提供）。"""
        if raw:
            raise RuntimeError(
                "rasterio provider 目前只返回显示拉伸结果，raw=True 原值窗口请用 gdal provider"
            )
        import base64
        from pathlib import Path

        import numpy as np
        from rasterio.windows import Window  # type: ignore[import-not-found]

        if not RASTERIO_AVAILABLE or rasterio is None:
            raise RuntimeError("Rasterio 运行时不可用")
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"文件不存在: {p}")

        with rasterio.open(str(p)) as src:
            tot_w, tot_h, tot_b = src.width, src.height, src.count
            x_off = max(0, min(x_off, tot_w - 1))
            y_off = max(0, min(y_off, tot_h - 1))
            win_w = max(1, min(win_w, tot_w - x_off))
            win_h = max(1, min(win_h, tot_h - y_off))
            out_w = max(1, min(out_w, 2048))
            out_h = max(1, min(out_h, 2048))

            target_bands = bands if bands else ([1] if tot_b == 1 else [1, 2, 3][:tot_b])
            window = Window(x_off, y_off, win_w, win_h)

            band_arrays = []
            stats = []
            for b_idx in target_bands:
                if b_idx < 1 or b_idx > tot_b:
                    continue
                arr = src.read(b_idx, window=window, out_shape=(out_h, out_w), resampling=rasterio.enums.Resampling.bilinear)
                arr = arr.astype(np.float32)
                nd = src.nodata
                valid_mask = (arr != nd) if nd is not None else np.isfinite(arr)
                if np.any(valid_mask):
                    b_min, b_max = float(np.min(arr[valid_mask])), float(np.max(arr[valid_mask]))
                else:
                    b_min, b_max = 0.0, 1.0
                stats.append({"band": b_idx, "min": b_min, "max": b_max})
                if b_max > b_min:
                    stretched = np.clip((arr - b_min) / (b_max - b_min) * 255.0, 0, 255).astype(np.uint8)
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

            b64_str = base64.b64encode(final_arr.tobytes()).decode("ascii")
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
