"""补一条防回归断言：provider adapter 不得在**模块级**访问 GDAL 常量。

背景（第 26 轮实测踩到）：adapter 顶层 `from osgeo import gdal` 是 try/except 保护的，
因此 adapter 必须在**没有 osgeo 的 GUI 进程**中也能被 import（GUI 侧单元测试正是如此）。
若在模块级构造引用 `gdal.GRA_*` 的字典，GUI 进程 import 时会抛
`AttributeError: 'NoneType' object has no attribute 'GRA_NearestNeighbour'`，
导致 GUI 侧测试在**收集阶段**就整体失败。

因此：凡是把 GDAL 常量放进模块级字典/常量的写法都是错的——常量访问必须惰性。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

ADAPTER = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "spectra_workbench"
    / "providers"
    / "gdal_provider"
    / "adapter.py"
)


def test_adapter_imports_in_process_without_osgeo() -> None:
    """在**没有 osgeo 的进程**里导入 adapter 必须成功，且不引入原生依赖。"""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(root / 'src')!r})\n"
        "from spectra_workbench.providers.gdal_provider.adapter import GDALProviderAdapter\n"
        "assert GDALProviderAdapter is not None\n"
        "bad = [m for m in ('osgeo', 'rasterio') if m in sys.modules]\n"
        "assert not bad, 'GUI 进程被拉入原生地理依赖: %s' % bad\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(
        # -E：忽略继承的 PYTHONPATH 等环境变量，源码根由脚本内的 sys.path.insert 指定
        [sys.executable, "-E", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, f"adapter 在无 osgeo 进程中导入失败：{proc.stderr[:800]}"
    assert "OK" in proc.stdout


def test_adapter_has_no_module_level_gdal_attribute_access() -> None:
    """adapter 的模块级语句不得访问 gdal 的属性（必须是惰性构造）。

    静态扫描模块级（函数体之外）的属性访问，找出 `gdal.XXX` 形式。
    """
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))

    # 收集「未受 try 保护」的模块级语句：
    # try 块内的访问由 except 分支兜底（无 osgeo 时 gdal 为 None），是安全的；
    # 真正危险的是 try 之外的模块级访问。
    module_level_nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Try):
            # try 内的语句受 except 保护，跳过；handlers/else 不受保护，需检查
            for sub in list(node.handlers) + list(node.orelse) + list(node.finalbody):
                module_level_nodes.append(sub)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        module_level_nodes.append(node)

    offenders: list[str] = []
    for node in module_level_nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute):
                value = sub.value
                if (
                    isinstance(value, ast.Name)
                    and value.id in ("gdal", "osr")
                    and not value.id.startswith("_")
                ):
                    offenders.append(f"line {sub.lineno}: {value.id}.{sub.attr}")
    assert not offenders, (
        "adapter 模块级不得访问 GDAL 属性（会在无 osgeo 的 GUI 进程中崩溃）："
        f"{offenders}"
    )


def test_adapter_module_level_has_no_gdal_constant_dict() -> None:
    """adapter 模块级不得存在引用 gdal 常量的字典字面量。"""
    text = ADAPTER.read_text(encoding="utf-8")
    tree = ast.parse(text)

    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) and not isinstance(node, ast.Assign):
            continue
        value = node.value
        if value is None:
            continue
        for sub in ast.walk(value):
            if (
                isinstance(sub, ast.Attribute)
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "gdal"
            ):
                offenders.append(f"line {sub.lineno}: {sub.attr}")
    assert not offenders, f"adapter 模块级不得引用 GDAL 常量：{offenders}"
