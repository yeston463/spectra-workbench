# Spectra Workbench 故障定位与系统诊断指南

本文档面向系统管理员、测试工程师与高级用户，详细介绍 Spectra Workbench 的自检诊断工具 (`doctor.py`) 的工作机制，以及针对常见运行期故障（PROJ 空间参考、DLL 加载、长路径限制、子进程超时、工程损坏、离线断网）的标准排查与解决手册。

---

## 1. 系统级 Doctor 自检体系

Spectra Workbench 内置全方位环境与引擎诊断模块 (`src/spectra_workbench/app/doctor.py`)，负责在启动前或排查期评估系统的整体就绪性。

### 运行自检诊断

| 运行环境 | 执行命令 | 输出形式 |
|---|---|---|
| **原生发布包 (CLI)** | `.\SpectraWorkbench-console.exe --doctor` | 彩色终端交互式体检报告 |
| **原生发布包 (JSON)** | `.\SpectraWorkbench-console.exe --doctor --json` | 机器可读的结构化 JSON（供自动化脚本消费） |
| **源码开发环境** | `powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1` | 开发态多环境联合诊断报告 |

### 核心评估字段含义

在输出的 JSON 诊断报告中，关注以下核心指标：

- `overall_ok: bool`：全局综合健康状态。当 UI 与底层核心地理引擎（GDAL/Rasterio）均可用时为 `true`；
- `gui_can_start: bool`：GUI 启动许可。当 PySide6、Qt 平台插件（windows / offscreen）与基础依赖满足时为 `true`；
- `engines: dict`：各独立引擎的详细体检状态：
  - `ui`：Python 解释器路径、PySide6 6.9.3 版本、pyqtgraph 与平台插件状态；
  - `gdal`：独立 GDAL 3.13.3 解释器路径、已注册驱动数（通常 180+）、CLI 工具数（如 gdalinfo, gdalwarp）、PROJ 空间数据库路径；
  - `rasterio`：Rasterio 1.5.2 解释器、`rio.exe` 路径与版本；
  - `science`：NumPy 2.5.4 与 SciPy 1.18.1 原生数学库状态；
- `issues: list[str]`：当前系统检测到的所有警报与问题清单。

---

## 2. 典型故障排查手册与处理方案

### 故障 1：PROJ 空间参考数据库丢失或 CRS 转换异常

- **故障现象**：
  执行投影转换或读取图层时终端报错：
  `ERROR 1: PROJ: proj_create_from_database: cannot find proj.db`
  或者坐标转换结果为全 0、NaN 或经纬度无法计算。
- **根本原因**：
  GDAL 依赖底层的 PROJ 空间参考库。当环境变量 `PROJ_DATA` 或 `PROJ_LIB` 缺失，或指向了不存在的目录时，PROJ 无法检索大地水准面与坐标系转换模型。
- **解决方案**：
  1. Spectra Workbench onedir 发布包在 `runtimes/gdal/share/proj/` 下完整捆绑了正版 `proj.db`；
  2. 启动脚本与 `runtimes/gdal/Lib/sitecustomize.py` 会自动将该目录注入 `PROJ_DATA` 环境变量，并在 Python 中调用 `osgeo.osr.SetPROJSearchPaths([proj_dir])`；
  3. 若在独立外部调用时遇到此问题，请在终端显式配置：
     ```powershell
     $env:PROJ_DATA = "C:\Path\To\SpectraWorkbench\runtimes\gdal\share\proj"
     ```

---

### 故障 2：Windows 原生 DLL 动态链接加载失败 (`WinError 126`)

- **故障现象**：
  启动时 Python 抛出异常：
  `ImportError: DLL load failed while importing _gdal: 找不到指定的模块` 或 `WinError 126`。
- **根本原因**：
  1. 目标计算机操作系统缺失 Microsoft Visual C++ 2015-2022 Redistributable (x64) 基础运行时库（如 `vcruntime140.dll`、`msvcp140.dll`）；
  2. Python 3.8+ 在 Windows 上改变了 DLL 搜索逻辑，不再自动从系统 PATH 搜索动态库，必须显式通过 `os.add_dll_directory` 声明。
