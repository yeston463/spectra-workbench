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
    "collect_protected_inputs",
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
        protected: list[Path] | None = None,
        code: str = "stale_sidecar_conflict",
    ):
        super().__init__(message)
        self.stale = stale or []
        self.unknown = unknown or []
        self.protected = protected or []
        self.code = code


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
    lower_name = candidate.name.lower()
    target_name = target.name.lower()
    # 只接受“目标名 + 已知 sidecar 后缀”或“同 stem + 已知后缀”。
    # 不再接受任意 target.name + "." 前缀（会把 mosaic.vrt.source.tif 误判）。
    for suffix in _SIDECAR_SUFFIXES:
        if lower_name == target_name + suffix.lower():
            return True
    return candidate.stem == target.stem and candidate.suffix.lower() in _SIDECAR_SUFFIXES


def collect_protected_inputs(sources: list[Path]) -> set[Path]:
    """Collect input main files and their real read dependencies.

    ENVI .dat/.img and its .hdr share a stem; VRT dependencies are reported by
    GetFileList. These files are read-only for the duration of an unrelated
    output and must never be overwritten by sidecar naming rules.
    """
    protected: set[Path] = set()
    for source in sources:
        src = source.resolve()
        protected.add(src)
        for candidate in known_sidecar_candidates(src):
            protected.add(candidate.resolve())
        try:
            from osgeo import gdal  # type: ignore[import-not-found]

            gdal.UseExceptions()
            ds = gdal.Open(str(src), gdal.GA_ReadOnly)
        except Exception:
            ds = None
        if ds is not None:
            try:
                for raw in ds.GetFileList() or []:
                    p = Path(str(raw))
                    if p.is_file():
                        protected.add(p.resolve())
            finally:
                ds = None
    return protected


def discover_sidecars(
    target: Path, *, protected_inputs: set[Path] | None = None
) -> SidecarReport:
    """Discover the existing target's owned sidecars without wildcard deletion."""
    target = target.resolve()
    protected = {p.resolve() for p in (protected_inputs or set())}
    report = SidecarReport(
        target=target,
        main_exists=target.exists(),
        driver=None,
    )

    # Known naming rules always count as owned candidates, unless they are
    # proved to be an input dependency (e.g. source.dat/source.hdr).
    for candidate in known_sidecar_candidates(target):
        resolved = candidate.resolve()
        if resolved in protected:
            continue
        report.owned.append(resolved)

    if not target.exists():
        report.owned = sorted(set(report.owned))
        return report

    try:
        from osgeo import gdal

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
        if candidate in protected:
            # Input dependencies are never old target sidecars, even if their
            # names happen to resemble target sidecars.
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


def ensure_no_stale_sidecars(
    target: Path,
    new_targets: list[Path],
    *,
    protected_inputs: set[Path] | None = None,
    allow_inplace: bool = False,
    allow_stale_deletion: bool = False,
) -> SidecarReport:
    """Refuse overwrite when output collides with inputs or leaves stale files.

    With ``allow_stale_deletion=True`` stale files are returned to the caller so
    they can be included in the commit transaction. Unknown ownership is still
    refused. The default remains conservative refusal.
    """
    target = target.resolve()
    protected = {p.resolve() for p in (protected_inputs or set())}
    report = discover_sidecars(target, protected_inputs=protected)
    new_set = {p.resolve() for p in new_targets}
    protected_collisions = sorted(new_set & protected)
    if protected_collisions and not (allow_inplace and target in protected):
        raise SidecarOwnershipError(
            "输出文件集合与受保护输入依赖冲突，拒绝写入："
            + ", ".join(p.name for p in protected_collisions),
            protected=protected_collisions,
            code="input_sidecar_collision",
        )

    stale = [p for p in report.owned if p not in new_set]
    unknown = [p for p in report.unknown if p not in new_set]
    if not report.ownership_complete and (stale or unknown):
        raise SidecarOwnershipError(
            f"目标 {target.name} 的 sidecar 所有权无法完整确认：{report.error}",
            stale=stale,
            unknown=unknown,
        )
    if unknown:
        raise SidecarOwnershipError(
            f"目标 {target.name} 存在归属不明的邻近文件，拒绝覆盖以免误删："
            + ", ".join(p.name for p in unknown),
            unknown=unknown,
        )
    if stale and not allow_stale_deletion:
        raise SidecarOwnershipError(
            f"目标 {target.name} 存在旧成果的 sidecar，拒绝覆盖以免新旧混用："
            + ", ".join(p.name for p in stale),
            stale=stale,
        )
    return report
