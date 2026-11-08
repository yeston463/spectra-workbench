"""GDAL 栅格处理参数的**单一契约源**（GUI 与 Worker 共用）。

为什么需要它
------------
第 25 轮复核发现三处「GUI 发的参数 Worker 不认」的静默失效：

1. GUI 写 ``parameters['resampling']='bilinear'``，Worker 只读 ``resample_alg``，
   于是双线性被静默执行成最近邻；
2. GUI 下拉框提供 5 种算法，Worker 的映射表只有 4 种，且名字对不上
   （GUI 的 ``near``、``lanczos`` 都不在表里）——选中后静默回退到最近邻；
3. ``dstNodata=0`` 被 ``or`` 短路丢弃，合法的零值参数丢失。

根因是**两处各维护一份参数名与取值集合**。本模块把契约收敛到一处：
GUI 提交前与 Worker 执行前都调用同一个 :func:`normalize_raster_params`，
从而做到「拼错即报错、未知算法不回退、别名冲突不择一」。

本模块刻意**不导入 osgeo**（只含纯 Python 数据与校验），
因此 GUI 进程可以安全导入，不违反「GUI 不 import 原生地理依赖」的硬约束。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from spectra_workbench.processing.tasseledcap_registry import sensor_keys

#: P6-03 PCA 模式与矩阵类型（GUI 与 Worker 共用）。
PCA_MODES: tuple[str, ...] = ("forward", "inverse")
PCA_MATRICES: tuple[str, ...] = ("covariance", "correlation")

#: P6-04 GCP / 差值 / 融合的受限选择（GUI 与 Worker 共用）。
GCP_TRANSFORMS: tuple[str, ...] = ("affine", "polynomial2")
LOCAL_RESAMPLES: tuple[str, ...] = ("nearest", "bilinear")
DIFF_MODES: tuple[str, ...] = ("difference", "absolute")
FUSION_METHODS: tuple[str, ...] = ("brovey", "hsv")

#: P6-05 FVC 端元模式（GUI 与 Worker 共用）。
FVC_ENDMEMBER_MODES: tuple[str, ...] = ("percentile", "values")

#: P6-06 定标模式 / DOS1（GUI 与 Worker 共用）。
CALIBRATE_MODES: tuple[str, ...] = ("physical", "toa_reflectance")

__all__ = [
    "CALIBRATE_MODES",
    "DIFF_MODES",
    "FUSION_METHODS",
    "FVC_ENDMEMBER_MODES",
    "GCP_TRANSFORMS",
    "LOCAL_RESAMPLES",
    "PCA_MATRICES",
    "PCA_MODES",
    "ParameterError",
    "RESAMPLE_ALGORITHMS",
    "RESAMPLE_ALIASES",
    "canonical_param_key",
    "normalize_raster_params",
    "resample_algorithm_label",
    "sensor_keys",
]


class ParameterError(ValueError):
    """参数非法（结构、取值或组合冲突）。

    Worker 侧转换为 ``invalid_parameters`` 错误；GUI 侧转换为提示文案。
    """


# --------------------------------------------------------------------------
# 重采样算法
# --------------------------------------------------------------------------
#: 支持的重采样算法（canonical 名）。Worker 会把名字映射到 GDAL 的 GRA_* 常量。
RESAMPLE_ALGORITHMS: tuple[str, ...] = (
    "nearest",
    "bilinear",
    "cubic",
    "cubic_spline",
    "lanczos",
    "average",
    "mode",
    "max",
    "min",
    "median",
    "q1",
    "q3",
)

#: 用户可写的小写别名 -> canonical 名。
RESAMPLE_ALIASES: dict[str, str] = {
    "near": "nearest",
    "neareast_neighbour": "nearest",
    "nearest_neighbour": "nearest",
    "nearestneighbor": "nearest",
    "bil": "bilinear",
    "cubic_spline": "cubic_spline",
    "cubicspline": "cubic_spline",
    "avg": "average",
    "med": "median",
    "quartile_1": "q1",
    "quartile_3": "q3",
}

#: GUI 下拉框展示用（canonical 名, 显示文本）。
#: **顺序即 GUI 默认值**：第一项是默认重采样算法。保持 bilinear 在首位，
#: 与改造前的 GUI 行为一致（本轮只修参数一致性，不顺带改默认行为）。
RESAMPLE_ALGORITHM_CHOICES: tuple[tuple[str, str], ...] = (
    ("bilinear", "双线性插值 (bilinear)"),
    ("nearest", "最近邻 (nearest)"),
    ("cubic", "三次卷积 (cubic)"),
    ("cubic_spline", "三次样条 (cubic_spline)"),
    ("lanczos", "Lanczos (lanczos)"),
    ("average", "平均 (average)"),
    ("mode", "众数 (mode)"),
    ("max", "最大值 (max)"),
    ("min", "最小值 (min)"),
    ("median", "中位数 (median)"),
    ("q1", "第一四分位 (q1)"),
    ("q3", "第三四分位 (q3)"),
)


def resample_algorithm_label(name: str) -> str:
    """返回算法名的中文显示文本。"""
    for canonical, label in RESAMPLE_ALGORITHM_CHOICES:
        if canonical == name:
            return label
    return name


def _normalize_resample_name(raw: Any) -> str:
    """把用户填写的算法名归一化为 canonical 名；未知则报错（绝不回退）。"""
    if raw is None:
        return "nearest"
    key = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if key in RESAMPLE_ALGORITHMS:
        return key
    if key in RESAMPLE_ALIASES:
        return RESAMPLE_ALIASES[key]
    raise ParameterError(
        f"未知的重采样算法 {raw!r}；可用算法：{', '.join(RESAMPLE_ALGORITHMS)}"
    )


# --------------------------------------------------------------------------
# 参数别名表：canonical key -> 可接受的别名（含 CLI 风格与 snake_case）
# --------------------------------------------------------------------------
_PARAM_ALIASES: dict[str, tuple[str, ...]] = {
    "resampleAlg": ("resample_alg", "resampling", "resample", "resample_method"),
    "srcWin": ("src_win", "source_window", "window", "pixel_window"),
    "projWin": ("proj_win", "projection_window", "geo_window"),
    "outputBounds": ("output_bounds", "bounds", "extent"),
    "xRes": ("x_res", "x_resolution", "xresolution"),
    "yRes": ("y_res", "y_resolution", "yresolution"),
    "outputType": ("output_type", "dtype", "data_type"),
    "noData": ("no_data", "nodata", "src_nodata"),
    "dstNodata": ("dst_nodata", "dstnodata", "dst_no_data"),
    "dstSRS": ("dst_srs", "dst_crs", "dstcrs", "target_crs"),
    "srcSRS": ("src_srs", "src_crs", "srccrs", "source_crs"),
    "targetAlignedPixels": ("target_aligned_pixels", "tap", "align_pixels"),
    "width": ("out_width", "outwidth", "output_width"),
    "height": ("out_height", "outheight", "output_height"),
    # --- P4-02 拼接 / VRT / 堆叠 / 波段管理 ---
    "overlapPriority": ("overlap_priority", "priority"),
    "resolution": ("resolution_policy", "res"),
    "srcNodata": ("src_nodata", "source_nodata"),
    "vrtNodata": ("vrt_nodata",),
    "separate": ("separate_sources",),
    "relativePaths": ("relative_paths", "use_relative_paths"),
    "bandList": ("band_list", "band_order", "bands"),
    # --- P4-03 元数据 / 概览 ---
    "levels": ("levels", "overview_levels", "decimations"),
    "overviewCompress": ("overview_compress", "ovr_compress"),
    # --- P5-02 地形分析 ---
    "processing": ("processing", "dem_processing", "dem_mode"),
    "zFactor": ("z_factor", "zfactor", "z_scale"),
    "scale": ("scale", "vertical_scale"),
    # --- P5-02 矢量栅格化 ---
    "burnValues": ("burn_values", "burn", "burn_value"),
    "attribute": ("attribute", "attribute_name", "field", "field_name"),
    "allTouched": ("all_touched", "alltouched"),
    # --- P5-02 散点栅格插值 ---
    "algorithm": ("algorithm", "grid_algorithm"),
    "zField": ("z_field", "zfield", "z_attribute"),
    # --- P5-02 栅格矢量化 / 等值线 ---
    # 注意：field_name / field 已是 attribute 的别名，不得重复占用。
    "fieldName": ("dn_field",),
    "connectedness": ("connected", "connectivity"),
    "band": ("band", "band_index"),
    "interval": ("contour_interval", "level_interval"),
    "offset": ("contour_base", "base_level"),
    "fixedLevels": ("fixed_levels", "fixed_levels_contour"),
    # --- P5-04 矢量处理 / SQL / 坐标转换（ogr:vectortranslate）---
    "sql": ("sql", "sql_statement", "sql_query"),
    "spat": ("spat", "spatial_filter", "spat_bbox"),
    "where": ("where", "where_clause"),
    "select": ("select", "select_fields"),
    # --- P6-01 光谱指数 ---
    "sindex": ("sindex", "spectral_index"),
    "bandA": ("band_a", "bandA"),
    "bandB": ("band_b", "bandB"),
    "saviL": ("savi_l", "saviL"),
    "usePhysical": ("use_physical", "physical_values"),
    # --- P8-01 多维数据（netCDF/HDF 经 GDAL multidim 模型读写）---
    "array": ("array", "mdarray", "md_array"),
    "dimSelect": ("dim_select", "dimselect", "dimensions", "dim_slice"),
    # --- P6-02 FFT 滤波 / 去条纹 ---
    "fftBand": ("fft_band", "fftBand"),
    "suppressBins": ("suppress_bins", "fft_suppress"),
    # --- P6-03 PCA / 缨帽 ---
    "pcaMode": ("pca_mode", "pcamode"),
    "pcaComponents": ("pca_components", "components", "n_components"),
    "pcaMatrix": ("pca_matrix", "pcamatrix"),
    "pcaTransform": ("pca_transform", "transform_file", "transformfile"),
    "tcSensor": ("tc_sensor", "tasseled_cap_sensor", "sensor"),
    "tcBands": ("tc_bands", "tasseled_cap_bands"),
    # --- P6-04 GCP / 差值 / 融合 ---
    "gcpTransform": ("gcp_transform", "gcp_mode"),
    "gcpFile": ("gcp_file", "gcp_points", "gcps"),
    "referenceFile": ("reference_file", "reference"),
    "gcpResample": ("gcp_resample", "gcp_resampling"),
    "diffMode": ("diff_mode", "difference_mode"),
    "msFile": ("ms_file", "multispectral"),
    "msBands": ("ms_bands", "multispectral_bands"),
    "fusionMethod": ("fusion_method", "pansharpen_method"),
    "fusionResample": ("fusion_resample", "fusion_resampling"),
    # --- P6-05 掩膜 / FVC ---
    "maskBand": ("mask_band", "mask_src_band"),
    "maskLow": ("mask_low", "mask_threshold_low"),
    "maskHigh": ("mask_high", "mask_threshold_high"),
    "maskInvert": ("mask_invert", "invert_mask"),
    "fvcBand": ("fvc_band", "ndvi_band"),
    "endmemberMode": ("endmember_mode", "fvc_endmember_mode"),
    "lowPercentile": ("low_percentile", "fvc_low_percentile"),
    "highPercentile": ("high_percentile", "fvc_high_percentile"),
    "fvcSoil": ("fvc_soil", "soil_endmember"),
    "fvcVeg": ("fvc_veg", "veg_endmember"),
    "fvcClassEdges": ("fvc_class_edges", "class_edges"),
    "maskFile": ("mask_file", "fvc_mask_file"),
    "crop": ("crop", "fvc_crop"),
    # --- P6-06 辐射定标 / DOS1 / 去云 ---
    "calibrateMode": ("calibrate_mode", "radiometric_mode"),
    "calibrateGains": ("calibrate_gains", "radiance_gains"),
    "calibrateOffsets": ("calibrate_offsets", "radiance_offsets"),
    "calibrateEsun": ("calibrate_esun", "esun_list", "calibrate_sun_zen_esun"),
    "sunZenAngle": ("sun_zen_angle", "sun_zenith_angle", "sun_elevation"),
    "dosBand": ("dos_band", "dos_src_band"),
    "hazeDN": ("haze_dn", "dark_object_dn", "haze_dn_value"),
    "dosEsun": ("dos_esun", "esun"),
    "earthSunDistance": ("earth_sun_distance", "d_squared"),
    "dosGain": ("dos_gain", "linear_gain"),
    "dosOffset": ("dos_offset", "linear_offset"),
    "secondaryFile": ("secondary_file", "secondary"),
    "cloudMaskFile": ("cloud_mask_file", "cloud_mask"),
    "primaryTime": ("primary_time", "obs_time_primary"),
    "secondaryTime": ("secondary_time", "obs_time_secondary"),
    "maskDilatePixels": ("mask_dilate_pixels", "dilate_pixels"),
    # --- P4-04 表达式 / 直方图与基础统计 ---
    "calc": ("calc_expr", "expression", "formula"),
    "statisticsMode": ("mode", "stats_mode", "statistics_mode"),
    "histogramBins": ("bins", "histogram_bins", "n_bins"),
    "maxSamples": ("max_samples", "sample_budget"),
    "fullLimit": ("full_limit", "full_pixel_limit"),
}

#: 重叠区的取值来源优先级。
#: **GDAL 3.13.3 实测**：BuildVRT 的重叠区取**后加入**的源（`[A,B]` → B 胜），
#: 因此 "last" 直接按原顺序传入，"first" 需**反转输入顺序**后传入。
OVERLAP_PRIORITIES: tuple[str, ...] = ("last", "first")

#: GDAL BuildVRT 的分辨率策略。
VRT_RESOLUTIONS: tuple[str, ...] = (
    "first",
    "estimate",
    "average",
    "highest",
    "lowest",
    "same",
)

#: 概览重采样方式（GDAL BuildOverviews 支持的常用子集）。
OVERVIEW_RESAMPLINGS: tuple[str, ...] = (
    "NEAREST",
    "AVERAGE",
    "GAUSS",
    "CUBIC",
    "MODE",
)

#: 概览压缩方式（作用于内嵌概览或外部 .ovr——两者本质都是 GTiff）。
OVERVIEW_COMPRESSIONS: tuple[str, ...] = ("NONE", "DEFLATE", "LZW")

#: GDAL DEMProcessing 支持的地形模式。
#: color-relief 需要额外颜色表文件，当前未接入，故不列入可提交集合。
DEM_PROCESSINGS: tuple[str, ...] = (
    "hillshade",
    "slope",
    "aspect",
    "TRI",
    "TPI",
    "roughness",
)

#: 允许出现在 parameters 里、但不属于上述栅格几何参数的非几何键。
_OTHER_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "format",
        "overwrite",
        "creation_options",
        "driver",
        "band",
        "select",
        "scale",
        "offset",
        "metadata",
        "options",
    }
)

#: canonical key -> 别名（含 canonical 自身）
_ALL_ALIASES: dict[str, tuple[str, ...]] = {
    canonical: (canonical, *aliases) for canonical, aliases in _PARAM_ALIASES.items()
}

#: 反查表：别名 -> canonical
_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _names in _ALL_ALIASES.items():
    for _name in _names:
        _ALIAS_TO_CANONICAL[_name] = _canonical


# --------------------------------------------------------------------------
# 能力支持矩阵
# --------------------------------------------------------------------------
# 「参数名被识别」不等于「当前能力会消费它」。
# 下列矩阵声明每种能力**实际支持**的参数；给出不支持的参数必须明确拒绝
# （invalid_parameters），不得接受后静默忽略。
_CAPABILITY_SUPPORT: dict[str, frozenset[str]] = {
    "gdal:translate": frozenset(
        {
            "srcWin",
            "projWin",
            "outputBounds",
            "xRes",
            "yRes",
            "width",
            "height",
            "outputType",
            "noData",
            "resampleAlg",
        }
    ),
    "gdal:warp": frozenset(
        {
            "srcWin",
            "projWin",
            "outputBounds",
            "xRes",
            "yRes",
            "width",
            "height",
            "outputType",
            "dstNodata",
            "dstSRS",
            "srcSRS",
            "targetAlignedPixels",
            "resampleAlg",
        }
    ),
    # Rasterio convert 当前不消费裁剪/网格/重投影参数：
    # 给出这些参数必须报错，而不是接受后忽略。
    "rasterio:convert": frozenset(),
    # --- P4-02：拼接 / VRT / 堆叠 / 波段管理 ---
    "gdal:buildvrt": frozenset(
        {
            "overlapPriority",
            "resolution",
            "srcNodata",
            "vrtNodata",
            "separate",
            "relativePaths",
        }
    ),
    "gdal:merge": frozenset(
        {
            "overlapPriority",
            "resolution",
            "srcNodata",
            "vrtNodata",
            "dstNodata",
            "xRes",
            "yRes",
            "outputBounds",
            "outputType",
        }
    ),
    "gdal:stack": frozenset(
        {
            "srcNodata",
            "vrtNodata",
            "xRes",
            "yRes",
            "outputBounds",
            "outputType",
        }
    ),
    "gdal:bandmanage": frozenset({"bandList", "outputType"}),
    # --- P4-03：元数据编辑 / 概览构建（均为 in-place 更新）---
    "gdal:editmeta": frozenset({"srcNodata"}),
    "gdal:overview": frozenset({"resampleAlg", "levels", "overviewCompress"}),
    # --- P5-02：地形分析 ---
    "gdal:dem": frozenset({"processing", "zFactor", "scale"}),
    # --- P5-02：矢量栅格化 ---
    "gdal:rasterize": frozenset(
        {
            "xRes",
            "yRes",
            "width",
            "height",
            "outputBounds",
            "outputType",
            "burnValues",
            "attribute",
            "allTouched",
            "noData",
        }
    ),
    # --- P5-02：散点栅格插值 ---
    "gdal:grid": frozenset(
        {
            "algorithm",
            "zField",
            "width",
            "height",
            "outputBounds",
            "outputType",
            "noData",
        }
    ),
    # --- P5-02：栅格矢量化 / 等值线（成果为矢量）---
    "gdal:polygonize": frozenset({"fieldName", "connectedness", "band"}),
    "gdal:contour": frozenset({"interval", "offset", "fixedLevels", "band"}),
    # --- P5-04：矢量处理 / SQL / 坐标转换（成果为矢量）---
    # format 是非几何透传键（_OTHER_ALLOWED_KEYS），不进矩阵
    "ogr:vectortranslate": frozenset({"sql", "dstSRS", "spat", "where", "select"}),
    # --- P6-01：光谱指数（成果为单波段 Float32 栅格）---
    "gdal:sindex": frozenset(
        {"sindex", "bandA", "bandB", "saviL", "usePhysical", "srcNodata", "dstNodata"}
    ),
    # --- P6-02：FFT 滤波 / 去条纹（成果为单波段 Float32 栅格 + 掩膜 sidecar）---
    "gdal:fftfilter": frozenset(
        {"fftBand", "suppressBins", "srcNodata", "dstNodata"}
    ),
    # --- P6-03：PCA 正/逆变换（成果为多波段 Float32 栅格 + 变换 sidecar）---
    "gdal:pca": frozenset(
        {
            "pcaMode",
            "pcaComponents",
            "pcaMatrix",
            "pcaTransform",
            "srcNodata",
            "dstNodata",
        }
    ),
    # --- P6-03：缨帽变换（成果为 3 波段 Float32 栅格 + 来源 sidecar）---
    "gdal:tasseledcap": frozenset(
        {"tcSensor", "tcBands", "srcNodata", "dstNodata"}
    ),
    # --- P6-04：GCP 配准（输入源影像 + 参考影像 + GCP JSON）---
    "gdal:gcpregister": frozenset(
        {
            "gcpTransform",
            "gcpFile",
            "referenceFile",
            "gcpResample",
            "srcNodata",
            "dstNodata",
        }
    ),
    # --- P6-04：栅格差值（输入 A + 参考 B，严格同网格）---
    "gdal:rasterdiff": frozenset(
        {"diffMode", "referenceFile", "srcNodata", "dstNodata"}
    ),
    # --- P6-04：Brovey/HSV 融合（Pan 输入 + MS 文件/3 波段）---
    "gdal:pansharpen": frozenset(
        {
            "msFile",
            "msBands",
            "fusionMethod",
            "fusionResample",
            "srcNodata",
            "dstNodata",
        }
    ),
    # --- P6-05：掩膜（二值 Byte，无 NoData）---
    "gdal:mask": frozenset(
        {
            "maskBand",
            "maskLow",
            "maskHigh",
            "maskInvert",
            "srcNodata",
        }
    ),
    # --- P6-05：FVC（NDVI 输入 + 端元/分位 + 分级）---
    "gdal:fvc": frozenset(
        {
            "fvcBand",
            "endmemberMode",
            "lowPercentile",
            "highPercentile",
            "fvcSoil",
            "fvcVeg",
            "fvcClassEdges",
            "maskFile",
            "crop",
            "srcNodata",
            "dstNodata",
        }
    ),
    # --- P6-06：辐射定标（physical / toa_reflectance）---
    "gdal:calibrate": frozenset(
        {
            "calibrateMode",
            "calibrateGains",
            "calibrateOffsets",
            "calibrateEsun",
            "sunZenAngle",
            "srcNodata",
            "dstNodata",
        }
    ),
    # --- P6-06：DOS1 大气校正（Chavez 1988 暗目标减法）---
    "gdal:dos1": frozenset(
        {
            "dosBand",
            "hazeDN",
            "dosEsun",
            "sunZenAngle",
            "dosGain",
            "dosOffset",
            "earthSunDistance",
            "srcNodata",
            "dstNodata",
        }
    ),
    # --- P6-06：多时相去云（掩膜驱动替换）---
    "gdal:cloudreplace": frozenset(
        {
            "secondaryFile",
            "cloudMaskFile",
            "bandList",
            "primaryTime",
            "secondaryTime",
            "maskDilatePixels",
            "srcNodata",
            "dstNodata",
        }
    ),
    # --- P8-01：多维数组二维切片（netCDF/HDF 按 GDAL multidim 模型）---
    "gdal:mdim_slice": frozenset({"array", "dimSelect"}),
    # --- P4-04：受限表达式计算 / 直方图与基础统计 ---
    "gdal:calc": frozenset({"calc", "srcNodata", "dstNodata", "outputType"}),
    "gdal:statistics": frozenset(
        {
            "statisticsMode",
            "histogramBins",
            "maxSamples",
            "fullLimit",
            "bandList",
            "srcNodata",
        }
    ),
}

#: 能力别名 -> canonical capability_id。
_CAPABILITY_ALIASES: dict[str, str] = {
    "gdal_translate": "gdal:translate",
    "gdaltranslate": "gdal:translate",
    "gdalwarp": "gdal:warp",
    "gdal_warp": "gdal:warp",
    # Rasterio：必须登记 RasterioProviderAdapter.SUPPORTED_CAPABILITIES 里的
    # **全部**能力，否则未登记的会落到「未知能力」分支而跳过能力级校验，
    # 从而绕过「参数必须被消费或被拒绝」的规则。
    "rasterio:convert": "rasterio:convert",
    "rasterio:cli:convert": "rasterio:convert",
    "rio_convert": "rasterio:convert",
    "rio:cli:convert": "rasterio:convert",
    "rasterio:copy": "rasterio:convert",
    "rasterio:api:copy": "rasterio:convert",
    # --- P4-02 能力别名（registry 的 CLI 风格名）---
    "gdalbuildvrt": "gdal:buildvrt",
    "gdal:build-vrt": "gdal:buildvrt",
    "gdal_merge": "gdal:merge",
    "gdal_merge.py": "gdal:merge",
    "gdal mosaic": "gdal:merge",
    # --- P4-03 能力别名 ---
    "gdal_edit": "gdal:editmeta",
    "gdal_edit.py": "gdal:editmeta",
    "gdaladdo": "gdal:overview",
    "gdaladdo.py": "gdal:overview",
    # --- P4-04 能力别名 ---
    "gdal_calc": "gdal:calc",
    "gdal_calc.py": "gdal:calc",
    "gdal raster calc": "gdal:calc",
    "gdal:histogram": "gdal:statistics",
    "gdalinfo -stats": "gdal:statistics",
    # --- P5-02 能力别名 ---
    "gdal_polygonize": "gdal:polygonize",
    "gdal_contour": "gdal:contour",
    # --- P5-04 能力别名 ---
    "ogr2ogr": "ogr:vectortranslate",
    # 保留 rio_convert / rio:convert 等 CLI 风格别名
    "rio:convert": "rasterio:convert",
}


def canonical_capability_id(capability_id: str | None) -> str | None:
    """把能力 ID 归一化为 canonical 名；未知返回 None。"""
    if not capability_id:
        return None
    key = str(capability_id).strip()
    if key in _CAPABILITY_SUPPORT:
        return key
    return _CAPABILITY_ALIASES.get(key.lower())


def supported_params_for(capability_id: str | None) -> frozenset[str] | None:
    """返回该能力支持的参数集合；未知能力返回 None（不做能力级校验）。"""
    canonical = canonical_capability_id(capability_id)
    if canonical is None:
        return None
    return _CAPABILITY_SUPPORT[canonical]


def canonical_param_key(key: str) -> str | None:
    """返回某个参数名的 canonical 名；不是栅格几何参数时返回 None。"""
    return _ALIAS_TO_CANONICAL.get(key)


# --------------------------------------------------------------------------
# 取值解析
# --------------------------------------------------------------------------
def _is_int_like(value: Any) -> bool:
    """是否是整数（含整型浮点），且不接受 bool。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value.is_integer()
    return False


