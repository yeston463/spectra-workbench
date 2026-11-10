"""Unit test for P10-03 documentation, tool index, examples, plugin protocol,

troubleshooting, and dependency inventory audit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from spectra_workbench.registry.catalog import get_default_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_USER = REPO_ROOT / "docs" / "user"
DOCS_DEV = REPO_ROOT / "docs" / "developer"


@pytest.mark.unit
def test_p10_03_all_required_documents_exist_and_non_empty():
    """验证 P10-03 所需的所有用户说明、开发说明、故障定位和清单文档齐全且非空。"""
    required_docs = [
        DOCS_USER / "01_工具索引与CLI.md",
        DOCS_USER / "02_项目与工作流示例.md",
        DOCS_DEV / "01_插件协议与自定义引擎.md",
        DOCS_DEV / "02_故障定位与Doctor诊断.md",
        DOCS_DEV / "03_依赖与组件清单.md",
        REPO_ROOT / "README.md",
    ]

    for doc in required_docs:
        assert doc.exists(), f"必需文档不存在: {doc}"
        content = doc.read_text(encoding="utf-8")
        assert len(content.strip()) > 1000, f"文档内容过短或不完整: {doc}"


@pytest.mark.unit
def test_p10_03_tool_index_contains_registered_capabilities():
    """验证 01_工具索引与CLI.md 编目的所有核心能力均真实存在于能力注册表中，绝无编造。"""
    tool_doc = (DOCS_USER / "01_工具索引与CLI.md").read_text(encoding="utf-8")

    core_capabilities = [
        "gdal:translate",
        "gdal:warp",
        "gdal:overview",
        "gdal:statistics",
        "gdal:stack",
        "gdal:bandmanage",
        "rasterio:convert",
        "rio:cli:warp",
        "gdal:dem",
        "gdal:contour",
        "gdal:grid",
        "gdal:rasterize",
        "gdal:polygonize",
        "ogr:vectortranslate",
        "gdal:mdim_slice",
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
    ]

    registry = get_default_registry()

    for cap_id in core_capabilities:
        assert f"`{cap_id}`" in tool_doc, f"工具文档缺少核心能力编目: {cap_id}"
        assert registry.get(cap_id) is not None, f"能力 {cap_id} 未在注册表中登记"

    # 验证命令行选项编目
    for opt in ["--doctor", "--json", "--headless", "--test-init", "--sample", "--open", "--screenshot"]:
        assert opt in tool_doc, f"工具文档缺少核心命令行参数说明: {opt}"


@pytest.mark.unit
def test_p10_03_json_examples_validity():
    """提取 02_项目与工作流示例.md 中的所有 JSON 块，验证语法完全有效且结构符合规范。"""
    example_doc = (DOCS_USER / "02_项目与工作流示例.md").read_text(encoding="utf-8")

    json_blocks = re.findall(r"```json\s*\n(.*?)\n```", example_doc, re.DOTALL)
    assert len(json_blocks) >= 3, "必须至少包含工程、工作流与批处理 3 组 JSON 完整示例"

    parsed_blocks = []
    for idx, block in enumerate(json_blocks):
        try:
            data = json.loads(block)
            parsed_blocks.append(data)
        except json.JSONDecodeError as err:
            pytest.fail(f"示例文档中第 {idx + 1} 个 JSON 代码块语法错误: {err}")

    # 验证工程文件 JSON 规范
    proj = parsed_blocks[0]
    assert proj.get("schema_version") == "1.0"
    assert "sources" in proj and len(proj["sources"]) >= 2
    assert "layers" in proj and len(proj["layers"]) >= 2
    assert "view_state" in proj
    assert "roi_items" in proj
    assert proj.get("path_anchor") == "project_dir"

    # 验证 DAG 工作流 JSON 规范
    wf = parsed_blocks[1]
    assert "nodes" in wf and len(wf["nodes"]) >= 3
    for node in wf["nodes"]:
        assert "node_key" in node
        assert "capability_id" in node
        assert "engine_provider" in node
        assert "inputs" in node
        assert "outputs" in node

    # 验证批处理 JSON 规范
    batch = parsed_blocks[2]
    assert "capability_id" in batch
    assert "template" in batch
    assert "items" in batch and len(batch["items"]) >= 2
    assert "continue_on_error" in batch


@pytest.mark.unit
def test_p10_03_plugin_protocol_spec_validity():
    """验证 01_插件协议与自定义引擎.md 包含合法的 JSONL 协议消息样例与进程隔离红线说明。"""
    plugin_doc = (DOCS_DEV / "01_插件协议与自定义引擎.md").read_text(encoding="utf-8")

    assert "import osgeo" in plugin_doc and "严禁" in plugin_doc
    assert "import rasterio" in plugin_doc
    assert "WorkerProcess" in plugin_doc
    assert "ProviderAdapter" in plugin_doc

    json_blocks = re.findall(r"```json\s*\n(.*?)\n```", plugin_doc, re.DOTALL)
    assert len(json_blocks) >= 4, "必须包含握手、提交、进度、成果与能力契约完整消息样例"

    for idx, block in enumerate(json_blocks):
        try:
            data = json.loads(block)
            assert isinstance(data, dict)
        except json.JSONDecodeError as err:
            pytest.fail(f"插件协议文档中第 {idx + 1} 个 JSON 代码块语法错误: {err}")


@pytest.mark.unit
def test_p10_03_doctor_and_troubleshooting_coverage():
    """验证 02_故障定位与Doctor诊断.md 完整覆盖了 6 类关键运行期故障。"""
    doctor_doc = (DOCS_DEV / "02_故障定位与Doctor诊断.md").read_text(encoding="utf-8")

    required_keywords = [
        "overall_ok",
        "gui_can_start",
        "proj.db",
        "PROJ_DATA",
        "WinError 126",
        "os.add_dll_directory",
        "WinError 206",
        "MAX_PATH",
        "taskkill",
        "atomic",
        "离线",
    ]

    for kw in required_keywords:
        assert kw in doctor_doc, f"故障诊断文档缺少关键故障特征或修复方案: {kw}"


@pytest.mark.unit
def test_p10_03_dependencies_and_version_consistency():
    """验证 03_依赖与组件清单.md 记录的版本与环境基准一致。"""
    dep_doc = (DOCS_DEV / "03_依赖与组件清单.md").read_text(encoding="utf-8")

    expected_versions = [
        ("GDAL", "3.13.3"),
        ("Rasterio", "1.5.2"),
        ("PySide6", "6.9.3"),
        ("NumPy", "2.5.4"),
        ("SciPy", "1.18.1"),
        ("Pydantic", "2.11.7"),
    ]

    for name, ver in expected_versions:
        assert name in dep_doc and ver in dep_doc, f"依赖清单缺少 {name} 版本 {ver} 记录"

    # 验证发布包运行时架构矩阵
    for runtime_dir in ["app/", "runtimes/gdal/", "runtimes/rasterio/", "runtimes/science/"]:
        assert runtime_dir in dep_doc, f"依赖清单缺少运行时目录说明: {runtime_dir}"

    # 验证 205 驱动记录
    assert "205" in dep_doc, "依赖清单应包含 205 驱动支持记录"
