"""插件扩展系统（P8-04）。

提供：
1. PluginManifest, PluginCapabilitySpec, WorkerSpec：清单契约与兼容性模型
2. PluginManager：插件安装、发现、状态机（enabled/disabled/incompatible/failed）与连续失败熔断恢复
3. PluginProviderAdapter：Worker 进程内安全加载与执行插件能力
"""

from __future__ import annotations

from spectra_workbench.plugins.adapter import PluginProviderAdapter
from spectra_workbench.plugins.manager import PluginError, PluginManager
from spectra_workbench.plugins.manifest import (
    PLATFORM_API_VERSION,
    PLATFORM_VERSION,
    PluginCapabilitySpec,
    PluginInfo,
    PluginManifest,
    PluginStatus,
    WorkerSpec,
)

__all__ = [
    "PLATFORM_API_VERSION",
    "PLATFORM_VERSION",
    "PluginCapabilitySpec",
    "PluginError",
    "PluginInfo",
    "PluginManager",
    "PluginManifest",
    "PluginProviderAdapter",
    "PluginStatus",
    "WorkerSpec",
]