def _strict_int(value: Any, key_hint: str) -> int:
    """整数字符串/整型浮点 → int（小数/bool/其他形状显式拒绝）。"""
    if isinstance(value, bool):
        raise ParameterError(f"dimSelect 的 {key_hint!r} 索引必须是整数，实际 {value!r}")
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ParameterError(
            f"dimSelect 的 {key_hint!r} 索引必须是整数，实际 {value!r}"
        )
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError as exc:
            raise ParameterError(
                f"dimSelect 的 {key_hint!r} 索引必须是整数，实际 {value!r}"
            ) from exc
    raise ParameterError(f"dimSelect 的 {key_hint!r} 索引必须是整数，实际 {value!r}")


def _parse_dim_select_text(text: str) -> dict[str, int]:
    """``time=1,depth=0`` → dict（重复维度/缺键/非整数索引显式拒绝）。"""
    result: dict[str, int] = {}
    for pair in text.split(","):
        if "=" not in pair:
            raise ParameterError(f"dimSelect 条目必须是 dim=index 形式，实际 {pair!r}")
        k, _, raw = pair.partition("=")
        key = k.strip()
        val = raw.strip()
        if not key or not val:
            raise ParameterError(f"dimSelect 条目键/值缺失：{pair!r}")
        if key in result:
            raise ParameterError(f"dimSelect 维度重复：{key!r}")
        result[key] = _strict_int(val, key)
    return result


