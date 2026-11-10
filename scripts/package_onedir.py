"""Spectra Workbench Windows onedir 打包与重定位验证脚本 (P10-01)。

功能：
1. 构建自包含 Windows onedir 发布目录结构：
   - SpectraWorkbench.exe：原生 Windows GUI 启动器（无黑框闪烁，自动解析相对路径与运行时环境）；
   - SpectraWorkbench-console.exe：原生控制台启动器（附带标准输出，供 CLI 与自动化测试调用）；
   - SpectraWorkbench.bat：跨环境兼容批处理启动脚本；
   - app/：包含 Python UI 运行时、PySide6 原生库及 Qt 平台插件（platforms/qwindows.dll）、项目核心包；
   - runtimes/gdal/：自带可重定位 GDAL 引擎运行时（含 python、bin/ 动态库、gdalinfo 等 CLI、osgeo、share/gdal 与 share/proj proj.db）；
   - runtimes/rasterio/：自带可重定位 Rasterio 引擎运行时（含 rasterio、rasterio.libs）；
   - resources/：包含基准契约、元数据 Schema 与中文字体资源；
   - manifest.json：包含发布元数据、Git SHA 与组件清单。
2. 自动化重定位验证：
   - 将打包成果完整复制/迁移至含深层中文与空格的路径（如 'work/dist_test/高光谱 工作台 2026/SpectraWorkbench'）；
   - 验证 UI 启动、Qt 平台插件加载、GDAL 引擎探测与投影变换（EPSG:4326 -> EPSG:3857 使用内嵌 proj.db）、Rasterio 读写完整通过。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from spectra_workbench.app.doctor import find_project_root, resolve_gdal_runtime

LAUNCHER_CS_TEMPLATE = r"""
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

