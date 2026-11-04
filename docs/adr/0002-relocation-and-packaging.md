# ADR-0002 · Windows Runtime 重定位与打包可行性验证 (P0-05)

状态：已采纳。日期：2026-11-03。影响任务：P0-05、P1-03、P10-01、P10-02。

## 背景

`docs/plan/02_里程碑与验收.md` 要求 P0-05 提前验证 Windows runtime 打包可行性：“最小 Qt 窗口和两个引擎在另一目录启动；发现重定位或 DLL 问题写 ADR 并解决”。
由于 Python C 扩展、Qt 插件以及 GDAL/PROJ 原生库在 Windows 上具有敏感的路径依赖（硬编码 DLL 搜索路径、`proj.db` 资源路径、Qt platform plugin 等），必须在深入业务开发前实测其脱离固定项目路径在任意目录启动的可行性。

## 实测验证记录

在完全独立于项目的操作系统临时目录（`AppData/Local/Temp/...`）实测启动并记录以下结果（见 `tests/integration/test_relocation.py`）：

1. **UI 引擎（PySide6 6.9.3）**：
   - 使用 offscreen 平台在临时目录启动，正常初始化 `QApplication` 和 `QLabel`；
   - **关键依赖**：PySide6 依赖 `plugins/platforms/qwindows.dll`。若在独立 onedir 打包时，Qt 必须保留该目录或显式注入 `QT_QPA_PLATFORM_PLUGIN_PATH`。

2. **Rasterio 引擎（Rasterio 1.5.2）**：
   - 在独立临时目录执行，成功写入并读回 float32 GeoTIFF（EPSG:4326）；
   - **机制**：PyPI Windows 轮子将 GDAL/PROJ DLL 及其资源文件内置于 `site-packages/rasterio.libs/`、`rasterio/gdal_data/`、`rasterio/proj_data/`，在导入时由 `_env.pyx` 自动配置 `GDAL_DATA` 和 `PROJ_DATA`，具备极佳的自包含重定位特性。

3. **GDAL 外部运行时（QGIS 3.44.15 / GDAL 3.13.3）**：
   - 在独立临时目录执行，成功写入 float32 GeoTIFF，并成功执行 EPSG:4326 到 EPSG:3857 坐标投影转换（北京坐标转换为 12957588.73, 4851421.18）；
   - **必要条件**：
     - `PATH` 必须包含 GDAL 可执行文件与 DLL 所在 `bin` 目录；
     - `GDAL_DATA` 必须指向有效的坐标系字典目录（含 `pcs.csv`、`gcs.csv`）；
     - `PROJ_DATA` 必须指向包含 `proj.db` 的目录；若缺失该环境变量，`osr.CoordinateTransformation` 将因找不到数据库而报错失败。

## 决策

1. **分阶段架构策略**：
   - **开发与 Smoke 阶段（P1–P9）**：通过 `resolve_gdal_runtime` 集中解析开发环境登记，子进程跨进程调用时显式注入 `PATH`、`GDAL_DATA`、`PROJ_DATA` 环境变量，确保开发期跨进程引擎可可靠启动。
   - **正式分发与发布阶段（P10）**：采用 UI `onedir` + 独立内嵌 Engine 运行时目录。发布包内自带精简自包含 GDAL 运行时树，启动器基于应用相对路径自动推导并设置环境变量，确保在无任何外部 GIS 软件的干净 Windows 环境下开箱即用。

2. **P0-05 验收判定**：
   - 最小 Qt 窗口、Rasterio 引擎、GDAL 引擎在独立目录启动验证全部通过，自动化测试纳入流水线（`tests/integration/test_relocation.py`）。P0-05 标记为 `implemented`。
