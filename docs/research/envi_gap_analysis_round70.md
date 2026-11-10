# Spectra Workbench ↔ ENVI 差距研究（第 70 轮 · 2026-11-10）

> 性质：研究纪要。只读对照，不改账本、不改代码。ENVI 侧口径 = 课程资料
> `C:\Users\赵航\Desktop\studio\_chaoxing_rs`（**只读**）中的
> 《ENVI操作与问题清单》（402 张步骤幻灯 OCR，实习 1-12，ENVI 5.6.3）；
> Workbench 侧口径 = `docs/progress/BACKLOG.json`（done 40 / implemented 2 /
> todo 13）+ `docs/progress/STATUS.md`（轮次已验收）+ 代码实测
> （`_CAPABILITY_SUPPORT` 参数契约、registry 对账、运行时探针）。
> 标注：〔B:任务号〕= 账本归属；〔轮N〕= 实际完成的轮次；✅=已实现且验收；
> ◐=部分实现/待验证；❌=缺失；⛔=刻意边界（有理由，不视为待办）。

---

## 0. 方法与事实口径

1. **ENVI 侧**：课程实习覆盖的功能（下文「ENVI 课上用到的」逐条来自课件 OCR
   摘要，注明实习编号）；课件还自带「课堂最想补的功能」与「常见 bug」两节，
   直接映射为 Workbench 的"发明点"检验清单。
2. **Workbench 侧**：只引用有验收证据的状态——done 40 项（P0-P7 全部 +
   P8-01）；implemented 2 项（P0-02 环境锁、P6-06，两项各留 blocked 子项）；
   运行期四维审计：基线 443 → 发现 455 / 可调用 250 / 平台可执行 24 /
   **缺失 7**（清单见 §4）/ 已验证 5。
3. 参数契约逐能力实测（`_CAPABILITY_SUPPORT`）：statistics 无 mask 入口、
   stack 无波段描述继承、ogr:vectortranslate 无 buffer、translate 只有
   srcWin/projWin/outputBounds（矩形）。以此定性的缺口不是猜测。

---

## 1. ENVI 课堂功能的逐条对照（实习真实用过）

### A. 查看与交互

| ENVI 课上操作 | Workbench 现状 |
|---|---|
| 数据管理加载/真彩·假彩/显示全部波段与波长〔实习1〕 | ✅ 数据面板 + 下拉波段组合〔轮22〕 |
| View 排列 + Link 同步（平移缩放联动） | ✅ 双视图联动同步〔P3-04〕 |
| **Swipe 卷帘对比**〔实习3/10〕 | ❌ 无卷帘/闪烁/混合控件（只有双窗并排） → 建议排期 |
| **Band 浏览器逐波段自动播放**〔实习1/10〕 | ❌ 无波段浏览/动画控件 → 建议排期 |
| **十字丝 + Cursor Value 读数**〔实习1/27〕 | ✅ 坐标/亮度查询（query_pixel，P3） |
| **测量工具量地物直径/距离**〔实习1/27〕 | ◐ 矩形 ROI 面积/425 平方单位实测〔P3-05〕；无折线测距交互 |
| 拉伸提亮 | ✅ 灰度/RGB/6 种伪彩 + min-max/分位〔P3-04〕 |
| 高德地图底图对照 | ❌ 远程底图属 P8-02〔B:P8-02〕未做 |

### B. 元数据与头文件

| ENVI 课上操作/痛点 | Workbench 现状 |
|---|---|
| 手工用记事本改 `.hdr`（回填波段名/波长）〔实习6[011-013]〕 | ✅ `gdal:editmeta`（in-place 元数据编辑）＋描述元数据随 cultivate 输出〔P4-03〕 |
| 运算后波段名/波长/FWHM/scale/offset 自动继承（课堂最想补 No.1） | ◐ 原值/scale-offset/NoData 分离是全链不变量〔AGENTS #4〕；但 `gdal:stack` 无波段描述/波长透传参数（实测），ENVI 式「波长继承」未完全实现 → 建议排期小项 |
| FLAASH 反射率 ×10000 存储要手工除回来（课堂最想补 No.2） | ✅ 设计层解决：原值 vs 物理值严格分离，显示侧应用 scale/offset〔P3-04〕；sidecar 记录换算口径（calibrate/dos1/cloudreplace）〔P6-06〕 |

### C. 辐射处理

