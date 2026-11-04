"""P3 测试辅助：用 GDAL 运行时生成验收样例。

GUI 测试进程没有 osgeo，因此样例生成通过子进程交给引擎解释器执行。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime

SAMPLE_DIR_NAME = "p3_samples"


def gdal_runtime_python() -> Path | None:
    info = resolve_gdal_runtime(find_project_root())
    py = Path(str(info.get("python", "")))
    return py if py.exists() else None


def runtime_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    info = resolve_gdal_runtime(find_project_root())
    env = os.environ.copy()
    env.update({k: str(v) for k, v in (info.get("env") or {}).items()})
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def ensure_p3_samples(dest: Path | None = None) -> dict[str, Any]:
    """生成（或复用）P3 样例目录，返回 manifest。缺少引擎时抛 RuntimeError。"""
    root = find_project_root()
    out = Path(dest) if dest else (root / "work" / SAMPLE_DIR_NAME)
    manifest = out / "manifest.json"
    py = gdal_runtime_python()
    if py is None:
        raise RuntimeError("GDAL 运行时不可用，无法生成 P3 样例")
    script = root / "tests" / "fixtures" / "make_p3_samples.py"
    if manifest.exists():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if all(Path(v["path"]).exists() for v in data["samples"].values()):
            return data
    proc = subprocess.run(
        [str(py), str(script), "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=runtime_env(),
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"样例生成失败 rc={proc.returncode}: {proc.stderr[:400]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def run_engine_script(script: Path, args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """在 GDAL 运行时里执行脚本（性能测量等非测试用途）。"""
    py = gdal_runtime_python()
    if py is None:
        raise RuntimeError("GDAL 运行时不可用")
    root = find_project_root()
    env = runtime_env({"PYTHONPATH": str(root / "src")})
    return subprocess.run(
        [str(py), str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )
