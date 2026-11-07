"""P5-01: reconcile the committed baseline files with the registry.

The audit report's denominator is the registry itself. These tests ensure the
contract baseline (GDAL 3.13.3 + Rasterio 1.5.2) has no silently missing entry
and that every runtime gap carries a concrete reason.
"""

from __future__ import annotations

import pytest

from spectra_workbench.registry import get_default_registry

pytestmark = [pytest.mark.unit]

PLATFORM_ONLY = {
    "gdal:bandmanage",
    "gdal:editmeta",
    "gdal:overview",
    "gdal:stack",
    "gdal:statistics",
}

TARGET_VERSION_TOOLS = {
    "gdal:buildvrt",
    "gdal:dem",
    "gdal:grid",
    "gdal:merge",
    "gdal:rasterize",
}


def test_baseline_reconciliation_has_no_missing_registry_entry() -> None:
    report = get_default_registry().reconcile_baseline()

    assert report["errors"] == []
    assert report["baseline_count"] == 443
    assert report["registry_count"] == 448
    assert report["missing_from_registry"] == []
    assert set(report["platform_only"]) == PLATFORM_ONLY
    assert report["complete"] is True


def test_target_version_tools_are_indexed_and_gaps_have_reasons() -> None:
    registry = get_default_registry()
    report = registry.reconcile_baseline()
    gap_ids = {item["capability_id"] for item in report["missing_or_blocked"]}

    for cid in TARGET_VERSION_TOOLS:
        assert registry.get(cid) is not None, f"目标版本工具未编目: {cid}"

    # Runtime gaps are explicitly retained instead of being silently dropped.
    for cid in (
        "gdal:cli:gdal_auth",
        "gdal:driver:HDF5",
        "rio:cli:cogeo",
        "rio:cli:color",
        "rio:cli:mbtiles",
    ):
        assert cid in gap_ids, f"运行期缺口未保留: {cid}"
        item = next(i for i in report["missing_or_blocked"] if i["capability_id"] == cid)
        assert item["limitations"], f"{cid} 缺少具体原因"

    # dem/grid/rasterize are indexed but the adapter is not wired yet.
    for cid in ("gdal:dem", "gdal:grid", "gdal:rasterize"):
        assert cid in gap_ids
        item = next(i for i in report["missing_or_blocked"] if i["capability_id"] == cid)
        assert item["availability"] == "blocked"
        assert item["limitations"]

    assert report["unexplained_gaps"] == []