| ENVI 课上操作 | Workbench 现状 |
|---|---|
| 辐射定标（默认参数直接出） | ✅ `gdal:calibrate` physical/TOA + sidecar〔P6-06〕 |
| **FLAASH 大气校正**；高光谱 224 波段 FLAASH 一次跑完坏波段标注+校正 | ⛔ FLAASH = 外部 MODTRAN，登记 `platform:flaash_external` blocked（"绝不以简化公式冒充"）〔P6-06〕；⚠️ **坏波段标注未实现**（高光谱组合谓两事一并做）→ 待排期 |
| **DOS 暗目标法**（ENVI 内置 QUAC/DOS 类） | ✅ `gdal:dos1`（Chavez 1988）〔P6-06〕 |
| **光谱曲线绘制/平滑/校正前后叠加**、水吸收特征估计水汽 | ❌ 光谱剖面/光谱库整链未做 → 建议排期（ENVI 光谱工具箱的核心） |
| 计算区域平均高程（加载世界 DEM 供 FLAASH 输入） | ❌ 无 DEM 采样服务能力 → 待排期 |

### D. 裁剪 / 掩膜 / 镶嵌

| ENVI 课上操作 | Workbench 现状 |
|---|---|
| ROI/矢量裁剪（装扩展 V5.5 才有） | ◐ 矩形 `srcWin/projWin/outputBounds` ✅〔P4-01〕；**ROI/矢量多边形裁剪交互入口缺失**（ROI 已有模型+绘制+存盘〔P3-05〕，缺"按 ROI 执行裁剪"管线）→ 建议排期 |
| Mask 遮罩（白=保留黑=屏蔽，参与运算） | ✅ `gdal:mask` 二值 Byte 掩膜 + sidecar 审计〔P6-05〕；calc 表达式可消费掩膜〔P4-04〕 |
| **无缝 mosaic：直方图匹配消色差 / 忽略 0 背景 / 羽化 50**〔实习4〕 | ◐ `gdal:merge/buildvrt`（overlapPriority/resolution/nodata）✅〔P4-02〕；**直方图匹配、羽化/接缝宽度未实现** → 建议排期（云影/接边返工的 CENTRE） |
| 矢量缓冲区扩大云范围 | ❌ `ogr:vectortranslate` 无 buffer（实测参数契约）→ 待排期 |
| 云影补丁（观测课云→缓冲→替换） | ✅ `gdal:cloudreplace`（mask 驱动 + 8 邻域膨胀）〔P6-06〕 |

### E. 几何校正与配准

| ENVI 课上操作 | Workbench 现状 |
|---|---|
| **GCP 手动选点 + RMS 表 + 多项式/重采样对比 + .pts 保存** | ✅ `gdal:gcpregister`：affine/polynomial2、控制点/检查点分离、残差分列〔P6-04〕；交互式 Zoom 十字丝选点的 GUI 对应能力——P6-04 有 GCP JSON 通道，交互工具待验证 |
| **自动选点**（手工引导 >3 对，按 RMS 排序删最大点） | ❌ 无自动找同名点（feature matching）→ 待排期 |
| GLT 地理查找表 / 传感器校正（蝴蝶结开关） | ❌ 未实现（ENVI 属内部机制）→ 待排期 |
| 配准用 **ENVI Classic 经典界面**专门工作流 | ✅ 对应「菜单交互-批-工作流复用同一任务通道」设计约束已达成（AGENTS #3） |

### F. 频率域（FFT）

| ENVI 课上操作 | Workbench 现状 |
|---|---|
| FFT 正变换 → **图形化画滤波器**（椭圆/矩形/多边形）→ IFFT | ✅ `gdal:fftfilter` 数值验收（抑制 bin 往返/条纹定位/共轭对称）〔P6-02〕；**交互式掩膜绘制 UI 缺**（遗留已记录） |
| **滤波器边缘羽化（0/1 突变 → 渐变宽度 10px）**（课堂最想补 No.5） | ❌ 无羽化/过渡宽度参数（bin 掩膜二值）→ 建议排期（擦痕痛点的根因） |
| 复数校验/镜像标注/多边形面要素 | ✅ 复显抑制逻辑 + 共轭对称（同上） |

### G. 变换与融合

