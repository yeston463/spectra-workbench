"""Spectra Workbench 能力基准与注册表（P2-01 ~ P2-03）。"""

from __future__ import annotations

from spectra_workbench.registry.baseline import (
    BaselineMetadata,
    compute_capabilities_sha256,
    load_baseline_from_dict,
    load_baseline_from_file,
    serialize_baseline,
)
from spectra_workbench.registry.catalog import (
    CapabilityRegistry,
    default_baseline_files,
    export_standard_baselines,
    get_default_registry,
)
from spectra_workbench.registry.gdal_index import (
    GDAL_ALIASES,
    GDAL_VERSION,
    build_gdal_algorithms,
    build_gdal_baseline,
    build_gdal_cli_programs,
    build_gdal_drivers,
    build_gdal_python_api_index,
)
from spectra_workbench.registry.rasterio_index import (
    RASTERIO_ALIASES,
    RASTERIO_VERSION,
    build_rasterio_baseline,
    build_rasterio_python_api,
    build_rio_cli_commands,
)

__all__ = [
    "BaselineMetadata",
    "CapabilityRegistry",
    "GDAL_ALIASES",
    "GDAL_VERSION",
    "RASTERIO_ALIASES",
    "RASTERIO_VERSION",
    "build_gdal_algorithms",
    "build_gdal_baseline",
    "build_gdal_cli_programs",
    "build_gdal_drivers",
    "build_gdal_python_api_index",
    "build_rasterio_baseline",
    "build_rasterio_python_api",
    "build_rio_cli_commands",
    "compute_capabilities_sha256",
    "default_baseline_files",
    "export_standard_baselines",
    "get_default_registry",
    "load_baseline_from_dict",
    "load_baseline_from_file",
    "serialize_baseline",
]
