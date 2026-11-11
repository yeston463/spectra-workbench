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
    "ext:external_atmospheric",
    "ext:external_gis",
    "ext:geopandas_vector",
    "ext:pdal_pointcloud",
    "ext:sklearn_classify",
    "ext:xarray_dask",
    "gdal:accuracy_assessment",
    "gdal:bandmanage",
    "gdal:calibrate",
    "gdal:classify",
    "gdal:clip_mask",
    "gdal:cloudreplace",
    "gdal:dem_sample",
    "gdal:density_slice",
    "gdal:dos1",
    "gdal:editmeta",
    "gdal:fftfilter",
    "gdal:fvc",
    "gdal:gcpregister",
    "gdal:mask",
    "gdal:mdim_slice",
    "gdal:mosaic_advanced",
    "gdal:overview",
    "gdal:pca",
    "gdal:rasterdiff",
    "gdal:sam",
    "gdal:sindex",
    "gdal:stack",
    "gdal:statistics",
    "gdal:tasseledcap",
    "ogr:buffer",
    "platform:agriculture_count",
    "platform:flaash_external",
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
    # 第 58/59/60/61/62/63 轮新增平台能力（pansharpen 升级既有条目）：448 -> 461；
    # 第 69 轮（P8-01）新增 gdal:mdim_slice：461 -> 462；
    # 第 73 轮（P8-05）新增 6 项可选引擎扩展登记（全部 missing/blocked，不作为已实现宣传）：462 -> 468
    # 第 76 轮（P11-06/04/08）：468 -> 470
    # 第 77 轮（P11-03/05/02/09）：470 -> 476
    assert report["registry_count"] == 476
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

    # dem, rasterize, grid, polygonize, contour, vectortranslate, sindex and fftfilter are wired.
    for cid in (
        "gdal:dem",
        "gdal:rasterize",
        "gdal:grid",
        "gdal:polygonize",
        "gdal:contour",
        "ogr:vectortranslate",
        "gdal:sindex",
        "gdal:fftfilter",
        "gdal:pca",
        "gdal:tasseledcap",
        "gdal:gcpregister",
        "gdal:rasterdiff",
        "gdal:pansharpen",
        "gdal:mask",
        "gdal:fvc",
        "gdal:calibrate",
        "gdal:clip_mask",
        "gdal:dos1",
        "gdal:cloudreplace",
        "gdal:density_slice",
        "gdal:mosaic_advanced",
        "gdal:sam",
        "gdal:classify",
        "gdal:accuracy_assessment",
        "ogr:buffer",
        "gdal:dem_sample",
    ):
        cap = registry.get(cid)
        assert cap is not None and str(cap.state.availability) == "ready"
    for cid in ("gdal:cli:gdal_auth", "gdal:driver:HDF5"):
        assert cid in gap_ids
        item = next(i for i in report["missing_or_blocked"] if i["capability_id"] == cid)
        assert item["limitations"]

    assert report["unexplained_gaps"] == []
