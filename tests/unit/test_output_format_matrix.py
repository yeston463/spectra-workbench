"""S4 pure format matrix contract tests."""

from __future__ import annotations

from spectra_workbench.processing.output_formats import (
    validate_output_format,
    validate_vector_output_format,
)


def test_gtiff_accepts_float_nodata_mask_alpha() -> None:
    assert validate_output_format(
        "GTiff",
        dtype="Float32",
        bands=4,
        has_nodata=True,
        has_alpha=True,
        has_mask=True,
    ) == []


def test_envi_accepts_complex_but_rejects_alpha() -> None:
    assert validate_output_format(
        "ENVI",
        dtype="CFloat32",
        bands=3,
        has_nodata=True,
        has_alpha=False,
        has_mask=True,
    ) == []
    assert validate_output_format(
        "ENVI",
        dtype="Byte",
        bands=4,
        has_nodata=False,
        has_alpha=True,
    )


def test_png_rejects_float_nodata_and_too_many_bands() -> None:
    assert validate_output_format(
        "PNG", dtype="Float32", bands=3, has_nodata=False, has_alpha=False
    )
    assert validate_output_format(
        "PNG", dtype="Byte", bands=5, has_nodata=False, has_alpha=False
    )
    assert validate_output_format(
        "PNG", dtype="Byte", bands=3, has_nodata=True, has_alpha=False
    )
    assert validate_output_format(
        "PNG", dtype="Byte", bands=3, has_nodata=False, has_alpha=False, has_mask=True
    )


def test_png_accepts_rgba_alpha() -> None:
    assert validate_output_format(
        "PNG", dtype="Byte", bands=4, has_nodata=False, has_alpha=True
    ) == []


def test_jpeg_only_byte_one_or_three_bands() -> None:
    assert validate_output_format(
        "JPEG", dtype="Byte", bands=3, has_nodata=False, has_alpha=False
    ) == []
    assert validate_output_format(
        "JPEG", dtype="Float32", bands=3, has_nodata=False, has_alpha=False
    )
    assert validate_output_format(
        "JPEG", dtype="Byte", bands=4, has_nodata=False, has_alpha=True
    )


def test_unknown_format_rejected() -> None:
    assert validate_output_format(
        "NOT_A_FORMAT", dtype="Byte", bands=1, has_nodata=False, has_alpha=False
    )


def test_cog_and_vrt_accept_standard_raster_semantics() -> None:
    for fmt in ("COG", "VRT"):
        assert validate_output_format(
            fmt,
            dtype="Float32",
            bands=2,
            has_nodata=True,
            has_alpha=False,
            has_mask=True,
        ) == []


# --- P5-02：矢量（OGR）输出格式矩阵 ---------------------------------------


def test_vector_matrix_accepts_single_file_ogr_drivers() -> None:
    assert validate_vector_output_format("GPKG") == []
    assert validate_vector_output_format("GeoJSON") == []
    assert validate_vector_output_format("geojson") == []


def test_vector_matrix_rejects_raster_formats_and_shapefile() -> None:
    for fmt in ("GTiff", "PNG", "JPEG", "COG", "ENVI", "VRT", "ESRI Shapefile", ""):
        violations = validate_vector_output_format(fmt)
        assert violations, f"{fmt!r} 应被矢量矩阵拒绝"
        joined = "；".join(violations)
        assert "GPKG" in joined and "GeoJSON" in joined
        if fmt == "ESRI Shapefile":
            assert "多文件 bundle" in joined
