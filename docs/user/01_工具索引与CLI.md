# Spectra Workbench 工具索引与命令行使用指南

Spectra Workbench 提供双重作业形态：
1. **图形桌面交互界面 (Desktop GUI)**：基于 PySide6 构建的多文档/多图层交互式遥感与 GIS 工作台，支持图层渲染、动态分块读取、ROI 交互绘制及可搜索动态算法工具箱；
2. **命令行与无头工作台 (CLI & Headless Engine)**：原生 PE 启动器支持全流程静默自检、任务分发、无头批处理与环境诊断。

---

## 1. 原生 PE 启动器与命令行选项

在发布包根目录下，Spectra Workbench 提供了经原生 Windows 编译的 64 位可执行文件：

| 启动程序 | 类型 | 适用场景 | 说明 |
|---|---|---|---|
| `SpectraWorkbench.exe` | Windows GUI (Win32 子系统) | 桌面端交互工作 | 启动时不产生控制台黑框窗口，适合日常双击打开 |
| `SpectraWorkbench-console.exe` | Console CLI (Console 子系统) | 自动化脚本、诊断与调试 | 完整输出控制台日志流与 JSON 结果，支持管道与退出码传递 |
| `SpectraWorkbench.bat` | 批处理包装器 | 紧急回退与环境变量调试 | 纯文本批处理脚本，直接调度内嵌运行环境 |

### 命令行参数详解

通过 `SpectraWorkbench-console.exe [OPTIONS]` 可调度以下系统参数：

```text
用法: SpectraWorkbench-console.exe [OPTIONS]

选项:
  --help               显示帮助信息并退出。
  --doctor             执行全系统运行环境与引擎就绪性诊断（UI、GDAL、Rasterio、Science）。
  --json               与 --doctor 配合使用，将诊断报告以机器可读的 JSON 格式输出至 stdout。
  --headless           启用无头模式（使用 offscreen 虚拟画布，不创建宿主桌面物理窗口）。
  --test-init          初始化应用程序框架、配置中心与主窗口后立即安全退出（返回码 0），用于健康自检。
  --sample             启动主程序时自动创建并载入内置合成多波段高光谱样例数据。
  --open PATH          启动主程序后自动打开指定文件（支持 GeoTIFF、ENVI、NetCDF、矢量 GPKG、.spectra.json 工程）。
  --screenshot PATH    在启动并加载数据后，自动捕获主画布高分辨率离屏渲染快照并写入指定 PNG 路径后退出。
```

#### 常见命令行范例

1. **环境与引擎自检（文本报告）**：
   ```powershell
   .\SpectraWorkbench-console.exe --doctor
   ```
2. **自动化流水线环境自检（JSON 报告）**：
   ```powershell
   .\SpectraWorkbench-console.exe --doctor --json
   ```
3. **静默烟雾测试与健康探测**：
   ```powershell
   .\SpectraWorkbench-console.exe --headless --test-init
   ```
4. **加载本地工程并捕获渲染结果**：
   ```powershell
   .\SpectraWorkbench-console.exe --open .\user_projects\demo.spectra.json --screenshot .\preview.png
   ```

---

## 2. 桌面端工具箱 (Toolbox) 与动态参数表单

在桌面 GUI 中，按下快捷键或通过菜单打开「能力工具箱」停靠面板。系统内置 468+ 项能力契约条目，支持即时搜索与动态表单交互：

### 工具箱交互机制
1. **即时关键字检索**：输入能力名称（如 `warp`、`ndvi`、`dem`、`pca`、`vector`）即可毫秒级过滤并呈现对应算法。
2. **能力可用性状态标识**：
   - 🟢 **就绪可执行 (`callable`)**：当前环境与独立引擎完全就绪，表单允许提交计算。
   - 🟡 **部分受限 (`platform_executable` / `restricted`)**：算法支持运行，但对波段维度或数据类型有严格约束。
   - 🔴 **外部缺失/阻断 (`blocked` / `missing`)**：依赖商业授权（如 FLAASH）或未接入外部模型，系统严格禁用提交并明确显示原因，杜绝以简化公式冒充物理算法。
