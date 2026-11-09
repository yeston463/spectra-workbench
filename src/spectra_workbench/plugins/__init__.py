"""插件扩展系统与可选引擎扩展登记（P8-04 / P8-05）。

提供：
1. PluginManifest, PluginCapabilitySpec, WorkerSpec：清单契约与兼容性模型
2. PluginManager：插件安装、发现、状态机（enabled/disabled/incompatible/failed）与连续失败熔断恢复
3. PluginProviderAdapter：Worker 进程内安全加载与执行插件能力
4. OptionalExtensionSpec 及登记表：Xarray/Dask、scikit-learn、PDAL、GeoPandas、外部校正与外部 GIS 引擎范围、依赖、入口与缺口登记
"""

from __future__ import annotations

from spectra_workbench.plugins.adapter import PluginProviderAdapter
from spectra_workbench.plugins.extensions import (
    EXTENSION_BASELINE_ID,
    OPTIONAL_EXTENSION_IDS,
    OPTIONAL_EXTENSION_SPECS,
    OptionalExtensionSpec,
    audit_optional_extensions,
    build_optional_extension_descriptors,
    get_optional_extension_spec,
    list_optional_extension_specs,
)
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
    "EXTENSION_BASELINE_ID",
    "OPTIONAL_EXTENSION_IDS",
    "OPTIONAL_EXTENSION_SPECS",
    "OptionalExtensionSpec",
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
    "audit_optional_extensions",
    "build_optional_extension_descriptors",
    "get_optional_extension_spec",
    "list_optional_extension_specs",
]
