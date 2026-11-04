# Spectra Workbench

Windows x64 本地遥感与 GIS 桌面工作台。界面为 Python + PySide6；GDAL/OGR/OSR、Rasterio 与科学计算在独立工作进程中执行，GUI 不加载原生地理依赖。

开发方案、里程碑与任务账本在 `docs/plan/`（输入基线，保持只读）。当前真实进度看 `docs/progress/STATUS.md` 与 `docs/progress/BACKLOG.json`。

## 运行入口

当前路线（依据 `docs/adr/0001-environment-strategy.md`）：三个 pip venv（`.venvs/sw-ui`、`sw-rasterio`、`sw-science`）+ 本机只读 GDAL 运行时（QGIS 3.44.15 / GDAL 3.13.3）。conda 路线（`scripts/bootstrap.ps1` + micromamba）保留但未被采用。

```powershell
# 1) 创建 ui / rasterio / science 三套 venv，并登记本机 GDAL 运行时
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_pip.ps1

# 2) 探测环境、引擎、CLI、数据目录与驱动（GUI 启动许可）
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1

# 3) 启动桌面界面（可选 -Sample 载入合成样例、-Open <文件>、-Screenshot <png>、-Headless）
powershell -ExecutionPolicy Bypass -File scripts\run-gui.ps1

# 4) 运行检查（-All 含集成测试）
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

`bootstrap_pip.ps1 -VerifyOnly` 只做校验与出报告，不写环境；`run-gui.ps1` 在找不到 `.venvs/sw-ui` 时回退到 `.venv`（开发用解释器）。以上入口的实际可用性以 `docs/progress/STATUS.md` 记录的执行结果为准；未跑通的入口不会在此列出。

## 仓库结构

```text
src/spectra_workbench/   app domain protocol registry scheduler providers workers
                         data rendering processing project ui scripting plugins resources
environments/            ui.yml gdal.yml rasterio.yml science.yml locks/
capabilities/            baselines/ overlays/ recipes/
contracts/               能力、任务、消息协议 schema 与样例
tests/                   unit integration gui fixtures benchmarks
docs/                    plan（输入基线） adr progress user
scripts/                 bootstrap bootstrap_pip doctor run-gui test audit-capabilities
packaging/               spectra.spec runtime-manifest.json installer/
```

## 工程约束

见 `AGENTS.md`。要点：引擎分进程、版本精确锁定、统一能力注册表与任务协议、原值/物理值/显示值分离、按需分块读取、真实取消与崩溃恢复、无占位实现。