3. **动态表单与安全约束**：
   - **参数类型自动识别**：根据 JSON Schema 自动生成路径选择器、波段下拉框、复选框、浮点微调框与枚举单选。
   - **覆盖保护机制 (`overwrite`)**：缺省状态下严禁静默覆盖已有文件；必须显式勾选「覆盖已有文件」方可提交相同目标路径。
   - **临时工作空间暂存 (`staging`)**：计算过程先写入临时目录，成功后经校验执行原子替换提交，杜绝写出一半的半截损坏产物。

---

## 3. 核心能力索引编目 (Capability Catalog)

Spectra Workbench 内置接入并全覆盖验证的核心能力清单如下：

### A. 栅格格式转换、坐标变换与基础管理

| 能力 ID | 推荐引擎 | 适用功能 | 关键参数与输出 |
|---|---|---|---|
| `gdal:translate` | `gdal` | 格式转换、重缩放、切片、NoData 修正 | `format`, `outsize`, `projwin`, `scaleParams`, `noData` |
| `gdal:warp` | `gdal` | 严密地图投影转换、空间重采样 | `dstSRS`, `resampleAlg`, `dstNodata`, `errorThreshold` |
| `gdal:overview` | `gdal` | 多级栅格金字塔概览构建 | `levels`, `resampling`, `clean` |
| `gdal:statistics` | `gdal` | 栅格逐波段极值、均值与标准差统计 | `approx_ok`, `compute_mode`（输出 `.stats.json`） |
| `gdal:stack` | `gdal` | 多个单波段栅格垂直堆叠为多波段影像 | `inputs`, `format` |
| `gdal:bandmanage` | `gdal` | 多波段影像提取特定波段或重排 | `bands`, `format` |
| `rasterio:convert` | `rasterio` | 基于 Rasterio 引擎的高性能分块格式转换 | `format`, `nodata`, `dtype` |
| `rio:cli:warp` | `rasterio` | 基于 Rasterio 独立引擎的重投影计算 | `dst_crs`, `resampling` |

### B. 地形分析与空间离散分析

| 能力 ID | 推荐引擎 | 适用功能 | 关键参数与输出 |
|---|---|---|---|
| `gdal:dem` | `gdal` | DEM 地形分析（坡度、坡向、山体阴影、粗糙度、TPI、TRI） | `mode: hillshade/slope/aspect/color-relief/TRI/TPI/roughness`, `zFactor`, `scale` |
| `gdal:contour` | `gdal` | 规则高程表面等值线与等值面多边形生成 | `interval`, `offset`, `fixedLevels`, `amin`, `amax`（支持 Shapefile / GPKG 输出） |
| `gdal:grid` | `gdal` | 离散散点矢量数据插值生成连续栅格表面 | `algorithm: invdist/average/nearest/linear`, `radius1`, `radius2`, `nodata` |
| `gdal:rasterize` | `gdal` | 矢量要素烧录栅格化 | `burn_values`, `attribute`, `all_touched`, `nodata` |
| `gdal:polygonize` | `gdal` | 栅格分区块提取多边形矢量化 | `mask: bool`, `connectedness: 4/8` |

### C. 矢量空间分析与 SQL 检索

| 能力 ID | 推荐引擎 | 适用功能 | 关键参数与输出 |
|---|---|---|---|
| `ogr:vectortranslate` | `gdal` | 矢量格式转换、空间重投影、WHERE 与 OGR SQL 属性筛选、BBOX 裁剪 | `format: GPKG/GeoJSON/ESRI Shapefile/FlatGeobuf`, `dstSRS`, `where`, `sql`, `spat` |

### D. 多维科学数据集

| 能力 ID | 推荐引擎 | 适用功能 | 关键参数与输出 |
|---|---|---|---|
| `gdal:mdim_slice` | `gdal` | NetCDF / HDF / CF 多维数据沿特定时间/深度维度提取二维物理切片 | `variable`, `slices: {dim: index}`, 自动生成 `.slice.json` 溯源 sidecar |

### E. 遥感科学专题分析

