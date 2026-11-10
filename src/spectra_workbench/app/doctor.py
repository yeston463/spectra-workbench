"""系统诊断与健康检查（P0-03）。

负责探测和验证 4 个运行环境（UI / GDAL / Rasterio / Science）：
- UI 进程隔离性（确保不导入原生地理依赖）
- GDAL 运行时（GTiff 往返、坐标转换、CLI 工具、资源目录、驱动报告；无 HDF4 仍可打开 GUI）
- Rasterio 运行时（GTiff 往返、rio CLI、驱动报告）
- Science 运行时（NumPy、SciPy、FFT 数值往返不变量）

本模块在 GUI 主进程中运行，遵循架构规则：**不得**在此直接导入 osgeo 或 rasterio，
所有原生地理验证均通过子进程隔离探针执行。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class EngineReport:
    role: str
    status: str  # "ok" | "warning" | "error" | "missing"
    python: str | None = None
    executable: str | None = None
    version_info: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, Any] = field(default_factory=dict)
    drivers: dict[str, bool] = field(default_factory=dict)
    cli_tools: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)


@dataclass
class DoctorReport:
    generated_utc: str
    project_root: str
    overall_ok: bool
    gui_can_start: bool
    envs: dict[str, EngineReport] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_project_root(start_path: Path | str | None = None) -> Path:
    onedir_root = os.environ.get("SPECTRA_ONEDIR_ROOT")
    if onedir_root and Path(onedir_root).is_dir():
        return Path(onedir_root).resolve()
    current = Path(start_path or Path.cwd()).resolve()
    for p in [current, *current.parents]:
        if (p / "pyproject.toml").is_file() and (p / "src" / "spectra_workbench").is_dir():
            return p
        if (p / "runtimes").is_dir() and ((p / "SpectraWorkbench.exe").is_file() or (p / "app").is_dir()):
            return p
    return current


def _run_subproc(cmd: list[str], env: dict[str, str] | None = None, timeout: float = 30.0) -> tuple[int, str, str]:
    sub_env = os.environ.copy()
    if env:
        sub_env.update(env)
    sub_env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=sub_env,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Timeout after {timeout} seconds"
    except Exception as exc:
        return -2, "", str(exc)


def probe_role(
    python_exe: str | Path,
    probe_script: str | Path,
    role: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], str]:
    code, stdout, stderr = _run_subproc([str(python_exe), str(probe_script), role], env=extra_env)
    if code != 0:
        return code, {}, stderr
    # Parse last json line
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                return 0, data, stderr
            except Exception:
                pass
    return -3, {}, f"No valid JSON output. stdout: {stdout[:200]}"


def resolve_gdal_runtime(project_root: Path | str | None = None) -> dict[str, Any]:
    """统一解析 GDAL 外部开发期运行时路径与环境变量。

    优先级：
    1. 环境变量 SPECTRA_GDAL_PYTHON, SPECTRA_GDAL_BIN, GDAL_DATA, PROJ_DATA
    2. docs/progress/runs/env_report.json 中的 gdal 运行时登记
    3. 默认 QGIS 3.44.15 开发期探测路径
    """
    root = find_project_root(project_root)
    env_py = os.environ.get("SPECTRA_GDAL_PYTHON")
    env_bin = os.environ.get("SPECTRA_GDAL_BIN")
    env_gdal_data = os.environ.get("GDAL_DATA")
    env_proj_data = os.environ.get("PROJ_DATA")

    py_path: Path | None = Path(env_py) if env_py else None
    bin_path: Path | None = Path(env_bin) if env_bin else None
    gdal_data_path: Path | None = Path(env_gdal_data) if env_gdal_data else None
    proj_data_path: Path | None = Path(env_proj_data) if env_proj_data else None

    # Check bundled onedir runtime in root / "runtimes" / "gdal"
    if not (py_path and py_path.exists()):
        bundled_gdal = root / "runtimes" / "gdal"
        if bundled_gdal.is_dir():
            cand_py = bundled_gdal / "python.exe"
            if not cand_py.exists():
                cand_py = bundled_gdal / "apps" / "Python312" / "python.exe"
            if cand_py.exists():
                py_path = cand_py
                cand_bin = bundled_gdal / "bin"
                bin_path = cand_bin if cand_bin.exists() else bundled_gdal
                cand_gdal_data = bundled_gdal / "share" / "gdal"
                if cand_gdal_data.exists():
                    gdal_data_path = cand_gdal_data
                cand_proj_data = bundled_gdal / "share" / "proj"
                if cand_proj_data.exists():
                    proj_data_path = cand_proj_data

    # Check env_report.json
    if not (py_path and py_path.exists()):
        report_file = root / "docs" / "progress" / "runs" / "env_report.json"
        if report_file.is_file():
            try:
                data = json.loads(report_file.read_text(encoding="utf-8-sig"))
                gdal_reg = data.get("envs", {}).get("gdal", {})
                if gdal_reg.get("python"):
                    p = Path(gdal_reg["python"])
                    if p.exists():
                        py_path = p
                if gdal_reg.get("bin"):
                    b = Path(gdal_reg["bin"])
                    if b.exists():
                        bin_path = b
                if gdal_reg.get("gdal_data"):
                    g = Path(gdal_reg["gdal_data"])
                    if g.exists():
                        gdal_data_path = g
                if gdal_reg.get("proj_data"):
                    pr = Path(gdal_reg["proj_data"])
                    if pr.exists():
                        proj_data_path = pr
            except Exception:
                pass

    # Fallback to default QGIS installation
    if not py_path:
        py_path = Path(r"C:\Program Files\QGIS 3.44.15\apps\Python312\python.exe")
    if not bin_path:
        bin_path = Path(r"C:\Program Files\QGIS 3.44.15\bin")
    if not gdal_data_path:
        gdal_data_path = Path(r"C:\Program Files\QGIS 3.44.15\apps\gdal\share\gdal")
    if not proj_data_path:
        proj_data_path = Path(r"C:\Program Files\QGIS 3.44.15\share\proj")

    runtime_env = {
        "PATH": f"{bin_path};{os.environ.get('PATH', '')}",
        "GDAL_DATA": str(gdal_data_path),
        "PROJ_DATA": str(proj_data_path),
    }

    return {
        "python": py_path,
        "bin": bin_path,
        "gdal_data": gdal_data_path,
        "proj_data": proj_data_path,
        "env": runtime_env,
    }


def run_doctor(project_root: Path | str | None = None) -> DoctorReport:
    root = find_project_root(project_root)
    probe_py = root / "environments" / "probe.py"
    venvs_dir = root / ".venvs"

    envs_report: dict[str, EngineReport] = {}
    warnings: list[str] = []
    errors: list[str] = []

    # 1. UI Environment
    ui_py = venvs_dir / "sw-ui" / "Scripts" / "python.exe"
    if not ui_py.exists():
        envs_report["ui"] = EngineReport(
            role="ui",
            status="missing",
            messages=[f"Python not found at {ui_py}"],
        )
        errors.append("UI environment missing (.venvs/sw-ui)")
    else:
        code, data, err = probe_role(ui_py, probe_py, "ui")
        if code != 0:
            envs_report["ui"] = EngineReport(
                role="ui",
                status="error",
                python=str(ui_py),
                messages=[f"Probe failed with exit code {code}: {err}"],
            )
            errors.append(f"UI probe failed: {err}")
        else:
            pkgs = data.get("packages", {})
            iso = data.get("isolation", {})
            ui_status = "ok"
            msgs = []
            # Check packages
            for req in ("PySide6", "pyqtgraph", "pydantic", "jsonschema"):
                if not pkgs.get(req):
                    ui_status = "error"
                    msgs.append(f"Missing required package: {req}")
            # Check strict isolation: no geospatial packages in UI
            for geo in ("osgeo", "rasterio", "fiona", "geopandas"):
                if iso.get(geo, False):
                    ui_status = "error"
                    msgs.append(f"Isolation violation: {geo} is importable in UI process!")
            if ui_status == "ok":
                msgs.append("All core UI packages present and geospatial engines properly isolated.")

            envs_report["ui"] = EngineReport(
                role="ui",
                status=ui_status,
                python=data.get("python"),
                executable=str(ui_py),
                version_info={
                    "PySide6": pkgs.get("PySide6"),
                    "pyqtgraph": pkgs.get("pyqtgraph"),
                    "pydantic": pkgs.get("pydantic"),
                    "jsonschema": pkgs.get("jsonschema"),
                    "qt_version": data.get("qt_version"),
                },
                checks={
                    "isolation_passed": all(not v for v in iso.values()),
                    "isolation_details": iso,
                },
                messages=msgs,
            )
            if ui_status == "error":
                errors.extend(msgs)

    # 2. GDAL Runtime
    # 统一解析外部开发期运行时（由 resolve_gdal_runtime 发现）
    gdal_rt = resolve_gdal_runtime(root)
    qgis_python = gdal_rt["python"]
    qgis_bin = gdal_rt["bin"]
    gdal_env = gdal_rt["env"]

    if not qgis_python or not qgis_python.exists():
        envs_report["gdal"] = EngineReport(
            role="gdal",
            status="missing",
            messages=[f"GDAL Python runtime not found at {qgis_python}"],
        )
        errors.append("GDAL external runtime missing")
    else:
        code, data, err = probe_role(qgis_python, probe_py, "gdal", extra_env=gdal_env)
        if code != 0:
            envs_report["gdal"] = EngineReport(
                role="gdal",
                status="error",
                python=str(qgis_python),
                messages=[f"GDAL probe failed with code {code}: {err}"],
            )
            errors.append(f"GDAL probe failed: {err}")
        else:
            gdal_data_info = data.get("gdal", {})
            gtiff_chk = gdal_data_info.get("gtiff_roundtrip", {})
            coord_chk = gdal_data_info.get("coord_transform", {})
            res_chk = gdal_data_info.get("resource_dirs", {})
            drivers = gdal_data_info.get("drivers_of_interest", {})

            gdal_status = "ok"
            msgs = []
            gdal_errors = []

            # CLI tools check：严格校验可执行文件存在、进程启动退出码与版本输出
            cli_results = {}
            for exe in ("gdal", "gdalinfo", "ogrinfo", "gdal_translate", "gdalwarp"):
                exe_path = qgis_bin / f"{exe}.exe"
                if not exe_path.exists():
                    cli_results[exe] = {
                        "available": False,
                        "path": str(exe_path),
                        "error": "executable not found",
                    }
                    gdal_errors.append(f"GDAL CLI tool {exe} not found at {exe_path}")
                else:
                    c_code, c_out, c_err = _run_subproc([str(exe_path), "--version"], env=gdal_env)
                    if c_code != 0:
                        cli_results[exe] = {
                            "available": False,
                            "path": str(exe_path),
                            "exit_code": c_code,
                            "stderr": c_err.strip(),
                            "error": f"Process exited with code {c_code}: {c_err.strip() or 'unknown failure'}",
                        }
                        gdal_errors.append(f"GDAL CLI {exe} execution failed (exit code {c_code}): {c_err.strip()}")
                    else:
                        ver_line = c_out.splitlines()[0].strip() if c_out.strip() else ""
                        if not ver_line:
                            cli_results[exe] = {
                                "available": False,
                                "path": str(exe_path),
                                "exit_code": 0,
                                "error": "empty version output from CLI",
                            }
                            gdal_errors.append(f"GDAL CLI {exe} returned exit code 0 but version output was empty")
                        else:
                            cli_results[exe] = {
                                "available": True,
                                "path": str(exe_path),
                                "exit_code": 0,
                                "version": ver_line,
                            }

            if not gtiff_chk.get("ok"):
                gdal_errors.append(f"GDAL GTiff roundtrip failed: {gtiff_chk.get('reason') or gtiff_chk.get('error')}")
            if not coord_chk.get("ok"):
                gdal_errors.append(f"GDAL coordinate transform failed: {coord_chk.get('error')}")
            if not res_chk.get("gdal_data_valid"):
                msgs.append(f"GDAL_DATA invalid or missing: {res_chk.get('gdal_data')}")
            if not res_chk.get("proj_data_valid"):
                msgs.append("PROJ_DATA invalid: proj.db not found in search paths")

            # Check drivers
            if not drivers.get("HDF4", False):
                warnings.append("GDAL driver HDF4 is absent (optional driver; core GUI & raster functions remain fully operational)")
                msgs.append("HDF4 absent: optional driver, core unaffected. GUI can start normally.")
            else:
                msgs.append("HDF4 driver present.")

            if not drivers.get("GTiff", False) or not drivers.get("ENVI", False):
                gdal_errors.append("Critical driver missing: GTiff or ENVI")

            if gdal_errors:
                gdal_status = "error"
                msgs.extend(gdal_errors)
                errors.extend(gdal_errors)
            else:
                msgs.append(f"GDAL {gdal_data_info.get('gdal_release')} verified ({gdal_data_info.get('driver_count')} drivers, PROJ {gdal_data_info.get('proj_version')}).")

            envs_report["gdal"] = EngineReport(
                role="gdal",
                status=gdal_status,
                python=data.get("python"),
                executable=str(qgis_python),
                version_info={
                    "gdal_version": gdal_data_info.get("gdal_release"),
                    "gdal_rev": gdal_data_info.get("gdal_rev"),
                    "proj_version": gdal_data_info.get("proj_version"),
                },
                checks={
                    "gtiff_roundtrip": gtiff_chk,
                    "coord_transform": coord_chk,
                    "resource_dirs": res_chk,
                    "gui_can_start_without_hdf4": bool(gtiff_chk.get("ok") and coord_chk.get("ok")),
                },
                drivers=drivers,
                cli_tools=cli_results,
                messages=msgs,
            )

    # 3. Rasterio Environment
    rio_py = venvs_dir / "sw-rasterio" / "Scripts" / "python.exe"
    if not rio_py.exists():
        envs_report["rasterio"] = EngineReport(
            role="rasterio",
            status="missing",
            messages=[f"Rasterio python not found at {rio_py}"],
        )
        errors.append("Rasterio environment missing (.venvs/sw-rasterio)")
    else:
        code, data, err = probe_role(rio_py, probe_py, "rasterio")
        if code != 0:
            envs_report["rasterio"] = EngineReport(
                role="rasterio",
                status="error",
                python=str(rio_py),
                messages=[f"Rasterio probe failed with code {code}: {err}"],
            )
            errors.append(f"Rasterio probe failed: {err}")
        else:
            rio_data = data.get("rasterio", {})
            gtiff_chk = rio_data.get("gtiff_roundtrip", {})
            rio_status = "ok"
            msgs = []
            rio_errors = []
            if not gtiff_chk.get("ok"):
                rio_errors.append(f"Rasterio GTiff roundtrip failed: {gtiff_chk.get('error') or gtiff_chk.get('reason')}")
            # CLI check：严格校验 rio 存在、退出码与版本输出
            rio_cli_path = venvs_dir / "sw-rasterio" / "Scripts" / "rio.exe"
            cli_res = {}
            if not rio_cli_path.exists():
                cli_res["rio"] = {
                    "available": False,
                    "path": str(rio_cli_path),
                    "error": "rio.exe not found in venv Scripts",
                }
                rio_errors.append("rio CLI not found in venv Scripts")
            else:
                c_code, c_out, c_err = _run_subproc([str(rio_cli_path), "--version"])
                if c_code != 0:
                    cli_res["rio"] = {
                        "available": False,
                        "path": str(rio_cli_path),
                        "exit_code": c_code,
                        "stderr": c_err.strip(),
                        "error": f"Process exited with code {c_code}: {c_err.strip() or 'unknown failure'}",
                    }
                    rio_errors.append(f"rio CLI execution failed (exit code {c_code}): {c_err.strip()}")
                else:
                    ver = c_out.strip()
                    if not ver:
                        cli_res["rio"] = {
                            "available": False,
                            "path": str(rio_cli_path),
                            "exit_code": 0,
                            "error": "empty version output from rio CLI",
                        }
                        rio_errors.append("rio CLI returned empty version output")
                    else:
                        cli_res["rio"] = {
                            "available": True,
                            "path": str(rio_cli_path),
                            "exit_code": 0,
                            "version": ver,
                        }

            if rio_errors:
                rio_status = "error"
                msgs.extend(rio_errors)
                errors.extend(rio_errors)
            else:
                msgs.append(f"Rasterio {rio_data.get('rasterio_version')} verified (linked GDAL {rio_data.get('gdal_version')}).")

            envs_report["rasterio"] = EngineReport(
                role="rasterio",
                status=rio_status,
                python=data.get("python"),
                executable=str(rio_py),
                version_info={
                    "rasterio_version": rio_data.get("rasterio_version"),
                    "gdal_version": rio_data.get("gdal_version"),
                },
                checks={
                    "gtiff_roundtrip": gtiff_chk,
                },
                drivers=rio_data.get("drivers_of_interest", {}),
                cli_tools=cli_res,
                messages=msgs,
            )

    # 4. Science Environment
    sci_py = venvs_dir / "sw-science" / "Scripts" / "python.exe"
    if not sci_py.exists():
        envs_report["science"] = EngineReport(
            role="science",
            status="missing",
            messages=[f"Science python not found at {sci_py}"],
        )
        errors.append("Science environment missing (.venvs/sw-science)")
    else:
        code, data, err = probe_role(sci_py, probe_py, "science")
        if code != 0:
            envs_report["science"] = EngineReport(
                role="science",
                status="error",
                python=str(sci_py),
                messages=[f"Science probe failed with code {code}: {err}"],
            )
            errors.append(f"Science probe failed: {err}")
        else:
            sci_data = data.get("science", {})
            fft_chk = sci_data.get("fft_roundtrip", {})
            sci_status = "ok"
            msgs = []
            sci_errors = []
            if not fft_chk.get("ok"):
                sci_errors.append(f"FFT 2D roundtrip test failed: {fft_chk.get('error') or fft_chk.get('reason')}")
            if sci_errors:
                sci_status = "error"
                msgs.extend(sci_errors)
                errors.extend(sci_errors)
            else:
                msgs.append(f"NumPy {sci_data.get('numpy_version')}, SciPy {sci_data.get('scipy_version')} verified (FFT roundtrip diff < 1e-12).")

            envs_report["science"] = EngineReport(
                role="science",
                status=sci_status,
                python=data.get("python"),
                executable=str(sci_py),
                version_info={
                    "numpy_version": sci_data.get("numpy_version"),
                    "scipy_version": sci_data.get("scipy_version"),
                },
                checks={
                    "fft_roundtrip": fft_chk,
                },
                messages=msgs,
            )

    # Overall determination
    ui_engine = envs_report.get("ui")
    ui_status_ok = ui_engine is not None and ui_engine.status == "ok"

    gdal_engine = envs_report.get("gdal")
    gdal_core_ok = (
        gdal_engine is not None
        and bool(gdal_engine.checks.get("gtiff_roundtrip", {}).get("ok"))
        and bool(gdal_engine.checks.get("coord_transform", {}).get("ok"))
    )

    # GUI 启动许可：UI 依赖就绪且 GDAL 核心检查通过（缺失可选驱动如 HDF4 不阻塞 GUI 启动）
    gui_can_start = ui_status_ok and (gdal_engine is None or gdal_core_ok)
    overall_ok = (len(errors) == 0) and all(r.status == "ok" for r in envs_report.values())

    return DoctorReport(
        generated_utc=datetime.now(UTC).isoformat(),
        project_root=str(root),
        overall_ok=overall_ok,
        gui_can_start=gui_can_start,
        envs=envs_report,
        warnings=warnings,
        errors=errors,
    )


def format_console_report(report: DoctorReport) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("   Spectra Workbench · 引擎与运行环境自检报告 (Doctor)")
    lines.append(f"   生成时间: {report.generated_utc}")
    lines.append(f"   项目根目录: {report.project_root}")
    lines.append("=" * 72)

    status_tag = "【通过 ALL OK】" if report.overall_ok else ("【就绪 READY (含警告)】" if report.gui_can_start else "【失败 FAIL】")
    lines.append(f"总体判定: {status_tag}")
    lines.append(f"GUI 启动许可: {'允许 (UI环境与核心依赖就绪)' if report.gui_can_start else '拒绝 (依赖未满足)'}")
    lines.append("-" * 72)

    for role, env in report.envs.items():
        role_title = {
            "ui": "1. UI 界面环境 (sw-ui)",
            "gdal": "2. GDAL / OGR 引擎 (QGIS 运行时)",
            "rasterio": "3. Rasterio 引擎 (sw-rasterio)",
            "science": "4. 科学计算引擎 (sw-science)",
        }.get(role, f"环境: {role}")

        icon = "[OK]" if env.status == "ok" else ("[ !]" if env.status == "warning" else "[X ]")
        lines.append(f"{icon} {role_title} -> 状态: {env.status.upper()}")
        if env.python:
            lines.append(f"   解释器: {env.python}")

        # Details per role
        if role == "ui":
            iso = env.checks.get("isolation_details", {})
            iso_str = ", ".join(f"{k}={'泄露' if v else '隔离'}" for k, v in iso.items())
            lines.append(f"   依赖隔离: {iso_str}")
            lines.append(f"   Qt / PySide6 版本: {env.version_info.get('qt_version', 'unknown')}")
        elif role == "gdal":
            lines.append(f"   GDAL 版本: {env.version_info.get('gdal_version')} | PROJ: {env.version_info.get('proj_version')}")
            gt = env.checks.get("gtiff_roundtrip", {})
            ct = env.checks.get("coord_transform", {})
            rd = env.checks.get("resource_dirs", {})
            lines.append(f"   GTiff 往返: {'成功' if gt.get('ok') else '失败'} | 坐标转换 (4326->3857): {'成功' if ct.get('ok') else '失败'}")
            lines.append(f"   资源目录: GDAL_DATA={'有效' if rd.get('gdal_data_valid') else '无效'}, PROJ_DATA={'有效' if rd.get('proj_data_valid') else '无效'}")
            drvs = env.drivers
            drv_str = " ".join(f"{k}:{'[OK]' if v else '[-]'}" for k, v in drvs.items())
            lines.append(f"   驱动支持: {drv_str}")
            clis = env.cli_tools
            cli_str = " ".join(f"{k}:{'[OK]' if v.get('available') else '[-]'}" for k, v in clis.items())
            lines.append(f"   CLI 工具: {cli_str}")
        elif role == "rasterio":
            lines.append(f"   Rasterio: {env.version_info.get('rasterio_version')} (链接 GDAL: {env.version_info.get('gdal_version')})")
            gt = env.checks.get("gtiff_roundtrip", {})
            lines.append(f"   GTiff 往返: {'成功' if gt.get('ok') else '失败'}")
            rio_cli = env.cli_tools.get("rio", {})
            lines.append(f"   rio CLI: {'可用 (' + rio_cli.get('version', '') + ')' if rio_cli.get('available') else '缺失'}")
        elif role == "science":
            lines.append(f"   NumPy: {env.version_info.get('numpy_version')} | SciPy: {env.version_info.get('scipy_version')}")
            fft = env.checks.get("fft_roundtrip", {})
            lines.append(f"   FFT 2D 往返: {'成功 (误差 < 1e-12)' if fft.get('ok') else '失败'}")

        for m in env.messages:
            lines.append(f"   - {m}")
        lines.append("")

    if report.warnings:
        lines.append("【提示与警告】")
        for w in report.warnings:
            lines.append(f"  * {w}")
        lines.append("")

    if report.errors:
        lines.append("【阻断性错误】")
        for e in report.errors:
            lines.append(f"  ! {e}")
        lines.append("")

    lines.append("=" * 72)
    return "\n".join(lines)


def save_doctor_report(report: DoctorReport, output_path: Path | str) -> None:
    from spectra_workbench.domain.credentials import sanitize_dict

    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(sanitize_dict(report.to_dict()), f, indent=2, ensure_ascii=False)


def main() -> int:
    import argparse

    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Spectra Workbench Doctor")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    parser.add_argument("--out", type=str, default=None, help="Save report JSON to path")
    parser.add_argument("--ui-only", action="store_true", help="Only verify UI environment readiness for starting the GUI")
    args = parser.parse_args()

    report = run_doctor()

    # Default output path
    default_out = Path(report.project_root) / "docs" / "progress" / "runs" / "doctor_report.json"
    save_path = Path(args.out) if args.out else default_out
    save_doctor_report(report, save_path)

    if args.json:
        from spectra_workbench.domain.credentials import sanitize_dict
        print(json.dumps(sanitize_dict(report.to_dict()), indent=2, ensure_ascii=False))
    else:
        print(format_console_report(report))
        print(f"报告已写入: {save_path}")

    # 独立退出语义：
    # --ui-only 模式只检查 UI 环境是否满足 GUI 进程启动
    # 全量 Doctor 模式必须保证 overall_ok 为 True（核心引擎与 CLI 诊断全部就绪）
    if args.ui_only:
        return 0 if (report.envs.get("ui") and report.envs["ui"].status == "ok") else 1
    return 0 if report.overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
