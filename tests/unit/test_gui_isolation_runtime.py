"""补强 GUI 隔离断言：抓「间接」拉入的原生地理依赖。

现有 `test_ui_modules_strictly_no_geospatial_imports` 只做**源码级**正则扫描
（`import osgeo` 等），抓不到间接依赖——例如 GUI 模块 import 了某个包的
子模块，而该包的 `__init__.py` 又 import 了会加载 osgeo 的模块，
于是 osgeo 在 GUI 进程里被间接导入，绕过源码扫描。

第 26 轮就踩了这个坑：GUI 的参数表单 import 了
`providers.gdal_provider.params`，而 `providers/gdal_provider/__init__.py`
会先 import adapter（adapter 顶层 import osgeo），导致 GUI 进程被拖入原生地理库。
修复方式是把契约模块放到不含 osgeo 依赖的包（`processing/raster_params.py`）。

本项用**运行时**断言兜底：导入 GUI 相关模块后，检查 osgeo 等确实没有进入 sys.modules。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]


def _gui_import_probe() -> str:
    """在**独立子进程**里导入 GUI 模块并报告被拉入的原生地理库。

    必须在独立进程里做：当前测试进程可能已经因别的原因导入了这些模块，
    会污染判定。
    """
    root = Path(__file__).resolve().parents[2]
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(root / 'src')!r})\n"
        "import osgeo_check_placeholder\n"  # 占位，保证下面替换为真实导入
    )
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(root / 'src')!r})\n"
        "from spectra_workbench.ui.forms.param_form import CapabilityFormWidget\n"
        "from spectra_workbench.ui.main_window import MainWindow\n"
        "from spectra_workbench.processing import raster_params\n"
        "bad = [m for m in ('osgeo', 'rasterio', 'fiona', 'geopandas') if m in sys.modules]\n"
        "print('BAD=' + ','.join(bad))\n"
    )
    # 以当前环境为基底，只覆盖必要项。
    # **不得清空 PATH / SYSTEMROOT**：Windows 子进程缺少 SYSTEMROOT 会影响
    # DLL 搜索与系统目录解析，可能导致子进程异常收尾。
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(
        # -E：让子进程忽略 PYTHONPATH 等环境变量，避免继承外部配置
        [sys.executable, "-E", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, f"GUI 导入探针失败：{proc.stderr[:600]}"
    return proc.stdout.strip()


def test_gui_import_does_not_pull_native_geospatial() -> None:
    """GUI 进程导入 GUI 模块后，不得间接拉入任何原生地理依赖。"""
    out = _gui_import_probe()
    assert "BAD=" in out, f"探针输出异常：{out}"
    bad_line = next((ln for ln in out.splitlines() if ln.startswith("BAD=")), "BAD=")
    bad = [m for m in bad_line[len("BAD=") :].split(",") if m]
    assert not bad, (
        f"GUI 进程被间接拉入原生地理依赖：{bad}；"
        "说明被导入模块所在的包 __init__ 带有原生依赖（应改放到纯 Python 包）"
    )


def test_shared_raster_params_package_is_pure_python() -> None:
    """共享参数契约模块所在包不得引入 osgeo 等原生依赖。"""
    root = Path(__file__).resolve().parents[2]
    forbidden = re.compile(
        r"^\s*(?:import|from)\s+(?:osgeo|rasterio|fiona|geopandas)\b", re.MULTILINE
    )
    offenders: list[str] = []
    for f in (root / "src" / "spectra_workbench" / "processing").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        if forbidden.search(text):
            offenders.append(str(f))
    assert not offenders, f"processing 包不得导入原生地理依赖：{offenders}"


def test_processing_package_init_does_not_import_adapter() -> None:
    """processing 包的 __init__ 不得连带导入 provider adapter（会拉入 osgeo）。"""
    root = Path(__file__).resolve().parents[2]
    init = root / "src" / "spectra_workbench" / "processing" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    assert "gdal_provider" not in text, "processing/__init__ 不得导入 gdal_provider"
    assert "adapter" not in text, "processing/__init__ 不得导入 provider adapter"