def _normalize_dim_select(value: Any) -> dict[str, int]:
    """P8-01 dimSelect：dict[str, int] 或 ``k=v`` 逗号分隔字符串。

    - dict：键 str、值整数（整型浮点可，小数拒绝；bool 拒绝）；
    - 字符串：``time=1,depth=0``；键/值两侧空白忽略；未知形状显式拒绝；
    - 空选择 / 空字符串 → ParameterError（钉死数量校验由 adapter 结合
      数组秩完成，这里只做形态校验）。
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ParameterError("参数 dimSelect 不能为空（dim=index 列表）")
        return _parse_dim_select_text(text)
    if isinstance(value, dict):
        result2: dict[str, int] = {}
        for k, v in value.items():
            if not isinstance(k, str) or not k.strip():
                raise ParameterError(f"dimSelect 键必须是非空字符串，实际 {k!r}")
            if k.strip() in result2:
                raise ParameterError(f"dimSelect 维度重复：{k.strip()!r}")
            result2[k.strip()] = _strict_int(v, k.strip())
        return result2
    raise ParameterError(
        "参数 dimSelect 必须是 dict 或 \"dim=index\" 逗号分隔字符串，实际 "
        f"{type(value).__name__}"
    )


def _require_int_list(value: Any, name: str, expected: int) -> list[int]:
    """解析定长整数列表。**小数必须报错，不得静默截断。**"""
    if not isinstance(value, (list, tuple)):
        raise ParameterError(f"参数 {name} 必须是长度 {expected} 的整数列表")
    if len(value) != expected:
        raise ParameterError(f"参数 {name} 必须有 {expected} 个元素，实际 {len(value)} 个")
    out: list[int] = []
    for v in value:
        if not _is_int_like(v):
            raise ParameterError(
                f"参数 {name} 的元素必须是整数（不接受小数截断），实际含 {v!r}"
            )
        out.append(int(v))
    return out


def _require_float_list_any(value: Any, name: str) -> list[float]:
    """解析**可变长度**浮点列表（要求有限值），用于 per-band gain/offset/esun 序列。"""
    if not isinstance(value, (list, tuple)):
        raise ParameterError(f"参数 {name} 必须是数值列表")
    out: list[float] = []
    for v in value:
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ParameterError(f"参数 {name} 含非数值元素：{v!r}") from exc
        if not math.isfinite(f):
            raise ParameterError(f"参数 {name} 含非有限值：{v!r}")
        out.append(f)
    return out


def _require_choice(value: Any, choices: tuple[str, ...], name: str) -> str:
    """解析受限字符串选项（归一化为小写），未知值明确拒绝。"""
    text = str(value or "").strip().lower()
    if text not in choices:
        raise ParameterError(
            f"参数 {name} 必须是 {', '.join(choices)} 之一，实际 {value!r}"
        )
    return text


def _require_float_list(value: Any, name: str, expected: int) -> list[float]:
    """解析定长浮点列表（要求有限值）。"""
    if not isinstance(value, (list, tuple)):
        raise ParameterError(f"参数 {name} 必须是长度 {expected} 的数值列表")
    if len(value) != expected:
        raise ParameterError(f"参数 {name} 必须有 {expected} 个元素，实际 {len(value)} 个")
    out: list[float] = []
    for v in value:
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ParameterError(f"参数 {name} 含非数值元素：{v!r}") from exc
        if not math.isfinite(f):
            raise ParameterError(f"参数 {name} 含非有限值：{v!r}")
        out.append(f)
    return out


def _require_positive_float(value: Any, name: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ParameterError(f"参数 {name} 必须是数值，实际 {value!r}") from exc
    if not math.isfinite(f):
        raise ParameterError(f"参数 {name} 必须是有限值，实际 {value!r}")
    if f <= 0:
        raise ParameterError(f"参数 {name} 必须为正数，实际 {f}")
    return f


def _require_positive_int(value: Any, name: str) -> int:
    if not _is_int_like(value):
        raise ParameterError(f"参数 {name} 必须是整数（不接受小数截断），实际 {value!r}")
    iv = int(value)
    if iv <= 0:
        raise ParameterError(f"参数 {name} 必须为正数，实际 {iv}")
    return iv


def _require_non_negative_int(value: Any, name: str) -> int:
    if not _is_int_like(value):
        raise ParameterError(f"参数 {name} 必须是整数（不接受小数截断），实际 {value!r}")
    iv = int(value)
    if iv < 0:
        raise ParameterError(f"参数 {name} 必须为非负整数，实际 {iv}")
    return iv


# --------------------------------------------------------------------------
# 归一化
# --------------------------------------------------------------------------
def normalize_raster_params(
    raw: Mapping[str, Any] | None,
    *,
    capability_id: str | None = None,
) -> dict[str, Any]:
    """归一化并校验栅格处理参数。

    返回 canonical 名 -> 解析后的值（只含**实际提供**的参数）。
    任何非法取值、未知算法、别名冲突、能力不支持的参数都抛 :class:`ParameterError`。

    关键约定：
    - **零值是合法值**：按「是否提供 / 是否为 None」判断，绝不用 truthiness；
    - **未知算法不回退**：直接报错；
    - **小数不截断**：整数参数遇到小数报错；
    - **别名冲突不择一**：同一 canonical 的多个别名给出不同值时报错；
    - **能力不支持就拒绝**：参数名被识别不等于当前能力会消费它；
      给出当前能力不消费的参数必须报错，不得接受后静默忽略
      （``capability_id`` 为 None 时跳过能力级校验，供纯契约单元测试使用）。
    - **互斥组合在契约层判断**：分辨率与输出尺寸互斥等规则在此判定，
      不再等到 GDAL 才报冲突。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ParameterError(f"parameters 必须是映射类型，实际 {type(raw).__name__}")

    # 1) 收集 canonical -> (别名, 值)，检测冲突
    collected: dict[str, tuple[str, Any]] = {}
    for key, value in raw.items():
        canonical = _ALIAS_TO_CANONICAL.get(key)
        if canonical is None:
            if key in _OTHER_ALLOWED_KEYS:
                continue
            raise ParameterError(
                f"未知参数 {key!r}；可用参数名："
                f"{', '.join(sorted(_ALIAS_TO_CANONICAL))}（另有 {', '.join(sorted(_OTHER_ALLOWED_KEYS))} 等非几何参数）"
            )
        if value is None:
            continue
        if canonical in collected:
            prev_name, prev_value = collected[canonical]
            if prev_value != value:
                raise ParameterError(
                    f"参数冲突：{prev_name!r} 与 {key!r} 同指 {canonical} 但取值不同"
                    f"（{prev_value!r} vs {value!r}）"
                )
            continue
        collected[canonical] = (key, value)

    # 1.5) 能力级校验：当前能力不消费的参数必须拒绝（不得接受后静默忽略）
    supported = supported_params_for(capability_id)
    if supported is not None:
        unsupported = sorted(set(collected) - supported)
        if unsupported:
            canonical_cap = canonical_capability_id(capability_id)
            raise ParameterError(
                f"能力 {canonical_cap!r} 不支持参数：{', '.join(unsupported)}；"
                f"该能力支持的参数：{', '.join(sorted(supported)) or '（无）'}。"
                "参数被识别不代表该能力会消费它——请改用支持这些参数的能力，或移除这些参数。"
            )

    out: dict[str, Any] = {}

    # 2) 重采样算法
    if "resampleAlg" in collected:
        _, value = collected["resampleAlg"]
        out["resampleAlg"] = _normalize_resample_name(value)

    # 3) 源窗口
    if "srcWin" in collected:
        _, value = collected["srcWin"]
        src_win = _require_int_list(value, "srcWin", 4)
        if src_win[2] <= 0 or src_win[3] <= 0:
            raise ParameterError(f"srcWin 的宽高必须为正数，实际 {src_win}")
        out["srcWin"] = src_win

    # 4) 源地理窗口
    if "projWin" in collected:
        _, value = collected["projWin"]
        out["projWin"] = _require_float_list(value, "projWin", 4)

    # 5) 目标范围
    if "outputBounds" in collected:
        _, value = collected["outputBounds"]
        out["outputBounds"] = _require_float_list(value, "outputBounds", 4)

    # 6) 输出像元尺寸
    if "xRes" in collected:
        _, value = collected["xRes"]
        out["xRes"] = _require_positive_float(value, "xRes")
    if "yRes" in collected:
        _, value = collected["yRes"]
        out["yRes"] = _require_positive_float(value, "yRes")

    # 7) 输出尺寸
    if "width" in collected:
        _, value = collected["width"]
        out["width"] = _require_positive_int(value, "width")
    if "height" in collected:
        _, value = collected["height"]
        out["height"] = _require_positive_int(value, "height")

    # 8) 输出数据类型（名字 -> 规范名，映射到 GDAL 常量由 worker 负责）
    if "outputType" in collected:
        _, value = collected["outputType"]
        out["outputType"] = _normalize_output_type(value)

    # 9) NoData：零值合法，按是否为 None 判断
    if "noData" in collected:
        _, value = collected["noData"]
        out["noData"] = _require_finite_float(value, "noData")
    if "dstNodata" in collected:
        _, value = collected["dstNodata"]
        out["dstNodata"] = _require_finite_float(value, "dstNodata")

    # 10) CRS
    if "dstSRS" in collected:
        _, value = collected["dstSRS"]
        out["dstSRS"] = str(value).strip()
    if "srcSRS" in collected:
        _, value = collected["srcSRS"]
        out["srcSRS"] = str(value).strip()

    # 11) 输出网格对齐
    if "targetAlignedPixels" in collected:
        _, value = collected["targetAlignedPixels"]
        if not isinstance(value, bool):
            raise ParameterError(f"targetAlignedPixels 必须是布尔值，实际 {value!r}")
        out["targetAlignedPixels"] = value

    # 11.5) P4-02：拼接 / VRT / 堆叠 / 波段管理
    if "bandList" in collected:
        _, value = collected["bandList"]
        if not isinstance(value, (list, tuple)) or not value:
            raise ParameterError("参数 bandList 必须是非空的整数列表")
        bands: list[int] = []
        for v in value:
            if not _is_int_like(v):
                raise ParameterError(f"参数 bandList 的元素必须是整数，实际含 {v!r}")
            iv = int(v)
            if iv < 1:
                raise ParameterError(f"参数 bandList 的波段号必须为正数（1-based），实际 {iv}")
            bands.append(iv)
        out["bandList"] = bands

    if "overlapPriority" in collected:
        _, value = collected["overlapPriority"]
        key = str(value).strip().lower()
        if key not in OVERLAP_PRIORITIES:
            raise ParameterError(
                f"未知的重叠优先级 {value!r}；可用：{', '.join(OVERLAP_PRIORITIES)}"
            )
        out["overlapPriority"] = key

    if "resolution" in collected:
        _, value = collected["resolution"]
        key = str(value).strip().lower()
        if key not in VRT_RESOLUTIONS:
            raise ParameterError(
                f"未知的 VRT 分辨率策略 {value!r}；可用：{', '.join(VRT_RESOLUTIONS)}"
            )
        out["resolution"] = key

    if "srcNodata" in collected:
        _, value = collected["srcNodata"]
        out["srcNodata"] = _require_finite_float(value, "srcNodata")

    if "vrtNodata" in collected:
        _, value = collected["vrtNodata"]
        out["vrtNodata"] = _require_finite_float(value, "vrtNodata")

    if "separate" in collected:
        _, value = collected["separate"]
        if not isinstance(value, bool):
            raise ParameterError(f"separate 必须是布尔值，实际 {value!r}")
        out["separate"] = value

    if "relativePaths" in collected:
        _, value = collected["relativePaths"]
        if not isinstance(value, bool):
            raise ParameterError(f"relativePaths 必须是布尔值，实际 {value!r}")
        out["relativePaths"] = value

    # 11.6) P5-02：地形分析
    if "processing" in collected:
        _, value = collected["processing"]
        raw_processing = str(value).strip()
        canonical_by_lower = {p.lower(): p for p in DEM_PROCESSINGS}
        canonical = canonical_by_lower.get(raw_processing.lower())
        if canonical is None:
            if raw_processing.lower() == "color-relief":
                raise ParameterError(
                    "processing='color-relief' 需要额外颜色表文件，当前未接入；"
                    f"可用：{', '.join(DEM_PROCESSINGS)}"
                )
            raise ParameterError(
                f"未知的地形模式 {value!r}；可用：{', '.join(DEM_PROCESSINGS)}"
            )
        out["processing"] = canonical

    if "zFactor" in collected:
        _, value = collected["zFactor"]
        out["zFactor"] = _require_finite_float(value, "zFactor")

    if "scale" in collected:
        _, value = collected["scale"]
        out["scale"] = _require_positive_float(value, "scale")

    # 11.7) P5-02：矢量栅格化
    if "burnValues" in collected and "attribute" in collected:
        raise ParameterError("burnValues 与 attribute 互斥，不能同时指定")
    if "burnValues" in collected:
        _, value = collected["burnValues"]
        if not isinstance(value, (list, tuple)) or not value:
            raise ParameterError("参数 burnValues 必须是非空的数值列表")
        burns: list[float] = []
        for v in value:
            try:
                f = float(v)
            except (TypeError, ValueError) as exc:
                raise ParameterError(f"burnValues 含非数值元素：{v!r}") from exc
            if not math.isfinite(f):
                raise ParameterError(f"burnValues 含非有限值：{v!r}")
            burns.append(f)
        out["burnValues"] = burns
    if "attribute" in collected:
        _, value = collected["attribute"]
        attr = str(value).strip()
        if not attr:
            raise ParameterError("参数 attribute 必须是非空字段名")
        out["attribute"] = attr
    if "allTouched" in collected:
        _, value = collected["allTouched"]
        if not isinstance(value, bool):
            raise ParameterError(f"allTouched 必须是布尔值，实际 {value!r}")
        out["allTouched"] = value

    # 11.8) P5-02：散点栅格插值
    if "algorithm" in collected:
        _, value = collected["algorithm"]
        algorithm = str(value).strip()
        if not algorithm:
            raise ParameterError("参数 algorithm 必须是非空插值算法名")
        out["algorithm"] = algorithm
    if "zField" in collected:
        _, value = collected["zField"]
        zfield = str(value).strip()
        if not zfield:
            raise ParameterError("参数 zField 必须是非空字段名")
        out["zField"] = zfield

    # 11.9) P5-02：栅格矢量化
    if "fieldName" in collected:
        _, value = collected["fieldName"]
        field_name = str(value).strip()
        if not field_name:
            raise ParameterError("参数 fieldName 必须是非空字段名")
        out["fieldName"] = field_name
    if "connectedness" in collected:
        _, value = collected["connectedness"]
        if not _is_int_like(value):
            raise ParameterError(
                f"参数 connectedness 必须是整数（4 或 8，不接受小数截断），实际 {value!r}"
            )
        conn = int(value)
        if conn not in (4, 8):
            raise ParameterError(f"参数 connectedness 只支持 4/8 邻域，实际 {conn}")
        out["connectedness"] = conn

    # 11.10) P5-02：等值线
    if "interval" in collected:
        _, value = collected["interval"]
        out["interval"] = _require_positive_float(value, "interval")
    if "offset" in collected:
        _, value = collected["offset"]
        # 零值合法：contourBase=0 是有效的基准高程
        out["offset"] = _require_finite_float(value, "offset")
    if "fixedLevels" in collected:
        _, value = collected["fixedLevels"]
        if not isinstance(value, (list, tuple)) or not value:
            raise ParameterError("参数 fixedLevels 必须是非空的数值列表")
        fixed: list[float] = []
        for v in value:
            try:
                f = float(v)
            except (TypeError, ValueError) as exc:
                raise ParameterError(f"fixedLevels 含非数值元素：{v!r}") from exc
            if not math.isfinite(f):
                raise ParameterError(f"fixedLevels 含非有限值：{v!r}")
            fixed.append(f)
        out["fixedLevels"] = fixed

    # 11.11) P5-02：矢量化的输入波段选择（1-based）
    if "band" in collected:
        _, value = collected["band"]
        out["band"] = _require_positive_int(value, "band")

    # 11.13) P6-01：光谱指数
    if "sindex" in collected:
        _, value = collected["sindex"]
        index_name = str(value).strip().lower()
        if index_name not in ("ndvi", "ndwi", "nbr", "savi"):
            raise ParameterError(
                f"未知光谱指数 {value!r}；可用：ndvi, ndwi, nbr, savi"
            )
        out["sindex"] = index_name
    if "bandA" in collected:
        _, value = collected["bandA"]
        out["bandA"] = _require_positive_int(value, "bandA")
    if "bandB" in collected:
        _, value = collected["bandB"]
        out["bandB"] = _require_positive_int(value, "bandB")
    if "saviL" in collected:
        _, value = collected["saviL"]
        l_value = _require_finite_float(value, "saviL")
        if not (0.0 <= l_value <= 1.0):
            raise ParameterError(f"SAVI 的土壤调节因子 L 必须在 [0, 1]，实际 {l_value}")
        out["saviL"] = l_value
    if "usePhysical" in collected:
        _, value = collected["usePhysical"]
        if not isinstance(value, bool):
            raise ParameterError(f"参数 usePhysical 必须是布尔值，实际 {value!r}")
        out["usePhysical"] = value

    # 11.14b) P8-01：多维切片 array / dimSelect
    if "array" in collected:
        _, value = collected["array"]
        arr_name = str(value).strip()
        if not arr_name:
            raise ParameterError("参数 array 必须是非空多维数组名")
        out["array"] = arr_name
    if "dimSelect" in collected:
        _, value = collected["dimSelect"]
        out["dimSelect"] = _normalize_dim_select(value)

    # 11.14) P6-02：FFT 滤波
    if "fftBand" in collected:
        _, value = collected["fftBand"]
        out["fftBand"] = _require_positive_int(value, "fftBand")
    if "suppressBins" in collected:
        _, value = collected["suppressBins"]
        if not isinstance(value, (list, tuple)):
            raise ParameterError(
                "参数 suppressBins 必须是 [fy, fx] 对的列表，例：[[0, 4], [0, -4]]"
            )
        bins: list[tuple[int, int]] = []
        for v in value:
            if not isinstance(v, (list, tuple)) or len(v) != 2:
                raise ParameterError(
                    f"suppressBins 的元素必须是 [fy, fx] 二元组，实际 {v!r}"
                )
            fy, fx = v
            if not _is_int_like(fy) or not _is_int_like(fx):
                raise ParameterError(
                    f"suppressBins 的 fy/fx 必须是整数（不接受小数截断），实际 {v!r}"
                )
            bins.append((int(fy), int(fx)))
        out["suppressBins"] = bins

    # 11.15) P6-03：PCA 正/逆变换
    if "pcaMode" in collected:
        _, value = collected["pcaMode"]
        mode = str(value).strip().lower()
        if mode not in PCA_MODES:
            raise ParameterError(
                f"未知 PCA 模式 {value!r}；可用：{', '.join(PCA_MODES)}"
            )
        out["pcaMode"] = mode
    if "pcaComponents" in collected:
        _, value = collected["pcaComponents"]
        out["pcaComponents"] = _require_positive_int(value, "pcaComponents")
    if "pcaMatrix" in collected:
        _, value = collected["pcaMatrix"]
        matrix = str(value).strip().lower()
        if matrix not in PCA_MATRICES:
            raise ParameterError(
                f"未知 PCA 矩阵类型 {value!r}；可用：{', '.join(PCA_MATRICES)}"
            )
        out["pcaMatrix"] = matrix
    if "pcaTransform" in collected:
        _, value = collected["pcaTransform"]
        transform = str(value).strip()
        if not transform:
            raise ParameterError("参数 pcaTransform 必须是非空文件路径")
        out["pcaTransform"] = transform

    # 11.16) P6-03：缨帽变换
    if "tcSensor" in collected:
        _, value = collected["tcSensor"]
        sensor = str(value).strip().lower()
        if sensor not in sensor_keys():
            raise ParameterError(
                f"未登记的缨帽传感器 {value!r}；可用：{', '.join(sensor_keys())}"
            )
        out["tcSensor"] = sensor
    if "tcBands" in collected:
        _, value = collected["tcBands"]
        bands = _require_int_list(value, "tcBands", 6)
        if any(b < 1 for b in bands):
            raise ParameterError(f"tcBands 的波段号必须为正数（1-based），实际 {bands}")
        if len(set(bands)) != 6:
            raise ParameterError(f"tcBands 的 6 个波段号必须互不相同，实际 {bands}")
        out["tcBands"] = bands

    # 11.17) P6-04：GCP 配准 / 差值 / 融合
    if "gcpTransform" in collected:
        _, value = collected["gcpTransform"]
        transform = str(value).strip().lower()
        if transform not in GCP_TRANSFORMS:
            raise ParameterError(
                f"未知 GCP 变换 {value!r}；可用：{', '.join(GCP_TRANSFORMS)}"
            )
        out["gcpTransform"] = transform
    for key in ("gcpFile", "referenceFile", "msFile"):
        if key in collected:
            _, value = collected[key]
            text = str(value).strip()
            if not text:
                raise ParameterError(f"参数 {key} 必须是非空文件路径")
            out[key] = text
    for key in ("gcpResample", "fusionResample"):
        if key in collected:
            _, value = collected[key]
            alg = str(value).strip().lower()
            if alg not in LOCAL_RESAMPLES:
                raise ParameterError(
                    f"未知重采样算法 {value!r}；可用：{', '.join(LOCAL_RESAMPLES)}"
                )
            out[key] = alg
    if "diffMode" in collected:
        _, value = collected["diffMode"]
        mode = str(value).strip().lower()
        if mode not in DIFF_MODES:
            raise ParameterError(
                f"未知差值模式 {value!r}；可用：{', '.join(DIFF_MODES)}"
            )
        out["diffMode"] = mode
    if "fusionMethod" in collected:
        _, value = collected["fusionMethod"]
        method = str(value).strip().lower()
        if method not in FUSION_METHODS:
            raise ParameterError(
                f"未知融合方法 {value!r}；可用：{', '.join(FUSION_METHODS)}"
            )
        out["fusionMethod"] = method
    if "msBands" in collected:
        _, value = collected["msBands"]
        ms_bands = _require_int_list(value, "msBands", 3)
        if any(b < 1 for b in ms_bands):
            raise ParameterError(
                f"msBands 的波段号必须为正数（1-based），实际 {ms_bands}"
            )
        if len(set(ms_bands)) != 3:
            raise ParameterError(f"msBands 的 3 个波段号必须互不相同，实际 {ms_bands}")
        out["msBands"] = ms_bands

    # 11.18) P6-05：掩膜 / FVC
    if "maskBand" in collected:
        _, value = collected["maskBand"]
        out["maskBand"] = _require_positive_int(value, "maskBand")
    for key in ("maskLow", "maskHigh"):
        if key in collected:
            _, value = collected[key]
            out[key] = _require_finite_float(value, key)
    if (
        "maskLow" in out
        and "maskHigh" in out
        and float(out["maskLow"]) > float(out["maskHigh"])
    ):
        raise ParameterError(
            "maskLow 必须小于等于 maskHigh，实际 "
            f"{out['maskLow']} > {out['maskHigh']}"
        )
    if "maskInvert" in collected:
        _, value = collected["maskInvert"]
        if not isinstance(value, bool):
            raise ParameterError(f"maskInvert 必须是布尔值，实际 {value!r}")
        out["maskInvert"] = value
    if "fvcBand" in collected:
        _, value = collected["fvcBand"]
        out["fvcBand"] = _require_positive_int(value, "fvcBand")
    if "endmemberMode" in collected:
        _, value = collected["endmemberMode"]
        mode = str(value).strip().lower()
        if mode not in FVC_ENDMEMBER_MODES:
            raise ParameterError(
                f"未知 FVC 端元模式 {value!r}；可用：{', '.join(FVC_ENDMEMBER_MODES)}"
            )
        out["endmemberMode"] = mode
    for key in ("lowPercentile", "highPercentile"):
        if key in collected:
            _, value = collected[key]
            out[key] = _require_finite_float(value, key)
    if (
        "lowPercentile" in out
        and "highPercentile" in out
        and float(out["lowPercentile"]) >= float(out["highPercentile"])
    ):
        raise ParameterError(
            "lowPercentile 必须小于 highPercentile，实际 "
            f"{out['lowPercentile']} >= {out['highPercentile']}"
        )
    for key in ("fvcSoil", "fvcVeg"):
        if key in collected:
            _, value = collected[key]
            out[key] = _require_finite_float(value, key)
    if (
        "fvcSoil" in out
        and "fvcVeg" in out
        and float(out["fvcSoil"]) >= float(out["fvcVeg"])
    ):
        raise ParameterError(
            "FVC 端元必须 fvcSoil < fvcVeg，实际 "
            f"{out['fvcSoil']} >= {out['fvcVeg']}"
        )
    if "fvcClassEdges" in collected:
        _, value = collected["fvcClassEdges"]
        if not isinstance(value, (list, tuple)) or not value:
            raise ParameterError("fvcClassEdges 必须是至少 1 个数值的列表")
        edges: list[float] = []
        for item in value:
            try:
                edges.append(float(item))
            except (TypeError, ValueError) as exc:
                raise ParameterError(
                    f"fvcClassEdges 必须为数值，实际 {item!r}"
                ) from exc
        for item in edges:
            if not math.isfinite(item) or not (0.0 < item < 1.0):
                raise ParameterError(
                    f"fvcClassEdges 必须在 (0, 1) 内且有限，实际 {item!r}"
                )
        if any(b <= a for a, b in zip(edges, edges[1:], strict=False)):
            raise ParameterError("fvcClassEdges 必须严格递增")
        out["fvcClassEdges"] = edges
    if "maskFile" in collected:
        _, value = collected["maskFile"]
        text = str(value).strip()
        if not text:
            raise ParameterError("参数 maskFile 必须是非空文件路径")
        out["maskFile"] = text
    if "crop" in collected:
        _, value = collected["crop"]
        if not isinstance(value, bool):
            raise ParameterError(f"crop 必须是布尔值，实际 {value!r}")
        out["crop"] = value
    if "calibrateMode" in collected:
        _, value = collected["calibrateMode"]
        out["calibrateMode"] = _require_choice(value, CALIBRATE_MODES, "calibrateMode")
    for key in ("calibrateGains", "calibrateOffsets", "calibrateEsun"):
        if key in collected:
            _, value = collected[key]
            if not isinstance(value, (list, tuple)) or not value:
                raise ParameterError(f"{key} 必须是至少 1 个数值的列表")
            out[key] = _require_float_list_any(value, key)
    if "sunZenAngle" in collected:
        _, value = collected["sunZenAngle"]
        angle = _require_finite_float(value, "sunZenAngle")
        out["sunZenAngle"] = angle
    if "dosBand" in collected:
        _, value = collected["dosBand"]
        out["dosBand"] = _require_positive_int(value, "dosBand")
    if "hazeDN" in collected:
        _, value = collected["hazeDN"]
        out["hazeDN"] = _require_finite_float(value, "hazeDN")
    if "dosEsun" in collected:
        _, value = collected["dosEsun"]
        out["dosEsun"] = _require_finite_float(value, "dosEsun")
    if "earthSunDistance" in collected:
        _, value = collected["earthSunDistance"]
        out["earthSunDistance"] = _require_finite_float(value, "earthSunDistance")
    if "dosGain" in collected:
        _, value = collected["dosGain"]
        out["dosGain"] = _require_finite_float(value, "dosGain")
    if "dosOffset" in collected:
        _, value = collected["dosOffset"]
        out["dosOffset"] = _require_finite_float(value, "dosOffset")
    if "secondaryFile" in collected:
        _, value = collected["secondaryFile"]
        text = str(value).strip()
        if not text:
            raise ParameterError("参数 secondaryFile 必须是非空文件路径")
        out["secondaryFile"] = text
    if "cloudMaskFile" in collected:
        _, value = collected["cloudMaskFile"]
        text = str(value).strip()
        if not text:
            raise ParameterError("参数 cloudMaskFile 必须是非空文件路径")
        out["cloudMaskFile"] = text
    if "primaryTime" in collected:
        _, value = collected["primaryTime"]
        out["primaryTime"] = str(value)
    if "secondaryTime" in collected:
        _, value = collected["secondaryTime"]
        out["secondaryTime"] = str(value)
    if "maskDilatePixels" in collected:
        _, value = collected["maskDilatePixels"]
        out["maskDilatePixels"] = _require_non_negative_int(value, "maskDilatePixels")

    # 11.12) P5-04：矢量处理 / SQL / 坐标转换
    if "sql" in collected:
        _, value = collected["sql"]
        sql_text = str(value).strip()
        if not sql_text:
            raise ParameterError("参数 sql 必须是非空 SQL 语句")
        out["sql"] = sql_text
    if "where" in collected:
        _, value = collected["where"]
        where_text = str(value).strip()
        if not where_text:
            raise ParameterError("参数 where 必须是非空过滤子句")
        out["where"] = where_text
    if "select" in collected:
        _, value = collected["select"]
        if not isinstance(value, (list, tuple)) or not value:
            raise ParameterError("参数 select 必须是非空的字段名列表")
        selected: list[str] = []
        for v in value:
            name = str(v).strip()
            if not name:
                raise ParameterError("参数 select 不能包含空字段名")
            selected.append(name)
        out["select"] = selected
    if "spat" in collected:
        _, value = collected["spat"]
        spat_box = _require_float_list(value, "spat", 4)
        min_x, min_y, max_x, max_y = spat_box
        if min_x > max_x or min_y > max_y:
            raise ParameterError(
                f"spat 过滤框必须为 minx,miny,maxx,maxy（min ≤ max），实际 {spat_box}"
            )
        out["spat"] = spat_box

    if "levels" in collected:
        _, value = collected["levels"]
        if not isinstance(value, (list, tuple)) or not value:
            raise ParameterError("参数 levels 必须是非空的整数列表")
        lv: list[int] = []
        prev = 0
        for v in value:
            if not _is_int_like(v):
                raise ParameterError(f"参数 levels 的元素必须是整数，实际含 {v!r}")
            iv = int(v)
            if iv < 2:
                raise ParameterError(
                    f"参数 levels 的各级倍率必须 ≥ 2（1 等于原图无意义），实际 {iv}"
                )
            if iv <= prev:
                raise ParameterError(
                    f"参数 levels 必须严格递增（GDAL 要求），实际 {iv} 不大于前值 {prev}"
                )
            lv.append(iv)
            prev = iv
        out["levels"] = lv

    if "overviewCompress" in collected:
        _, value = collected["overviewCompress"]
        key = str(value).strip().upper()
        if key not in OVERVIEW_COMPRESSIONS:
            raise ParameterError(
                f"未知的概览压缩方式 {value!r}；可用：{', '.join(OVERVIEW_COMPRESSIONS)}"
            )
        if key != "NONE":
            out["overviewCompress"] = key

    # 11.6) P4-04：表达式与统计模式
    # 契约层只做类型/取值校验；表达式语法树的白名单校验在 worker 侧
    # （expression_ops 依赖 numpy，契约模块刻意保持纯 Python 以便 GUI 导入）。
    if "calc" in collected:
        _, value = collected["calc"]
        if not isinstance(value, str) or not value.strip():
            raise ParameterError(f"参数 calc 必须是非空表达式字符串，实际 {value!r}")
        expr = value.strip()
        if len(expr) > 512:
            raise ParameterError(f"参数 calc 过长（>{512} 字符），请化简表达式")
        out["calc"] = expr

    if "statisticsMode" in collected:
        _, value = collected["statisticsMode"]
        key = str(value).strip().lower()
        if key not in ("sampled", "full"):
            raise ParameterError(
                f"未知的统计模式 {value!r}；可用：sampled, full（full 超像元上限会被拒绝）"
            )
        out["statisticsMode"] = key

    for stat_key in ("maxSamples", "fullLimit"):
        if stat_key in collected:
            _, value = collected[stat_key]
            out[stat_key] = _require_positive_int(value, stat_key)

    if "histogramBins" in collected:
        _, value = collected["histogramBins"]
        n_bins = _require_positive_int(value, "histogramBins")
        if not 2 <= n_bins <= 4096:
            raise ParameterError(
                f"参数 histogramBins 必须在 2..4096 之间，实际 {n_bins}"
            )
        out["histogramBins"] = n_bins

    # 12) 互斥组合：分辨率与输出尺寸不能同时指定
    # （GDAL 侧表现为 "-outsize 与 -tr 冲突"，但那是在引擎里才发现；
    #   契约层提前判定，GUI 与 worker 都能给出明确错误。）
    has_res = "xRes" in out or "yRes" in out
    has_size = "width" in out or "height" in out
    if has_res and has_size:
        raise ParameterError(
            "参数互斥：输出像元尺寸（xRes/yRes）与输出尺寸（width/height）"
            "不能同时指定——前者按分辨率缩放，后者按固定像元数缩放"
        )
    # 源窗口与目标范围在 Warp 下语义不同（源像元/源地理 vs 目标 CRS 轴对齐范围）
    if canonical_capability_id(capability_id) == "gdal:warp":
        if ("srcWin" in out or "projWin" in out) and "outputBounds" in out:
            raise ParameterError(
                "参数互斥：源窗口（srcWin/projWin）与目标范围（outputBounds）"
                "语义不同，不能同时指定"
            )
        if "srcWin" in out and "projWin" in out:
            raise ParameterError("参数互斥：srcWin 与 projWin 都是源窗口语义，不能同时指定")

    return out


