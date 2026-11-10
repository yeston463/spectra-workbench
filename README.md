# Spectra Workbench

Windows x64 本地遥感与 GIS 桌面工作台。界面为 Python + PySide6；GDAL/OGR/OSR、Rasterio 与科学计算在独立工作进程中执行，GUI 不加载原生地理依赖。

开发方案、里程碑与任务账本在 `docs/plan/`（输入基线，保持只读）。当前真实进度看 `docs/progress/STATUS.md` 与 `docs/progress/BACKLOG.json`。

---

## 快速导航与文档索引

系统提供完备的用户手册、开发者指南与架构诊断文档：

### 用户使用文档 (`docs/user/`)
- [**工具索引与命令行指南**](docs/user/01_工具索引与CLI.md)：原生 PE 启动器（`SpectraWorkbench-console.exe`）、完整 39+ 核心能力分类编目、参数表单交互与内置 GDAL/Rasterio CLI 索引。
- [**项目与工作流示例**](docs/user/02_项目与工作流示例.md)：工程文件（`.spectra.json`）规范、跨引擎强类型 DAG 工作流示例、批处理模板及隔离脚本会话用法。

### 开发者与运维文档 (`docs/developer/`)
- [**插件协议与自定义引擎扩展指南**](docs/developer/01_插件协议与自定义引擎.md)：进程物理隔离红线、JSONL 进程间通信协议、能力契约 Schema 及编写自定义算法插件完整范例。
- [**故障定位与 Doctor 诊断指南**](docs/developer/02_故障定位与Doctor诊断.md)：系统 Doctor 自检原理、PROJ 数据库缺失、Windows DLL 加载、长路径 MAX_PATH 突破、子进程超时及断网离线环境排查手册。
- [**依赖与组件版本清单**](docs/developer/03_依赖与组件清单.md)：上游精确版本对照表（GDAL 3.13.3, Rasterio 1.5.2, PySide6 6.9.3, NumPy 2.5.4, SciPy 1.18.1）、发布包架构矩阵、205 个驱动支持清单与开源合规许可证。

---

## 运行入口

### 1. 独立发布包开箱即用 (Onedir Distribution)

若使用组装好的自包含发布包（`dist/SpectraWorkbench/`），无需安装全局 Python、Conda 或 QGIS，离线即可直接运行：

```powershell
# 1) 启动图形界面 (无控制台黑框)
.\dist\SpectraWorkbench\SpectraWorkbench.exe

# 2) 命令行自检与运行环境体检
.\dist\SpectraWorkbench\SpectraWorkbench-console.exe --doctor

# 3) 静默健康自检测试
.\dist\SpectraWorkbench\SpectraWorkbench-console.exe --headless --test-init
```

### 2. 源码开发环境运行

```powershell
# 1) 创建 ui / rasterio / science 三套 venv，并登记本机 GDAL 运行时
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_pip.ps1

# 2) 探测环境、引擎、CLI、数据目录与驱动（GUI 启动许可）
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1

# 3) 启动桌面界面（可选 -Sample 载入合成样例、-Open <文件>、-Screenshot <png>、-Headless）
powershell -ExecutionPolicy Bypass -File scripts\run-gui.ps1

# 4) 运行检查流水线（-All 含集成测试）
powershell -ExecutionPolicy Bypass -File scripts\test.ps1

# 5) 组装独立发布包
python scripts\package_onedir.py
```

`bootstrap_pip.ps1 -VerifyOnly` 只做校验与出报告，不写环境；`run-gui.ps1` 在找不到 `.venvs/sw-ui` 时回退到 `.venv`（开发用解释器）。以上入口的实际可用性以 `docs/progress/STATUS.md` 记录的执行结果为准；未跑通的入口不会在此列出。

---

## 仓库结构

```text
src/spectra_workbench/   app domain protocol registry scheduler providers workers
                         data rendering processing project ui scripting plugins resources
environments/            ui.yml gdal.yml rasterio.yml science.yml locks/
capabilities/            baselines/ overlays/ recipes/
contracts/               能力、任务、消息协议 schema 与样例
tests/                   unit integration gui fixtures benchmarks
docs/                    plan（输入基线） adr progress user developer
scripts/                 bootstrap bootstrap_pip doctor run-gui test audit-capabilities package_onedir
packaging/               spectra.spec runtime-manifest.json installer/
```

---

## 工程约束

见 `AGENTS.md`。要点：引擎分进程、版本精确锁定、统一能力注册表与任务协议、原值/物理值/显示值分离、按需分块读取、真实取消与崩溃恢复、无占位实现。
