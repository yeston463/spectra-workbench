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

__all__ = ["CONNECTED_CAPABILITIES", "MULTI_INPUT_CAPABILITIES", "is_multi_input"]


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
    }
)


def is_multi_input(capability_id: str) -> bool:
    """该能力是否需要多输入（P4-02 的拼接/VRT/堆叠）。"""
    return capability_id in MULTI_INPUT_CAPABILITIES