namespace SpectraWorkbenchLauncher
{
    class Program
    {
        static int Main(string[] args)
        {
            try
            {
                string baseDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
                
                // 1. 定位 UI Python 解释器
                string uiPy = Path.Combine(baseDir, "app", "python.exe");
                if (!File.Exists(uiPy))
                {
                    uiPy = Path.Combine(baseDir, "app", "Scripts", "python.exe");
                }
                if (!File.Exists(uiPy))
                {
                    Console.Error.WriteLine("[Spectra Launcher] 错误：未能在发布包中找到 UI Python 解释器: " + uiPy);
                    return 1;
                }

                // 2. 构造启动参数
                ProcessStartInfo psi = new ProcessStartInfo();
                psi.FileName = uiPy;
                psi.WorkingDirectory = baseDir;
                psi.UseShellExecute = false;

                StringBuilder sb = new StringBuilder();
                sb.Append("-m spectra_workbench.ui.app");
                foreach (string arg in args)
                {
                    sb.Append(" ");
                    if (arg.Contains(" ") || arg.Contains("\""))
                    {
                        sb.Append("\"").Append(arg.Replace("\"", "\\\"")).Append("\"");
                    }
                    else
                    {
                        sb.Append(arg);
                    }
                }
                psi.Arguments = sb.ToString();

                // 3. 动态配置相对路径环境变量（杜绝写死盘符）
                psi.EnvironmentVariables["SPECTRA_ONEDIR_ROOT"] = baseDir;

                string qtPlugins = Path.Combine(baseDir, "app", "Lib", "site-packages", "PySide6", "plugins");
                if (Directory.Exists(qtPlugins))
                {
                    psi.EnvironmentVariables["QT_QPA_PLATFORM_PLUGIN_PATH"] = qtPlugins;
                }

                string gdalDir = Path.Combine(baseDir, "runtimes", "gdal");
                if (Directory.Exists(gdalDir))
                {
                    string gdalPy = Path.Combine(gdalDir, "python.exe");
                    if (File.Exists(gdalPy)) psi.EnvironmentVariables["SPECTRA_GDAL_PYTHON"] = gdalPy;
                    string gdalBin = Path.Combine(gdalDir, "bin");
                    if (Directory.Exists(gdalBin))
                    {
                        psi.EnvironmentVariables["SPECTRA_GDAL_BIN"] = gdalBin;
                        psi.EnvironmentVariables["PATH"] = gdalBin + ";" + Environment.GetEnvironmentVariable("PATH");
                    }
                    string gdalData = Path.Combine(gdalDir, "share", "gdal");
                    if (Directory.Exists(gdalData)) psi.EnvironmentVariables["GDAL_DATA"] = gdalData;
                    string projData = Path.Combine(gdalDir, "share", "proj");
                    if (Directory.Exists(projData)) psi.EnvironmentVariables["PROJ_DATA"] = projData;
                }

                string rasterioDir = Path.Combine(baseDir, "runtimes", "rasterio");
                if (Directory.Exists(rasterioDir))
                {
                    string rioPy = Path.Combine(rasterioDir, "python.exe");
                    if (!File.Exists(rioPy)) rioPy = Path.Combine(rasterioDir, "Scripts", "python.exe");
                    if (File.Exists(rioPy)) psi.EnvironmentVariables["SPECTRA_RASTERIO_PYTHON"] = rioPy;
                }

                string scienceDir = Path.Combine(baseDir, "runtimes", "science");
                if (Directory.Exists(scienceDir))
                {
                    string sciPy = Path.Combine(scienceDir, "python.exe");
                    if (!File.Exists(sciPy)) sciPy = Path.Combine(scienceDir, "Scripts", "python.exe");
                    if (File.Exists(sciPy)) psi.EnvironmentVariables["SPECTRA_SCIENCE_PYTHON"] = sciPy;
                }

                string srcDir = Path.Combine(baseDir, "app", "src");
                string sitePackages = Path.Combine(baseDir, "app", "Lib", "site-packages");
                string existingPP = Environment.GetEnvironmentVariable("PYTHONPATH") ?? "";
                psi.EnvironmentVariables["PYTHONPATH"] = srcDir + ";" + sitePackages + ";" + existingPP;
                psi.EnvironmentVariables["PYTHONUTF8"] = "1";
                psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";

                // 4. 启动进程并等待退出
                using (Process proc = Process.Start(psi))
                {
                    proc.WaitForExit();
                    return proc.ExitCode;
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[Spectra Launcher] 启动异常: " + ex.Message);
                return 2;
            }
        }
    }
}
"""

LAUNCHER_BAT_TEMPLATE = r"""@echo off
setlocal
set "BASE_DIR=%~dp0"
if "%BASE_DIR:~-1%"=="\" set "BASE_DIR=%BASE_DIR:~0,-1%"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "SPECTRA_ONEDIR_ROOT=%BASE_DIR%"

set "QT_QPA_PLATFORM_PLUGIN_PATH=%BASE_DIR%\app\Lib\site-packages\PySide6\plugins"
set "SPECTRA_GDAL_PYTHON=%BASE_DIR%\runtimes\gdal\python.exe"
set "SPECTRA_GDAL_BIN=%BASE_DIR%\runtimes\gdal\bin"
set "GDAL_DATA=%BASE_DIR%\runtimes\gdal\share\gdal"
set "PROJ_DATA=%BASE_DIR%\runtimes\gdal\share\proj"
set "PATH=%BASE_DIR%\runtimes\gdal\bin;%BASE_DIR%\app;%PATH%"
set "PYTHONPATH=%BASE_DIR%\app\src;%BASE_DIR%\app\Lib\site-packages;%PYTHONPATH%"

"%BASE_DIR%\app\python.exe" -m spectra_workbench.ui.app %*
if errorlevel 1 (
    echo.
    echo ========================================================
    echo Spectra Workbench 异常退出，退出代码: %ERRORLEVEL%
    echo ========================================================
    echo 请查看上方错误堆栈，按任意键关闭此窗口...
    pause >nul
)
exit /b %ERRORLEVEL%
"""


def _to_extended_path(p: Path) -> Path:
    p_res = p.resolve()
    if os.name == "nt":
        s = str(p_res)
        if not s.startswith("\\\\?\\"):
            return Path(f"\\\\?\\{s}")
    return p_res


def _ensure_dir(p: Path) -> Path:
    ext_p = _to_extended_path(p)
    os.makedirs(ext_p, exist_ok=True)
    return ext_p


def _link_or_copy(src: Path, dst: Path, mode: str = "link") -> None:
    r"""在 Windows NTFS 上优先创建硬链接，长路径使用 \\?\ 前缀规避 260 字符 MAX_PATH 限制。"""
    src_res = _to_extended_path(src)
    dst_res = _to_extended_path(dst)

    _ensure_dir(dst_res.parent)
    if dst_res.exists():
        dst_res.unlink()

    if mode == "link":
        try:
            os.link(src_res, dst_res)
            return
        except Exception:
            pass

    shutil.copy2(src_res, dst_res)


def _copy_tree_efficient(src_dir: Path, dst_dir: Path, mode: str = "link", ignore_patterns: set[str] | None = None) -> int:
    """高效批量镜像目录树。"""
    count = 0
    ignore = ignore_patterns or {"__pycache__", "*.pyc", "*.tmp", ".git"}
    for root, dirs, files in os.walk(src_dir):
        rel_root = Path(root).relative_to(src_dir)
        # 过滤忽略目录
        dirs[:] = [d for d in dirs if d not in ignore and not d.startswith(".")]

        target_root = dst_dir / rel_root
        _ensure_dir(target_root)

        for f in files:
            if any(f.endswith(ext.replace("*", "")) for ext in ignore if "*" in ext):
                continue
            src_file = Path(root) / f
            dst_file = target_root / f
            _link_or_copy(src_file, dst_file, mode=mode)
            count += 1
    return count


def compile_native_launcher(output_exe: Path, is_gui: bool = True) -> bool:
    """使用 Windows 内置 C# 编译器（csc.exe）将启动器编译为原生 PE 可执行程序。"""
    csc_path = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    if not csc_path.exists():
        csc_path = Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")

    if not csc_path.exists():
        print("未找到 Windows 内置 csc.exe，跳过原生 exe 编译")
        return False

    output_exe.parent.mkdir(parents=True, exist_ok=True)
    cs_src = output_exe.parent / f"{output_exe.stem}.cs"
    cs_src.write_text(LAUNCHER_CS_TEMPLATE.strip(), encoding="utf-8")