| 能力 ID | 推荐引擎 | 适用功能 | 算法原理与契约 |
|---|---|---|---|
| `gdal:sindex` | `science/gdal` | 归一化植被指数 (NDVI)、水体指数 (NDWI)、燃烧指数 (NBR)、土壤调节指数 (SAVI) | 采用物理量换算公式，支持 `usePhysical: true/false`；NaN/Inf/NoData 严格合并至输出 NoData |
| `gdal:fftfilter` | `science` | 二维快速傅里叶变换 (2D FFT) 频域滤波与周期性条纹抑制 | 厄米对称频域抑制掩膜，生成单波段 Float32 滤波成果与 `.fft_mask.json` 溯源元数据 |
| `gdal:pca` | `science` | 多波段主成分分析 (PCA) 降维与正反变换 | 输出前 N 主成分栅格及 `.pca.json`（含均值、特征向量、解释方差），支持严格可逆无损重建 |
| `gdal:tasseledcap` | `science` | 缨帽变换 (Tasseled Cap) 物理反演 | 支持 Landsat 5 TM、Landsat 7 ETM+、Landsat 8 OLI 权威正交系数，提取亮度、绿度、湿度分量 |
| `gdal:gcpregister` | `gdal` | 地面控制点 (GCP) 仿射与多项式严密几何纠正 | 支持控制点与独立检查点分离评估，自动输出残差与拟合矩阵 |
| `gdal:rasterdiff` | `science` | 严格同网格像元差值对比与变化检测 | `mode: difference / absolute`，输出差值图与 `.diff_stats.json` 统计文件 |
| `gdal:pansharpen` | `science` | 高空间分辨率全色与多光谱影像融合 | 支持 Brovey 与 HSV 严格波段物理融合，生成 3 波段高分辨率栅格及方法 sidecar |
| `gdal:mask` | `science` | 阈值二值掩膜生成与范围翻转 | 严格闭区间 DN 筛选，输出 Byte (0/1) 掩膜，生成包含有效像元统计的 `.mask.json` |
| `gdal:fvc` | `science` | 基于像元二分模型的植被覆盖度 (FVC) 反演与分级 | 基于 NDVI 端元反演 FVC 连续像元值，同时生成分级类别图 `.fvc_class.tif` 与分析报告 |
| `gdal:calibrate` | `gdal` | 传感器绝对辐射定标与大气顶表观反射率 (TOA) 计算 | 支持 DN×Gain+Offset 逐波段定标，以及太阳天顶角、日地距离修正的 TOA 反射率物理计算 |
| `gdal:dos1` | `science` | Chavez 1988 暗目标减法 (DOS1) 基础大气校正 | 消除大气路径辐射散射，sidecar 独立核算各波段散射扣除量 |
| `gdal:cloudreplace` | `science` | 基于有效掩膜的多时相云区影像替换填充 | 支持 8 邻域边缘羽化膨胀与多时相插补，sidecar 明确记录各像元时相来源 |

---

## 4. 内置独立 CLI 工具使用参考

发布包中完整附带了独立的 GDAL 与 Rasterio CLI 二进制工具，开发者或自动化流程可直接调用：

### GDAL 原生二进制工具 (`runtimes/gdal/bin/`)
- `gdalinfo.exe`：探测栅格元数据、空间参考、仿射变换、波段角点坐标与像元统计。
- `gdalwarp.exe`：高性能高保真影像重投影与重采样。
- `gdal_translate.exe`：格式转换、驱动重采样、压缩与裁剪。
- `ogrinfo.exe`：矢量数据源结构探测、图层摘要与属性表检索。
- `ogr2ogr.exe`：矢量数据转换、空间裁剪、重投影与 SQL 过滤。
- `gdal_contour.exe`：矢量等值线生成工具。
- `gdal_rasterize.exe`：矢量栅格化工具。
- `gdal_grid.exe`：散点空间插值工具。

### Rasterio 命令行工具 (`runtimes/rasterio/Scripts/rio.exe`)
- `rio info <raster.tif>`：以 JSON 格式输出栅格边界、波段、dtype 与坐标系。
- `rio bounds <raster.tif>`：输出外边界地理坐标。
- `rio warp <input.tif> <output.tif> --dst-crs EPSG:3857`：栅格重投影。
