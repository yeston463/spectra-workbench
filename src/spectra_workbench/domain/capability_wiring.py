"""GUI 已接通能力的**单一来源**声明。

背景：此前 `ui/forms/param_form.py` 与 `ui/panels/toolbox_panel.py` **各自维护**
一份 `CONNECTED_CAPABILITIES`。两份声明一旦不同步，就会出现
「工具箱显示可连、表单却禁止提交」或反之的矛盾状态——
这与第 26 轮参数契约「GUI 与 Worker 各维护一套」是同类问题。

本模块是唯一来源，两处 UI 都从这里导入。

**为什么不直接复用 provider 的 SUPPORTED_CAPABILITIES**：
GUI 进程不得导入原生地理依赖（AGENTS.md 硬约束），而 provider adapter
顶层会 `from osgeo import gdal`。因此这里用纯 Python 声明，
并由 `tests/unit/test_capability_wiring_consistency.py` 在 worker 侧
校验两者一致——声明在 UI 层，一致性由测试保证。
"""

from __future__ import annotations

__all__ = [
    "CONNECTED_CAPABILITIES",
    "FORMAT_CAPABILITIES",
    "MULTI_INPUT_CAPABILITIES",
    "VECTOR_OUTPUT_CAPABILITIES",
    "is_multi_input",
    "is_vector_output",
    "supports_output_format",
]


#: GUI 已接通（可提交执行）的能力。必须与各 provider adapter 的
#: SUPPORTED_CAPABILITIES 保持一致，由一致性测试守护。
CONNECTED_CAPABILITIES: frozenset[str] = frozenset(
    {
        # GDAL 转换 / 重投影
        "gdal:translate",
        "gdal_translate",
        "gdal:warp",
        "gdalwarp",
        # Rasterio 格式转换（表单通道；注意 rio:cli:* 属 CLI 类能力，
        # 只有 CLI 入口、没有 form 入口，不应放进"表单可提交"集合）
        "rasterio:convert",
        # P4-02：拼接 / VRT / 波段堆叠 / 波段管理
        "gdal:buildvrt",
        "gdalbuildvrt",
        "gdal:merge",
        "gdal_merge",
        "gdal:stack",
        "gdal:bandmanage",
        # P4-03：元数据编辑 / 概览构建（in-place）
        "gdal:editmeta",
        "gdal:overview",
        # P5-02：地形分析 / 矢量栅格化 / 散点插值
        "gdal:dem",
        "gdal:rasterize",
        "gdal:grid",
        # P5-02：栅格矢量化 / 等值线（成果为矢量，P5-02）
        "gdal:polygonize",
        "gdal:contour",
        # P5-04：矢量处理 / SQL / 坐标转换（成果为矢量）
        "ogr:vectortranslate",
        # P4-04：受限表达式计算 / 直方图与基础统计（后者输出 JSON artifact）
        "gdal:calc",
        "gdal:statistics",
        # P6-01：光谱指数（成果为单波段栅格）
        "gdal:sindex",
        # P6-02：FFT 滤波 / 去条纹（成果为单波段栅格 + 掩膜 sidecar）
        "gdal:fftfilter",
        # P6-03：PCA 正/逆变换与缨帽变换（多波段栅格 + 来源 sidecar）
        "gdal:pca",
        "gdal:tasseledcap",
        # P6-04：GCP 配准 / 栅格差值 / Brovey-HSV 融合
        "gdal:gcpregister",
        "gdal:rasterdiff",
        "gdal:pansharpen",
        # P6-05：二值掩膜 / FVC 植被覆盖度
        "gdal:mask",
        "gdal:fvc",
        # P6-06：辐射定标 / DOS1 大气校正 / 掩膜驱动多时相去云
        "gdal:calibrate",
        "gdal:dos1",
        "gdal:cloudreplace",
        # P8-01：多维数组二维切片（netCDF/HDF 按 GDAL multidim 模型）
        "gdal:mdim_slice",
        # P9-02：杂斑滤除 / 无效值填补 / 近黑边清理 / 邻近距离 / 视域（成果为单个栅格）
        "gdal:sieve",
        "gdal:fillnodata",
        "gdal:nearblack",
        "gdal:proximity",
        "gdal:viewshed",
    }
)


#: 需要**多个**输入源的能力（表单需切换为可排序的多输入列表）。
MULTI_INPUT_CAPABILITIES: frozenset[str] = frozenset(
    {
        "gdal:buildvrt",
        "gdalbuildvrt",
        "gdal:merge",
        "gdal_merge",
        "gdal:stack",
        # gdal:calc 用列表形式表达 A/B/C… 映射（允许只给 1 个输入）
        "gdal:calc",
    }
)


def is_multi_input(capability_id: str) -> bool:
    """该能力是否使用可排序多输入列表（P4-02 拼接/VRT/堆叠、P4-04 表达式）。"""
    return capability_id in MULTI_INPUT_CAPABILITIES


#: 成果为**矢量**（OGR 数据集）的能力：输出计划 kind=vector、格式矩阵走
#: OGR 驱动（GPKG/GeoJSON/Shapefile/FlatGeobuf）、提交校验走 OGR 重开而非栅格重开。
VECTOR_OUTPUT_CAPABILITIES: frozenset[str] = frozenset(
    {
        "gdal:polygonize",
        "gdal:contour",
        # P5-04：矢量处理 / SQL / 坐标转换（ogr2ogr 通道）
        "ogr:vectortranslate",
    }
)


def is_vector_output(capability_id: str) -> bool:
    """该能力的成果是否为矢量数据集（OutputPlan kind=vector）。"""
    return capability_id in VECTOR_OUTPUT_CAPABILITIES


#: 输出格式由用户选择、且适配器真实消费 ``format`` 参数的能力。
#: ``gdal:buildvrt`` 只产出 VRT，不接受其它格式；统计报告固定 JSON artifact。
FORMAT_CAPABILITIES: frozenset[str] = frozenset(
    {
        "gdal:translate",
        "gdal_translate",
        "gdal:warp",
        "gdalwarp",
        "rasterio:convert",
        "gdal:merge",
        "gdal_merge",
        "gdal:stack",
        "gdal:bandmanage",
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
        "gdal:dos1",
        "gdal:cloudreplace",
        "gdal:calc",
        "gdal:sieve",
        "gdal:fillnodata",
        "gdal:nearblack",
        "gdal:proximity",
        "gdal:viewshed",
    }
)


def supports_output_format(capability_id: str) -> bool:
    """该能力的表单是否应展示输出格式选择。"""
    return capability_id in FORMAT_CAPABILITIES