    target_type = "winexe" if is_gui else "exe"
    cmd = [
        str(csc_path),
        "/nologo",
        f"/target:{target_type}",
        f"/out:{output_exe}",
        str(cs_src),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    cs_src.unlink(missing_ok=True)

    if proc.returncode != 0:
        print(f"编译启动器 {output_exe.name} 失败: {proc.stderr}")
        return False

    return True


UNINSTALLER_CS_TEMPLATE = r"""
using System;
using System.IO;
using System.Collections.Generic;

namespace SpectraWorkbenchUninstaller
{
    class Program
    {
        static int Main(string[] args)
        {
            try
            {
                string appDir = AppDomain.CurrentDomain.BaseDirectory;
                Console.WriteLine("[Spectra Uninstaller] 正在执行 Spectra Workbench 安全卸载...");
                Console.WriteLine("[Spectra Uninstaller] 安装目录: " + appDir);

                // 收集并保全所有用户项目文件 (*.spectra.json)
                List<string> preservedProjects = new List<string>();
                try
                {
                    foreach (string f in Directory.GetFiles(appDir, "*.*", SearchOption.AllDirectories))
                    {
                        if (f.EndsWith(".spectra.json", StringComparison.OrdinalIgnoreCase) ||
                            f.IndexOf("user_projects", StringComparison.OrdinalIgnoreCase) >= 0)
                        {
                            preservedProjects.Add(f);
                        }
                    }
                }
                catch {}

                // 清理应用核心目录: app, runtimes, resources, environments
                string[] appDirs = new string[] { "app", "runtimes", "resources", "environments" };
                foreach (string sub in appDirs)
                {
                    string subPath = Path.Combine(appDir, sub);
                    if (Directory.Exists(subPath))
                    {
                        try { Directory.Delete(subPath, true); } catch {}
                    }
                }

                // 清理启动器与发布清单
                string[] rootFiles = new string[] {
                    "SpectraWorkbench.exe", "SpectraWorkbench-console.exe",
                    "SpectraWorkbench.bat", "run-app.cmd", "manifest.json", "install_manifest.json"
                };
                foreach (string rf in rootFiles)
                {
                    string rfp = Path.Combine(appDir, rf);
                    if (File.Exists(rfp))
                    {
                        try { File.Delete(rfp); } catch {}
                    }
                }

                if (preservedProjects.Count > 0)
                {
                    Console.WriteLine("[Spectra Uninstaller] [用户项目保全] 成功保全以下 " + preservedProjects.Count + " 个用户项目文件：");
                    foreach (string p in preservedProjects)
                    {
                        Console.WriteLine("  -> " + p);
                    }
                }

                Console.WriteLine("[Spectra Uninstaller] 应用程序二进制与运行时已安全清理完毕。");
                return 0;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("[Spectra Uninstaller] 卸载出现异常: " + ex.Message);
                return 1;
            }
        }
    }
}
"""

UNINSTALLER_BAT_TEMPLATE = r"""@echo off
setlocal
set "APP_DIR=%~dp0"
echo [Spectra Workbench] 正在执行安全卸载...
if exist "%APP_DIR%uninstall.exe" (
    "%APP_DIR%uninstall.exe" %*
    goto :done
)
if exist "%APP_DIR%app" rmdir /s /q "%APP_DIR%app"
if exist "%APP_DIR%runtimes" rmdir /s /q "%APP_DIR%runtimes"
if exist "%APP_DIR%resources" rmdir /s /q "%APP_DIR%resources"
if exist "%APP_DIR%environments" rmdir /s /q "%APP_DIR%environments"
if exist "%APP_DIR%SpectraWorkbench.exe" del /f /q "%APP_DIR%SpectraWorkbench.exe"
if exist "%APP_DIR%SpectraWorkbench-console.exe" del /f /q "%APP_DIR%SpectraWorkbench-console.exe"
if exist "%APP_DIR%SpectraWorkbench.bat" del /f /q "%APP_DIR%SpectraWorkbench.bat"
if exist "%APP_DIR%manifest.json" del /f /q "%APP_DIR%manifest.json"
if exist "%APP_DIR%install_manifest.json" del /f /q "%APP_DIR%install_manifest.json"
echo [Spectra Workbench] 应用程序组件已清理完毕。用户项目文件与配置已安全保留。
:done
endlocal
"""


def compile_native_uninstaller(output_exe: Path) -> bool:
    """使用 Windows 内置 C# 编译器编译原生卸载器 PE 可执行程序。"""
    csc_path = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    if not csc_path.exists():
        csc_path = Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")

    if not csc_path.exists():
        return False

    output_exe = Path(output_exe).resolve()
    output_exe.parent.mkdir(parents=True, exist_ok=True)
    cs_src = output_exe.parent / f"{output_exe.stem}.cs"
    cs_src.write_text(UNINSTALLER_CS_TEMPLATE.strip(), encoding="utf-8")

    cmd = [
        str(csc_path),
        "/nologo",
        "/target:exe",
        f"/out:{output_exe}",
        str(cs_src),
    ]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    cs_src.unlink(missing_ok=True)

    if proc.returncode != 0:
        print(f"编译卸载器 {output_exe.name} 失败: {proc.stderr}")
        return False

    return True


def build_onedir_package(dist_dir: Path, mode: str = "link") -> dict[str, Any]:
    """组装完整的 Windows onedir 发布目录。"""
    root = find_project_root()
    dist_dir = Path(dist_dir).resolve()
    print(f">>> 开始构建 Spectra Workbench onedir 发布包至: {dist_dir} (mode={mode})")

    if dist_dir.exists():
        shutil.rmtree(dist_dir, ignore_errors=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    stats: dict[str, int] = {}

    # 1. 组装 app/ (UI 运行时 + 源码)
    app_dir = dist_dir / "app"
    app_dir.mkdir(parents=True, exist_ok=True)

    base_py_dir = Path(sys.base_prefix)
    venv_dir = root / ".venv"
    if not venv_dir.exists():
        venv_dir = root / ".venvs" / "sw-ui"

    base_exe = base_py_dir / "python.exe"
    base_w_exe = base_py_dir / "pythonw.exe"
    _link_or_copy(base_exe, app_dir / "python.exe", mode=mode)
    if base_w_exe.exists():
        _link_or_copy(base_w_exe, app_dir / "pythonw.exe", mode=mode)

    # 拷贝 Python DLLs
    for dll in base_py_dir.glob("*.dll"):
        _link_or_copy(dll, app_dir / dll.name, mode=mode)

    # 镜像基础 DLLs 目录
    if (base_py_dir / "DLLs").is_dir():
        stats["app_dlls"] = _copy_tree_efficient(base_py_dir / "DLLs", app_dir / "DLLs", mode=mode)

    # 镜像基础标准库 Lib (排除 site-packages)
    if (base_py_dir / "Lib").is_dir():
        stats["app_stdlib"] = _copy_tree_efficient(
            base_py_dir / "Lib",
            app_dir / "Lib",
            mode=mode,
            ignore_patterns={"site-packages", "__pycache__", "*.pyc", "*.tmp"},
        )

    # 镜像 site-packages
    src_sp = venv_dir / "Lib" / "site-packages"
    if not src_sp.exists():
        src_sp = venv_dir / "lib" / "site-packages"
    dst_sp = app_dir / "Lib" / "site-packages"

    print(">>> [1/4] 镜像 UI 运行时与核心依赖 (PySide6 / pyqtgraph / numpy)...")
    stats["app_files"] = _copy_tree_efficient(src_sp, dst_sp, mode=mode)

    # 镜像源码
    src_code = root / "src"
    dst_code = app_dir / "src"
    stats["src_files"] = _copy_tree_efficient(src_code, dst_code, mode=mode)

    # 写入 pyvenv.cfg 与 qt.conf
    (app_dir / "pyvenv.cfg").write_text("home = .\ninclude-system-site-packages = false\nversion = 3.12.10\n", encoding="utf-8")
    qt_conf = app_dir / "qt.conf"
    qt_conf.write_text("[Paths]\nPrefix = .\nPlugins = Lib/site-packages/PySide6/plugins\n", encoding="utf-8")

    # 2. 组装 runtimes/gdal/ (自带 GDAL 引擎运行时)
    print(">>> [2/4] 组装自包含 GDAL 引擎运行时 (DLLs + CLI + proj.db)...")
    gdal_rt = resolve_gdal_runtime(root)
    gdal_dist = dist_dir / "runtimes" / "gdal"
    gdal_dist.mkdir(parents=True, exist_ok=True)

    qgis_py = Path(gdal_rt["python"])
    qgis_py_dir = qgis_py.parent
    _link_or_copy(qgis_py, gdal_dist / "python.exe", mode=mode)
    if (qgis_py_dir / "pythonw.exe").exists():
        _link_or_copy(qgis_py_dir / "pythonw.exe", gdal_dist / "pythonw.exe", mode=mode)
    for dll in qgis_py_dir.glob("*.dll"):
        _link_or_copy(dll, gdal_dist / dll.name, mode=mode)

    if (qgis_py_dir / "DLLs").is_dir():
        _copy_tree_efficient(qgis_py_dir / "DLLs", gdal_dist / "DLLs", mode=mode)
    if (qgis_py_dir / "Lib").is_dir():
        _copy_tree_efficient(
            qgis_py_dir / "Lib",
            gdal_dist / "Lib",
            mode=mode,
            ignore_patterns={"site-packages", "__pycache__", "*.pyc", "*.tmp"},
        )

    # 镜像 QGIS bin 下所有 DLL 与 gdal CLI
    qgis_bin = Path(gdal_rt["bin"])
    dst_gdal_bin = gdal_dist / "bin"
    dst_gdal_bin.mkdir(parents=True, exist_ok=True)
    bin_count = 0
    for dll in qgis_bin.glob("*.dll"):
        _link_or_copy(dll, dst_gdal_bin / dll.name, mode=mode)
        bin_count += 1
    for pattern in ("gdal*.exe", "ogr*.exe"):
        for exe in qgis_bin.glob(pattern):
            _link_or_copy(exe, dst_gdal_bin / exe.name, mode=mode)
            bin_count += 1
    stats["gdal_bin_files"] = bin_count

    # 镜像 share/gdal 与 share/proj (含 proj.db)
    dst_gdal_share = gdal_dist / "share" / "gdal"
    dst_proj_share = gdal_dist / "share" / "proj"
    stats["gdal_share_files"] = _copy_tree_efficient(Path(gdal_rt["gdal_data"]), dst_gdal_share, mode=mode)
    stats["proj_share_files"] = _copy_tree_efficient(Path(gdal_rt["proj_data"]), dst_proj_share, mode=mode)

    # 镜像 osgeo 与 numpy site-packages
    qgis_sp = qgis_py_dir / "Lib" / "site-packages"
    dst_gdal_sp = gdal_dist / "Lib" / "site-packages"
    dst_gdal_sp.mkdir(parents=True, exist_ok=True)
    if (qgis_sp / "osgeo").is_dir():
        _copy_tree_efficient(qgis_sp / "osgeo", dst_gdal_sp / "osgeo", mode=mode)
    if (qgis_sp / "numpy").is_dir():
        _copy_tree_efficient(qgis_sp / "numpy", dst_gdal_sp / "numpy", mode=mode)

    # 写入 sitecustomize.py 自动注册 DLL 目录与环境变量
    sitecustomize_code = """import os, sys
from pathlib import Path
base = Path(__file__).resolve().parent.parent
bin_dir = base / "bin"
if bin_dir.is_dir():
    try:
        os.add_dll_directory(str(bin_dir))
    except Exception:
        pass
    os.environ["PATH"] = f"{bin_dir};" + os.environ.get("PATH", "")
gdal_data = base / "share" / "gdal"
if gdal_data.is_dir() and "GDAL_DATA" not in os.environ:
    os.environ["GDAL_DATA"] = str(gdal_data)
proj_data = base / "share" / "proj"
if proj_data.is_dir():
    if "PROJ_DATA" not in os.environ:
        os.environ["PROJ_DATA"] = str(proj_data)
    if "PROJ_LIB" not in os.environ:
        os.environ["PROJ_LIB"] = str(proj_data)
"""
    (gdal_dist / "Lib" / "sitecustomize.py").write_text(sitecustomize_code, encoding="utf-8")

    # 3. 组装 runtimes/rasterio/
    print(">>> [3/4] 组装自包含 Rasterio 引擎运行时...")
    rio_venv = root / ".venvs" / "sw-rasterio"
    rio_dist = dist_dir / "runtimes" / "rasterio"
    rio_dist.mkdir(parents=True, exist_ok=True)

    _link_or_copy(base_exe, rio_dist / "python.exe", mode=mode)
    if base_w_exe.exists():
        _link_or_copy(base_w_exe, rio_dist / "pythonw.exe", mode=mode)
    for dll in base_py_dir.glob("*.dll"):
        _link_or_copy(dll, rio_dist / dll.name, mode=mode)

    if (base_py_dir / "DLLs").is_dir():
        _copy_tree_efficient(base_py_dir / "DLLs", rio_dist / "DLLs", mode=mode)
    if (base_py_dir / "Lib").is_dir():
        _copy_tree_efficient(
            base_py_dir / "Lib",
            rio_dist / "Lib",
            mode=mode,
            ignore_patterns={"site-packages", "__pycache__", "*.pyc", "*.tmp"},
        )

    if rio_venv.exists():
        rio_sp = rio_venv / "Lib" / "site-packages"
        if rio_sp.exists():
            dst_rio_sp = rio_dist / "Lib" / "site-packages"
            stats["rasterio_files"] = _copy_tree_efficient(rio_sp, dst_rio_sp, mode=mode)
        rio_exe = rio_venv / "Scripts" / "rio.exe"
        if rio_exe.exists():
            _link_or_copy(rio_exe, rio_dist / "rio.exe", mode=mode)
            (rio_dist / "Scripts").mkdir(parents=True, exist_ok=True)
            _link_or_copy(rio_exe, rio_dist / "Scripts" / "rio.exe", mode=mode)

    (rio_dist / "pyvenv.cfg").write_text("home = .\ninclude-system-site-packages = false\nversion = 3.12.10\n", encoding="utf-8")

    # 4. 组装 runtimes/science/
    print(">>> [4/5] 组装自包含 Science 引擎运行时...")
    sci_venv = root / ".venvs" / "sw-science"
    sci_dist = dist_dir / "runtimes" / "science"
    sci_dist.mkdir(parents=True, exist_ok=True)

    _link_or_copy(base_exe, sci_dist / "python.exe", mode=mode)
    if base_w_exe.exists():
        _link_or_copy(base_w_exe, sci_dist / "pythonw.exe", mode=mode)
    for dll in base_py_dir.glob("*.dll"):
        _link_or_copy(dll, sci_dist / dll.name, mode=mode)

    if (base_py_dir / "DLLs").is_dir():
        _copy_tree_efficient(base_py_dir / "DLLs", sci_dist / "DLLs", mode=mode)
    if (base_py_dir / "Lib").is_dir():
        _copy_tree_efficient(
            base_py_dir / "Lib",
            sci_dist / "Lib",
            mode=mode,
            ignore_patterns={"site-packages", "__pycache__", "*.pyc", "*.tmp"},
        )

    if sci_venv.exists():
        sci_sp = sci_venv / "Lib" / "site-packages"
        if sci_sp.exists():
            dst_sci_sp = sci_dist / "Lib" / "site-packages"
            stats["science_files"] = _copy_tree_efficient(sci_sp, dst_sci_sp, mode=mode)

    (sci_dist / "pyvenv.cfg").write_text("home = .\ninclude-system-site-packages = false\nversion = 3.12.10\n", encoding="utf-8")

    # 5. 拷贝 resources 与编译启动器及卸载器
    print(">>> [5/5] 拷贝资源与编译原生 Windows 启动器 (SpectraWorkbench.exe) 及卸载器...")
    res_src = root / "src" / "spectra_workbench" / "resources"
    res_dst = dist_dir / "resources"
    if res_src.exists():
        stats["resource_files"] = _copy_tree_efficient(res_src, res_dst, mode=mode)

    # 拷贝 probe.py 至 resources 与 environments 目录供干净机自检调用
    probe_src = root / "environments" / "probe.py"
    if probe_src.exists():
        _link_or_copy(probe_src, dist_dir / "resources" / "probe.py", mode=mode)
        (dist_dir / "environments").mkdir(parents=True, exist_ok=True)
        _link_or_copy(probe_src, dist_dir / "environments" / "probe.py", mode=mode)

    # 编译原生启动器与卸载器
    compile_native_launcher(dist_dir / "SpectraWorkbench.exe", is_gui=True)
    compile_native_launcher(dist_dir / "SpectraWorkbench-console.exe", is_gui=False)
    compile_native_uninstaller(dist_dir / "uninstall.exe")
    bat_text = LAUNCHER_BAT_TEMPLATE.strip().replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n"
    uninst_text = UNINSTALLER_BAT_TEMPLATE.strip().replace("\r\n", "\n").replace("\n", "\r\n") + "\r\n"
    (dist_dir / "SpectraWorkbench.bat").write_bytes(bat_text.encode("utf-8"))
    (dist_dir / "run-app.cmd").write_bytes(bat_text.encode("utf-8"))
    (dist_dir / "uninstall.bat").write_bytes(uninst_text.encode("utf-8"))

    # 生成发布清单
    manifest = {
        "schema_version": "1.0",
        "app_name": "Spectra Workbench",
        "version": "1.0.0",
        "layout": "onedir",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": stats,
    }
    (dist_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f">>> onedir 发布包构建完成！目录文件概况: {stats}")
    return manifest


def verify_relocation(dist_dir: Path, verify_base_dir: Path) -> dict[str, Any]:
    """将发布包完整迁至包含中文与空格的深层目录，验证运行能力与 CRS 资源完整性。"""
    verify_base_dir = Path(verify_base_dir).resolve()
    relocated_app = verify_base_dir / "SpectraWorkbench"
    print(f"\n=== 开始验证发布包重定位: 目标目录 -> {relocated_app} ===")

    if relocated_app.exists():
        shutil.rmtree(relocated_app, ignore_errors=True)
    relocated_app.parent.mkdir(parents=True, exist_ok=True)

    # 复制发布包至中文与空格目录
    print(">>> 正在复制/硬链接发布包至中文与空格测试目录...")
    _copy_tree_efficient(dist_dir, relocated_app, mode="link")

    results: dict[str, Any] = {}

    # 1. 验证 UI 启动器与 Qt 平台插件
    console_exe = relocated_app / "SpectraWorkbench-console.exe"
    if not console_exe.exists():
        console_exe = relocated_app / "SpectraWorkbench.bat"

    ui_py = relocated_app / "app" / "python.exe"
    qt_script = """
import sys
from PySide6.QtWidgets import QApplication, QLabel

app = QApplication(['test_onedir', '-platform', 'offscreen'])
lbl = QLabel('Spectra Workbench Onedir UI Relocation Verified')
lbl.show()
assert app.platformName() == 'offscreen'
print('QT_UI_OFFSCREEN_VERIFIED_OK')
sys.exit(0)
"""
    env_ui = os.environ.copy()
    env_ui["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(relocated_app / "app" / "Lib" / "site-packages" / "PySide6" / "plugins")
    env_ui["PYTHONPATH"] = f"{relocated_app / 'app' / 'src'};{relocated_app / 'app' / 'Lib' / 'site-packages'}"
    proc_ui = subprocess.run([str(ui_py), "-c", qt_script], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env_ui, timeout=20)
    assert proc_ui.returncode == 0, f"UI 在中文重定位目录启动失败: stdout={proc_ui.stdout}, stderr={proc_ui.stderr}"
    assert "QT_UI_OFFSCREEN_VERIFIED_OK" in proc_ui.stdout
    results["ui_qt_verified"] = True
    print("  [PASS] 1. UI 引擎与 Qt 平台插件在中文/空格目录下成功初始化")

    # 2. 验证 GDAL 引擎运行时与内嵌 proj.db 坐标转换
    gdal_py = relocated_app / "runtimes" / "gdal" / "python.exe"
    gdal_bin = relocated_app / "runtimes" / "gdal" / "bin"
    gdal_data = relocated_app / "runtimes" / "gdal" / "share" / "gdal"
    proj_data = relocated_app / "runtimes" / "gdal" / "share" / "proj"
    gdal_sp = relocated_app / "runtimes" / "gdal" / "Lib" / "site-packages"

    gdal_script = f"""
import os, sys
os.add_dll_directory(r'{gdal_bin}')
os.environ['PATH'] = r'{gdal_bin};' + os.environ.get('PATH', '')
os.environ['GDAL_DATA'] = r'{gdal_data}'
os.environ['PROJ_DATA'] = r'{proj_data}'
os.environ['PROJ_LIB'] = r'{proj_data}'
sys.path.insert(0, r'{gdal_sp}')

from osgeo import gdal, osr
if hasattr(osr, 'SetPROJSearchPaths'):
    osr.SetPROJSearchPaths([r'{proj_data}'])

# 验证版本
ver = gdal.__version__
assert ver.startswith('3.'), f'GDAL 版本异常: {{ver}}'

# 验证内嵌 proj.db 坐标转换
s_srs = osr.SpatialReference()
s_srs.ImportFromEPSG(4326)
s_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

t_srs = osr.SpatialReference()
t_srs.ImportFromEPSG(3857)
t_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

tx = osr.CoordinateTransformation(s_srs, t_srs)
x, y, _ = tx.TransformPoint(116.4074, 39.9042)
assert abs(x - 12958412.49) < 1.0, f'X 投影坐标不符: {{x}}'
assert abs(y - 4852030.63) < 1.0, f'Y 投影坐标不符: {{y}}'

print(f'GDAL_RELOCATION_VERIFIED_OK: GDAL={{ver}}, Beijing3857=({{x:.2f}}, {{y:.2f}})')
"""
    env_gdal = os.environ.copy()
    env_gdal["PATH"] = f"{gdal_bin};{env_gdal.get('PATH', '')}"
    env_gdal["GDAL_DATA"] = str(gdal_data)
    env_gdal["PROJ_DATA"] = str(proj_data)
    env_gdal["PROJ_LIB"] = str(proj_data)
    proc_gdal = subprocess.run([str(gdal_py), "-c", gdal_script], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env_gdal, timeout=20)
    assert proc_gdal.returncode == 0, f"GDAL 引擎在中文重定位目录执行失败: stdout={proc_gdal.stdout}, stderr={proc_gdal.stderr}"
    assert "GDAL_RELOCATION_VERIFIED_OK" in proc_gdal.stdout
    results["gdal_crs_verified"] = True
    print("  [PASS] 2. GDAL 引擎与内嵌 CRS proj.db 在中文/空格目录下投影转换精度精确达标")

    # 3. 验证 Rasterio 引擎运行时
    rio_py = relocated_app / "runtimes" / "rasterio" / "python.exe"
    rio_sp = relocated_app / "runtimes" / "rasterio" / "Lib" / "site-packages"
    rio_script = f"""
import sys
sys.path.insert(0, r'{rio_sp}')
import rasterio
import numpy as np

out_path = 'reloc_sample.tif'
with rasterio.open(
    out_path, 'w',
    driver='GTiff',
    height=4, width=4, count=1,
    dtype='float32',
    crs='EPSG:4326'
) as ds:
    ds.write(np.ones((1, 4, 4), dtype='float32'))

with rasterio.open(out_path) as ds:
    assert ds.count == 1
    assert ds.crs.to_string() == 'EPSG:4326'

print('RASTERIO_RELOCATION_VERIFIED_OK')
"""
    proc_rio = subprocess.run([str(rio_py), "-c", rio_script], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(verify_base_dir), timeout=20)
    assert proc_rio.returncode == 0, f"Rasterio 在中文重定位目录执行失败: stdout={proc_rio.stdout}, stderr={proc_rio.stderr}"
    assert "RASTERIO_RELOCATION_VERIFIED_OK" in proc_rio.stdout
    results["rasterio_verified"] = True
    print("  [PASS] 3. Rasterio 引擎在中文/空格目录下栅格读写与 CRS 识别完全正常")

    # 4. 验证原生启动器
    proc_launcher = subprocess.run([str(console_exe), "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    assert proc_launcher.returncode == 0, f"原生启动器在中文/空格目录下执行 --help 失败: {proc_launcher.stderr}"
    results["launcher_verified"] = True
    print("  [PASS] 4. 原生启动器 SpectraWorkbench-console.exe 在中文/空格目录下参数转发与调度正常")

    results["all_verified"] = True
    print("=== 重定位验证 100% 通过！满足 P10-01 验收标准 ===")
    return results


def install_package(source_dir: Path, target_dir: Path, mode: str = "link") -> dict[str, Any]:
    """将发布包安装至目标目录，生成安装元数据清单。"""
    source_dir = Path(source_dir).resolve()
    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    _copy_tree_efficient(source_dir, target_dir, mode=mode)

    installed_files = []
    for f in target_dir.rglob("*"):
        if f.is_file() and not f.name.endswith(".spectra.json"):
            installed_files.append(str(f.relative_to(target_dir)).replace("\\", "/"))

    install_manifest = {
        "schema_version": "1.0",
        "app_name": "Spectra Workbench",
        "version": "1.0.0",
        "installed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "installation_dir": str(target_dir),
        "files_count": len(installed_files),
        "files": installed_files,
    }
    (target_dir / "install_manifest.json").write_text(
        json.dumps(install_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return install_manifest


def upgrade_package(source_dir: Path, target_dir: Path, new_version: str = "1.0.1", mode: str = "link") -> dict[str, Any]:
    """升级已有安装目录：更新应用与运行时，严格保全所有用户项目文件与用户自定义数据。"""
    source_dir = Path(source_dir).resolve()
    target_dir = Path(target_dir).resolve()
    assert target_dir.is_dir(), f"升级目标目录不存在: {target_dir}"

    # 1. 扫描并保全现有用户工程与自定义数据
    user_projects = list(target_dir.rglob("*.spectra.json"))
    user_project_hashes = {}
    for up in user_projects:
        user_project_hashes[str(up.relative_to(target_dir))] = up.read_bytes()

    # 2. 覆盖安装新版本文件
    _copy_tree_efficient(source_dir, target_dir, mode=mode)

    # 3. 校验所有用户项目完整无损保留
    for rel_path, expected_bytes in user_project_hashes.items():
        current_file = target_dir / rel_path
        assert current_file.exists(), f"用户项目文件在升级中丢失: {rel_path}"
        assert current_file.read_bytes() == expected_bytes, f"用户项目文件内容在升级中发生改变: {rel_path}"

    # 4. 更新 install_manifest.json
    installed_files = []
    for f in target_dir.rglob("*"):
        if f.is_file() and not f.name.endswith(".spectra.json"):
            installed_files.append(str(f.relative_to(target_dir)).replace("\\", "/"))

    upgrade_manifest = {
        "schema_version": "1.0",
        "app_name": "Spectra Workbench",
        "version": new_version,
        "upgraded_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "installation_dir": str(target_dir),
        "preserved_user_projects": list(user_project_hashes.keys()),
        "files_count": len(installed_files),
        "files": installed_files,
    }
    (target_dir / "install_manifest.json").write_text(
        json.dumps(upgrade_manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return upgrade_manifest


def uninstall_package(target_dir: Path) -> dict[str, Any]:
    """卸载应用：彻底移除应用二进制与运行时，严格保全所有用户工程文件与数据。"""
    target_dir = Path(target_dir).resolve()
    assert target_dir.is_dir(), f"卸载目标目录不存在: {target_dir}"

    uninstaller_exe = target_dir / "uninstall.exe"
    if uninstaller_exe.exists():
        subprocess.run([str(uninstaller_exe)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)

    # 深度清理应用核心目录（使用 \\?\ 前缀突破 Windows MAX_PATH 限制）
    for sub in ("app", "runtimes", "resources", "environments"):
        sub_dir = _to_extended_path(target_dir / sub)
        if sub_dir.is_dir():
            shutil.rmtree(sub_dir, ignore_errors=True)
    for rf in ("SpectraWorkbench.exe", "SpectraWorkbench-console.exe", "SpectraWorkbench.bat", "run-app.cmd", "manifest.json", "install_manifest.json", "uninstall.exe", "uninstall.bat"):
        f = _to_extended_path(target_dir / rf)
        if f.is_file():
            f.unlink(missing_ok=True)

    remaining_files = list(target_dir.rglob("*"))
    remaining_regular_files = [f for f in remaining_files if f.is_file() and f.name not in ("uninstall.exe", "uninstall.bat")]
    user_projects = [f for f in remaining_regular_files if f.name.endswith(".spectra.json")]

    return {
        "uninstalled": True,
        "remaining_files": [str(f.relative_to(target_dir)) for f in remaining_regular_files],
        "preserved_user_projects": [str(p.relative_to(target_dir)) for p in user_projects],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Spectra Workbench Windows onedir 打包与重定位验证 (P10-01)")
    parser.add_argument("--dist-dir", default="dist/SpectraWorkbench", help="发布包输出目录")
    parser.add_argument("--mode", choices=("link", "copy"), default="link", help="构建镜像方式 (link 为高效硬链接，copy 为物理复制)")
    parser.add_argument("--verify", action="store_true", help="构建完成后自动执行重定位至中文/空格目录测试")
    args = parser.parse_args()

    root = find_project_root()
    target_dist = root / args.dist_dir

    t0 = time.perf_counter()
    build_onedir_package(target_dist, mode=args.mode)
    elapsed = time.perf_counter() - t0
    print(f">>> 打包总耗时: {elapsed:.2f}s")

    if args.verify:
        verify_dir = root / "work" / "dist_test" / "高光谱 空间 2026"
        verify_relocation(target_dist, verify_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
