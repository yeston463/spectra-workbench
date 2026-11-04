# Spectra Workbench

Windows x64 本地遥感与 GIS 桌面工作台。界面为 Python + PySide6；GDAL/OGR/OSR、Rasterio 与科学计算在独立工作进程中执行，GUI 不加载原生地理依赖。

开发方案、里程碑与任务账本在 `docs/plan/`（输入基线，保持只读）。当前真实进度看 `docs/progress/STATUS.md` 与 `docs/progress/BACKLOG.json`。

## 运行入口

```powershell
# 1) 解析并创建 ui / gdal / rasterio / science 四个独立环境
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1

# 2) 探测环境、引擎、CLI、数据目录与驱动
powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1

# 3) 启动开发版界面 / 运行检查
powershell -ExecutionPolicy Bypass -File scripts\run-dev.ps1
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

以上脚本与环境的实际可用性以 `docs/progress/STATUS.md` 记录的执行结果为准；未跑通的入口不会在此列出。

## 仓库结构

```text
src/spectra_workbench/   app domain protocol registry scheduler providers workers
                         data rendering processing project ui scripting plugins resources
environments/            ui.yml gdal.yml rasterio.yml science.yml locks/
capabilities/            baselines/ overlays/ recipes/
contracts/               能力、任务、消息协议 schema 与样例
tests/                   unit integration gui fixtures benchmarks
docs/                    plan（输入基线） adr progress user
scripts/                 bootstrap doctor run-dev test build audit-capabilities
packaging/               spectra.spec runtime-manifest.json installer/
```

## 工程约束

见 `AGENTS.md`。要点：引擎分进程、版本精确锁定、统一能力注册表与任务协议、原值/物理值/显示值分离、按需分块读取、真实取消与崩溃恢复、无占位实现。