def _require_finite_float(value: Any, name: str) -> float:
    """解析有限浮点数（零值合法）。"""
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise ParameterError(f"参数 {name} 必须是数值，实际 {value!r}") from exc
    if not math.isfinite(f):
        raise ParameterError(f"参数 {name} 必须是有限值，实际 {value!r}")
    return f


_OUTPUT_TYPE_ALIASES: dict[str, str] = {
    "byte": "Byte",
    "uint8": "Byte",
    "ubyte": "Byte",
    "int8": "Int8",
    "uint16": "UInt16",
    "int16": "Int16",
    "uint32": "UInt32",
    "int32": "Int32",
    "float32": "Float32",
    "float64": "Float64",
    "cfloat32": "CFloat32",
    "cfloat64": "CFloat64",
}


def _normalize_output_type(value: Any) -> str:
    """把输出数据类型名归一化为 GDAL 规范名；未知则报错。"""
    key = str(value).strip().lower()
    if key in _OUTPUT_TYPE_ALIASES:
        return _OUTPUT_TYPE_ALIASES[key]
    if key.startswith("gdt_"):
        candidate = key[4:]
        if candidate in set(_OUTPUT_TYPE_ALIASES.values()):
            return candidate
    raise ParameterError(
        f"未知的输出数据类型 {value!r}；可用：{', '.join(sorted(_OUTPUT_TYPE_ALIASES))}"
    )