| ENVI 课上操作 | Workbench 现状 |
|---|---|
| **Band Math b1-b4（掩膜 FVC）** | ✅ `gdal:calc` 受限 AST 表达式（含掩膜消费、dstNodata 口径）〔P4-04〕 |
| FVC：分位数端元 + 分级类别栅格 + 越界剔除 | ✅ `gdal:fvc`（percentile/values 端元 + .fvc_class.tif + 越界质量标记 + 裁剪口径）〔P6-05〕——课堂手抄分位数的流程被参数化 |
| **快速统计混用 → 需波段统计工具；按地类掩膜取 2%/98% 端元**（课堂最想补 No.6） | ◐ `gdal:statistics`（mode/bands/histogram bins/抽样预算）✅〔P4-04〕；**无 maskFile 入口**（实测）→ 建议排期 |
| 密度分割分 4 级出统计表 | ◐ `fvc_class` 分级类别栅格 + 统计 ✅；**交互式密度分割（区间编辑+统计表 UI）缺** → 建议排期 |
| NDVI 指数（多算法比较） | ✅ `gdal:sindex`（ndvi/ndwi/nbr/savi；物理/原值双口径；无效区进 NoData）〔P6-01〕；EVI 三波段遗留已登记 |
| 缨帽（1984/2001/**LandSat8 需装插件**） | ✅ 三套公开系数（TM/ETM+/**OLI**）〔P6-03〕——**课堂 ENVI 反而要从第三方装**（课件最想补 No.3） |
| **PCA 正变换逐分量查看 + 只选有用成分逆变换** | ✅ `gdal:pca` 正/逆（写入 .pca.json 可复现、重建 ≤1e-10、disCarded variance）〔P6-03〕；PC 散点图 UI 缺 |
| `.sta` 保存统计侧车 | ✅ sidecar 体系统一（.calibrate/.dos1/.cloudreplace/.pca.json/...） |
| Brovey / HSV 锐化 | ✅ `gdal:pansharpen`（定名方法 + sidecar）〔P6-04〕 |
| Band Stack（多源扩 6 波段） | ✅ `gdal:stack`〔P4-02〕；波段顺序语义（课堂 bug No.2 逆序）——sidecar 语义显式记录，无"恰好对上"的侥幸 |

### H. 分类 / 专题制图

| ENVI 课上操作 | Workbench 现状 |
|---|---|
| 非监督分类（ISODATA/K-means，类别数上限调大分薄云） | ❌ scikit-learn 运行时未安装（`ext:sklearn_classify` missing_dependency）→ 待 P8-05/P8-04 插件通道 或后再排 |
| 监督分类（ROI 样本 ×4 类 + Mask 防背景 + 神经网络 + 迭代曲线） | ❌ 同上；**监督分类整链（ROI→训练→推理→评估）是当前最大功能缺口**（P6-05 明示遗留） |
| **分类结果转矢量出专题图** | ✅ `gdal:polygonize`〔P5-02〕 |
| 分类逐类 Mask 让 Band Math 消费 | ✅ mask → calc 链真实可用〔P6-05/P4-04〕 |
| **基于掩膜的地类分位统计** | ❌ statistics 无 mask 入口（见 G） |
| 专题图制图（图例/标注） | ❌ 制图导出未做（本项目范围外的 stretch/draw 仅有 16x16 测试）→ 建议排期（轻） |

### I. 多维 / 高光谱

| ENVI 课上操作 | Workbench 现状 |
|---|---|
| HDF5/netCDF 数据显示 | ✅ P3-02 容器子数据集 + **P8-01 多维树/二维切片**（time/depth/x/y、单位、raw 像元，CRS 保留实测） |
| **高光谱坏波段标注**（黄色叹号） | ❌ 未实现 → 待排期 |
| 高光谱 FLAASH + 水汽/水吸收 | ❌（FLAASH ⛔ 边界 + 光谱分析链 ❌） |
| 光谱库 / Endmember 交互采集 | ❌ 未实现。当前 `fvc` 端元仅 percentile/values 静态输入（ROI 采样端元待做） |

---

## 2. 课件"最想补"逐条命中检——Workbench 是否已解决

| # | 课堂愿望 | Workbench |
|---|---|---|
| 1 | 运算后元数据自动继承 | ◐ 设计隔离（原值/物理值/显示/NoData 四域分离）已路由；波长/FWHM 继承**未完成**（stack 无参数透传） |
| 2 | 反射率 ×10000 自动还 | ✅ 显示域 scale/offset（AGENTS #4） |
| 3 | 官方缺口需装扩展（矢量/ROI 裁剪；LandSat8 缨帽） | ⚡ **反超**：LandSat8 OLI 原生三套系数；矢量/ROI 裁剪待补 |
| 4 | 参数文件 + 输出同目录保存 → 复现 | ✅ **反超**：历史/参数重开/工作流/批处理全链〔P7-01~05〕 |
| 5 | FFT 滤波器羽化/过渡宽度 + 预览 | ❌ 待排期 |
| 6 | 按掩膜分地类分位统计一键出 | ❌ 待排期（statistics 需 mask 输入） |

## 3. 课件 bug 清单 → Workbench 防/缺对照

| # | ENVI 课堂 bug | Workbench 对策 |
|---|---|---|
| 1 | 量级与元数据错 | ✅ 四域值空间分离 + NoData/掩膜/sidecar 口径显式 |
| 2 | Band Stack 逆序侥幸 | ◐ 输出语义显式（sidecar+可审计）；无逆序发射设计 |
| 3 | 系数/工具缺失中断 retry 循环 | ✅ Registry 缺口全部有 reason、blocked 入口不假按钮（AGENTS #7/#8） |
| 4 | ROI 加错影像 / 点删除链 RM 不足 | ✅ 请求代际 + 图层 ID 隔离丢弃迟到响应（P3）；RMS 分列渲染 |
| 5 | 镶嵌硬接边 + 云影黑斑 | ❌ 羽化/直方图匹配缺（buffer 云影有 cloudreplace 但 buffer 仍缺） |
| 6 | FFT 滤波器擦痕 | ❌ 羽化缺（F2） |
| 7 | 统计口径混用 | ❌ mask 统计缺（G/H） |
| 8 | 密度分割不回车不生效 | ❌ 无密度分割交互 |

---

## 4. 运行期审计缺口（本机事实，registry 口径）

对账 `reconcile_baseline()`：**缺失/受限 12 条**——

- ⛔ 刻意边界：`platform:flaash_external`（外部引擎）、`platform:agriculture_count`（方法未定）
- ❌ **外部依赖未装**（这类是 ENVI 有、本机无）：`ext:sklearn_classify`（分类整链）/ `ext:xarray_dask`（多维图计算）/ `ext:external_gis`（Whitebox/SAGA/GRASS GIS 工具链）/ `ext:geopandas_vector` / `ext:pdal_pointcloud`（点云）/ `ext:external_atmospheric`（6S/MODTRAN）
- ❌ **运行时驱动/插件缺**：`gdal:driver:HDF5`（本机 HDF5 writer 缺——P8-01 的验收边界）；`rio:cli:cogeo/color/mbtiles`（可选插件，COG 可由 GDAL 驱动兜底）；`gdal:cli:gdal_auth`（Windows QGIS 未编译该独立 CLI）

---

## 5. Workbench 独有优势（ENVI 课堂痛点）

1. **可复现**：历史存储/重开参数/配方导出（SQLite + 幂指纹）〔P7-05〕、
   工作流 DAG（环/端口/缓存失效自动下传，跨引擎物化）〔P7-04〕、
   批处理 fail-policy〔P7-03〕、隔离脚本会话〔P7-02〕、项目迁移/崩溃安全〔P7-01〕。
2. **数值可信**：一切映射到 TaskSpec + 同一注册表/适配器（无"各走一套"）；
   合成影像独立公式验收；sidecar 语义显式（可追溯事件链）。
3. **诚实 UI**：无占位按钮、无假进度、blocked 明示；四值域分离；NoData 独立。
4. **多维**：HDF/netCDF 经统一 multidim 模型，单位/单位坐标维度钉死。

---

## 6. 目前最要紧的真实问题排序（供协调者排期）

1. **查看/交互表层**缺 Swipe（卷帘）、Band 浏览、光谱剖面、测距——课堂演示密集。
2. **分类整链缺失**（非监督+监督+精度评估）。受 `ext:sklearn_classify` 依赖卡；
   P8-04 插件协议先打通是正道。
3. **镶嵌高级化**（直方图匹配/羽化/seamline），课堂最强返工点。
4. **FFT 羽化 + 交互掩膜编辑**。
5. **光谱分析链**（剖面曲线/水吸收/光谱库/ROI 端元采集）。
6. **按掩膜分地类统计**（statistics maskFile 参数）。
7. **ROI/矢量裁剪交互管线**（ROI 模型已有，缺执行桥）。
8. **高光谱坏波段标注**（多维容器元标注，读侧）。
9. P9/P10 产品化事项照账本（对账/性能/故障注入/打包与诊断）。

（1-8 均为**账本里暂无任务号**的缺口——建议协调者逐条登记后再排期；
 Integr 报一道"未进入账本的缺口清单"会失于遗漏。）

---

## 7. 结论一句话

- **工程红线与可复现体系已跟平甚至反超 ENVI**（元数据/尺度分离、无假入口、
  历史/工作流/批处理）。
- **ENVI 课堂重型演示中，交互显示层（卷帘/剖面/测距/波段浏览）和分类整链、
  镶嵌高级化、FFT 羽化、光谱分析链、按地类统计**是可数且重要的真实缺口，
  目前账本**无任务号覆盖**，需要协调者登记成 TBD（避免账本的 done 观感）。
- 运行时依赖缺口（scikit-learn/xarray/HDF5 writer/点云）不是编码缺口而
  是**环境/插件协议缺口**，由 P8-04（插件）+ P8-05（可选引擎登记）优先破。
