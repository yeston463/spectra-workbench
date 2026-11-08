"""Domain models and utilities."""

from spectra_workbench.domain.paths import (
    ARCHIVE_EXTENSIONS,
    VSI_PREFIXES,
    extract_local_path_from_vsi,
    get_display_name,
    is_archive,
    is_vsi_or_remote,
    normalize_vsi_path,
    sanitize_url,
)

__all__ = [
    "ARCHIVE_EXTENSIONS",
    "VSI_PREFIXES",
    "extract_local_path_from_vsi",
    "get_display_name",
    "is_archive",
    "is_vsi_or_remote",
    "normalize_vsi_path",
    "sanitize_url",
]
