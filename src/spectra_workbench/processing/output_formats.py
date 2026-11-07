"""Output format support matrix (pure Python, GUI-safe).

The matrix is deliberately conservative and based on the actual GDAL 3.13.3
runtime: unsupported combinations are rejected before writing rather than
silently losing alpha, mask, NoData or dtype.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "FORMAT_RULES",
    "FormatRule",
    "VECTOR_OUTPUT_FORMATS",
    "normalize_output_format",
    "validate_output_format",
    "validate_vector_output_format",
]


@dataclass(frozen=True)
class FormatRule:
    name: str
    dtypes: frozenset[str] | None
    band_counts: frozenset[int] | None
    supports_nodata: bool
    supports_mask: bool
    supports_alpha: bool
    supports_georef: bool = True


_ALL = None
_GTIFF_DTYPES = frozenset(
    {
        "Byte",
        "UInt16",
        "Int16",
        "UInt32",
        "Int32",
        "UInt64",
        "Int64",
        "Float32",
        "Float64",
        "CInt16",
        "CInt32",
        "CFloat32",
        "CFloat64",
    }
)

FORMAT_RULES: dict[str, FormatRule] = {
    "GTIFF": FormatRule("GTiff", _GTIFF_DTYPES, _ALL, True, True, True),
    "COG": FormatRule("COG", _GTIFF_DTYPES, _ALL, True, True, True),
    "ENVI": FormatRule(
        "ENVI",
        frozenset(
            {
                "Byte",
                "UInt16",
                "Int16",
                "UInt32",
                "Int32",
                "Float32",
                "Float64",
                "CFloat32",
                "CFloat64",
            }
        ),
        _ALL,
        True,
        True,
        False,
    ),
    "PNG": FormatRule(
        "PNG",
        frozenset({"Byte", "UInt16"}),
        frozenset({1, 2, 3, 4}),
        False,
        False,
        True,
    ),
    "JPEG": FormatRule(
        "JPEG",
        frozenset({"Byte"}),
        frozenset({1, 3}),
        False,
        False,
        False,
    ),
    # VRT is a description; pixel semantics are inherited from sources.
    "VRT": FormatRule("VRT", _ALL, _ALL, True, True, True),
}


def normalize_output_format(format_name: str) -> str:
    return str(format_name or "").strip().upper()


def validate_output_format(
    format_name: str,
    *,
    dtype: str,
    bands: int,
    has_nodata: bool,
    has_alpha: bool,
    has_mask: bool = False,
) -> list[str]:
    """Return human-readable violations for an output combination."""
    key = normalize_output_format(format_name)
    rule = FORMAT_RULES.get(key)
    if rule is None:
        return [f"未登记的输出格式 {format_name!r}"]
    violations: list[str] = []
    dtype_name = str(dtype or "").replace("GDT_", "")
    if rule.dtypes is not None and dtype_name not in rule.dtypes:
        violations.append(
            f"{rule.name} 不支持数据类型 {dtype_name}；支持: {', '.join(sorted(rule.dtypes))}"
        )
    try:
        band_count = int(bands)
    except Exception:
        band_count = 0
    if band_count <= 0:
        violations.append("输出波段数无效")
    elif rule.band_counts is not None and band_count not in rule.band_counts:
        violations.append(
            f"{rule.name} 不支持 {band_count} 个波段；支持: "
            + ", ".join(str(b) for b in sorted(rule.band_counts))
        )
    if has_nodata and not rule.supports_nodata:
        violations.append(f"{rule.name} 不支持 NoData")
    if has_mask and not rule.supports_mask:
        violations.append(f"{rule.name} 不支持显式 mask")
    if has_alpha and not rule.supports_alpha:
        violations.append(f"{rule.name} 不支持 alpha 通道")
    if has_alpha and rule.band_counts is not None and band_count not in rule.band_counts:
        violations.append(f"{rule.name} 的 alpha 组合需要受支持的波段布局")
    if not rule.supports_georef:
        violations.append(f"{rule.name} 不支持地理参考")
    return violations


# ---------------------------------------------------------------------------
# 矢量（OGR）输出格式矩阵（P5-02 polygonize / contour 专用）
# ---------------------------------------------------------------------------
# 与栅格矩阵同一哲学：刻意保守、写盘前拒绝。
# 只接**单文件成果**驱动（GPKG / GeoJSON）——staging→commit 事务按单对文件移动；
# ESRI Shapefile 是 .shp/.shx/.dbf/.prj 多文件 bundle，需要专门的矢量 bundle
# 收集器才能与提交事务配合，未接入前必须明确拒绝而不是写出残缺成果。
VECTOR_OUTPUT_FORMATS: dict[str, str] = {
    # 规范化名 -> OGR 驱动短名
    "GPKG": "GPKG",
    "GEOJSON": "GeoJSON",
}


def validate_vector_output_format(format_name: str) -> list[str]:
    """Return human-readable violations for a vector (OGR) output format."""
    key = normalize_output_format(format_name)
    if key in VECTOR_OUTPUT_FORMATS:
        return []
    return [
        f"矢量成果仅支持 GPKG / GeoJSON，不支持 {format_name!r}；"
        "ESRI Shapefile 为多文件 bundle（.shp/.shx/.dbf/.prj），暂未接入"
    ]
