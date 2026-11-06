"""Sidecar ownership discovery for transactional raster outputs.

S3 phase 1 uses a conservative rule:

- ``old_owned``: files that the existing target/driver explicitly reports as
  belonging to the target, plus known sidecar naming patterns for that target;
- ``new_owned``: the complete staging bundle mapped to final target names;
- ``stale_owned = old_owned - new_owned``: refuse the overwrite before commit.

VRT ``GetFileList`` also lists input dependencies. Those are never sidecars and
must never be deleted. This module therefore filters dependencies by target
name/stem and ignores VRT source entries entirely.

No ``basename*`` wildcard deletion is used. Unrecognized files are never
deleted; when a non-VRT driver reports a same-directory file that cannot be
classified, ownership is reported as unknown and the overwrite is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SidecarOwnershipError",
    "SidecarReport",
    "discover_sidecars",
    "ensure_no_stale_sidecars",
    "known_sidecar_candidates",
]

_SIDECAR_SUFFIXES = {
    ".hdr",
    ".aux.xml",
    ".ovr",
    ".msk",
    ".wld",
    ".tfw",
    ".tifw",
    ".jgw",
    ".pgw",
    ".bpw",
    ".gfw",
    ".enp",
    ".sta",
}


class SidecarOwnershipError(RuntimeError):
    """Existing target sidecars cannot be safely replaced by this output."""

    def __init__(
        self,
        message: str,
        *,
        stale: list[Path] | None = None,
        unknown: list[Path] | None = None,
    ):
        super().__init__(message)
        self.stale = stale or []
        self.unknown = unknown or []


@dataclass
class SidecarReport:
    target: Path
    main_exists: bool
    driver: str | None
    owned: list[Path] = field(default_factory=list)
    unknown: list[Path] = field(default_factory=list)
    ownership_complete: bool = True
    error: str | None = None


def _suffixes_for_target(target: Path) -> set[str]:
    suffixes = {".wld", ".hdr", ".aux.xml", ".ovr", ".msk", ".enp", ".sta"}
    suffix = target.suffix.lower()
    if suffix in {".tif", ".tiff"}:
        suffixes.update({".tfw", ".tifw"})
    elif suffix in {".jpg", ".jpeg"}:
        suffixes.add(".jgw")
    elif suffix == ".png":
        suffixes.add(".pgw")
    return suffixes


def known_sidecar_candidates(target: Path) -> list[Path]:
    """Return existing files that follow known sidecar naming for this target."""
    candidates: set[Path] = set()
    for suffix in _suffixes_for_target(target):
        if suffix == ".aux.xml" or suffix == ".ovr" or suffix == ".msk":
            candidates.add(target.with_name(target.name + suffix))
        else:
            candidates.add(target.with_suffix(suffix))
    return sorted(
        {p for p in candidates if p != target and p.is_file()},
        key=lambda p: p.name,
    )


def _is_owned_name(target: Path, candidate: Path) -> bool:
    if candidate == target or not candidate.is_file():
        return False
    if candidate.parent.resolve() != target.parent.resolve():
        return False
    if candidate.name.startswith(target.name + "."):
        return True
    return candidate.stem == target.stem and candidate.suffix.lower() in _SIDECAR_SUFFIXES


def discover_sidecars(target: Path) -> SidecarReport:
    """Discover the existing target's owned sidecars without wildcard deletion."""
    target = target.resolve()
    report = SidecarReport(
        target=target,
        main_exists=target.exists(),
        driver=None,
    )

    # Known naming rules always count as owned candidates.
    for candidate in known_sidecar_candidates(target):
        report.owned.append(candidate.resolve())

    if not target.exists():
        report.owned = sorted(set(report.owned))
        return report

    try:
        from osgeo import gdal  # type: ignore[import-not-found]

        gdal.UseExceptions()
        ds = gdal.Open(str(target), gdal.GA_ReadOnly)
    except Exception as exc:
        report.ownership_complete = False
        report.error = f"open_target_failed:{type(exc).__name__}:{exc}"
        report.owned = sorted(set(report.owned))
        return report

    if ds is None:
        report.ownership_complete = False
        report.error = "open_target_failed:None"
        report.owned = sorted(set(report.owned))
        return report

    try:
        report.driver = ds.GetDriver().ShortName
        try:
            raw_files = ds.GetFileList() or []
        finally:
            ds = None
    except Exception as exc:
        report.ownership_complete = False
        report.error = f"filelist_failed:{type(exc).__name__}:{exc}"
        report.owned = sorted(set(report.owned))
        return report

    for raw in raw_files:
        candidate = Path(str(raw)).resolve()
        if candidate == target:
            continue
        if _is_owned_name(target, candidate):
            report.owned.append(candidate)
            continue
        if report.driver.upper() == "VRT":
            # VRT lists source dependencies; they are never target sidecars.
            continue
        if candidate.parent.resolve() == target.parent.resolve():
            report.unknown.append(candidate)

    report.owned = sorted(set(report.owned))
    report.unknown = sorted(set(report.unknown))
    return report


def ensure_no_stale_sidecars(target: Path, new_targets: list[Path]) -> SidecarReport:
    """Refuse overwrite when the old target owns files this run will not produce.

    This is the phase-1 conservative policy: no deletion is attempted. A later
    phase can extend the transaction to back up and delete stale files.
    """
    report = discover_sidecars(target)
    new_set = {p.resolve() for p in new_targets}
    stale = [p for p in report.owned if p not in new_set]
    unknown = [p for p in report.unknown if p not in new_set]
    if not report.ownership_complete and (stale or unknown):
        raise SidecarOwnershipError(
            f"目标 {target.name} 的 sidecar 所有权无法完整确认：{report.error}",
            stale=stale,
            unknown=unknown,
        )
    if stale or unknown:
        parts: list[str] = []
        if stale:
            parts.append("本次不生成的旧 sidecar: " + ", ".join(p.name for p in stale))
        if unknown:
            parts.append("归属不明: " + ", ".join(p.name for p in unknown))
        raise SidecarOwnershipError(
            f"目标 {target.name} 存在旧成果的 sidecar，拒绝覆盖以免新旧混用："
            + "；".join(parts),
            stale=stale,
            unknown=unknown,
        )
    return report
