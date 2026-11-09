"""插件引擎适配器（P8-04）。

在独立 Worker 进程中运行，负责：
1. 动态加载插件 Entry Point 对应的 Python 执行函数
2. 执行校验、资源估算与算法计算
3. 隔离插件执行期异常，将其转化为符合规范的 TaskResult
4. 代理栅格探测与窗口读取（复用 GDALProviderAdapter）
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from spectra_workbench.domain.credentials import sanitize_text
from spectra_workbench.domain.models import (
    CapabilityDescriptor,
    TaskResult,
    TaskSpec,
    TaskStatus,
)
from spectra_workbench.plugins.manager import PluginManager
from spectra_workbench.plugins.manifest import PluginStatus
from spectra_workbench.providers.base import ExecutionContext, ProviderAdapter


class PluginProviderAdapter(ProviderAdapter):
    """插件运行时适配器。"""

    def __init__(self, plugins_dir: Path | str | None = None) -> None:
        self.manager = PluginManager(plugins_dir=plugins_dir)
        self._gdal_adapter: Any = None
        self.preload_errors: dict[str, str] = {}
        self._preload_native_modules()

    # 带原生扩展的基础依赖：必须在主线程导入。
    _BASE_PRELOAD: tuple[str, ...] = ("numpy",)

    def _preload_native_modules(self) -> None:
        """在主线程预导入插件依赖与入口模块。

        Windows 下 worker 主线程阻塞在 stdin 读取时，执行线程首次加载 numpy 等
        原生扩展 DLL 会死锁（已由看门狗栈转储确认）。adapter 由 WorkerRuntime
        在主线程、进入 stdin 循环之前构造，因此在此处预导入可彻底规避。
        导入失败只记录，不阻断 worker 启动；执行时会再次报出真实错误。
        """
        modules: list[str] = list(self._BASE_PRELOAD)
        try:
            plugins = self.manager.discover()
        except Exception as exc:
            self.preload_errors["<discover>"] = sanitize_text(str(exc))
            plugins = []
        for p in plugins:
            if p.status != PluginStatus.enabled:
                continue
            if p.install_path:
                p_str = str(p.install_path)
                if p_str not in sys.path:
                    sys.path.insert(0, p_str)
            modules.extend(p.manifest.worker_spec.dependencies)
            for c in p.manifest.capabilities:
                if c.entrypoint:
                    modules.append(c.entrypoint.split(":", 1)[0])
        for name in dict.fromkeys(modules):
            try:
                importlib.import_module(name)
            except Exception as exc:
                self.preload_errors[name] = sanitize_text(f"{type(exc).__name__}: {exc}")

    @property
    def provider_id(self) -> str:
        return "plugin"

    def _get_gdal_adapter(self) -> Any:
        if self._gdal_adapter is None:
            try:
                from spectra_workbench.providers.gdal_provider.adapter import GDALProviderAdapter

                self._gdal_adapter = GDALProviderAdapter()
            except Exception:
                self._gdal_adapter = None
        return self._gdal_adapter

    def runtime_info(self) -> dict[str, Any]:
        return {
            "available": True,
            "provider_id": "plugin",
            "plugin_count": len(self.manager.list_plugins()),
            "preload_errors": dict(self.preload_errors),
        }

    def discover(self) -> list[CapabilityDescriptor]:
        """枚举所有已启用插件声明的能力描述符。"""
        plugins = self.manager.discover()
        descriptors: list[CapabilityDescriptor] = []
        for p in plugins:
            if p.status.value == "enabled":
                for cap in p.manifest.capabilities:
                    descriptors.append(cap.to_descriptor(p.manifest))
        return descriptors

    def validate(self, spec: TaskSpec) -> dict[str, Any]:
        """校验 TaskSpec 是否有效。"""
        cid = spec.capability_id
        # 查找对应能力
        found_cap = None
        found_plugin = None
        discovered = self.manager.discover()
        for p in discovered:
            for c in p.manifest.capabilities:
                if c.capability_id == cid:
                    found_cap = c
                    found_plugin = p
                    break
            if found_cap:
                break

        if not found_cap or not found_plugin:
            return {"valid": False, "code": "capability_not_found", "errors": [f"Plugin capability {cid} not found"]}

        if found_plugin.status != PluginStatus.enabled:
            return {
                "valid": False,
                "code": "plugin_not_enabled",
                "errors": [
                    f"Plugin '{found_plugin.manifest.id}' is {found_plugin.status.value}: {found_plugin.incompatibility_reason or 'Not enabled'}"
                ],
            }

        # 校验必填输入
        for inp in found_cap.inputs:
            if inp.get("required", True) and inp.get("key") not in spec.inputs:
                return {
                    "valid": False,
                    "code": "missing_required_input",
                    "errors": [f"Missing required input '{inp.get('key')}'"],
                }

        return {"valid": True, "errors": []}

    def estimate(self, spec: TaskSpec) -> dict[str, Any]:
        return {"estimated_ram_mib": 64, "estimated_disk_bytes": 1024 * 1024}

    def _resolve_handler(self, spec: TaskSpec) -> tuple[str, Callable[[TaskSpec, ExecutionContext], TaskResult]]:
        """按 capability_id 解析并加载插件的执行函数（仅在 Worker 子进程执行）。"""
        cid = spec.capability_id
        entrypoint_str = None
        plugin_id = None
        found_plugin = None
        for p in self.manager.discover():
            for c in p.manifest.capabilities:
                if c.capability_id == cid:
                    entrypoint_str = c.entrypoint
                    plugin_id = p.manifest.id
                    found_plugin = p
                    break
            if entrypoint_str:
                break

        if not entrypoint_str or not plugin_id:
            raise ValueError(f"未找到插件能力的执行入口: {cid}")

        # 如果插件有安装目录，确保其在 sys.path 中
        if found_plugin and found_plugin.install_path:
            p_str = str(found_plugin.install_path)
            if p_str not in sys.path:
                sys.path.insert(0, p_str)

        # 解析 "module.path:function_name"
        if ":" in entrypoint_str:
            mod_name, func_name = entrypoint_str.split(":", 1)
        else:
            mod_name, func_name = entrypoint_str, "run"

        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            short_mod = mod_name.split(".")[-1]
            mod = importlib.import_module(short_mod)

        handler = getattr(mod, func_name, None)
        if handler is None:
            raise AttributeError(f"模块 {mod_name} 中未找到执行函数 {func_name}")

        return plugin_id, handler

    def execute(self, spec: TaskSpec, context: ExecutionContext) -> TaskResult:
        """执行插件任务，捕获异常并返回终态 TaskResult。"""
        plugin_id = None
        try:
            pid, handler = self._resolve_handler(spec)
            plugin_id = pid
            res = handler(spec, context)
            if res.status == TaskStatus.succeeded:
                self.manager.record_success(plugin_id)
            else:
                self.manager.record_failure(plugin_id, str(res.error or "task_failed"))
            return res
        except Exception as exc:
            context.log("ERROR", f"插件执行异常: {exc}")
            if plugin_id:
                self.manager.record_failure(plugin_id, str(exc))
            return TaskResult(
                task_id=spec.task_id,
                attempt=spec.attempt,
                status=TaskStatus.failed,
                error={"code": "plugin_execution_exception", "message": sanitize_text(str(exc))},
            )

    def probe_dataset(self, path: Path | str) -> dict[str, Any]:
        gdal_ad = self._get_gdal_adapter()
        if gdal_ad is not None:
            return gdal_ad.probe_dataset(path)
        raise NotImplementedError("GDAL runtime not available in plugin adapter for probe_dataset")

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
        gdal_ad = self._get_gdal_adapter()
        if gdal_ad is not None:
            return gdal_ad.read_window(path, x_off, y_off, win_w, win_h, out_w, out_h, bands, raw, overview)
        raise NotImplementedError("GDAL runtime not available in plugin adapter for read_window")
