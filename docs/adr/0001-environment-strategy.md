# ADR-0001 · 环境策略：conda-forge 路线受阻，改用 pip venv + 本机 QGIS/OSGeo4W GDAL 运行时

状态：已采纳（开发期）。日期：2026-11-03。影响任务：P0-02、P0-03、P0-05、P10-01。

## 背景

`docs/plan/01_完整开发方案.md` 第 2 节与 3.1 节要求：conda-forge 解析原生依赖、项目独立环境、GUI 与引擎分进程、版本先解析再精确锁定。候选范围 Python 3.12 / PySide6 6.x / GDAL 3.11+ <4 / Rasterio 1.4+ <2。

## 实测依据（全部为本次实际执行结果）

1. 本机无 conda / mamba / micromamba。
2. 已在项目内落地 **micromamba 2.10.0 win-64**（从 conda-forge 取 `micromamba-2.10.0-0.tar.bz2`，4,696,444 字节，与 Content-Length 一致；解压得 `Library/bin/micromamba.exe` 11,723,264 字节，`--version` = 2.10.0），位置 `.tooling/bin/`，未写系统 PATH。
3. **PyPI 的 `gdal` 没有任何 Windows 轮子**：`pypi.org/pypi/gdal/json` 中各版本仅 1 个文件（sdist）；`pip download gdal==3.11.5` 实际取回 `gdal-3.11.5.tar.gz`。→ `osgeo` 无法用 pip 在本机装成可用二进制。
4. conda-forge 可达但**下行慢且解析失败**：`repodata.json`（win-64）全量 293,168,216 字节，curl 60 秒仅取回 46,045,952 字节（约 0.8 MB/s，需约 6 分钟）；`current_repodata.json` 47,945,151 字节 38 秒完成。micromamba 报 `Failed to fetch shard index`，回退 flat repodata 时 `Using Flat Repodata … Done (0.0 sec)`、`Parsing Packages' Records Done (0.5 sec)`，随后求解报 `python =3.12 * does not exist`、`numpy =* * does not exist`。而缓存文件 `.tooling/envs/pkgs/cache/1f08abba.json` 实测 293,189,115 字节且内容有效（首字节为 `{"info":{"subdir":"win-64"},"packages":{…`）→ 表已下到位，但 0.5 秒不可能解析 293 MB，判定为**解析阶段在内存压力下未真正加载**。
5. 内存实测：探测时 `GlobalMemoryStatusEx` 空闲 **0.37 GB**、负载 97%；占用前列为 `vmmemWSL` 1,995 MB、ChatGPT 999 MB、Qoder 781+475 MB 等**用户自身进程**。未终止任何用户进程。
6. 本机已存在完整 GDAL 引擎运行时（只读使用）：`C:\Program Files\QGIS 3.44.15`（OSGeo4W 派生）。
   - `apps\Python312\python.exe` = Python 3.12.14，`osgeo` 绑定为 `cp312-win_amd64.pyd`；
   - 子进程设 `PATH=<QGIS>\bin`、`GDAL_DATA=<QGIS>\share\gdal`、`PROJ_DATA=<QGIS>\share\proj` 后实测：`gdal.__version__ = 3.13.3`、驱动数 **205**、`GTiff/ENVI/GPKG/COG/VRT/HDF4/netCDF = True`，`HDF5 = False`，PROJ 主版本 9；
   - CLI 齐备：`gdal.exe`、`gdalinfo.exe`（`--version` 输出 `GDAL 3.13.3 "Iowa City", released 2026-08-13`）、`gdal_translate.exe`、`gdalwarp.exe`、`gdal_rasterize.exe`、`gdal_contour.exe`、`gdal_viewshed.exe`、`gdaladdo.exe`、`gdalbuildvrt.exe`、`gdal-config` 等。
   - 注：驱动名判定要用真实 ShortName，`shapefile` 的 ShortName 是 `ESRI Shapefile`，早期一次探测因键名写错而误报缺失。
7. pip 轮子可得性（同一 API 实测）：`rasterio` 1.5.1/1.5.2 有 cp312-win_amd64 轮子；`PySide6`、`pyqtgraph`、`numpy`、`scipy`、`shapely`、`pyproj`、`fiona` 均有轮子；`geopandas` 为纯 Python。

## 决策

1. **GDAL 引擎运行时**：开发期直接以只读方式使用本机 QGIS 3.44.15 运行时（Python 3.12.14 + GDAL 3.13.3 + PROJ 9 + 205 驱动 + 全套 CLI），由 `scripts/bootstrap_pip.ps1` 登记绝对路径、版本与驱动清单到 `docs/progress/runs/env_report.json`。子进程环境按运行时显式设置，不改系统 PATH。
2. **ui / rasterio / science**：用 `C:\python\python.exe`（3.12）创建项目内 venv（`.venvs/`），按 `environments/*-pip.txt` 安装，`pip freeze` 落 `environments/locks/*.lock.txt` 作为精确锁。
3. **进程隔离不变**：GUI venv 不含 GDAL/Rasterio/Fiona/GeoPandas，且必须实测 `import osgeo`、`import rasterio` 失败作为隔离证据（不是假设）。
4. **micromamba 保留**在 `.tooling/`，等空闲内存≥约 2.5 GB 时可重试 conda-forge 路线；重试成功后再评估是否把 GDAL 引擎切到自管环境。

## 后果与风险（明确不隐藏）

- **违反 P10 的独立性要求，属已知缺口**：本机 QGIS 运行时只可作为**开发期**引擎。P0-05（打包重定位提前验证）与 P10-01/02（onedir + engine runtime、干净机离线）**必须自带可重定位的 GDAL 运行时**，不得依赖 `C:\Program Files\QGIS 3.44.15`。在该项完成前，P0-02 只能标 `implemented`，不能标 `done`。
- 版本偏离候选范围：GDAL 3.13.3（候选为 3.11+ <4，仍合规）；Rasterio 锁 1.5.2（候选 1.4+ <2，合规）。
- 该运行时支持 netCDF 但缺 HDF5 驱动（驱动短名为 `netCDF`，实测支持）→ P8-01 涉及 HDF5 部分在此运行时下受限，须登记 `missing_dependency`，不得从基准分母移除。
- 若用户关闭 QGIS 相关安装或升级它，引擎版本会变；`env_report.json` 是版本快照，doctor 必须每次实测而非引用本文数字。

## 备选（未采纳原因）

- 纯 pip 装 `gdal`：无 Windows 轮子，需自建工具链，且仍不提供 CLI。
- OSGeo4W 独立安装器：`C:\osgeo4w` 实测只有 `etc/`、`var/`，无 `bin/`，不是可用安装；且下载体积大、当前网络与内存条件下风险高。
- 等内存空闲后走 conda：保留为后续动作，但会阻塞 P1，故不作为当前路径。