- **解决方案**：
  1. 安装微软官方 Visual C++ 2015-2022 x64 运行库；
  2. Spectra Workbench 自带了 `runtimes/gdal/bin/` 目录下的 162 个原生 C/C++ DLLs，发布包已在 `sitecustomize.py` 中原生自注册 `os.add_dll_directory(bin_path)`，请确保发布包完整解压，不要拆散目录结构。

---

### 故障 3：长路径限制导致文件读写或解压失败 (`WinError 206`)

- **故障现象**：
  在较深层级的目录中解压、安装或运行程序时，系统抛出：
  `FileNotFoundError: [WinError 206] 文件名或扩展名太长`。
- **根本原因**：
  Windows API 早期遗留的 `MAX_PATH = 260` 字符硬限制。PySide6 等库自身存在长达 10 级的子目录（如 `PySide6/qml/Qt/labs/assetdownloader/...`），在深层安装路径下会超出 260 字符。
- **解决方案**：
  1. **软件内建支持**：Spectra Workbench 在核心文件操作与安装打包脚本中全面引入了 Windows 扩展路径前缀 (`\\?\`)，自动解除 260 字符限制，支持最长 32,767 字符路径；
  2. **系统级开启长路径支持**（推荐管理员执行）：
     ```powershell
     New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name "LongPathsEnabled" -Value 1 -PropertyType DWORD -Force
     ```

---

### 故障 4：Worker 进程假死、挂起或卡死超长未响应

- **故障现象**：
  某项复杂的网格插值或重投影计算长期处于执行状态，主界面点击「取消」未能即时终止，或自动化测试套件卡死在后台。
- **根本原因**：
  C 语言底层在执行密集循环或阻塞性系统调用时，Python 的 GIL 与常规线程中断信号无法切入。
- **解决方案**：
  1. Spectra Workbench 实现了协作式双向心跳与硬超时守护机制（`scripts/run_with_timeout.py`）；
  2. 主界面通过底层的 `QProcess.kill()` 或调度器的 `WorkerClient.cancel()` 发送强制终止信号；
  3. 守护进程在超时触发后，利用 Windows 原生 `taskkill.exe /F /T` 级联强杀整棵子进程树，并自动写入 `<log>.timeout.json` 诊断现场，确保系统零残留孤儿进程。

---

### 故障 5：工程文件意外断电损坏或路径歧义

- **故障现象**：
  系统遭遇意外停电或强退后，再次启动打开工程，提示文件格式损坏；或数据源被移动后无法加载图层。
- **解决方案**：
  1. **原子保存防损 (`atomic save`)**：Spectra Workbench 在保存工程时采用两阶段写入（先写同卷临时文件，刷盘校验无误后再原子替换），原工程在强退下 100% 保持最后一次有效保存的状态，绝不生成 0 字节废损文件；
  2. **缺失源与歧义确认**：打开移动后的工程时，如果数据源未找到，系统会弹出「数据源路径迁移与重关联」对话框。用户可以明确指定新文件路径，也可以选择「保留引用并在界面中标记缺失」，绝不在用户未知晓的情况下擅自加载同名文件。

---

### 故障 6：完全离线环境与无外网内网部署

- **故障现象**：
  在保密内网或无外网接入的物理机器上运行软件，担心联网下载依赖或报错。
- **解决方案**：
  1. Spectra Workbench 遵循纯自包含 (Self-contained Onedir) 交付规范；
  2. 发布包无需访问 PyPI、Conda 或任何外网服务器；
  3. 内置包含了所有必要的静态元数据（161 个 GDAL 规范文件、CRS 字典、Proj.db 空间数据库）；
  4. 系统已通过完全洗脱网络代理的离线端到端专项验证，断网状态下所有计算与界面交互功能 100% 正常运行。
