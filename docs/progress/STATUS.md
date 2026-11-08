# 执行状态

更新：2026-11-08（第 70 轮 · P8-02 VSI/归档/远程数据：统一 VSI 路径规范 + 本地归档 /vsizip//vsitar/ 读取与容器子数据集探测 + HTTP Range 零全量内存加载 + 超时重试与协作式取消 + 项目持久化与跨会话重开 + GUI 远程输入菜单；12 项集成测试全部通过，clean_exit_verified=True，residual=0）✅。

## 当前里程碑

- **P0 基础设施与自动化验证闭环**：P0-01、P0-03、P0-04、P0-05 为 done（第 57 轮收官）；P0-02 为 implemented——环境锁与隔离导入已满足，但 GDAL 运行时依赖本机 QGIS，自带可重定位运行时归 P10-01/P10-02 后方可转 done。
- **P1 核心作业生命周期全面闭环**：P1-01~05 全部 done（第 57 轮收官；P1-02/03 此前已 done）。
- **P2 能力基准、注册表与动态表单**：P2-01~05 全部 done（第 57 轮收官）。锁定 GDAL 3.13.3 与 Rasterio 1.5.2 基准（443 项条目）、205 驱动 + 119 CLI + API 索引编目、可搜索工具箱与动态表单、四维覆盖审计。
- **P3 主窗口、数据与影像查看**：P3-01~05 全部 done（第 57 轮收官）。主窗口/Dock/项目持久化/高 DPI；栅格/矢量/子数据集元数据（含请求上下文隔离与廉价计数）；可见窗口分块与 LRU 缓存预算；灰度/RGB/伪彩/拉伸/分屏（原值与显示严格分离）；view CRS/重投影/ROI（含故障注入矩阵）。
  - **P3-01 主窗口、Dock、项目树、图层模型**：done（第 57 轮收官）。实现主窗口与 Dock 布局、图层顺序保序；显式设置 Dock 与工具栏 objectName，实测验证浮动（isFloating）、显隐（isVisible）与停靠区域重排跨会话无损保存与重开恢复；分离运行时根与用户项目目录，新项目显式记录 `path_anchor='project_dir'`；保存事务边界加固；旧版 1.0 格式且缺少路径基准标记的项目进入明确的待确认流程，程序化打开返回 checkable 状态，用户确认前不自动派发同名文件、未确认前禁止保存，用户确认后按真实路径派发并迁移持久化，取消确认原文件保持不变；无论确认的物理目标是否存在均保留用户确认选择（存在性仅决定是否请求加载，不影响路径更新），重开后严格保持目标一致，杜绝劫持同名文件；自定义空路径提供校验阻断；旧绝对引用直接按确定语义解析；缺失源状态指示与显式移除；移除图层与关窗彻底释放文件句柄与缓存。
  - **P3-02 栅格元数据读取**：done（第 57 轮收官）。通过 Worker 提取尺寸、波段数、驱动、数据类型、CRS、GeoTransform（含旋转 Affine 系数）、地理范围与 NoData；统一异常隔离与 `retryable` 标准错误 Schema，坏文件探测不中断执行线程。
  - **P3-02 基础矢量与子数据集元数据**（第 22 轮补齐原缺口）：done（第 57 轮收官）。Worker 新增 `probe_vector`（图层名称、几何类型、CRS、范围、字段名称/类型/可空性；要素数与范围不可得时以 `feature_count_expensive` / `extent_available` 如实标注，不返回编造数值）、`list_subdatasets`（原样保留引擎返回的数据源引用）与 `probe_subdataset`（尺寸、dtype、NoData、波段、GeoTransform、CRS）。GUI 新增「数据源元数据」面板展示矢量结构与可选子数据集；栅格探测失败时回退矢量探测，使矢量文件可作为图层打开、可保存重开；**矢量渲染与多维分析尚未实现，明确标注而不提供假入口**。
  - **P3-03 瓦片请求、概览、缓存、过期请求处理**：done（第 57 轮收官）。仅按可见窗口向 Worker 索取数据；实现 `TileCache`（LRU 淘汰与字节预算限制）；请求代际（`_active_request_id`）与图层 ID 校验彻底丢弃过期旧图响应；支持四种概览采样策略（auto/nearest/average/gauss）；在真实交互式 GUI 下以 6144x6144 多级概览大影像完成缩放、平移、切波段与切层全流程实测，验证 LRU 淘汰真实触发（9 次逐出），缓存严格受限在预算内，图层移除后缓存彻底清零。
  - **P3-04 灰度/RGB/伪彩、拉伸、透明度与分屏**：done（第 57 轮收官）。支持灰度、RGB 与 6 种伪彩色（terrain/viridis/plasma/inferno/magma/jet）；min-max 与 2%~98% 分位数拉伸；原始像元值与显示拉伸值严格物理分离；实现双视图联动同步模式（`test_dual_canvas_link_mode`）。
  - **P3-05 view CRS、重投影、查询与 ROI**：done（第 57 轮收官）。支持旋转 Affine 栅格与无 GT 栅格加载；地图坐标 ↔ 像元行列支持中心 (center) 与角点 (corner) 两种坐标约定（`test_map_to_pixel_center_vs_corner`）；实现 PointROI / RectROI / PolygonROI 模型与点在多边形判定；跨 CRS 视图重投影网格索取；通过真实 Qt 鼠标事件（QTest）完成多边形 ROI 逐点绘制、右键/起点闭合、选中、顶点手柄真实拖拽形变、删除与保存重开恢复；多图层切换下 ROI 所属图层严格隔离与保全；旋转 Affine 栅格与视图 CRS 切换（像元 vs EPSG:4326）下坐标空间与点位无损保持；矩形 ROI 权威几何与显示轮廓物理分离，在旋转 Affine 栅格下展开 4 角点经 Worker 转换并渲染实际轮廓，实测断言 4 个真实地图角点与 425.0 平方地图单位覆盖面积（彻底杜绝错误轴对齐面积 375.0），变换视图下手柄隐藏禁用，防止扭曲矩形；坐标转换失败时绝不伪装恒等变换，严格隐藏图形项（`item.isVisible() is False`）并清空 `display_points`（为 None），保全权威模型，切回有效视图自动恢复；拖动反算 Worker 报错时安全回退，不崩溃不污染。本轮补齐五类故障注入矩阵（向地图失败 / 回像元失败 / 项目重开失败 / 拖动反算失败 / 旧响应迟到），每类均验证图形状态、权威几何与保存结果；修复跨坐标空间数据混用缺陷（state 显式记录显示点所属空间与 CRS），拖动反算失败时显示图形恢复原轮廓、权威几何保持不变。
- **P4 能力应用与完整用户流程**：
  - **P4-02 拼接、VRT、堆叠与波段管理**：**done**（第 45 轮 S5 桌面端到端验收）。真实 `MainWindow` + 真实 GDAL Worker + QTest 按钮/任务面板交互，验证输入顺序即重叠优先级（first/last 双向）、混合 CRS 明确拒绝且旧成果字节不变、ENVI 新建与覆盖、PNG alpha 保留、JPEG/ENVI alpha 与 PNG Float32 在提交前拦截、真实 4096×4096 计算中取消并重试、成果自动加载与项目保存重开、VRT 相对源失效时明确报错并在项目重开时进入缺失源列表。
  - **P4-01 / P4-03 / P4-04 / P4-05**：done（第 57 轮收官）。P4-05 的完整用户流程验收以第 36 轮专项与 test_p4_full_user_flow.py 端到端为准；P4-02 转为 done 不改变其余任务的证据边界。
- **P5 全量栅格、矢量、地形与坐标能力**：
  - **P5-01 补齐栅格算法/传统工具入口**：**done**（第 47 轮基准逐项对账）。契约基准 443 条全部在注册表；注册表 448 条，多出的 5 条为平台组合/受限实现；8 条运行期缺口（3 条 blocked、5 条 missing）均有具体原因，未解释缺口 0。
  - **P5-02 栅格化、多边形化、等值线及地形工具**：**done**（第 48–52 轮逐项接入并验收，第 53 轮边界完善）。`gdal:dem` 地形分析（解析平面坡度/坡向/山体阴影数值验证）、`gdal:rasterize` 矢量栅格化（已知矩形烧录值/属性字段）、`gdal:grid` 散点插值（linear 逐像元解析值）、`gdal:polygonize` 栅格矢量化（标签图多边形 DN/面积、4/8 连通差异）与 `gdal:contour` 等值线（解析平面逐顶点验证、offset 对齐语义）共 5 项全部接入 Worker + GUI；第 51 轮建立**矢量成果输出通道**（GPKG/GeoJSON 单文件矩阵、`inspect_vector` OGR 重开校验、`kind=vector` 输出计划与矢量图层登记），提交校验不复用仅栅格路径；第 53 轮补齐 Shapefile 多文件 bundle 输出、contour fixedLevels 与 band 参数。审计 `platform_executable` 8 → 10（polygonize/contour 接入），运行期缺口维持 5 且均有具体原因。
  - **P5-03 矢量图层与分页属性表**：**done**（第 54 轮）。worker `read_vector_page` 按范围读取（单页硬上限 1000、`GetFeatureCount(force=0)` 廉价计数拿不到就如实标注、空几何/多部件/编码逐要素标注）+ 属性表面板（一页 100 条、翻页、请求代际丢弃迟到旧页、多子图层下拉、矢量图层选中联动）；GBK 编码 Shapefile 中文字段名/值往返一致；大图层全程无一次性全读。
  - **P5-04 矢量处理、SQL 与坐标转换**：**done**（第 55 轮）。`ogr:vectortranslate`（ogr2ogr 通道）接入：GPKG/GeoJSON/Shapefile/FlatGeobuf 转换、OGR SQL、where/select 属性过滤、spat 空间过滤（源 CRS 单位）、dstSRS 重投影（恒配 reproject=True + 无 SRS 源预检拒绝，杜绝假重投影）；已知坐标/轴序/空间关系/字段值逐项断言。
  - **P5-05 驱动读写能力及高级选项**：**done**（第 56 轮）。`describe_driver` 暴露 DCAP 四访问模式声明与 `DMD_CREATIONOPTIONLIST` 高级选项；ENVI/GPKG/GTiff/COG + 扩展驱动（netCDF/BMP/FlatGeobuf/GeoJSON/PNG/JPEG）四模式分开实测，已装可打开驱动枚举 180+；声明 vs 实况错位六项白名单化（ENVI CreateCopy 等元数据保守项），新错位强制显式过目。**P5 里程碑 P5-01~05 全部 done。**
- **P6 遥感专题处理**：
  - **P6-01 光谱、剖面、NDVI 等指数**：**done**（第 58 轮）。`gdal:sindex`（NDVI/NDWI/NBR/SAVI，numpy 求值）：物理量 = 原值 × scale + offset（波段元数据驱动，usePhysical 可关）、无效区三源（NoData/分母零/非有限值）进输出 NoData；合成值逐像元公式断言（原值 1/3 vs 物理量 0.0/−1/3/−3.0）、SAVI L=0 退化实测并修复零值 truthiness 缺陷。架构边界：science 独立 worker 待 sw-science 环境补装（registry 已声明），EVI 三波段指数与光谱剖面曲线归后续。第 59 轮修复 P6-02 重构误伤的 sindex 门禁回归，并补齐 GUI 的 format 下发。
  - **P6-02 FFT 专用界面与去条纹**：**done**（第 59 轮）。`gdal:fftfilter`（numpy.fft）：FFT2 → 厄米对称抑制掩膜 → IFFT2 → 单波段 Float32 + 掩膜 sidecar；8×8/7×9 合成图空抑制往返、已知条纹频率定位（抑制 (0,2) 后逐像元等于剩余正弦）、对称伙伴 (0,W−2) 与 sidecar 重跑复现全部数值验收；NoData 标签/像元一致；复数输入明确拒绝；GUI 支持 fftBand/suppressBins 与 ENVI 等格式真实下发。遗留：分块/流式 FFT、频谱中心化与交互式掩膜编辑 UI 归后续。
  - **P6-03 PCA、逆变换与缨帽**：**done**（第 60 轮）。`gdal:pca`：全图均值/协方差或相关矩阵/特征向量/主成分顺序/解释方差，正变换写可复现 `.pca.json`，逆变换按 sidecar（含 SHA256）重建；完整重建 ≤1e-10，降维误差按 `RMSE²×波段数 == discarded_variance` 定义并实测；NoData 保持；模式专属参数严格拒绝。`gdal:tasseledcap`：Landsat 5 TM / 7 ETM+ / 8 OLI 三套公开系数，显式 6 波段映射，3 波段亮度/绿度/湿度 + 来源/产品级别 sidecar。遗留：PCA 整幅读入、更多缨帽传感器与交互式载荷图 UI 归后续。
  - **P6-04 GCP、配准、差值与融合**：**done**（第 61 轮）。`gdal:gcpregister`：GCP JSON 控制点/独立检查点分离，affine/polynomial2 正反变换拟合，参考网格重采样，残差与检查点误差分列；`gdal:rasterdiff`：严格同网格 difference/absolute + 逐波段统计；`gdal:pansharpen`：Brovey 与 HSV 精确定名/公式，3 波段输出 + 方法 sidecar，NNDiffuse/GS/PCA 明确不提供入口。遗留：TPS、GCP 分块重采样、更多融合方法归后续。
  - **P6-05 分类与 FVC 完整流程**：**done**（第 62 轮）。`gdal:mask`：闭区间阈值 1/0 Byte 二值掩膜（显式不设 NoData、invert 仅在有效源像元内翻转、sidecar 计数审计）；`gdal:fvc`：NDVI 输入 → percentile（缺省 2%/98%）/values 端元 → FVC + 分级类别栅格 `.fvc_class.tif`，`fvc.json` 记录端元来源/像元面积/分级统计/越界质量标记，无效区与 crop 策略显式。遗留：监督式 ROI 分类（scikit-learn）与混淆矩阵未实现且不提供假入口。
  - **P6-06 辐射处理、去云、农业及外部引擎适配**：**implemented**（第 63 轮；不转 done——2 个子项 blocked）。`gdal:calibrate`：physical（DN×gain+offset 逐波段）与 toa_reflectance（π·L·d²/(ESUN·cos θz)），sidecar 明确 radiance/reflectance 单位、BT 未实现；`gdal:dos1`：Chavez 1988 暗目标减法，显式 hazeDN/ESUN/太阳天顶角/d，sidecar 独立核算；`gdal:cloudreplace`：掩膜驱动的多时相替换 + 8 邻域膨胀，sidecar 记录时相与像素来源，明确不声称「恢复原时刻真实地表」。`platform:flaash_external` 与 `platform:agriculture_count` 登记为 missing/blocked：FLAASH 明确「绝不通过简化公式宣称实现」、农业计数「方法未确定，需保留直径/尺度/阈值/重叠与人工标注对照」。

- **P7 项目、脚本、批处理与流程**：
  - **P7-01 项目保存、迁移、重定位与恢复**：**done**（第 64 轮）。`project/migrate.py`：legacy 0.9 → 当前 1.0 的显式迁移器（src/label/metadata → source/name/metadata_snapshot、comment → notes 前缀「[legacy 0.9 迁移]」、未知字段/版本显式拒绝、path_anchor=None 走 P3-01 legacy 确认流程）；`load_project` 无感迁移并把 Windows `\` 规范化为 `/`；真实子进程 `os._exit(-1)` 在写盘段崩溃 → 上次保存字节原样保留；`os.replace` 中断 → 原文件原样保留、无残留 `.tmp`；ROI 原样保留并通过 domain.ROI 校验。遗留：P3-01 已验收移动/重定位语义不变。
  - **P7-02 隔离脚本会话**：**done**（第 65 轮）。`scripting/session.py::ScriptSession`：按 engine 分别启动**真实**引擎解释器（gdal=doctor 解析的 QGIS/conda、rasterio=`.venvs/sw-rasterio`；未知 engine SessionError）；用户脚本在引擎内 exec 原生 API（`from osgeo import gdal` / `import rasterio`），GUI 进程不 import osgeo/rasterio；`stop` 幂等（proc None / poll None → 0）；stdout/stderr.log 写会话目录，每次 run 前清空；`register_artifact` 显式登记成果（artifacts.json：path/sha256/size/mtime/registered_at）；session.json 记录 engine/python_path/started_at；同 session_dir 再 run → 全新 interpreter。`scripting/__main__.py` 提供 CLI 入口。遗留：会话非线程安全（调用方自串）；scipy 未注册。
  - **P7-03 批处理**：**done**（第 66 轮）。`processing/batch.py`：模板（capability_id/engine_provider 必填校验、inputs/outputs 逐 item `{item[key]}` 展开、未知键显式 `BatchError`）→ N 份 TaskSpec；单输入 key=`input`、多输入 `list[DatasetRef]`，单输出 key=`output`、多输出 `output_0/output_1`；`overwrite` 模板参数透传 OutputPlan.overwrite，`params` 内字符串同样经模板展开（非原样透传）；`run_batch` 严格串行（execute 返回 TaskStatus），`continue_on_error=True` 失败继续并如实搜集 succeeded/failed/cancelled 终态与 duration，`False` 首败立即停止且未处置条目显式 `pending`（error=skipped，不冒充成功）；`BatchReport.summary()/all_terminal()` 供调用方审计；`write_manifest` 把 batch manifest（batch_id/summary/entries）落盘为 JSON。验收测试 `tests/integration/test_p7_batch_worker.py` 5 项全部基于真实 QGIS GDAL 3.44.15 worker 端到端（多输入/多输出展开、参数变化、同名冲突 overwrite=True 真实覆盖 stub 文件、单项失败继续、strict 停止 + manifest 汇总）。顺带诊断并修复全量回归缺陷：MainWindow 构造期 `attribute_panel.set_client(self.gdal_client)` 懒建真实 gdal client 在测试夹具换槽后无人停止（进程残留 4 个 → clean_exit_verified=false），`MainWindow._owned_clients` 登记 + closeEvent 全回收。全量 `scripts/test.ps1 -All` 恢复 **clean_exit_verified=True / residual=0**。遗留：批处理严格串行（并行与 platform:* 外部引擎不在范围）；GUI 批处理面板归后续。
  - **P7-04 DAG 工作流**：**done**（第 67 轮）。`processing/workflow.py`：带版本的节点图（节点引用稳定 capability_id、缺省引擎约束 gdal→gdal-qgis/3.13.3 / rasterio→rasterio-venv/1.5.2 可覆盖、类型化端口 raster/vector）；`render_workflow_nodes` 提交前校验——环检测（Kahn，环成员精确列出）、端口错配拒绝（生产者声明 kind × 消费者期望 kind）、未知上游节点/输出键拒绝、输出 URI 冲突与「输出=他人字面输入」写穿拒绝、按引擎的节点计数汇总（不承诺估算）；`run_workflow` 拓扑串行（execute 注入 TaskStatus|TaskResult，TaskResult.error 转写报告；TaskSpec 通道与批处理/菜单共用注册表与 adapter）；**跨引擎物化** gdal→rasterio 经真实 committed 文件衔接（数值独立重开断言）；**失败/恢复**——引擎级失败下游 pending(skipped)、独立分支继续、node_key/输入/输出指纹未变的成功节点重跑 `cached`（引擎调用数 0、文件 mtime 不变）；**缓存失效**——上游参数变化重跑使产物内容变化（Float32→Byte 实测）→ 下游指纹失效自动重执行并消费新产物；指纹 fast=size+mtime / strong=sha256 记录复现等级；`workflow-cache.json` manifest 落盘。顺带修复预置缺陷：rasterio convert `commit_files` 未透传 `overwrite`（真实 worker 重跑已存在目标被误拒），与 gdal 侧语义对齐。官方闸门 **805 passed / clean_exit_verified=True / residual=0**。遗留：同进程管线优化、调度器并行、GUI 工作流面板归后续。
  - **P7-05 历史与可复现导出**：**done**（第 68 轮）。`processing/history.py`（纯 stdlib）：`HistoryStore` SQLite（schema_version 显式迁移：旧库自动升 v1、更高未知版本拒开不降级；单写入方）：`record` 登记 TaskSpec 通道真实执行——kind/group、capability/task/attempt/status/duration、**spec 引擎约束 + worker 上报真实引擎版本**（TaskResult.engine_runtime）、完整 params JSON、错误 JSON、输入（端口→URI/kind+指纹 size+mtime，strong 可选 sha256）与输出（key→URI/committed_files+指纹）；`reopen_params(run_id)` 从历史重开参数（capability/engine/params/inputs），实测真实 gdal worker 重提后数值与历史一致（[[1,2],[3,4]] 2×2）；`export_workflow_recipe(run_ids, path)` 导出**标准 workflow 文档**（P7-04 run_workflow 唯一入口）：capability/engine/params 原样回填、历史 URI 解码为本地路径（中文百分号往返）、**边自动合成**（输入 URI=他节点输出 → node 引用保 DAG 序），跨引擎链回放实测：每能力引擎恰再调用一次、final 值 [[5,6],[7,8]] 与历史一致、mid mtime 变化（真实重写非读缓）。官方闸门 **808 passed / clean_exit_verified=True / residual=0**。遗留：GUI 历史面板/重跑 UI、多写入方并发策略、远程来源稳定标识归后续。
  - **P8-01 多维数据树与二维切片**：**done**（第 69 轮）。worker `probe_multidim` 查询通道（entrypoint._SIMPLE_CALLS + mdim_ops.build_multidim_tree + client 同步封装）递归展开 group/attrs/dims（has_coordinate_array）/arrays，数组节点 dims/shape/unit/scale/offset/nodata/srs_wkt 无值如实 None；`gdal:mdim_slice`（TaskSpec 通道，可进批处理/工作流/历史）：非地理维全部显式钉死（恰 rank-2，k=v 文本或 dict 双形态且严格整型拒绝 1.5），GetView 取剩余两维原序 rows/cols，像元 raw 原值（独立推导 [[12,13,14],[15,16,17]] 断言），sidecar 记 selected（含坐标值）/remaining_dims/coords/unit/scale/offset/nodata/georeferenced，输出带 SetUnitType/SetScale/SetOffset/SetNoDataValue；SetSpatialRef 的数组 CRS 保留实测 EPSG:4326；无参考数组不编造地理参考（crs=None + georeferenced=false）；拒绝矩阵 7 例全不落盘；注册面四维对账 462/455/250/24（registry 七件套 + GUI 表单行 + 能力切换状态隔离）。遗留：HDF5 writer 本运行时缺失（netCDF/CF 容器验收；HDF5 待驱动补齐）、GUI 多维树面板/交互预览、complex 数组支持。
  - **P8-02 VSI/归档/远程数据**：**done**（第 70 轮）。
    - **路径契约与 Windows 安全隔离**：新增 `domain/paths.py`，集中管理 GDAL VSI 规范（`/vsi*`）与远程 URL（`http://`, `https://`, `s3://`, `gs://`, `az://` 等）。`normalize_vsi_path` 严格规范化为 POSIX 前缀，保留正斜杠，在 Windows 环境下彻底绕开 `pathlib.Path.resolve()`，杜绝盘符前缀污染（如 `C:/vsicurl/...`）与反斜杠破坏；提供 `extract_local_path_from_vsi` 提取本地归档底层文件句柄；`sanitize_url` 脱敏敏感 query 参数；`get_display_name` 提供干净的用户可见名。
    - **本地归档读取与容器子数据集探测**：`gdal_provider` 适配器全面支持 `/vsizip/`、`/vsitar/`、`/vsigzip/` 等归档路径；单栅格压缩包自动解析打开；多栅格归档容器通过 `gdal.ReadDir` 动态探测内部栅格项，并在元数据与 `list_subdatasets` 中无缝暴露子数据集 URI，支持单独探测与图层加载。
    - **HTTP Range 与零全量内存加载**：搭建本地 `LocalHttpRangeServer`（支持 `Range: bytes=start-end`，返回 206 Partial Content、可控延迟与 503 临时故障注入）；实测验证读取 128x128 瓦片窗口仅传输数 KB 字节，绝无全量文件下载（零全量内存加载，A13 标准完整闭环）。
    - **网络弹性与协作式取消**：配置 `GDAL_HTTP_TIMEOUT=10`、`GDAL_HTTP_CONNECTTIMEOUT=5`、`GDAL_HTTP_MAX_RETRY=2`、`GDAL_HTTP_RETRY_DELAY=1.0`；实测验证网络超时优雅报错（不挂起 UI/worker）、503 瞬时故障自动重试恢复；实测 WorkerProcess 异步远程读取在 2 秒内协作式取消完成，无孤儿进程残留。
    - **项目持久化与跨会话重开**：更新 `project/model.py`，`store_source` 与 `resolve_source` 对 VSI/远程路径自动标记 `source_is_relative=False`，保存到 `.spectra.json` 时保持原规范化 URI；重开项目无损还原远程图层。
    - **桌面 GUI 集成**：主窗口文件菜单新增「打开远程 URL / VSI (&U)…」动作与对话框；文件对话框筛选器扩充支持 `.zip`, `.tar`, `.tar.gz`, `.tgz`；通过真实 GUI 验证直接打开远程 URL、菜单输入打开、项目保存与跨会话完全恢复。
    - **测试与官方验证**：`tests/integration/test_p8_vsi_remote.py` 12 项测试全部通过；官方监管闸门 `scripts/test.ps1 -Integration` 全部通过（exit_mode=clean, pytest_exit_code=0, clean_exit_verified=True, residual=0）。

## 原 P3 验收标准逐项对账与剩余缺口

| ID | 任务 | 原始验收标准 | 当前已满足项与实测证据 | 剩余缺口与待验条件 | 当前状态 |
|---|---|---|---|---|---|
| P3-01 | 主窗口、Dock、项目树、图层模型 | 基础项目可保存重开源/样式/视图；布局可恢复；关闭图层释放文件；中文高 DPI 可用 | 项目 JSON 相对路径序列化与重开恢复通过；分离运行时根与用户项目目录；新项目显式记录 `path_anchor`；保存事务边界加固，写失败无内存污染；整体复制搬移保持可用；另存为重算引用指向原数据；旧版 1.0 路径歧义进入待确认流程不自动猜测，用户确认后按真实路径派发并迁移持久化，取消确认原文件保持不变；无论确认目标是否存在均保留用户选择并在保存重开后目标一致；缺失图层与 ROI 在保存/另存为时不丢失；缺失源列表明确指示并支持重新关联与主动移除；图层移除后重命名与删除证明 Windows 句柄已彻底释放；为所有 Dock 窗体显式设置 `objectName`，调整浮动（isFloating）、显隐（isVisible）、左右停靠区互换，保存项目并在全新实例重开验证布局无损恢复（`tests/integration/test_p3_dock_layout_persistence.py` 全绿）；中文路径测试通过。 中文字体族可用性、字体初始化返回值与高 DPI 缩放下主窗口/子控件几何已补直接断言
（`tests/integration/test_p3_acceptance_gaps.py` 3 项）；
原生桌面不同物理显示器 DPI 切换与排版微调待更广泛物理环境实测。 | implemented |
| P3-02 | 栅格/矢量/子数据集的元数据读取 | 数据类型、NoData、mask、单位、波段与空间模型准确；不完整数据明确报错 | 栅格尺寸、dtype、NoData、mask 标志、单位、波段、空间参考、旋转 Affine 准确探测；不完整/损坏文件返回清晰错误且不中断 Worker；**矢量图层（OGR）基础元数据已实现**：图层名称、几何类型、CRS、范围、字段名称/类型/可空性，要素数与范围不可得时如实标注（`GetFeatureCount(force=0)`，不强制全表扫描）；**科学子数据集已实现**：发现列表原样保留引擎数据源引用（`NETCDF:"file.nc":Band1`，不作路径解析），选中后探测尺寸/dtype/NoData/波段/GeoTransform/CRS；范围按四个像元外边界角点计算，旋转仿射下与四角预期相等；矢量文件可作为图层打开、选中并保存重开；元数据请求带项目代际/图层/数据源/当前选择/请求代际五重上下文校验，跨图层与同容器子数据集的迟到成功与迟到错误均不得覆盖当前内容 | 课程原始 ENVI 栅格与 lena.tif 的真实打开验证待条件具备时补做（原退出条件允许以合成影像完成同类验收并保留真实数据待验状态）；矢量渲染与多维分析不属 P3-02 范围，未实现 | implemented |
| P3-03 | 瓦片请求、概览、缓存、过期请求处理 | 只读可见窗口；切换波段不出现旧图；内存增长受缓存预算限制 | 仅读视口局部数据；快速切层/切波段过期响应安全丢弃；TileCache 具备字节预算与 LRU 淘汰机制；在离屏 Qt GUI 环境（`QT_QPA_PLATFORM=offscreen`，包含真实 QMainWindow、QGraphicsView 绘制与显示数组及独立 WorkerProcess）下，使用 6144x6144 多级概览大影像（303 MB），在 10 MiB 小缓存预算下连续实测缩放、平移、切波段与切层，真实触发 7 次 LRU 淘汰，缓存占用严格限制在预算内（阶段采样缓存峰值 10106361 bytes ≈ 9.64 MiB < 10 MiB 预算），GUI 阶段采样 RSS 最大值为 194.91 MiB，Worker 峰值 77.89 MiB；图层移除与关窗后缓存严格清零（0 bytes），GUI 退出保留 RSS 约 187.8 MiB（受 Python 内存分配器池化保留影响，底层 TileCache 清空无泄露）（`docs/progress/runs/p3_gui_memory.json`，`tests/integration/test_p3_real_gui_memory.py` 全绿）。 | 「切换波段不出现旧图」已补直接断言：可控客户端注入旧波段迟到响应，
断言画布不渲染旧内容且最终显示为新波段（`tests/integration/test_p3_acceptance_gaps.py`）；
超大影像长周期持续交互在不同显卡驱动与物理桌面下的渲染平滑度待后续实测。 | implemented |
| P3-04 | 灰度/RGB/伪彩、拉伸、透明度与分屏 | 显示改变不改原始像元；抽样统计有标识；跨视图同步模式正确 | 灰度/RGB/伪彩/透明度完整支持；显示拉伸与原始像元查询严格解耦；双视图联动同步测试通过。 | 极值与分位数拉伸在特殊极端动态范围数据下的精细边界表现待进一步实测验证。 | implemented |
| P3-05 | view CRS、重投影、查询与 ROI | 旋转 affine、不同 CRS、无 CRS 视图、像元中心/角点均有样例 | 旋转 Affine 与无 CRS 栅格支持；地图坐标与像元行列中心/角点换算通过；ROI 模型支持多点、矩形与多边形；视图跨 CRS 重投影网格读取通过；使用真实 Qt 鼠标事件（QTest）完成多边形 ROI 逐点绘制、右键/起点闭合、选中、顶点手柄拖拽形变、删除与保存重开恢复；修复快速连续点击误闭合缺陷，严格区分位置邻近双击与快速多顶点绘制，支持四边形、八边形快速普通点击完整保留顶点；实现 ROI 存储几何（权威坐标空间、源 CRS、像元中心/角点约定）与显示几何的分离与 Worker 双向转换，在旋转 Affine 栅格下精确验证已知像元角点 (10, 10) 在原生地图 CRS (EPSG:32650) 下渲染于 (500025, 3199985)；地图视图下手柄拖动反算回权威像元坐标，不将地图坐标写入像元 ROI；像元 -> 原生地图 CRS -> EPSG:4326 -> 像元往返覆盖无损；不同 ROI 坐标空间混合保存重开恢复；无 CRS / 无地理参考数据集明确限制地图换算，不盲猜；异步换算带视图/图层代际保护，旧响应安全丢弃；矩形 ROI 权威模型与显示轮廓物理分离，在旋转 Affine 栅格下展开 4 角点经 Worker 转换，精确断言 4 个地图角点与 425.0 平方地图单位覆盖面积（彻底杜绝错误轴对齐面积 375.0），变换视图下手柄隐藏禁用，防止扭曲矩形；坐标转换失败绝不伪装恒等变换，严格隐藏图形项（`item.isVisible() is False`）并清空 `display_points`（为 None），保全权威模型，切回有效视图自动恢复；拖动反算 Worker 报错时安全回退，不崩溃不污染；本轮新增五类坐标转换失败故障注入矩阵与跨坐标空间数据混用缺陷修复，显示点空间与 CRS 显式入状态、反算按显示点自身空间判定，拖动反算失败恢复原轮廓（`tests/integration/test_p3_interactive_polygon_roi.py` 9 项测试全绿）。 | 复杂自相交多边形与拓扑重叠高级几何关系待后续专题完善。 | implemented |

## 退出异常 0xC0000409 诊断（第 21 轮）

### 现象与失败轮/成功轮证据（两者均保留，不以最后一次成功覆盖）

`work/test_runs/test_all.log`（已归档至 `docs/progress/runs/exit_anomaly/test_all_history.log`）完整保留了 9 轮全量运行：

| 时间 | 用例数 | 退出码 | 说明 |
|---|---|---|---|
| 11-04 19:25:19 | 256 passed | 0 | 成功 |
| 11-04 19:36:37 | 256 passed | 0 | 成功 |
| 11-04 20:00:24 | 265 passed | **3221226505 (0xC0000409)** | **失败轮** |
| 11-04 20:49:56 | 267 passed | 0 | 成功（与失败轮不同快照，其间有 50a139e 等提交） |
| 11-04 20:53:22 | 267 passed | 0 | 成功 |
| 11-04 21:16:40 | 272 passed | 0 | 成功 |
| 11-04 21:56:11 | 277 passed | 0 | 成功 |
| 11-04 22:39:14 | 283 passed | 0 | 成功 |
| 11-05 07:48:47 | 286 passed | **3221226505 (0xC0000409)** | **失败轮** |
| 11-05 07:56:16 | 286 passed | 0 | 成功（ce4e60d 提交于 07:58:51，07:48:47 与本轮均在其之前；**两轮是否字节完全一致未能确认**，见下） |
| 11-05 08:21:26 | 288 passed | 0 | 成功 |

关键结论：该异常**间歇性出现**——11 轮全量中 2 轮 rc=3221226505，其余轮次通过，
且其中一次「失败轮 07:48:47」与紧随其后的「成功轮 07:56:16」都发生在 ce4e60d
提交（07:58:51）之前。

**快照一致性说明（不夸大）**：两轮都在同一提交之前，只能说明「两轮之间没有新的
已提交变更」，**不足以单独证明两轮工作树字节完全一致**——期间仍可能有未提交
的本地改动，且当时未留存源码哈希或快照。因此「同一代码快照下随机出现」应降级为
「**未能完整确认快照一致性**」；可以确证的是：该异常非稳定复现，属间歇性崩溃。

### 崩溃现场（Windows 应用程序事件日志）

`Get-WinEvent -LogName Application -Id 1000` 给出确定证据：

- **11-05 07:50:53**：`ucrtbase.dll`，异常代码 `0xc0000409`，偏移 `0xa527e`。
  与 07:48:47 失败轮（elapsed 131.2s，结束约 07:50:58）时间精确吻合，确认该轮退出码即此崩溃。
- **11-04 22:31:50**：`Qt6Core.dll`，异常代码 `0xc0000409`（同一代码，不同模块）。
- **11-04 20:41:45**：`ucrtbase.dll`，同一偏移。

对 `ucrtbase.dll` 反汇编核对：偏移 `0xa527e` 处的字节为 `cd 29`，即 x86-64 的
`int 0x29`（`__fastfail`）指令 —— 该中断向量号对应 x64 上的快速失败机制，
CRT 检测到栈状态异常后立即终止进程，这正是 `STATUS_STACK_BUFFER_OVERRUN`
的产生方式。**注意**：该字节序列本身只表明「此处调用了 `__fastfail`」，
不能单独证明崩溃的具体根因（更不能直接指向 Qt 析构）；具体失败码与调用点
需符号与栈回溯才能确定。

### 定位到的实际原因

两条现场都发生在**解释器退出阶段**（pytest 已输出 `passed` 之后）。
实测证据：`win.deleteLater()` + `qapp.processEvents()` **并未真正销毁窗口**
（`work/probe_deferred_delete.py`：即使追加 `sendPostedEvents(DeferredDelete)`，
窗口仍然存活）。原因是 `processEvents()` 默认不处理 DeferredDelete 事件，
窗口连同其持有的 `QtWorkerClient`（`parent=MainWindow` 的 QObject）堆积到
解释器收尾才集中析构，此时 QApplication 单例与其下属 QObject 的析构顺序不确定，
一旦 QWidget 在 QApplication 之后被销毁即命中未定义行为。

### 已实施的对策（非绕过）

以下三项都是「让清理真正发生并按确定顺序发生」，不是屏蔽退出码或跳过清理：

1. `tests/conftest.py` 新增 `pytest_sessionfinish` 钩子：在解释器退出前，
   关闭全部顶层控件 → 派发 DeferredDelete → `gc.collect()` 回收 Python 引用 →
   `app.quit()`，使 Qt 对象析构顺序确定。
2. `src/spectra_workbench/scheduler/client.py`：新增 `_closed` 标志，
   `stop()`/`kill()` 后不再向已销毁的 Qt 对象派发信号
   （覆盖 `_on_worker_envelope`、`call_async`、probe/read 三个派发通道）。
3. `src/spectra_workbench/ui/main_window.py`：`closeEvent` 无条件回收请求线程，
   不再以 worker 子进程是否存活为前提（`client.is_alive` 只反映子进程状态）。
4. `tests/unit/test_p3_exit_lifecycle.py`：6 项回归断言上述行为。

### 证据强度与结论边界（如实记录，不夸大）

**已确证的事实**

- 崩溃发生在解释器退出阶段：pytest 已输出 `passed`，随后进程以
  0xC0000409 结束。
- 故障模块已由 Windows 事件日志定位：`ucrtbase.dll`（偏移 `0xa527e`，字节为
  `cd 29`，即 x64 的 `int 0x29` / `__fastfail` 指令）与 `Qt6Core.dll`；
  07:50:53 的事件与 07:48:47 失败轮（elapsed 131.2s）时间精确吻合。
  该指令序列只能说明「此处触发了快速失败」，不能单独证明具体根因。
- 间歇性出现：11 轮全量中 2 轮 rc=3221226505。07:48:47 失败轮与 07:56:16 成功轮
  都发生在 ce4e60d 提交（07:58:51）之前，但**两轮工作树是否字节完全一致未能确认**
  （当时无源码哈希/快照留存），故「同一快照」仅为推断而非已确证事实。
- `win.deleteLater()` + `qapp.processEvents()` 并未真正销毁窗口
  （`work/probe_deferred_delete.py` 实测）。

**对照实验结果（关键，不可省略）**

| 组别 | 轮次 | 结果 |
|---|---|---|
| 修复后（含 conftest 有序销毁） | 6 轮全量 | 全部 `295 passed`、rc=0 |
| 对照组（临时禁用 conftest 钩子） | 7 轮全量 | 全部 `295 passed`、rc=0 |
| 定向复现器（窗口/worker 两种场景各 8+6 次） | 28 次独立进程 | 全部 rc=0，未复现 |

**结论**：修复后与对照组均未再复现，说明**该崩溃窗口很窄**
（历史 11 轮全量中仅 2 轮触发，约 18%）。当前证据**不足以证明**
上述三项对策中的任何一项（或其组合）就是根因修复——对照组同样 7 轮全绿。
因此本轮**不宣称退出异常已被根治**，只如实记录：

1. 崩溃的阶段（解释器退出）、故障模块（ucrtbase.dll `__fastfail` / Qt6Core.dll）
   与「同一快照随机出现」这三点已确证；
2. 已实施的清理加固在语义上正确、可回归、有测试覆盖，且未引入任何绕过手段；
3. 底层根因仍未确证——本机缺少符号工具（dumpbin / llvm-symbolizer 均缺失），
   无法把 `ucrtbase.dll+0xa527e` 映射到具体函数；定向复现器也未能触发。
4. **遗留缺口**：需要更高频的复现手段（长期多轮 CI 或专用最小复现脚本）
   才能做统计意义上的归因。若再次出现，须同时保留 Windows 事件日志
   （`Get-WinEvent -LogName Application -Id 1000`）与完整全量日志。

### 诊断中发现并修复的另一处真实缺陷：worker 并发启动竞态

在排查退出异常时，另一次全量运行暴露出**真实用例失败**：
`test_interactive_polygon_roi_layer_switching_and_isolation` 报
「等待图层超时：期望 2 个，实际 0 个」。单独复跑通过，全量负载下偶发。

根因定位（`src/spectra_workbench/scheduler/client.py`）：`start()` 没有任何锁保护，
而 `probe_dataset_async` / `read_window` / `call_async` 的后台线程都执行
`if not self.is_alive: self.start()`。多个请求线程可同时看到 worker 未启动而并发
调用 `start()`，**后一次会覆盖前一次的子进程句柄**：前一个 worker 进程泄漏，
它的请求永远等不到响应——界面表现正是「图层加载不出来 / 0 个图层」。

修复：`start()` 用 `_start_lock` 串行化，并在锁内二次检查 `is_alive`，
确保并发调用复用同一个 worker 而非重复拉起子进程。
回归断言见 `tests/unit/test_p3_exit_lifecycle.py::test_concurrent_start_launches_single_worker`
与 `::test_start_is_serialized_by_lock`。

该缺陷与退出异常同属「worker 生命周期管理」范畴，但不是同一个根因：
它发生在运行期（请求丢失），退出异常发生在解释器收尾阶段。

### P3 原验收清单逐句核对（2026-11-05 第 25 轮）

对照 `docs/plan/02_里程碑与验收.md` 的 P3 原验收标准逐分句核对。
**状态含义**：已满足 = 有可执行验收证据；待验 = 实现存在但缺直接断言，或需真实课程数据终判。
按 AGENTS.md 第 9 条，`implemented` 状态本身不等于验收通过，下表只陈述证据事实。

**P3-01 主窗口、Dock、项目树、图层模型**
「基础项目可保存重开源/样式/视图；布局可恢复；关闭图层释放文件；中文高 DPI 可用」

| 分句 | 状态 | 证据 |
|---|---|---|
| 基础项目可保存重开源/样式/视图 | 已满足 | `test_p3_viewing_flow.py::test_project_saves_order_visibility_display_and_view`；`test_p3_persistence_regression.py`（19 项，含保存重开与目标防同名劫持） |
| 布局可恢复 | 已满足 | `test_p3_dock_layout_persistence.py`（2 项：浮动/显隐/停靠区域重排跨会话持久化与重开恢复） |
| 关闭图层释放文件 | 已满足 | `test_p3_viewing_flow.py::test_closing_layer_releases_file_handle` |
| 中文高 DPI 可用 | **部分已满足 / 高 DPI 部分待验** | 中文字体族可用、`ensure_application_fonts` 返回可用字体数、主窗口与子控件布局几何非负且非零——这三项已有直接断言（`test_p3_acceptance_gaps.py`）。**但实际高 DPI 缩放下的渲染与可读性仍待验**：原测试只在已创建的 QApplication 之后调整窗口尺寸，既未设置也未测量实际缩放因子，不能据此认定高 DPI 已验收；已新增 `test_gui_exposes_actual_device_pixel_ratio` 证明缩放因子可被测量，作为后续在已知缩放环境（独立进程 + 明确 DPI）下补证的手段 |

**P3-02 栅格/矢量/子数据集的元数据读取**
「数据类型、NoData、mask、单位、波段与空间模型准确；不完整数据明确报错」

| 分句 | 状态 | 证据 |
|---|---|---|
| 数据类型 / NoData | 已满足 | `probe_dataset` 返回 `dtypes` 与逐波段 `nodata`；`test_p3_query_stats.py`（19 项） |
| mask | 已满足 | 逐波段 `mask_flags` / `all_valid` / `has_nodata_flag` / `has_alpha`；`test_p3_viewing_flow.py::test_invalid_pixels_are_transparent_valid_zero_is_not` |
| 单位 | 已满足 | 逐波段 `unit`；`test_p3_viewing_flow.py::test_hover_query_shows_raw_physical_and_status` |
| 波段 | 已满足 | `bands` 列表含 dtype/nodata/description/overviews/scale/offset |
| 空间模型 | 已满足 | `spatial_model` 区分 `affine` / `gcp` / `none`，另有 `gcp_count` / `gcp_crs_wkt`；无 GT 栅格有专门样例 |
| 不完整数据明确报错 | 已满足 | `probe_failed` 错误 Schema；缺失矢量与缺失子数据集的明确错误见 `test_p3_vector_subdataset_metadata.py`（10 项） |
| 矢量与子数据集元数据 | 已满足（第 22–24 轮） | `probe_vector` / `list_subdatasets` / `probe_subdataset`；四角仿射范围、要素数 `force=0`、图层类型路由、跨图层与同容器请求上下文隔离均已有回归 |

**P3-03 瓦片请求、概览、缓存、过期请求处理**
「只读可见窗口；切换波段不出现旧图；内存增长受缓存预算限制」

| 分句 | 状态 | 证据 |
|---|---|---|
| 只读可见窗口 | 已满足 | `read_window` 协议按可见窗口分块读取（最大 1024×1024），严禁整幅读入 |
| 切换波段不出现旧图 | 已满足（第 25 轮补齐） | 第 25 轮新增 `test_p3_acceptance_gaps.py::test_switching_band_does_not_show_previous_band`：可控客户端注入旧波段迟到响应，断言画布**不渲染**旧内容（`img_item.image is None`），最终显示为新波段；另 `::test_switching_band_invalidates_pending_response` 断言代际推进 |
| 内存增长受缓存预算限制 | 已满足 | `test_p3_viewing_flow.py::test_cache_budget_is_enforced_across_many_views`；真实 GUI 6144×6144 大影像实测 LRU 逐出 9 次（`docs/progress/runs/p3_gui_memory.json`，`test_p3_real_gui_memory.py`） |

**P3-04 灰度/RGB/伪彩、拉伸、透明度与分屏**
「显示改变不改原始像元；抽样统计有标识；跨视图同步模式正确」

| 分句 | 状态 | 证据 |
|---|---|---|
| 显示改变不改原始像元 | 已满足 | `test_p3_viewing_flow.py::test_display_changes_never_alter_source_pixels`（灰度/伪彩/RGB 三种模式切换后源像元不变） |
| 抽样统计有标识 | 已满足 | `test_p3_viewing_flow.py::test_stats_buttons_label_sampled_and_full`；`test_p3_query_stats.py::test_stats_sampled_is_labelled_and_full_is_exact`（抽样标 `mode=sampled`/`source=sampled_grid`/`stride>1`） |
| 跨视图同步模式正确 | 已满足 | `test_p3_viewing_flow.py::test_dual_view_sync_and_release`（双视图视图状态联动与资源释放） |
| 拉伸区分 | 已满足 | `test_p3_viewing_flow.py::test_percentile_and_minmax_bounds_differ_on_spike` |

**P3-05 view CRS、重投影、查询与 ROI**
「旋转 affine、不同 CRS、无 CRS 视图、像元中心/角点均有样例」

| 分句 | 状态 | 证据 |
|---|---|---|
| 旋转 affine | 已满足 | `test_p3_query_stats.py`（19 项，含旋转 Affine 样例）；矩形四角在旋转仿射下精确断言 4 个地图角点与 425.0 平方单位覆盖面积 |
| 不同 CRS | 已满足 | `test_p3_viewing_flow.py::test_view_crs_switch_uses_worker_wkt`；跨 CRS 视图重投影网格读取 |
| 无 CRS 视图 | 已满足 | 无 GT 栅格样例（`p3_nogt.tif`）与无 CRS 图层样例；`test_p3_interactive_polygon_roi.py::test_no_crs_and_no_gt_refuses_map_conversion_and_stale_generation_dropped` |
| 像元中心/角点 | 已满足 | `map_to_pixel` 支持 `center` / `corner` 两种坐标约定，均有样例 |
| ROI 完整流程 | 已满足 | 真实鼠标事件多边形 ROI 绘制/闭合/选中/手柄拖拽/删除/保存重开（`test_p3_interactive_polygon_roi.py`，9 项） |

**P3 里程碑退出条件**
原退出条件：「可打开已有 FFT 包中的 ENVI 栅格与 lena.tif，查看多波段影像、像元数值、ROI 和对比；
若课程路径不存在，用合成影像完成同类验收并保留真实数据待验状态。」

| 分项 | 状态 | 说明 |
|---|---|---|
| 用合成影像完成同类验收 | 已满足 | 合成 ENVI/GTiff/GeoJSON/netCDF 样例覆盖多波段、像元数值、ROI 与对比 |
| 保留真实数据待验状态 | **待验** | 课程原始数据位于 `C:\Users\赵航\Desktop\studio\_chaoxing_rs`（**只读**，不得运行其中的抓取/下载/OCR 脚本）；本轮全部用合成影像验收，未对课程原始 ENVI 栅格与 lena.tif 做真实打开验证 |

### 第 69 轮：P8-01 多维数据树与二维切片（P8-01 → done）

**一、运行时探针（先探测后实现，实测入实现）**

- GDAL 3.13.3 netCDF 驱动 `DCAP_MULTIDIM_RASTER=YES`；`CreateMultiDimensional` 需
  「chdir 到目录 + 相对文件名」（P3-02 定档限制完全复现，读取不受限）；
  `_FillValue` 必须在数据写入前创建；python 3.13 包装用 `ExtendedDataType.Create`
  而非 ArrayDataType；切片走 `GetView('[1,0,:,:]')` → dims [y,x]。

**二、实现**

- `providers/gdal_provider/mdim_ops.py`（**仅 worker 进程 import**；`_gdal()` 惰性模式）：
  - `build_multidim_tree(path)`：递归 group（attrs/dims 含 has_coordinate_array/arrays/子组）；
    数组节点 dims/shape/**unit/scale/offset/nodata（无值 None 不编造）/srs_wkt**；
  - `run_multidim_slice(in_path, array, dim_select, staged_path)`：钉死校验（数量恰
    rank-2、未知维、越界、非整数索引全部显式拒绝）→ `GetView` 取剩余两维原序 →
    GTiff raw 值 + band SetUnitType/SetScale/SetOffset/SetNoData/SetSpatialRef（有则用）→
    sidecar `<name>.mdim_slice.json`（selected 含坐标值/remaining_dims/coords/
    unit/scale/offset/nodata/georeferenced/value_semantics=raw）。
- 查询通道：`entrypoint._SIMPLE_CALLS["probe_multidim"]` → adapter `probe_multidim`
  → 查询面与 P3-02 probe_* 同型；`workers/process.py`、`scheduler/client.py` 同步封装。
- 处理能力接 adapter：SUPPORTED_CAPABILITIES + 分派 job（参数缺失/形态错 →
  invalid_parameters；mdim 错误 → mdim_error）+ staged_files sidecar 收集。
- 注册面（P6-01 七件套模式）：registry `gdal:mdim_slice`（limitations 5 条）；
  capability_wiring CONNECTED_CAPABILITIES；raster_params 别名/SUPPORT/
  `_normalize_dim_select`（k=v 文本 + dict 双形态、严格整型拒绝 1.5）；GUI param_form
  新增 array/dimSelect 行 + 可见性/重置/collect 必填校验。

**三、验收（真实 worker，独立引擎重开断言）**

- 数据树：dims {time:2,depth:2,y:2,x:3}、has_coordinate_array 全 true、
  temp Kelvin/0.5/1.0、temp 无参考如实 None、sst 带 WGS 84；
- 切片像元：temp|time=1,depth=0 → [[12,13,14],[15,16,17]]（=arange(24).reshape
  (2,2,2,3)[1][0]，与实现无关）；band scale/offset/unit 保留；无 CRS → 输出
  crs=None + georeferenced=false；
- CRS 保留：sst|time=0,depth=1 → [[1006..1011]] 且输出 crs_epsg=4326、unit=degC；
- 拒绝矩阵 7 例（array 缺失/未知数组/未知维/钉死数量不足/越界/小数索引/rank<3）
  全部不落盘。

**四、闸门**

- P8-01 4 项集成 + param/UI 靶向 43 项全绿；unit 对账 20 项全绿；
- 官方 `scripts/test.ps1 -All -TimeoutSeconds 1800`：Ruff PASS / mypy 88 文件 0 错误 /
  **812 passed / rc=0 / clean_exit_verified=True / residual=0 / cleanup_complete=True**。

**五、边界与结论**

- 验收「合成 netCDF/HDF5 的 time/depth/x/y、单位和切片像元正确」：netCDF/CF 容器
  全部口径通过——**P8-01 转 done**；账本现为 done 40 / implemented 2 / todo 13。
- 边界：本运行时 **HDF5 writer 缺失**（无容器级 HDF5 验收，待驱动补齐——已在
  registry limitations 与 STATUS 如实记录）；complex/bool 数组显式拒绝；
  GUI 多维树面板/交互预览归后续。

### 第 68 轮：P7-05 历史与可复现导出（P7-05 → done）

**一、实现**

- 新增 `processing/history.py`（纯 stdlib sqlite3 + hashlib，GUI 进程零原生依赖）：
  - `HistoryStore`：SQLite 单写入方；`meta.schema_version` 显式迁移（空库建 v1；旧版本自动升级；**更高未知版本拒绝打开**，绝不静默降级）；表 `runs / run_inputs / run_outputs`（计划 §12 的 task/attempt/artifact/engine_runtime/迁移列位）；
  - `record(spec_dict, result, kind, group_id, strong)`：TaskSpec 通道真实执行后登记——能力/任务号/attempt/终态/created_at/duration/项目根/descriptor_revision；**spec 引擎约束 + worker 上报的真实引擎版本**（`TaskResult.engine_runtime`）；完整 params JSON 与错误 JSON；输入（端口 → URI/kind + 指纹 size+mtime，strong 可选 SHA-256）与输出（key → URI/committed_files + 指纹）；
  - `reopen_params(run_id)`：从历史重开参数——capability/engine(provider/runtime/expected+actual)/params/descriptor_revision/inputs（URI+kind），可直接构造重新提交的 TaskSpec；
  - `export_workflow_recipe(run_ids, path)`：把历史导出为**标准 workflow 文档**（P7-04 `run_workflow` 为唯一执行入口）：capability/engine/params 原样回填；记录的 file:// URI 解码回本地路径（中文百分号往返监护）；**边自动合成**——输入 URI 命中他节点输出 URI → 改写 `[node, id, key, kind]`，保住 DAG 执行序。

- 验收测试 `tests/integration/test_p7_history.py`（3 项，真实双引擎）：
  1. `test_history_records_inputs_versions_and_reopen_params`：真实 gdal 翻译 → record（strong 含 sha256、真实版本 3.x）→ reopen_params 重提新输出 → 数值一致（2×2 [[1,2],[3,4]]）；
  2. `test_history_export_recipe_replays_cross_engine_workflow`：组记录 2 条（kind=workflow_node, group=wf-demo）→ 导出配方（rio 节点输入已合成 node 引用）→ 全新缓存目录回放 → 每能力引擎恰再调一次、final.tif 值 [[5,6],[7,8]] 与历史一致、mid.tif mtime 变化（真实重写，非读缓存）；
  3. `test_history_schema_migration_and_newer_version_refused`：建库/migrate-回-1/拒绝-99 三态。

**二、验证**

- P7-05 3 项 + P7-04 workflow 3 项 + P7-03 batch 5 项 + P7-01 migration 6 项 + P7-02 scripting 5 项 + P4 rasterio contract + P1-05 staging = **45 项靶向全绿**（21.42s）。
- Ruff 干净；Mypy 87 源文件 0 错误。

**三、全量闸门**

- `scripts/test.ps1 -All -TimeoutSeconds 1800` → Ruff PASS / mypy 87 文件 0 错误 /
  **pytest 808 passed / 0 failed / rc=0** /
  **clean_exit_verified=True, residual=0, cleanup_complete=True, cleanup_errors=0**。

**四、边界与结论**

- 验收「从历史重开参数；记录输入/版本/关键配置；导出的执行入口跑出同类成果」逐项满足——**P7-05 转 done**；
  账本现为 done 39 / implemented 2（P0-02, P6-06）/ todo 14，**P7 里程碑五项全部完成**。
- 遗留边界：SQLite 单写入方（多并发写入由调用方串行化，符合计划 §12「应用服务集中写入」）；GUI 历史面板/重跑 UI、
  远程来源稳定标识指纹、增量落盘均归后续；指纹默认快速，strong 显式选择。



### 第 67 轮：P7-04 DAG 工作流（P7-04 → done）

**一、实现**

- 新增 `processing/workflow.py`（公共入口 `load_workflow_document` / `render_workflow_nodes` / `run_workflow` / `write_report`）：
  - 文档形态：`{schema_version, project_root, nodes:[{id, capability_id, engine_provider?, engine_runtime?/engine_version?, inputs, outputs, params, driver?, task_prefix?}]}`；节点唯一 id、provider 必须已登记（缺省 gdal→gdal-qgis/3.13.3，rasterio→rasterio-venv/1.5.2，可逐节点覆盖）；
  - **类型化端口**：输出 `{key: {uri, kind}}`（kind ∈ raster/vector，缺省 raster）；输入 `{port: ["literal", path] | ["node", node_id, output_key, expected_kind?]}`（期望 kind 缺省 raster）；
  - **提交前校验**（计划 §11）：Kahn 环检测（环成员精确列出）；端口错配/未知上游节点/输出键拒绝；输出 URI 冲突与「输出=他人字面输入」写穿拒绝；按引擎节点计数汇总资源（不承诺未实现的估算数值）；
  - **执行**：拓扑序严格串行；每节点构造 TaskSpec（与批处理/菜单共用注册表 + provider adapter 的唯一计算通道）；`execute` 返回 `TaskStatus | TaskResult`（TaskResult.error 详情转写报告，不吞引擎错误）；
  - **缓存（恢复 + 失效）**：node_key = SHA-256(引擎约束+能力+完整参数+输入引用+输出键)；输入指纹 literal 现算、node 引用取上游产物指纹；输出指纹 fast=size+mtime_ns / strong=sha256（记录复现等级）；node_key/输入/输出指纹全部不变且文件仍在 → `cached`（不执行 worker），任一变化 → 失效重跑；上一轮失败节点保留 failed，修复后重跑；`workflow-cache.json` 每轮整份覆写；
  - **失败传播**：节点引擎失败 → 直接下游显式 `pending`（error=skipped + 上游名），独立分支按 `continue_on_error` 继续（True 继续执行；False 首败后全部剩余显式 pending，与批处理同语义）。

- 验收测试 `tests/integration/test_p7_workflow.py`（3 项）：
  1. 提交前拒绝矩阵 6 样例（环×2 节点、端口错配 vector→raster、ghost 节点、错误输出键、同 URI 双输出、写穿冲突）；
  2. 跨引擎物化 gdal:translate → rasterio:convert：数值独立重开断言 final.tif（2×2、GT、[[1,2],[3,4]]）；无变化重跑双节点 cached 且引擎调用差值 0；
  3. 失败/恢复/缓存失效：corrupt.tif 引擎级真实失败 → n_ds pending(skipped)、独立分支成功；修复后重跑（cached 差值空、ind.tif mtime 不变、下游数值与新产物一致）；上游 outputType Float32→Byte 重跑 → midB 整型、下游自动失效重物化 [[9,8],[7,6]]。

**二、顺带修复预置缺陷（先 probe 后修）**

- `work/probe_rio_overwrite.py` 真实 worker 复现：rasterio convert 对「已存在目标 + overwrite=True」误报
  `Target already exists and overwrite=false`（出自 `providers/base.py::commit_files` 守卫）。
- 归因：`providers/rasterio_provider/adapter.py` convert 通道 `commit_files([(staging_out, out_path)])`
  **未透传** `out_plan.overwrite`（gdal adapter 3493 行是正确写法）。工作流「缓存失效重跑」目标已存在是常态，
  恰好暴露该缺陷。
- 修复：`commit_files(..., overwrite=out_plan.overwrite)`，与 gdal 侧语义对齐；P4 rasterio contract 全绿背书。

**三、验证**

- P7-04 workflow 3 项 + P4 rasterio contract + P7-03 batch 5 + P7-01 migration 6 + P7-02 scripting 5 = **33 项靶向全绿**（20.34s）。
- Ruff 干净；Mypy 86 源文件 0 错误。

**四、全量闸门**

- `scripts/test.ps1 -All -TimeoutSeconds 1800` → Ruff PASS / mypy 86 文件 0 错误 /
  **pytest 805 passed / 0 failed / rc=0** /
  **clean_exit_verified=True, residual=0, cleanup_complete=True, cleanup_errors=0**。

**五、边界与结论**

- 验收「环/端口错配拒绝；失败、恢复、缓存失效和跨引擎物化通过」逐项满足——**P7-04 转 done**；
  账本现为 done 38 / implemented 2（P0-02, P6-06）/ todo 15。
- 遗留边界：同进程管线优化、调度器并行执行、GUI 工作流面板、远程来源稳定标识缓存（外库/URL）均属后续；
  strict 模式与批处理语义一致。

### 第 66 轮：P7-03 批处理（P7-03 → done）

**一、实现**

- 新增 `processing/batch.py`（四个公共入口）：
  - `load_batch_document`：清单 JSON 顶层必须对象且 `items` 非空列表；文件缺失/坏 JSON/结构错显式 `BatchError`（不静默降级）；
  - `render_batch_specs(template, items)`：把一份模板展开成 N 份 TaskSpec（返回 `(spec_dict, item, index)` 序列）。模板必填 `capability_id`，缺省 `engine_provider="gdal"`（提供则须非空）。`inputs`/`outputs` 为路径模板列表，逐 item 以 `{item[key]}` 展开——未知模板键抛 `BatchError`（绝不展开成空串）；单输入挂 `inputs["input"]=DatasetRef`、多输入为 `list[DatasetRef]`；单输出 key=`output`、多输出依次 `output_0/output_1`/…（每个 OutputPlan 统一 `kind=raster`）。`overwrite` 模板字段透传到每个 OutputPlan.overwrite；`params` 值为字符串时同经 `{item[key]}` 展开、非字符串原样透传（参数变化语义）。`workspace` 取输出父目录（输出/输入的父目录，保证 commit_files 运行目录约定）；`project_root` 必填；TaskSpec.model_validate 提前验证并在错误处暴露真实原因。
  - `run_batch(template, items, execute, *, continue_on_error=True, batch_id=None)`：严格串行按序执行；`execute: dict -> TaskStatus` 是真实 runner 约定（判断终态用 `is TaskStatus.succeeded` 而非 isinstance）。`continue_on_error=True`：失败继续下一条并如实记录 succeeded/failed/cancelled + duration；`False`：遇到首个失败后，余下条目逐条显式 `pending`（error=skipped + 原文说明），`all_terminal=False`——**不冒充成功也不伪装完结**。
  - `BatchReport`：`entries`（index/task_id/status/duration/error）+ `summary()` 计数 + `all_terminal()` 审计 + `to_dict()`；`write_manifest(path, payload)` 把 batch manifest JSON 落盘（UTF-8 无 BOM、ensure_ascii=False，审计或人工重放均可直接读取）。

- 验收测试 `tests/integration/test_p7_batch_worker.py`（5 项，全部真实 QGIS GDAL 3.44.15 worker 端到端）：
  1. `test_render_batch_specs_supports_multi_io_and_params`：双输入双输出模板逐 item 展开断言（`in_a_0.tif/in_b_0.tif`、`output_0/output_1` key、`overlapPriority=first/last`）；
  2. `test_render_batch_specs_unknown_template_key_rejects`：未知模板键显式 BatchError；
  3. `test_batch_continue_on_error_keeps_remaining`：第 0 条输入缺失 → failed，第 1 条有输入 → 真实 succeeded（out_1.tif 真实产出）+ manifest 落盘可审计；
  4. `test_batch_stop_on_first_failure`：`continue_on_error=False` 第 0 条 failed 后第 1/2 条显式 pending（failed=1/pending=2/all_terminal=False）；
  5. `test_batch_overwrite_semantics_seen_by_worker`：预置 `ow_out_0.tif` 为 stub，`overwrite=True` 经 OutputPlan→P1 commit_files 真实覆盖（文件字节 ≠ stub），2 条全部 succeeded。

**二、批处理执行实测（真实 worker）**

- 每条目独立 QtWorkerClient（支出：start→submit→sig_result→stop 全链路）真实执行 `gdal:translate`，strict 批处理第 1/2 条未被触达时保持 pending（不写入任何输出文件）。
- run_batch 内条目失败/成功/被跳过均写入 BatchReport；manifest `has-ok.json` 断言 batch_id、2 条 entries 的逐条终态（failed/succeeded），可直接作为复现审计。

**三、验证**

- 新增 5 项全绿（4.01–5.35s）；受影响靶向回归（P6-06 radiometric 4 项、P7-01 migration 6 项、P7-02 scripting 5 项、scheduler/staging/worker 状态机 41 项）全绿。
- Ruff 干净；Mypy 85 源文件 0 错误。

**四、全量闸门与顺带修复（重要）**

- 第一次 `scripts/test.ps1 -All -TimeoutSeconds 1800` 失败（exit 90 = clean_exit_verified=false）：pytest 802 passed 全绿但 job 观察到 **4 个残留 gdal worker 子进程**。
- 诊断（临时 `work/worker_track.py` pytest 插件，追踪 WorkerProcess init/start/stop/kill 及归属测试，work/ 下不入 git）：
  - 全量运行 4 个 started-但-unstopped 的 gdal worker 全部源出自 `MainWindow` 构造期创建的 auto-id client（`self.attribute_panel.set_client(self.gdal_client)` 懒建，自第 54 轮 P5-03 起）；
  - 测试夹具（P3/P4 系列）以 `win._clients["gdal"] = Spy` 换槽后真实对象由 attribute_panel 继续引用；MainWindow.closeEvent 只回收 `_clients` 现值 → 真实 client 的 stop() 永不执行；其子进程与 stdout/stderr reader 线程撑到解释器退出。
- 修复（最小架构，不引入测试专用逻辑）：`MainWindow.__init__` 新增 `self._owned_clients: list[QtWorkerClient]`；`_get_client` 创建即登记；`closeEvent` 在停 `_clients` 现值之后**再遍历 `_owned_clients` 全量 stop(timeout=1.0)** 且 `clear()`（生产路径 _clients 与 _owned_clients 同集，语义不变；换槽场景下实现「父窗口必须回收自己spawned的全部子进程」的硬保证，对齐 AGENTS「子进程清理要有 Windows 实测证据」）。
- 复测：全量 802 passed 后 worker_track unstopped 仅剩 2 个**故意不启动**的生命周期守卫用例（del-guard-test / hardening-join，无子进程， strands 为预期）；正式闸门 `scripts/test.ps1 -All -TimeoutSeconds 1800` → **ruff PASS / mypy 85 文件 0 错误 / pytest 802 passed / clean_exit_verified=True / residual=0 / cleanup_complete=True**。

**五、边界与结论**

- 验收「多输入、多输出、参数变化、同名冲突、单项失败继续策略通过」逐项满足——**P7-03 转 done**；账本现为 done 37 / implemented 2（P0-02, P6-06）/ todo 16。
- 限制：批处理严格串行（并行化可能引发输出侧写冲突，属后续）；模板只承接 `gdal:*`/registry 就绪能力，`platform:*` 外部引擎不在范围；GUI 批处理面板与历史重放集成归后续；manifest 由调用方决定是否落盘（模块只返回 dict 与可选写出函数）。

### 第 65 轮：P7-02 隔离脚本会话（P7-02 → done）

**一、实现**

- 新增 `scripting/session.py::ScriptSession`：按 engine 分别启动**真实**引擎解释器——gdal 通过 `app/doctor.resolve_gdal_runtime` 解析（QGIS/conda），rasterio 使用 `.venvs\sw-rasterio\Scripts\python.exe`；未知 engine 显式 `SessionError`。
- 用户脚本通过 `exec()` 在引擎环境内执行，可真实 `from osgeo import gdal` / `import rasterio`；**GUI 进程不 import osgeo / rasterio**（严格保持 P3 约束），且 bootstrap 只 stdlib。
- `stop()` 幂等（proc None / poll None → 0）；`run()` 后自动清理以便下次 run 使用全新进程；`is_alive` 仅在 proc 活时 True。
- `logs/stdout.log` / `logs/stderr.log` 写会话目录，每次 run 前清空（不残留旧日志）。
- `register_artifact(path)` 显式登记成果（`artifacts.json` 含 path / sha256 / size / mtime / registered_at），**不**依赖自动 glob；`session.json` 记录 engine / python_path / started_at。
- 同 session_dir 再 `run` → 全新 interpreter（产物存进同一目录）。
- `scripting/__main__.py` 提供 CLI 入口：`python -m spectra_workbench.scripting <engine> <session_dir> <script.py>`。

**二、验收（真实引擎解释器子进程）**

- gdal 会话：`from osgeo import gdal` → Create 3×2 Float32 GTiff + GT/CRS/NoData，独立重开断言尺寸；产出真实 GTiff。
- rasterio 会话：`import rasterio` → open 上述 GTiff；shape/count 校验；`register_artifact` 登记 GDAL 会话产物（SHA-256 精确比对）。
- stop/restart：同 session_dir 内先 run 『hello-1』再 run 『hello-2』，stdout.log 仅为 hello-2（旧日志不残留）。
- 空 script / 非 gdal/rasterio engine → `SessionError` 明确拒绝。

**三、验证**

- 新增 `tests/integration/test_p7_scripting_sessions.py`（5 项）；P7-01 migration / P3-01 regression 回归全绿。
- Ruff 干净；Mypy 84 源文件 0 错误。

**四、边界与结论**

- 验收「GDAL/Rasterio 会话分别导入原生 API；停止、日志、成果登记和会话重启可用」逐项满足——**P7-02 转 done**；账本现为 done 36 / implemented 2（P0-02, P6-06）/ todo 17。
- 遗留边界：会话**非线程安全**（调用方自行保证串行）；`scipy` 是未注册的 engine（SessionError），需要时单独扩充
engine 集合并新增对应测试。

### 第 64 轮：P7-01 项目迁移与磁盘安全（P7-01 → done）

**一、实现**

- 新增 `project/migrate.py`：legacy 0.9 → 当前 1.0 的**显式迁移器**；`layers[].src/label/metadata` → `source/name/metadata_snapshot`，`comment` → `notes` 前缀「[legacy 0.9 迁移]」显式可审计；未知字段、未知版本（0.5/9.9）**显式拒绝**；`path_anchor=None` 保留走 P3-01 legacy 确认流程。
- `load_project` 接入迁移器：读到 legacy 0.9 文档即翻译为 1.0，保持 Windows `\` → `/` 规范化、图层顺序与 `source_is_relative == not looks_absolute` 语义。
- **异常退出不破坏上次保存**：真实子进程 `os._exit(-1)` 在写盘段模拟崩溃 → 上次保存字节原样保留；`monkeypatch os.replace` 模拟「替换阶段失败」 → 原文件原样保留、无残留 `.tmp`（原子事务语义正确）。
- ROI 字段原样保留并通过 `domain.ROI` 校验（rectangle/pixel）。

**二、验收（独立 GDAL 重开）**

- legacy 0.9 双层翻译与 1.0 语义严格一致；未知字段/版本显式拒绝。
- `os._exit(-1)` 子进程崩溃：save 前后字节完全一致；monkeypatch `os.replace` → 原文件完整、临时文件被清理。
- ROI 字段可加载为 `domain.ROI.rectangle/pixel`。

**三、验证**

- 新增 `tests/integration/test_p7_project_migration.py`（6 项）；受影响靶向（P3-01 + P7-01）25 项全绿。
- Ruff 干净；Mypy 82 个源文件 0 错误。

**四、边界与结论**

- 验收「移动项目后重新关联；旧 schema 可迁移；异常退出不破坏上次保存」逐项满足（移动/重定位 由 P3-01 既有证据覆盖，本 round 新增旧迁移与 crash 安全）——**P7-01 转 done**；账本现为 done 35 / implemented 2（P0-02, P6-06）/ todo 18。
- 遗留边界：`P7-02~05` 批处理与历史复现等归后续。

### 第 63 轮：P6-06 辐射定标 / DOS1 / 去云（P6-06 → implemented；2 子项 blocked）

**一、实现**

- 新增 `providers/gdal_provider/radiometric_ops.py` → **`gdal:calibrate`**：`physical=DN×gain+offset`（逐波段 gains/offsets）与 `toa_reflectance=π·L·d²/(ESUN·cos θz)`（逐波段 ESUN + sunZenAngle∈[0,180)）；NoData/复数输入拒绝；sidecar `<成果名>.calibrate.json` 记录 mode/spec_kind/formula/spectral_type/sun 几何与每波段 gain/offset/ESUN；波段 metadata 写入 `CALIB_GAIN`/`CALIB_OFFSET`/`CALIB_ESUN`。
- 新增 `providers/gdal_provider/dos_ops.py` → **`gdal:dos1`**（Chavez 1988 暗目标减法）：显式 `hazeDN`/`dosEsun`/`sunZenAngle`（(0,90)）/`dosGain`/`dosOffset`/`earthSunDistance`；`ρ=π·(L−L_1haze)·d²/(ESUN·cos θz)`，`L_1haze=gain·hazeDN+offset−0.01·ESUN·cosθz/(π·d²)`；sidecar `<成果名>.dos1.json` 记录 Chavez 1988 完整引用、暗对象项、`one_percent_radiance`、n_valid。
- 新增 `providers/gdal_provider/cloudreplace_ops.py` → **`gdal:cloudreplace`**：primary + secondary + cloudMaskFile（1=云），`bandList` 可选、`maskDilatePixels`（≥0，3×3 8 邻域膨胀 N 次）；两时相必须同尺寸/波段/GeoTransform/CRS；晴 → primary、云 → secondary；sidecar `<成果名>.cloudreplace.json` 记录 primary/secondary_time、`pixel_source_rule` 与「云下来的值来自 secondaryTime=…，不是『恢复了原时刻真实地表』」。
- 新增 2 项显式 blocked 登记：`platform:flaash_external`（FLAASH 外部引擎适配，绝不以简化公式宣称实现，P8 插件协议接入）与 `platform:agriculture_count`（方法未确定，需保留直径/尺度/阈值/重叠 4 项参数与人工标注对照）。
- 契约/接线：新增 `calibrateMode`/`calibrateGains`/`calibrateOffsets`/`calibrateEsun`/`sunZenAngle`/`dosBand`/`hazeDN`/`dosEsun`/`dosGain`/`dosOffset`/`earthSunDistance`/`secondaryFile`/`cloudMaskFile`/`bandList`/`primaryTime`/`secondaryTime`/`maskDilatePixels` 别名与支持矩阵；adapter 新增 3 能力门禁（Float32/1 或 n 波段/必设 NoData）、执行分支与 sidecar 成套提交（`.calibrate.json`/`.dos1.json`/`.cloudreplace.json`）；GUI 新增定标模式/每波段 gain/offset/Esun/sunZenAngle 与 DOS1 全套、去云 primaryTime/secondaryTime/dilate 控件及状态隔离。

**二、数值验收（独立 GDAL 重开）**

- 定标 physical：2×2 DN × gain=0.1, offset=-2.0 → `[[-2,0.5],[3,8]]` 逐像元；定标 toa_reflectance：与独立推导 rel≤1e-6；sidecar mode/spec_kind/sun 几何逐项一致。
- DOS1：2×2 数组 hazeDN=2、ESUN=1500、θz=30°、gain=0.1、offset=-2、d=1 → `ρ` 与独立推导逐像元一致；NoData 像元排除 n_valid=3；`one_percent_radiance` 与独立计算一致。
- 去云：2×2 一期 cloud=1、二期 `[5,15]` 替换 `[10,20]`，`n_cloud=1/n_clean=3`；`maskDilatePixels=1` → 3×3 全膨胀 n_cloud=4 且输出=secondary；`maskDilatePixels=0` 合法（不膨胀）；primary_time/secondary_time 逐项写入 sidecar。
- 拒绝路径：physical 缺 gains、toa 缺 esun/sunZenAngle、esun 数量错、dos1 缺必填、cloudreplace 缺 secondary/mask 全部明确拒绝且不落盘。

**三、验证**

- 新增 `tests/integration/test_p6_radiometric_cloud.py`（4 项）；四维审计/对账/算法计数同步为 **461/454/249/23**、算法规格 31→36（3 个 gdal:* + 2 个 platform:*）、GDAL 编目 415→420、missing 5→7。
- 受影响靶向：56 项 P6-04/05/06 + sidecar/表单/能力边界全绿；298 项单测全绿。
- Ruff 干净；Mypy 81 个源文件 0 错误； pymy 不再出现；本测试中 reg.audit_report()、gdal_rep 等计数与 782+ 项全量测试共同验证。
- 修复 `tests/integration/test_p3_viewing_flow.py::test_layer_order_follows_open_request_not_probe_arrival` 的 `ReverseProbeClient` 替身缺失 `probe_vector_async` / `list_subdatasets_async` 的预存缺陷（只修替身，不改动被测代码）。

**四、边界与结论**

- 验收「每项有方法、依赖与真实证据；缺少输入/引擎时如实标注待验，继续其他项」：辐射定标 / DOS1 / 多时相去云三项已实现且数值验收，FLAASH 与农业计数显式 blocked——**P6-06 转 implemented（不转 done）**，blocking_reason 记录 2 个 blocked 子项；账本现为 done 34 / implemented 2（P0-02, P6-06）/ todo 19。
- 遗留边界：FLAASH 外部引擎适配、农业计数（方法未落实）、热红外亮温（BT，K）、监督式 ROI 分类（scikit-learn）均为真实待验项，显式登记且不提供假入口。

### 第 62 轮：P6-05 掩膜与 FVC 完整流程（P6-05 → done）

**一、实现**

- 新增 `providers/gdal_provider/mask_ops.py`：按波段闭区间阈值 `low<=value<=high`（可单边；两者都不给时为全体有效像元）生成 **1/0 Byte 二值掩膜**，源 NoData/非有限值排除，`maskInvert` 只在有效源像元内翻转（源无效像元恒为 0）；sidecar `<成果名>.mask.json` 记录波段/阈值/取反/`n_true`/`n_false` 与「1=真，0=假，未设 NoData」语义，可直接作为 FVC 的 `maskFile`。
- 新增 `providers/gdal_provider/fvc_ops.py`：输入 NDVI（可由 `gdal:sindex` 生成）→ 端元模式 **percentile**（缺省 2%/98%，`np.percentile` 独立核算）或 **values**（显式 `fvcSoil`/`fvcVeg`，契约强制 `soil<veg`、`low<high`）→ `fvc=(ndvi-soil)/(veg-soil)`；**无效区策略显式**：源 NoData、`maskFile`（非零=有效）、复数输入明确拒绝；**越界质量标记**：`n_below_soil`/`n_above_veg` 恒在 sidecar，`crop=true`（默认）才夹到 [0,1]——裁剪是显式选项；**分级**：`fvcClassEdges` 严格递增且 (0,1) 内（缺省 0.25/0.5/0.75），区间 `[0,e1)/[e1,e2)/…/[ek,1)`，输出类别栅格 `<成果名>.fvc_class.tif`（Byte，NoData=0 表示无效区）随事务提交；sidecar `<成果名>.fvc.json` 记录端元来源/requested 百分位/公式/像元面积（`|gt[1]*gt[5]-gt[2]*gt[4]|`）/分级统计（每类 count 与 pixel_area）/observed 范围，支持独立核算。
- 契约/接线：新增 `maskBand`/`maskLow`/`maskHigh`/`maskInvert`/`fvcBand`/`endmemberMode`/`lowPercentile`/`highPercentile`/`fvcSoil`/`fvcVeg`/`fvcClassEdges`/`maskFile`/`crop` 别名与支持矩阵（含 low<high、soil<veg、edges 严格递增 (0,1) 的契约级校验）；adapter 新增两能力格式门禁（mask Byte/1/无 NoData；fvc Float32/1 主成果/必设 NoData）与执行分支/sidecar 成套提交（`.mask.json`/`.fvc.json`/`.fvc_class.tif`）；registry 新增两项平台能力编目；GUI 新增掩膜波段/上下限/取反与 FVC 波段/端元模式/分位/显式端元/分级边界/掩膜文件/裁剪控件及状态隔离。

**二、数值验收（独立 GDAL 重开）**

- mask：阈值/取反逐像元与硬编码布尔一致；1/0 语义成立且 band nodata 为 `None`。
- FVC values 模式：fvc 与 `searchsorted` 分级逐像元一致；sidecar 端元/edges/像元面积 = 每类 count×100㎡ 逐项独立核算。
- FVC percentile 模式：端元与 numpy 独立计算 rel≤1e-6，n_valid=20；mask→fvc 串联 `n_valid == mask n_true` 且端元只对被掩膜像元求分位。
- crop=false：保留原始数值并报告越界（98% 分位在 20 像元内非最大值 → `n_above_veg` 与独立计数一致 >0）。
- 拒绝路径：values 缺 fvcSoil、edges 非递增、PNG（Float32 拒绝）、未知格式、maskBand 越界全部明确拒绝且不落盘。

**三、验证**

- 新增 `tests/unit/test_p6_mask_fvc_ops.py`（4 项）、`tests/integration/test_p6_mask_fvc.py`（5 项）、`test_p6_mask_fvc_gui.py`（2 项桌面路径）；四维审计/对账/算法计数同步为 **456/451/246/20**、算法规格 29→31、GDAL 编目 413→415。
- 受影响定向：298 项单测、52 项 P6-05/sidecar/表单/能力边界回归全绿。
- Ruff 干净；Mypy 78 个源文件 0 错误；Clean 全量：**782 passed / 0 failed / rc=0 / clean_exit_verified=true**（531.42s，residual=0、cleanup_complete=true）。

**四、边界与结论**

- 验收「ROI→分类/掩膜→分位端元→FVC→分级统计；独立验证与无效区策略清楚」以「阈值掩膜通道 + 分位/显式端元 FVC + 分级统计 + 独立核算 sidecar + 显式无效区策略」逐项满足——**P6-05 转 done**；账本现为 done 34 / implemented 1（P0-02）/ todo 20。
- 遗留边界：监督式 ROI 分类（训练 ROI、空间独立训练/验证划分、混淆矩阵、scikit-learn 算法）未实现且不提供假入口，归后续轮次；FVC 端元当前仅来自 NDVI 本身或显式值。

### 第 61 轮：P6-04 GCP、配准、差值与融合（P6-04 → done）

**一、实现**

- 新增 `providers/gdal_provider/gcp_ops.py`：GCP JSON（`points` 含 `pixel`/`line`/`x`/`y`，`role=control/check`）→ 控制点最小二乘拟合源像素→参考地图坐标（affine 1 阶 / polynomial2 2 阶）→ 单独拟合反变换（地图→源像素）→ 参考网格逐像元中心反变换 + nearest/bilinear 采样。拟合残差与独立检查点误差严格分列（地图单位/源像素单位），check 点绝不参与拟合；输出网格严格取参考影像；NoData/越界/邻域含 NoData 输出 NoData；sidecar `<成果名>.gcpregistration.json` 记录正/反系数、basis、控制/检查点 RMSE/max、输出网格与重采样器。
- 新增 `providers/gdal_provider/diff_ops.py`：`difference=A−B`、`absolute=|A−B|`；严格同宽高/同波段数/同 GeoTransform/同 CRS，不一致直接拒绝且不隐式重采样/重投影；逐波段 sidecar `<成果名>.rasterdiff.json` 记录 n_valid/min/max/mean/RMSE/mean_abs；NoData/非有限值逐波段排除。
- 新增 `providers/gdal_provider/fusion_ops.py`：**Brovey** `out_i = MS_i × Pan_scaled / sum(MS_1..3)`（分母≤0→NoData）；**HSV** RGB→HSV→用归一化 Pan 替换 V→RGB（H/S 保留）。仅登记 Brovey/HSV，精确命名并写公式/波段/量程/适用范围；NNDiffuse/Gram-Schmidt/PCA 明确未实现、不提供入口。要求 Pan 单波段、MS 3 波段、同 CRS、Pan north-up，MS 重采样到 Pan 网格；sidecar `<成果名>.pansharpen.json`。
- 契约/接线：新增 `gcpTransform`/`gcpFile`/`referenceFile`/`gcpResample`/`diffMode`/`msFile`/`msBands`/`fusionMethod`/`fusionResample` 别名与支持矩阵；adapter 新增三能力门禁/执行分支/sidecar 成套提交；registry 新增 gcpregister/rasterdiff，原 pansharpen 由 CLI 条目升级为 form 平台实现；GUI 新增 GCP 文件/参考影像/变换/重采样、差值参考影像/模式、MS 文件/3 波段/方法/重采样控件与状态隔离。

**二、数值验收（独立 GDAL 重开）**

- GCP 合成仿射：affine 与 polynomial2 的独立检查点 RMSE 均 ≤1e-8，输出与解析平面 ≤1e-3。
- GCP 合成二次多项式：affine 检查点误差 >0.1，polynomial2 ≤1e-8，输出与参考 ≤0.5——证明阶数选择与「检查点不参与拟合」真实生效。
- 差值：逐像元与硬编码差值一致，统计 min/max/mean 逐项一致；NoData 排除后 n_valid=3；网格/波段/格式不匹配明确拒绝且不落盘。
- 融合：常量 MS `[0.2,0.4,0.6]` + Pan 0.8 → Brovey `[0.1333333,0.2666667,0.4]`、HSV `[0.2666667,0.5333333,0.8]`（≤1e-6）；MS 4×4 重采样到 Pan 8×8 后常量结果保持；缺 msFile/重复 msBands/未知方法/多波段 Pan/CRS 不一致/PNG 全部明确拒绝。

**三、验证**

- 新增 `tests/unit/test_p6_gcp_diff_fusion_ops.py`（9 项）、`tests/integration/test_p6_gcpregister.py`（3 项）、`test_p6_rasterdiff.py`（3 项）、`test_p6_pansharpen.py`（4 项）、`test_p6_gcp_fusion_gui.py`（3 项桌面路径）；四维审计/对账/算法计数同步为 **454/449/244/18**、算法规格 27→29、GDAL 编目 411→413。
- 受影响定向：294 项单测、43 项 P6-04/sidecar/表单回归全绿。
- Ruff 干净；Mypy 76 个源文件 0 错误；Clean 全量：**771 passed / 0 failed / rc=0 / clean_exit_verified=true**（476.80s，residual=0、cleanup_complete=true）。

**四、边界与结论**

- 验收「合成已知变换和独立检查点验证；各融合算法准确命名」逐项满足——**P6-04 转 done**；账本现为 done 33 / implemented 1（P0-02）/ todo 21。
- 遗留边界：GCP 当前仅 affine/polynomial2（TPS 未实现）且自定义重采样整幅读入源影像；差值要求严格同网格；融合仅 Brovey/HSV（NNDiffuse/GS/PCA 未实现），HSV 对 Float32 输入假设近似 [0,1] 反射率。

### 第 60 轮：P6-03 PCA、逆变换与缨帽（P6-03 → done）

**一、实现**

- 新增 `providers/gdal_provider/pca_ops.py`：多波段实数栅格 → 全图均值 → 协方差/相关矩阵（总体方差 ddof=0）→ `numpy.linalg.eigh` → 特征值降序 + 符号约定（绝对值最大的载荷为正）；前 k 个主成分输出（缺省全部波段）。`<成果名>.pca.json` 记录 mean/std/eigenvalues/eigenvectors/explained_variance_ratio/total_variance/discarded_variance/reconstruction_rmse/relative_rmse/matrix/components/n_valid。逆变换读同一 sidecar（校验类型/版本/形状/正交性并计算 SHA256），按 `pcaComponents` 重建原始波段数，写 `<成果名>.pca_inverse.json`；正/逆变换均保持 NoData。
- PCA 误差定义：完整重建（k=n）为浮点噪声量级；降维误差 `reconstruction_rmse = sqrt(mean((X-X_hat)^2))`（有效像元×全部波段），并满足 `RMSE² × 波段数 == discarded_variance`。模式专属参数严格：forward 拒绝 pcaTransform，inverse 拒绝 pcaMatrix 且必须提供 pcaTransform。
- 新增 `providers/gdal_provider/tasseledcap_ops.py` + `processing/tasseledcap_registry.py`：登记 Landsat 5 TM（Crist 1985，DOI 10.1016/0034-4257(85)90102-6）、Landsat 7 ETM+（Huang et al. 2002，USGS 70024815）、Landsat 8 OLI（Baig et al. 2014，DOI 10.1080/2150704X.2014.915434）三套系数；每套记录来源、产品级别、系数矩阵与波段顺序。调用方必须显式给出 6 个输入波段号（Blue,Green,Red,NIR,SWIR1,SWIR2），未知传感器/重复/越界/缺波段拒绝。输出 3 波段 Float32（brightness/greenness/wetness），数据集元数据与 `<成果名>.tasseledcap.json` 同时记录传感器、产品级别、完整系数、输入波段与来源 URL。
- 契约/接线：新增 `pcaMode`/`pcaComponents`/`pcaMatrix`/`pcaTransform`/`tcSensor`/`tcBands` 别名与支持矩阵；adapter 新增两能力声明、模式门禁、按实际波段数校验格式（PNG/JPEG 拒绝）、执行分支与 sidecar 成套提交；registry 新增两项平台能力编目（PLATFORM_ONLY）；GUI 新增 PCA 模式/成分数/矩阵/变换文件选择与缨帽传感器/6 波段输入/来源说明，状态隔离与格式预检同步。

**二、数值验收（独立 GDAL 重开）**

- PCA 3 波段合成图（精确线性相关，协方差秩 2）：全成分正变换 sidecar 完整重建 RMSE ≤ 1e-9；inverse 重建逐像元 ≤ 1e-4；1 主成分降维 sidecar 的 `RMSE²×3 == discarded_variance`（rel ≤ 1e-9），inverse 实测误差与 sidecar 报告同量级；相关矩阵模式正/逆变换均完整重建。
- NoData 像元在正/逆变换中均保持 NoData；越界成分数、缺失/损坏变换文件、模式专属参数、PNG 输出全部明确拒绝且不落盘。
- 缨帽三套传感器逐像元与测试侧独立硬编码系数矩阵一致（≤1e-6）；打乱输入波段顺序后 `tcBands` 角色映射仍正确；未知传感器/缺 tcBands/重复/越界/PNG 全部明确拒绝且不落盘。

**三、验证**

- 新增 `tests/unit/test_p6_pca_tasseledcap_ops.py`（19 项）、`tests/integration/test_p6_pca.py`（5 项）、`tests/integration/test_p6_tasseledcap.py`（6 项）、`tests/integration/test_p6_transform_gui.py`（2 项桌面路径）；四维审计/对账/算法计数同步为 **452/447/242/15**、算法规格 25→27、GDAL 编目 409→411。
- 受影响定向：285 项单测、13 项 P6-03 集成、2 项 P6 GUI、30 项 sidecar/表单回归全绿。
- Ruff 干净；Mypy 73 个源文件 0 错误；Clean 全量：**749 passed / 0 failed / rc=0 / clean_exit_verified=true**（484.37s，residual=0、cleanup_complete=true）。

**四、边界与结论**

- 验收「PCA 完整重建与降维误差符合定义；缨帽系数来源、传感器匹配可检查」逐项满足——**P6-03 转 done**；账本现为 done 32 / implemented 1（P0-02）/ todo 22。
- 遗留边界：PCA 统计整幅读入（分块统计/流式实现归后续）；缨帽仅登记三套传感器且要求输入为对应产品级别；交互式 PCA 载荷图/频谱 UI 与更多传感器归后续轮次。

### 第 59 轮：P6-02 FFT 频域滤波 / 去条纹（P6-02 → done）

**一、实现**

- 新增 `providers/gdal_provider/fft_ops.py`：单波段实数栅格 **FFT2 → 厄米对称抑制掩膜 → IFFT2 → Float32** 成果；空 `suppressBins` 为恒等往返（相对容差校验），抑制 bin 允许负数并按 `fftfreq` 语义模归一化、自动补 `(−fy,−fx)` 伙伴；逆变换虚部超容差即报实现缺陷；NoData 像元以波段均值填充参与 FFT、逆变换后按原始有效掩膜回填输出 NoData；**复数输入明确拒绝**（不静默丢弃虚部）。
- 掩膜 sidecar `<成果名>.fft_mask.json` 记录 requested/applied bins、尺寸、`hermitian_symmetric` 与输出 NoData；用 applied bins 重跑可逐像元复现。sidecar 所有权识别 `.fft_mask.json`，覆盖旧成果时随事务替换，不残留陈旧掩膜。
- 契约/接线：`fftBand`/`suppressBins`/`srcNodata`/`dstNodata` 别名与支持矩阵；adapter 新增 `SUPPORTED_CAPABILITIES`、格式门禁（恒单波段 Float32+NoData，PNG/JPEG 写盘前拒绝）与执行分支；registry 新增平台能力编目；GUI 新增 fftBand 微调框、suppressBins 文本解析（分号分隔 `[fy,fx]` 对、整数校验）与状态隔离，预检按 Float32/1 波段/必设 NoData。
- **修复第 58 轮 P6-01 回归**：未提交的 FFT 重构把 `if not bandA or not bandB` 误改成 `if is_sindex`，导致所有 sindex 任务无条件失败；已恢复条件并用 `is None` 守零值铁律，`bandA==bandB` 分支重新可达。同时补齐 sindex/fftfilter 的 `format` 下发——此前 GUI 选择 COG/ENVI 会被 Worker 静默写成 GTiff。

**二、数值验收（独立 GDAL 重开）**

- 8×8 与 7×9 合成图 = 常量 10 + x 方向 `cos(2π·2x/W)` + y 方向 `sin(2π·y/H)`。
- **往返**：空 suppressBins 输出逐像元等于输入，最大误差 ≤ 1e-4。
- **频率定位**：抑制 (0,2) 后 x 条纹消失，输出逐像元等于 `10 + 5sin(2π·y/H)`（≤ 1e-4）。
- **掩膜对称性**：sidecar 的 applied bins 含 (0,2) 与厄米伙伴 (0,W−2)；逐项 `mask[fy,fx] == mask[(−fy)%H,(−fx)%W]`。
- **保存掩膜可复现**：用 sidecar applied bins 重跑第二份成果，逐像元一致（≤ 1e-6）。
- **NoData**：标签/像元/计数三者一致，其余像元无 NaN/Inf；**ENVI** 输出真实写出 `.dat/.hdr` 且掩膜 sidecar 成套提交；PNG 写盘前拒绝；缺 fftBand、越界波段、非法 suppressBins、复数输入均明确拒绝且不落盘；覆盖写后旧掩膜被新掩膜替换。

**三、验证**

- 新增 `tests/unit/test_p6_fft_ops.py`（14 项）、`tests/integration/test_p6_fft.py`（9 项）、`tests/integration/test_p6_fft_gui.py`（1 项桌面路径）；四维审计/对账/算法计数同步为 **450/445/240/13**、算法规格 24→25、GDAL 编目 408→409。
- 受影响定向：266 项单测、13 项 P6-01/P6-02 集成、2 项 P6 GUI、30 项 sidecar/表单回归全绿。
- Ruff 干净；Mypy 70 个源文件 0 错误；Clean 全量：**717 passed / 0 failed / rc=0 / clean_exit_verified=true**（475.89s，residual=0、cleanup_complete=true）。

**四、边界与结论**

- 验收「往返、频率定位、掩膜对称性、复数/奇偶尺寸通过；保存掩膜可复现」逐项满足——**P6-02 转 done**；账本现为 done 31 / implemented 1（P0-02）/ todo 23。
- 遗留边界：分块/流式 FFT（当前整幅读入）、频谱中心化与交互式掩膜编辑 UI（本期为文本 bin 列表）归后续轮次。

### 第 58 轮：P6-01 光谱指数（P6-01 → done）

**一、实现**

- 运行时实测：QGIS GDAL 运行时自带 numpy 2.4.6 / scipy 1.18.1——指数在 gdal worker 内以 numpy 求值；**架构边界如实记录**：方案规划的 science 独立 worker 因 sw-science 环境仅有 numpy/scipy（无 pydantic/栅格 IO）暂不能承载 JSONL worker，待环境补装后建立（已写入 registry limitations）。
- 新增 `providers/gdal_provider/index_ops.py::compute_index`（NDVI/NDWI/NBR/SAVI）：
  - **物理量/原值**：`usePhysical=true`（默认）时按波段元数据换算（物理值 = 原值 × scale + offset，仅当存在非平凡 scale/offset），是否实际应用逐波段如实返回；
  - **无效区三源**：波段 NoData 标签（srcNodata 可覆盖）、分母为零、非有限值 → 输出 NoData（默认 -9999），标签/无效区/像元值一致；
  - 自审发现并修复顺序缺陷：NoData 检测必须在物理变换**之前**用原始值判定（变换会平移数值导致漏检）。
- 契约：`sindex`/`bandA`/`bandB`/`saviL`/`usePhysical` 别名与支持矩阵；registry 新增平台能力 `gdal:sindex`（基线 448→449）；adapter 门禁 bandA==bandB 拒绝、输出恒单波段 Float32 必设 NoData（PNG/JPEG 预检拒绝）。
- GUI：指数下拉（公式随选项展示）、bandA/bandB/saviL/usePhysical 控件与状态隔离。

**二、数值验收（独立 GDAL 重开，双波段 fixture，A scale=0.1 NoData=-1、B scale=0.2）**

- NDVI 原值：(200−100)/300=**1/3**、(100−100)/200=**0**（合法零不是 NoData）、分母零→NoData、A=-1→NoData。
- NDVI 物理量：同像元 **(0,0)=0.0、(1,0)=−1/3、(2,0)=−3.0**——与原值结果不同，证明波段元数据真实参与（物理量/原值对应）。
- SAVI：L=0.5 = (100/300.5)×1.5≈**0.499168**；L=0 退化为 NDVI——实测揪出并修复 `or 0.5` 把合法 L=0 当缺省的零值 truthiness 缺陷。
- NDWI 反向组合正确；拒绝路径（缺 bandA/bandB、bandA==bandB、越界波段、未知指数、PNG）全部明确拒绝且不落盘。

**三、验证**

- 新增 `tests/integration/test_p6_index.py`（4 项）与 `tests/integration/test_p6_index_gui.py`（1 项桌面路径）；四维审计/对账/算法计数单测同步（449/444/239/12、算法规格 23→24）。
- 受影响定向（P6-01 集成/GUI + 全部单测）：**285 passed**；Ruff 干净；Mypy 69 个源文件 0 错误。
- Clean 全量：**693 passed / 0 failed / rc=0 / clean_exit_verified=true**（403.22s，residual=0、cleanup_complete=true）。

**四、边界与结论**

- 验收「物理量/原值与波段元数据对应；合成值和无效区符合公式」逐项满足——**P6-01 转 done**。
- 遗留边界：EVI 等三波段指数、光谱剖面曲线（PyQtGraph）、science 独立 worker 归后续轮次。

### 第 57 轮：implemented 收官——19 项转 done（P0-02 维持 implemented）

**一、审计方法**

- 逐项提取 20 项 implemented 的验收标准与全部既有证据，核对「验收逐项 ↔ 真实命令/结果/报告」；引用的测试文件全部实存核对（含 test_contracts_schema.py 43 项、test_scheduler_lifecycle.py 6 项、test_staging_manifest.py 9 项、test_p3_persistence_regression.py 19 项、test_p4_full_user_flow.py 3 项端到端、test_dual_view_sync_and_release 等）。
- 当日最新回归：Clean 全量 **688 passed / 0 failed / rc=0 / clean_exit_verified=true**（392.20s）——各条目引用的回归套件均在本次全量中真实通过。

**二、转 done 的 19 项**

- **P0**：P0-03（doctor 分项报告 + 7 项单测）、P0-05（隔离目录启动 3 项 + ADR 0002）。
- **P1**：P1-01（契约 schema 43 项双向对齐）、P1-04（调度生命周期 6 项：协作取消/强杀降级/崩溃重试/严格 FIFO）、P1-05（staging 事务 9 项：真实强杀保护、committed 不可逆、恢复失败不吞）。
- **P2**：P2-01（基准双哈希 + schema 100%）、P2-02（205 驱动 + 119 CLI + 缺失保留 + 真实 Warp）、P2-03（rasterio worker + API 索引分类）、P2-04（表单全量参数 + 第 26–28 轮契约回归 42 项）、P2-05（审计脚本 + 工具箱分色）。
- **P3**：P3-01（持久化 19 项 + Dock 布局 + 零地理导入 + 高 DPI）、P3-02（矢量/子数据集 10 项 + 第 23/24 轮上下文隔离回归 8 项）、P3-03（6144² 真实 GUI 内存 LRU 实测）、P3-04（原值/显示分离 + 双视图同步）、P3-05（19+17 项 + 交互 ROI + 故障注入矩阵）。
- **P4**：P4-01（14+11+16+15 项参数与边界）、P4-03（7 项 ENVI 未知字段/概览/尺寸契约）、P4-04（69 项表达式/统计）、P4-05（3 项端到端全流程含取消/重试/导出/重开）。

**三、维持 implemented（1 项）**

- **P0-02**：验收字面要求已满足，但证据明示「GDAL 运行时依赖本机 QGIS 安装，不得据此标 done」——维持 implemented，前置条件为 P10-01/P10-02 自带可重定位运行时并在干净机验证。

**四、结论**

- 任务账本现为：**done 29 / implemented 1（P0-02，打包依赖）/ todo 25**。
- 后续主线：P5 收官后的 P6（光谱/FFT/PCA/配准/分类/辐射）→ P7（迁移/会话/批处理/DAG/历史）→ P8~P10。

### 第 56 轮：P5-05 驱动读写能力矩阵与高级选项（P5-05 → done，P5 收官）

**一、实现**

- worker 新增 `describe_driver`（adapter + `entrypoint._SIMPLE_CALLS` 无路径通道 + process + client 同步/异步）：
  - **能力声明**：`DCAP_OPEN/CREATE/CREATECOPY/UPDATE` 四访问模式与 raster/vector 种类**分开**如实报告——声明与实测不合并；
  - **高级选项**：解析 `DMD_CREATIONOPTIONLIST`，逐选项名称/类型/默认值/说明与 string-select 候选值（GTiff 含 COMPRESS[LZW/DEFLATE…]/TILED，GPKG 含 VERSION）；个别不规范 XML 保留原文不编造；`DMD_EXTENSION` 提供典型扩展名。
- 四模式**分开实测矩阵**（GDAL 运行时子进程逐模式真实执行）：GTiff/ENVI/COG/GPKG 四个点名驱动 + netCDF/BMP/FlatGeobuf/GeoJSON/PNG/JPEG 扩展代表；双能力驱动（GPKG）按 kind 覆盖表以真实使用方式（矢量）实测。

**二、实测结论（GDAL 3.13.3，声明 vs 实况错位如实记录）**

- GTiff 四模式全通过；ENVI 建/更新/复制建实测可用（**CreateCopy 声明 false 实测可用**）；COG Create 实测可用（**3.13 已支持 Create**，旧版 CreateCopy-only 认知过时）、Update 实测失败；PNG Create 实测失败（CreateCopy-only）、CreateCopy 可用；GPKG 读/建/更新全通过；netCDF/BMP/FlatGeobuf/GeoJSON 进入矩阵；已装可打开驱动 **180+**（netCDF/FlatGeobuf/BMP/HFA/PCIDSK 均在清单）。
- 错位治理分两类：「声明 true 实测失败」= 真问题（测试直接失败）；「声明 false 实测可用」= 元数据保守，六项白名单化（ENVI CreateCopy、FlatGeobuf Update/CreateCopy、GeoJSON CreateCopy、BMP CreateCopy/Update），新错位必须显式过目。

**三、验证**

- 新增 `tests/integration/test_p5_driver_matrix.py`（2 项：四模式矩阵 + describe_driver 通道与选项解析）。
- Ruff 干净；Mypy 68 个源文件 0 错误。
- Clean 全量：**688 passed / 0 failed / rc=0 / clean_exit_verified=true**（392.20s，residual=0、cleanup_complete=true）。

**四、边界与结论**

- 验收「读/建/复制建/更新分开；ENVI/GPKG/GTiff/COG 和已装扩展驱动实测」逐项满足——**P5-05 转 done，P5 里程碑全部完成**。
- 遗留边界：GUI 逐驱动创建选项表单化留待 P9-02 能力路由补齐；高级选项目前经 describe_driver 暴露并经既有 creation_options 参数消费。

### 第 55 轮：P5-04 ogr:vectortranslate——矢量处理、SQL 与坐标转换（P5-04 → done）

**一、实现**

- worker：`SUPPORTED_CAPABILITIES` 加入 `ogr:vectortranslate`，复用矢量成果通道（`VECTOR_OUTPUT_CAPABILITIES`/CONNECTED/FORMAT_CAPABILITIES 同步）。输入 `gdal.OpenEx(OF_VECTOR)`；支持 `format`（GPKG/GeoJSON/ESRI Shapefile/FlatGeobuf）、`sql`（OGR SQLStatement）、`where`、`select`、`spat`（bbox，源 CRS 单位）、`dstSRS` 重投影；staged `inspect_vector` → 事务提交（Shapefile bundle/stale 删除复用）→ 提交后逐层要素数比对。
- **运行时实测四项关键语义**（GDAL 3.13.3，写入实现注释与限制声明）：
  - `dstSRS` 不配 `reproject=True` **只改 SRS 标签、不换算坐标**（假重投影）→ adapter 恒置 `reproject=True`；
  - 源图层无空间参考时连 `reproject=True` 都不换算 → adapter 写盘前逐图层预检、明确拒绝（`invalid_parameters`，目标不落盘）；
  - 输出几何轴序为**传统 GIS 顺序**（EPSG:4326 → X=经度，Y=纬度）；
  - FlatGeobuf 驱动 `DCAP_CREATE` 可用，返回句柄释放后才落盘。
- 契约：`sql`/`where`/`select`/`spat` 别名与 `ogr:vectortranslate` 支持矩阵（spat 定长 4 浮点、min≤max）；矢量格式矩阵加入 FlatGeobuf。
- GUI：sql/where/spat/select/dstSRS 五个控件与状态隔离；矢量格式候选四项；`DatasetRef` 对 vectortranslate 标注 `kind=vector`。
- 注册表 `ogr:vectortranslate` 翻转 ready/form，限制如实声明（轴序、spat 单位、无 SRS 拒绝、OGR SQL 方言）。

**二、数值验收（独立已知坐标 + 独立 OGR 重开）**

- UTM 50N `(500000, 3000000)` → EPSG:4326：输出 **X=117.0（经度，轴序断言）**、**Y=27.1224696415507**（与独立 `osr.TransformPoint` 计算一致，1e-6）。
- 无 SRS 源 + dstSRS → `invalid_parameters` 不落盘（GeoJSON 驱动自动补 WGS84，fixture 改用 GPKG undefined SRS 构造）。
- 字段值：`where "v>=20"` 恰 5 值 `[20..24]`；`select ["name"]` 字段子集；`SQL "SELECT name, v FROM grid WHERE v < 5"` 恰 5 行 2 字段。
- 空间关系：`spat (2,2,3,3)`（源 CRS 单位=米）恰命中 g12/g13/g17/g18；反向 bbox 契约层拒绝。
- FlatGeobuf 单文件输出往返一致；无 dstSRS 时坐标与 SRS 原样透传。

**三、验证**

- 新增 `tests/integration/test_p5_ogr_translate.py`（5 项）与 `tests/integration/test_p5_ogr_translate_gui.py`（1 项桌面路径）；矢量矩阵/对账/四维审计单测同步。
- 受影响定向（P5-04 集成/GUI + 全部单测）：**286 passed**；Ruff 干净；Mypy 68 个源文件 0 错误。
- Clean 全量：**686 passed / 0 failed / rc=0 / clean_exit_verified=true**（402.60s，residual=0、cleanup_complete=true）。

**四、边界与结论**

- 验收「独立已知坐标、空间关系与字段值验证；轴顺序与距离单位明确」逐项满足——**P5-04 转 done**。
- 遗留边界：几何级处理（clip/dissolve/buffer 等需 GEOS）未接入；距离单位随成果 CRS 明确（投影=米、地理=度）。

### 第 54 轮：P5-03 分页属性表端到端（P5-03 → done）

**一、实现**

- worker 新增 `read_vector_page`（adapter + `entrypoint._SIMPLE_CALLS` + `workers/process.py` + client 同步/异步通道）：
  - **按范围查询**：`SetNextByIndex(start)` 后只读 `count` 个要素，单页硬上限 1000（超过直接拒绝）——从接口上杜绝「一页拿全表」；
  - **廉价计数**：`GetFeatureCount(force=0)`；驱动给不出时如实返回 `feature_count_expensive=true` 与 `feature_count=null`，绝不代为发起全表扫描；
  - **空几何/多部件/编码**：逐要素标注 `geometry_type`/`empty_geometry`/`part_count`；字段值由 GDAL 按数据集声明编码（如 `.cpg`）解码。
  - 运行时实测纠正两处想当然：`SetNextByIndex` 返回 **0 = OGRERR_NONE（成功）**而非真值；GPKG 的 FID 来自 SQLite rowid，**1-based**。
- GUI 新增 `ui/panels/attribute_panel.py`：`VectorPageModel`（FID | 几何 | 字段…；几何列如实标注「空」「类型 ×部件数」）+ `AttributePanel`（一页 100 条、上一页/下一页、页码与要素数、多子图层下拉、**请求代际**丢弃迟到旧页；廉价计数不可得时以「最后一页不满」为结束条件并显示「≥ N」）。MainWindow 把面板加入右侧「属性表」页签，选中矢量图层联动加载，栅格图层清空。

**二、数值验收（真实 worker + 独立 GDAL fixture）**

- 150 点 GPKG：分页恰 `[1..100]`/`[101..150]`（FID 1-based）；60/页逐页拼接 == 全表；越界/零/负数/超大页/`layer_index` 超范围全部明确拒绝且不产出数据。
- 混合几何：NULL 几何 → `geometry_type=None, empty=true`；显式空多边形 → `empty=true`；MultiPolygon ×2 部件 → `part_count=2`。
- 编码保留：`ENCODING=GBK` 写出 `.cpg` 的 Shapefile，中文字段名「名称/数值」与字段值「地块甲/乙/丙」往返一致。
- 单页 payload 保持小（不携带 WKT 全量几何）；GUI 真实 MainWindow + QTest：第一页 100 行、页码「第 1 / 2 页」、要素数「150」，点击下一页首行 `value=100`、末行 `p149`；几何列「Multi Polygon ×2」与「空几何」如实显示。

**三、验证**

- 新增 `tests/integration/test_p5_vector_page.py`（5 项）与 `tests/integration/test_p5_attribute_panel_gui.py`（2 项桌面路径）。
- Ruff 干净；Mypy 68 个源文件 0 错误。
- Clean 全量：**680 passed / 0 failed / rc=0 / clean_exit_verified=true**（400.10s，residual=0、cleanup_complete=true）。

**四、边界与结论**

- 验收「按范围查询；字段、空几何、多部件和编码保留；大图层无一次性全读」由分页边界/几何标注/编码往返/单页上限断言逐项满足——**P5-03 转 done**。
- 矢量画布渲染不在本项验收范围（仍未实现，界面如实标注）；属性表为只读，编辑/过滤/排序留待后续。

### 第 53 轮：矢量成果通道边界完善——Shapefile bundle、fixedLevels、band（P5-02 保持 done）

**一、实现**

- **ESRI Shapefile 多文件 bundle 输出**（polygonize/contour）：
  - `VECTOR_OUTPUT_FORMATS` 增加 Shapefile（键 `SHAPEFILE`/`ESRI SHAPEFILE` → OGR 驱动 `ESRI Shapefile`）；
  - 矢量分支按已知后缀收集 staged bundle（`.shx/.dbf/.prj/.cpg`）映射到目标 stem 构造提交对；提交前复用 `ensure_no_stale_sidecars` 所有权预检，旧残留 sidecar（如旧 `.qix`）随事务删除，提交后整体 OGR 重开逐层比对；
  - `sidecars.py` 增强：`_suffixes_for_target` 对 `.shp` 目标识别 `.shx/.dbf/.prj/.cpg/.qix`；`discover_sidecars` 在 `gdal.Open` 失败时**回退 `ogr.Open`**——实测 Shapefile 驱动只有 `DCAP_VECTOR`，`gdal.Open` 打不开曾把覆盖请求误判为「所有权无法确认」而拒绝；Shapefile 后缀语义仅在目标为 `.shp` 时启用，**栅格流行为逐字节不变**。
- **contour fixedLevels**：契约新增 `fixedLevels`（非空有限浮点列表，别名 `fixed_levels`）；实测 Python 绑定第 4 参即 fixedLevels 序列且非空时**完全替代** interval/base 等差序列（interval 可省略）；`interval` 与 `fixedLevels` 二选一，皆缺 → `invalid_parameters`。
- **band 参数**（polygonize/contour）：1-based、默认 1、别名 `band_index`；契约正整数校验，Worker 校验 `1 ≤ band ≤ RasterCount`（越界 → `invalid_parameters` 且不写目标）。
- GUI：矢量格式候选增加 `Shapefile (.shp)`；新增 band 微调框与 fixedLevels 文本框及状态隔离；registry 两个 descriptor 补参数并如实更新限制（移除「仅第 1 波段」「fixedLevels 未接入」）。

**二、数值验收（GDAL 3.13.3 运行时实测 + 独立 OGR 重开）**

- Shapefile bundle：`.prj` 正常写出且图层 SRS 回读 `32650`；提交集合含 `.shp/.shx/.dbf(/.prj)`；预置的旧 `.qix` 在覆盖提交后随事务删除；OGR 重开 DN∈{3,9}、面积各 4.0。
- fixedLevels=[2.5, 5.5]：ELEV 恰为 `{2.5, 5.5}`，顶点逐点位于解析等值线上；空列表契约层拒绝。
- 双波段栅格 band=2：多边形来自第二波段（DN∈{7,8}）；band=3 越界与 band=0 均明确拒绝且目标不落盘。

**三、验证**

- 受影响定向（全部单测 + polygonize/contour 集成 + GUI 复跑）：**294 passed**；Ruff 干净；Mypy 67 个源文件 0 错误。
- Clean 全量：**673 passed / 0 failed / rc=0 / clean_exit_verified=true**（418.71s，residual=0、cleanup_complete=true）。

**四、边界与下一步**

- 剩余遗留：DEM color-relief 颜色表参数（需要「文件路径参数」通道）、矢量画布渲染（P5-03 范围）。
- 下一步：P5-03 矢量图层与分页属性表（按范围查询；字段、空几何、多部件和编码保留；大图层无一次性全读）。

### 第 52 轮：P5-02 增量五——contour 等值线生成，P5-02 转 done

**一、实现**

- `GDALProviderAdapter.SUPPORTED_CAPABILITIES` 加入 `gdal:contour`，`_execute_vector_output` 的 contour 分支激活：
  - `gdal.OpenEx(OF_RASTER)` 打开单波段栅格，建 `wkbLineString25D` 图层与 `ID`(OFTInteger64)/`ELEV`(OFTReal) 字段；
  - `gdal.ContourGenerate(band, interval, offset, [], useNoData, nodata, layer, 0, 1, callback)`——GDAL 3.13.3 运行时实测：Python 绑定把 docstring 里的 `fixedLevelCount` 折叠为 `fixedLevels` 序列参数（传 `[]` 表示无固定等级）；
  - 遵循输入 NoData 标签（`useNoData`）；`interval` 必填且为正（缺省/非正 → `invalid_parameters`）；
  - 复用与 polygonize 相同的矢量矩阵预检、staging `inspect_vector`、单对 `commit_files` 事务与提交后逐层要素数比对。
- `processing/raster_params.py` 的 `gdal:contour` 支持矩阵 `{interval, offset}`（interval 正数、offset 0 合法）与别名；`capability_wiring.py` 的 CONNECTED/FORMAT_CAPABILITIES += `gdal:contour`。
- 注册表 `gdal:contour` 翻转为 ready/form，限制如实写明（仅 GPKG/GeoJSON、仅第 1 波段、遵循 NoData、fixedLevels 需 ContourGenerateEx 未接入、输出 3D Line String 与 ID/ELEV 字段）。
- GUI 新增 interval（默认 10.0，必填>0）/offset（默认 0.0）控件与状态隔离；格式下拉复用矢量族候选——**表单中 PNG 等栅格格式根本不可选**，即矢量预检的正确形态；OutputPlan `kind=vector`。

**二、数值验收（解析平面 z = 2*col + 3*row，独立 OGR 重开）**

- `interval=1`、`offset=0`：恰 **35 条**等值线，`ELEV` 集合 = `{1..35}`（0 级仅在 (0,0) 单点退化，无等值线）；`ID` 按等级递增；每个 3D 顶点 `Z == ELEV`；内部顶点满足 `|2*col + 3*row − ELEV| < 1e-4`（`col=x−0.5`、`row=7.5−y`），边界顶点被裁剪到数据集范围——等值线几何与解析平面**逐点一致**，不是只检查文件存在。
- `offset` 是等差序列的**对齐点**（GDAL 向两侧延伸覆盖数据范围，非等级下限）：`interval=2` 时 `offset=0/1` 分别产生偶数 `{2,4,…,34}` 与奇数 `{1,3,…,35}` 等级——offset 语义实证。
- GeoJSON 单文件输出同性质；缺 `interval`、`interval=0`、PNG 均明确拒绝且目标不落盘。
- GUI 真实 `MainWindow` + Worker + QTest：GPKG 提交成功、图层计数 2（矢量成果自动登记）、ELEV 集合独立复核、格式候选断言恰为 GPKG/GeoJSON。

**三、验证**

- 新增 `tests/integration/test_p5_contour.py`（4 项数值/边界）与 `tests/integration/test_p5_contour_gui.py`（1 项桌面路径）；对账测试 ready 断言加入 `gdal:contour`。
- 受影响定向：**36 passed**；Ruff 干净；Mypy 67 个源文件 0 错误。
- Clean 全量：**669 passed / 0 failed / rc=0 / clean_exit_verified=true**（351.45s，residual=0、cleanup_complete=true）。
- 审计刷新：`platform_executable` 9 → **10**；缺口维持 **5**（`gdal:cli:gdal_auth`、HDF5、3 个 rio 插件）。

**四、边界与 P5-02 结论**

- P5-02 的全部工具（`gdal:dem` / `gdal:rasterize` / `gdal:grid` / `gdal:polygonize` / `gdal:contour` 共 5 项）均已从契约编目接入为可执行表单入口；验收「已知矩形/标签图和解析平面验证；不能只检查文件存在」由数值/几何断言满足——**P5-02 转 done**。
- 遗留边界（不影响验收，已在注册表/证据如实声明）：Shapefile 多文件 bundle 输出、polygonize 指定波段/外部 mask 文件、contour fixedLevels（需 ContourGenerateEx）、矢量画布渲染（P5-03 范围）。

### 第 51 轮：P5-02 增量四——polygonize 栅格矢量化与矢量成果输出通道（P5-02 in_progress）

**一、实现**

- `processing/output_formats.py` 新增 `VECTOR_OUTPUT_FORMATS` 与 `validate_vector_output_format`：刻意保守只接**单文件** OGR 驱动 GPKG/GeoJSON；ESRI Shapefile 因 `.shp/.shx/.dbf/.prj` 多文件 bundle 需专门收集器而明确拒绝（拒绝消息写明原因）。
- 新增 `providers/gdal_provider/vector_ops.py::inspect_vector`：`ogr.Open` 只读真实重开，逐图层收集几何类型/要素数/CRS/范围/字段，并**实际读取最多 5 个要素的 WKT 与字段值**（对齐 `inspect_raster`「samples 是真实读出的像元」哲学）；惰性导入保证无 osgeo 环境可导入。
- `GDALProviderAdapter` 新增 `_execute_vector_output` 独立分支（矢量成果不进栅格主流程，不复用 `gdal.Open` 重开）：
  - GDAL 3.13.3 运行时实测：`gdal.Polygonize` 的 `options` 必须传**列表**（`None` 被拒绝）；8 连通用 `options=["8CONNECTED=YES"]`；
  - 显式 `format`/`driver` 必须过矢量矩阵（否则 `unsupported_format_combination`），未给时默认 GPKG；
  - 建数据源/图层（图层名=输出 stem）/`fieldName` 字段（默认 DN）后调 `gdal.Polygonize(band, band.GetMaskBand(), …, callback)`，取消经 callback 生效；
  - staging `inspect_vector` → `commit_files` 单对事务提交 → target 二次 `inspect_vector` 并逐层比对要素数（不一致即 `final_reopen_failed`）；`TaskResult.outputs` 登记 `data_type=vector`、driver、layer_count、feature_count。
- `processing/raster_params.py` 新增 `fieldName`（别名 `dn_field`，避开 `attribute` 已占用的 `field_name`）、`connectedness`（4/8，拒绝小数截断）与 `gdal:polygonize` 支持矩阵。
- `capability_wiring.py` 新增 `VECTOR_OUTPUT_CAPABILITIES` 与 `is_vector_output`；CONNECTED/FORMAT_CAPABILITIES += `gdal:polygonize`；注册表 `gdal:polygonize` 翻转为 ready/form，限制写明（仅 GPKG/GeoJSON、仅第 1 波段、mask 遵循 GetMaskBand）。
- GUI：格式下拉按能力族重填（矢量只有 GPKG/GeoJSON）；新增 fieldName/connectedness 控件与状态隔离；预检走矢量矩阵；OutputPlan `kind=vector`；输出对话框补 GPKG/GeoJSON 过滤器；矢量成果经 P3-02「栅格探测失败→矢量探测」回退登记为图层（主窗口零改动）。

**二、数值验收（GDAL 3.13.3 运行时独立 OGR 重开）**

- 标签图 `[[3,3,9,9],[3,3,9,9]]` → GPKG 恰 2 个要素、`DN∈{3,9}`、每个多边形面积 4.0（像元单位）、SRS 32650 传递。
- 对角像元 `[[5,0],[0,5]]` → 4 邻域 2 个 DN=5 多边形、8 邻域 1 个——**8CONNECTED 实证生效**，不是只检查文件存在。
- GeoJSON 单文件输出、默认 DN 字段；显式 GTiff → `unsupported_format_combination`（消息含 GPKG/GeoJSON）；矢量输入 → `polygonize_error`；目标均不落盘。
- GUI 真实 `MainWindow` + Worker + QTest：GPKG 提交成功、图层计数 2（矢量成果自动登记）、面积/字段独立复核。

**三、验证**

- 新增 `tests/integration/test_p5_polygonize.py`（4 项数值/边界）与 `tests/integration/test_p5_polygonize_gui.py`（1 项桌面路径）；`test_output_format_matrix.py` 新增矢量矩阵 2 项；对账测试 ready 断言加入 `gdal:polygonize`。
- 受影响定向（单测三件套 + 基线四维 + 集成/GUI 复跑）：**41 passed**。
- Ruff 干净；Mypy 67 个源文件 0 错误。
- Clean 全量：**664 passed / 0 failed / rc=0 / clean_exit_verified=true**（382.37s，residual=0、cleanup_complete=true）。
- 审计刷新：`platform_executable` 8 → **9**（`gdal:polygonize` 接入，callable 238 不变）；缺口维持 **5**。

**四、边界与下一步**

- P5-02 保持 **in_progress**：contour 尚未接入。
- 矢量输出 v1 边界：Shapefile 多文件 bundle、polygonize 指定波段/mask 文件、矢量渲染——均未接入，限制已在注册表如实声明。
- 下一步：`gdal:contour`（解析平面 z=2x+3y 等高线，逐顶点解析验证 ELEV），完成后 P5-02 转 done。

### 第 50 轮：P5-02 增量三——grid 散点栅格插值（P5-02 in_progress）

**一、实现**

- `processing/raster_params.py` 新增 `algorithm`、`zField` 别名与 `gdal:grid` 支持矩阵；算法名与高程字段做非空校验。
- `GDALProviderAdapter` 新增 `gdal:grid` 分支：
  - `gdal.OpenEx(OF_VECTOR)` 打开点/多点矢量；
  - 必须给出 `width/height`，否则明确拒绝，不猜测网格；
  - 未指定 `outputBounds` 时按图层 extent 自动计算（复用 `_vector_bounds`，显式处理 OGR 轴顺序）；
  - 支持 `algorithm`、`zField`、`width/height`、`outputBounds`、`outputType`、`noData`；
  - GTiff 直接创建；PNG/JPEG/COG/ENVI 等 CreateCopy-only 驱动先写临时 GTiff 再 Translate。
- 注册表 `gdal:grid` 从 blocked/indexed 改为 ready/form，补充 zField/width/height/outputBounds 参数；GUI 新增插值算法、高程字段控件，显示输出格式/数据类型/输出尺寸。
- 空 `algorithm`/`zField` 与非法组合在契约层拒绝；输出格式矩阵按 Float64/显式 outputType 预检。

**二、数值验收（解析平面 z = 2x + 3y）**

点集：`(0,0)`、`(4,0)`、`(0,2)`、`(4,2)`、`(2,1)`，属性 `value = 2x + 3y`。

- `algorithm=linear`, `width=4`, `height=2`, `outputBounds=(0,0,4,2)`, `zField=value`：
  - 输出 4×2 Float64；
  - 顶行 `[5.5, 7.5, 9.5, 11.5]`，底行 `[2.5, 4.5, 6.5, 8.5]`，与像素中心解析值逐项一致。
- 缺 `width/height`：`invalid_parameters`，不写文件。
- PNG 默认 Float64：`unsupported_format_combination`，不写文件。
- GUI 真实 `MainWindow` + Worker + QTest：打开点图层、选择 grid、linear/zField/4×2 提交成功并自动加载成果。

**三、验证**

- 新增 `tests/integration/test_p5_grid.py`（2 项数值/边界）与 `tests/integration/test_p5_grid_gui.py`（1 项桌面路径）。
- 受影响定向组合：**51 passed**（29.18s）；Grid GUI 单项 1 passed（1.69s）。
- Ruff 干净；Mypy 66 个源文件 0 错误。
- Clean 全量：**657 passed / 0 failed / rc=0 / clean_exit_verified=true**（358.19s，job 观测、residual=0、cleanup_complete=true）。
- 审计刷新：缺口 6 → **5**（`gdal:grid` 移出 blocked）；剩余为 `gdal:cli:gdal_auth`、HDF5 与 3 个 rio 插件。

**四、边界与下一步**

- P5-02 保持 **in_progress**：polygonize、contour 尚未接入。
- 两者需要矢量成果输出计划、OGR 重开校验与图层登记；当前提交校验只打开栅格，不能直接复用。
- 下一步：`gdal:polygonize`（已知标签栅格→多边形，验证字段值与面积），随后 `gdal:contour`（解析平面等高线高程属性）。

### 第 49 轮：P5-02 增量二——rasterize 矢量栅格化（P5-02 in_progress）

**一、实现**

- `processing/raster_params.py` 新增 `burnValues`、`attribute`、`allTouched` 别名与 `gdal:rasterize` 支持矩阵；`burnValues` 与 `attribute` 在契约层互斥，禁止同时提交。
- `GDALProviderAdapter` 新增 `gdal:rasterize` 分支：
  - 以 `gdal.OpenEx(..., OF_VECTOR)` 打开 GeoJSON/GPKG/SHP 等矢量输入；
  - 未指定 `outputBounds` 时按所有图层 extent 自动计算（OGR `GetExtent` 的 `minX,maxX,minY,maxY` 显式转换为 GDAL 的 `minX,minY,maxX,maxY`）；
  - 支持 `burnValues`、`attribute`、`allTouched`、`xRes/yRes`、`width/height`、`outputBounds`、`outputType`、`noData`；
  - 无 `xRes/yRes` 且无 `width/height` 时明确拒绝，不猜测输出网格；
  - GTiff 直接创建；PNG/JPEG/COG/ENVI 等 CreateCopy-only 驱动先栅格化到临时 GTiff，再 `Translate` 到目标格式，避免 `Rasterize` 直接创建失败。
- 注册表 `gdal:rasterize` 从 blocked/indexed 改为 ready/form，补充 xRes/yRes/outputBounds 参数与限制说明；`CONNECTED_CAPABILITIES`、`FORMAT_CAPABILITIES` 与 GUI 同步。
- GUI 新增 burnValues / attribute / allTouched 控件，rasterize 显示输出格式、数据类型、xRes/yRes 与输出尺寸；提交前按输出 dtype/波段数执行格式矩阵预检。

**二、数值验收（已知矩形/标签图）**

GeoJSON：两个 2×2 矩形，x=0..2 的 `value=3`，x=2..4 的 `value=9`。

- `burnValues=[7]`, `xRes=1`, `yRes=1`：输出 4×2，全部为 7，CRS 保留。
- `attribute="value"`：输出 `[[3,3,9,9],[3,3,9,9]]`，字段标签图正确。
- `allTouched=True`：输出全部 7，覆盖规则生效。
- `burnValues` + `attribute` 同时给出：`invalid_parameters` 且说明互斥。
- 无输出网格参数：`invalid_parameters`，不写文件。
- PNG 默认 Float64：`unsupported_format_combination`；显式 `outputType=Byte` 后通过两步 CreateCopy 成功，输出 Byte 且值正确。
- GUI 真实 `MainWindow` + Worker + QTest：矢量源打开、rasterize 表单提交成功并自动加载成果；PNG 默认 Float64 在发出 TaskSpec 前被拦截。

**三、验证**

- 新增 `tests/integration/test_p5_rasterize.py`（4 项数值/边界）与 `tests/integration/test_p5_rasterize_gui.py`（1 项桌面路径）。
- 受影响定向组合（DEM、rasterize、基准对账、基线审计、UI、smoke、wiring）：**49 passed**（27.76s）。
- Ruff 干净；Mypy 66 个源文件 0 错误。
- Clean 全量：**654 passed / 0 failed / rc=0 / clean_exit_verified=true**（356.58s，job 观测、residual=0、cleanup_complete=true）。
- 审计刷新：缺口 7 → **6**（`gdal:rasterize` 移出 blocked）；`gdal:grid` 仍 blocked，`gdal_auth`/HDF5/3 个 rio 插件仍缺失。

**四、边界与下一步**

- P5-02 保持 **in_progress**：polygonize、contour、grid 尚未接入。
- polygonize/contour 需要“成果为矢量”的输出计划、OGR 重开校验与图层登记；当前提交校验只打开栅格，不能直接复用。
- 下一步：`gdal:polygonize`（已知标签栅格→多边形，验证字段值与面积），随后 `gdal:contour`（解析平面等高线高程属性），最后 `gdal:grid`（散点平面插值）。

### 第 48 轮：P5-02 首增量——DEM 地形分析接入（P5-02 in_progress）

**一、实现**

- `processing/raster_params.py` 新增 `DEM_PROCESSINGS`、`processing`/`zFactor`/`scale` 别名与 `gdal:dem` 参数支持矩阵；`color-relief` 因缺少颜色表参数，在契约层明确拒绝，不做“接受后忽略”。
- `GDALProviderAdapter` 新增 `gdal:dem` 分支，调用 `gdal.DEMProcessing`，复用既有 staging → 重开校验 → 事务提交 → sidecar 处理流程；支持 hillshade/slope/aspect/TRI/TPI/roughness 与 zFactor/scale。
- DEM 输出固定 Float32 单波段；执行前按格式矩阵拒绝 PNG/JPEG，避免“源是 Byte 所以放行、成果却写不出”的错配。
- 注册表 `gdal:dem` 从 blocked/indexed 改为 ready/form；限制说明明确 color-relief 与格式矩阵边界。
- GUI 表单新增地形模式、zFactor、scale 控件，输出格式选择；提交前预检复用共享矩阵。

**二、数值验收（解析平面 z = 2·col + 3·row，1×1 像元）**

- 坡度：`atan(√13)` **74.49864°**，中心与内部像元一致，边缘按 GDAL 语义为 NoData。
- 坡向：**326.30994°**，与 GDAL 解析平面对照一致。
- 山体阴影：默认参数 **219.0**；`zFactor=2` 时为 **200.0**，证明参数真实参与。
- `scale=0.5` 时坡度 **82.10486°**，证明垂直比例因数真实参与。
- `color-relief` 返回 `invalid_parameters` 并说明需要颜色表；PNG 返回 `unsupported_format_combination`，两者均不写出目标。
- GUI 真实 `MainWindow` + 真实 Worker + QTest 提交 slope 成功并自动加载成果；同一表单选择 PNG 时在发出 TaskSpec 前被拦截。

**三、验证**

- 新增 `tests/integration/test_p5_dem.py`（2 项，解析平面数值）与 `tests/integration/test_p5_dem_gui.py`（1 项，真实桌面路径）。
- 受影响定向组合（DEM、基准对账、基线审计、UI、smoke、wiring）：**44 passed**（22.60s）。
- Ruff 干净；Mypy 66 个源文件 0 错误。
- Clean 全量：**649 passed / 0 failed / rc=0 / clean_exit_verified=true**（360.88s，job 观测、residual=0、cleanup_complete=true）。
- 审计刷新：注册表 448 条；缺口 8 → **7**（`gdal:dem` 移出 blocked）；`gdal:grid`/`gdal:rasterize` 仍 blocked，`gdal:cli:gdal_auth`、HDF5 与 3 个 rio 插件仍缺失。

**四、边界与下一步**

- P5-02 保持 **in_progress**：rasterize、polygonize、contour、grid 尚未接入；其中 polygonize/contour 需要扩展“成果为矢量”的输出计划、OGR 重开校验与图层登记，不能复用仅栅格的提交校验。
- `color-relief` 需要颜色表文件参数，尚未接入。
- 下一步：`gdal:rasterize`（矢量→栅格，已知矩形/标签图验证），随后 `gdal:polygonize`/`gdal:contour` 与矢量成果通道，最后 `gdal:grid`。

### 第 47 轮：P5-01 基准逐项对账（P5-01 → done）

**一、对账口径**

- 此前的 `audit_report().total_baseline_count` 实际是**注册表条目数**（448），不是契约基准文件条目数（443），容易把“注册表分母”误读为“基准分母”。
- 新增 `CapabilityRegistry.reconcile_baseline()`：读取 `contracts/baselines/gdal-3.13.3.json` 与 `rasterio-1.5.2.json`，逐项比较 capability_id，并输出：
  - `missing_from_registry`：基准有、注册表无（必须为空）；
  - `platform_only`：注册表有、基准无的平台组合/受限实现（显式列出）；
  - `missing_or_blocked`：所有缺失/受阻条目及其 limitations；
  - `unexplained_gaps`：没有原因说明的缺口（必须为空）；
  - `complete`：只表示“基准对账完整且缺口均有说明”，**不表示所有能力 ready/verified**。
- `scripts/audit-capabilities.ps1` 的 JSON 增加 `baseline_reconciliation` 段，并在控制台输出基准/注册表差异、平台扩展与对账结论。

**二、实测结果**

- 契约基准：**443** 条（GDAL 402 + Rasterio 41），全部在注册表中，`missing_from_registry=[]`。
- 注册表：**448** 条；`platform_only` 恰为 5 条平台实现：`gdal:bandmanage` / `gdal:editmeta` / `gdal:overview` / `gdal:stack` / `gdal:statistics`。
- 运行期缺口 **8** 条，均有具体原因：
  - `gdal:dem` / `gdal:grid` / `gdal:rasterize`：已编目，适配器尚未接线（blocked）；
  - `gdal:cli:gdal_auth`：Windows QGIS 运行时未编译该命令（unsupported）；
  - `gdal:driver:HDF5`：运行时未链接 HDF5（missing_dependency）；
  - `rio:cli:cogeo` / `rio:cli:color` / `rio:cli:mbtiles`：可选插件未安装（missing_dependency）。
- 目标版本（GDAL 3.13.3）新增/组合工具均包含在基准与注册表中；P5-01 只证明入口与基准完整性，不把这些 blocked 能力表述为可执行。

**三、验证**

- 新增 `tests/unit/test_capability_baseline_reconciliation.py`（2 项）：基准无缺失、平台扩展集合明确、目标版本工具已编目、8 条缺口均有原因、`complete=true`。
- `scripts/audit-capabilities.ps1` 实测退出码 0，报告写入 `docs/progress/runs/capability_audit.json`。
- Ruff 干净；Mypy 66 个源文件 0 错误。
- Clean 全量：**646 passed / 0 failed / rc=0 / clean_exit_verified=true**（428.99s，job 观测、residual=0、cleanup_complete=true）。

**四、状态与下一步**

- P5-01 转为 **done**。
- 下一步 P5-02：栅格化、多边形化、等值线及地形工具。必须用已知矩形/标签图与解析平面做数值验证，不能只检查文件存在；`gdal:rasterize` / `gdal:dem` / `gdal:grid` 的适配器接线属于该任务范围。

### 第 46 轮：长周期 Worker 压力与 native dump 证据

**一、长周期 Worker/句柄压力（可复现脚本）**

- 新增 `scripts/stress_worker_lifecycle.py`：同一 `WorkerProcess` 连续 200 轮 start/probe/stop，每 10 轮执行一次真实 `gdal:translate`，每轮记录 PID、generation、父进程句柄数、线程数、工作集/私有内存；任一任务失败、线程残留或句柄增长超过 16 即返回非零。
- 实测：**200 轮完成 / 20 个 GDAL 任务成功 / 111.2s**；句柄 160 → 167（增长 **+7**，最大增长 +7）；Worker 线程残留 **0**；私有内存仅 +96 KB；失败列表为空。
- 报告：`docs/progress/runs/worker_long_run.json`。该结果覆盖短中期 start/stop 压力，不替代数小时级真实桌面会话；P9-04 仍为 todo，只登记为部分证据。

**二、native dump 与历史 0xC0000409 签名**

- 纠正：HKCU `LocalDumps` 实际已配置（`DumpFolder=work/crash_dumps`、`DumpType=2`、`DumpCount=5`），并非“未配置”；但本机 WER 不把 `os.abort()` 的 dump 留在该目录，而是在 `C:\ProgramData\Microsoft\Windows\WER\Temp` 生成临时 `.mdmp` 后删除。
- 通过轮询 WER Temp 并即时复制，成功捕获受控 `os.abort()` 的 **1,783,483 字节 minidump**（SHA-256 `e8df873d…58a7857`）。解析结果：`ExceptionCode=0xC0000409`、异常地址 `ucrtbase.dll+0xa527e`、`fastfail` 参数 **7**（fatal-app-exit）。
- 历史 WER `Report.wer`（2026-11-06 18:51/18:57，项目 pytest 进程）签名完全相同：`ucrtbase.dll` / `0xa527e` / `c0000409` / 参数 7。故可确认历史异常走的是 **CRT abort / fatal-app-exit 路径**，不是笼统的随机栈破坏；**仍未定位是谁调用了 abort**，不宣称根因或已根治。
- 负结果：`tests/conftest.py` 安装的 SIGABRT handler 在该环境**拦截不到** `os.abort()`（子进程仍直接 `rc=3221226505`，无 `[abort-capture]` 输出）。已修正注释，避免把该 handler 当成已生效的防线。
- 分析报告：`docs/progress/runs/controlled_abort_dump_analysis.json`。受控 dump 仅用于验证捕获链路与签名对照，不是产品崩溃现场。

**三、验证与边界**

- 定向：`scripts/stress_worker_lifecycle.py` 退出码 0；Ruff 检查通过；诊断改动后的最终 Clean 全量：**644 passed / 0 failed / rc=0 / clean_exit_verified=true**（362.45s，`process_observation=job`、`residual=0`、`cleanup_complete=true`）。
- native dump 捕获目前依赖 WER Temp 轮询，不是自动常驻机制；若未来再次复现，应按同一流程保留 dump 后再分析调用方。
- P4-02 保持 **done**；P9-04/P9-05 仍为 todo，本轮只登记部分证据，不改变里程碑状态。

### 第 45 轮：S5 P4-02 桌面端到端验收

**一、桌面路径补强**

- 输出格式选择器扩展为「适配器真实消费 `format` 的能力」：merge / stack / bandmanage 现在可在表单选择 GTiff/ENVI/COG/PNG/JPEG 并写入 TaskSpec；buildvrt 仍只产出 VRT，统计仍固定 JSON artifact。该范围由 `domain/capability_wiring.py` 的 `supports_output_format` 单一声明，并新增一致性测试守护。
- `probe_dataset` 对 VRT 增加失效源预检：读取 `GetFileList()`，对本地源逐个检查存在性（跳过 `/vsi*` 与远程 URI）。VRT 元数据可能脱离源打开，但像元读取必然失败；桌面打开现在明确报错，不再登记一个看似正常却无法读取的空图层。
- 保留既有的 GUI 格式矩阵预检与 Worker 权威校验；S5 不引入新的绕过路径。

**二、真实桌面端到端用例（`tests/integration/test_p4_desktop_e2e.py`，6 项）**

1. **输入顺序即重叠优先级**：真实打开源 A、在工具箱选中 `gdal:merge`、向多输入列表加入 A/B，通过真实按钮提交。默认 `last` 重叠区取 B，切到 `first` 后取 A；两个成果均自动进入图层树。保存 `.spectra.json` 后在新窗口重开，3 个图层路径与顺序一致、无缺失源。
2. **混合 CRS 拒绝且旧成果不变**：目标文件预置为 99 值。提交 EPSG:4326 + EPSG:3857 拼接，Worker 返回 `incompatible_sources`；目标文件字节完全不变、独立重开仍为 4×4/99，失败成果不进入图层树。
3. **ENVI 新建与覆盖**：`gdal:bandmanage` 选择 ENVI、`bandList=2,1` 提交，`.dat/.hdr` 成套落盘并按 2,1 重排；同目标改为 `bandList=1,2` 覆盖，重开后顺序正确，无旧头文件语义残留。
4. **alpha / 格式矩阵**：Byte RGBA 源输出 PNG 成功，4 波段且 alpha color interpretation 保留；JPEG+alpha、ENVI+alpha、PNG+Float32 均在提交前被 GUI 拦截，未写出文件、未发出 TaskSpec。元数据来自真实 Worker 探测，不是测试注入的假字典。
5. **真实计算取消与重试**：4096×4096 Float32 源经 `gdal:translate` 真实计算，在 GDAL progress ≥2% 时点击任务面板「取消」，得到 `cancelled` / `task_cancelled`，目标文件不存在；随后点击「重试任务」，同一 `task_id`、`attempt=2`、`succeeded`，成果可独立重开为 4096×4096。
6. **VRT 源失效与项目缺失源**：GUI 构建 VRT（相对源引用），保存项目后仅把 VRT 移到更深目录，`open_dataset` 明确提示「数据打开失败」且不新增图层；原项目重开时 VRT 进入缺失源列表，状态栏显示「待重新关联 1」，其余源正常加载。

**三、验证**

- 定向组合（S5 + P4-02 GUI + GUI 格式预检 + wiring 一致性）：**32 passed**（30.22s）。
- Ruff 干净；Mypy 66 个源文件 0 错误。
- Clean 全量：**644 passed / 0 failed / rc=0 / clean_exit_verified=true**（354.68s，`process_observation=job`、`residual=0`、`cleanup_complete=true`、`source_snapshot_changed=false`）。

**四、状态与剩余边界**

- P4-02 依据本轮桌面验收转为 **done**；不再以「仅后端可调用」表述。
- 全局剩余项不变：0xC0000409 根因未确证；物理高 DPI/多显示器切换、长周期 worker/句柄压力仍需真实环境验证；GUI 元数据探测在提交路径内同步执行，本地文件耗时可接受，但远程/慢盘路径可能阻塞 UI；`audit-capabilities.ps1` 仍只审计注册表级状态，当前输入可执行性尚未成为独立审计维度。这些不改变 P4-02 原验收结论。

### 第 44 轮：S4 GUI 格式预检与注册表限制同步

**一、GUI 预检**

- `CapabilityFormWidget` 新增 `_preflight_output_format`，直接复用 `processing/output_formats.py` 的 `validate_output_format`，在组装并发出 `TaskSpec` 之前拦截不支持的组合。
- `MainWindow` 通过 `set_metadata_provider` 注入只读元数据提供者（`gdal_client.probe_dataset`），表单据此获得源 dtype、波段数、NoData、alpha 与 mask 标志。
- 无元数据时仍执行不依赖源数据的规则：PNG/JPEG + NoData 参数明确拒绝，显式输出 dtype 也会校验；不做「没有元数据就放行一切」的静默放行。
- Worker 仍是权威校验点：GUI 拦截只改善反馈时机，后端 `unsupported_format_combination` 与提交前实际重开核对保持不变。
- 注册表 `limitations` 同步 buildvrt / stack / bandmanage / merge 的混合 CRS、波段结构、格式矩阵与 sidecar 边界说明。`ready` 仍只表示「已接入」，不代表任意输入可执行。

**二、测试与证据**

- 新增 `tests/integration/test_p4_gui_format_preflight.py`（6 项）：元数据驱动的 PNG Float32 拒绝、JPEG alpha 拒绝、PNG Byte RGB 放行、无元数据 PNG+NoData 拒绝、提交被拦截且不发出 TaskSpec、主窗口注入提供者。
- 定向 GUI 回归（格式预检 + P4-02 GUI + 完整用户流程）**22 passed**（15.69s）。
- Ruff / Mypy（66 个源文件）干净。
- Clean 全量：**637 passed / 0 failed / rc=0 / clean_exit_verified=true**（378.96s，`process_observation=job`、`residual=0`、`cleanup_complete=true`、`source_snapshot_changed=false`）。

**三、失败样本保留**

- 新增 GUI 用例首跑时未在构造 `QWidget` 前创建 `QApplication`，子进程 `rc=3221226505`；`work/test_runs/s4_gui.log`、`s4_gui1.log` 保留现场。
- 修正为模块级 `qapp` + autouse 夹具后 6 项通过。这是测试夹具错误，不是产品退出路径异常；**不据此声称 0xC0000409 已根治**。

**四、边界**

- 元数据提供者在提交路径内同步调用 `probe_dataset`，最多等待 10 秒，可能阻塞 UI；S5 桌面验收需评估并处理。
- `scripts/audit-capabilities.ps1` 仍只审计注册表级 presence/binding/validation/availability，当前输入可执行性尚未成为独立审计维度。
- S5 P4-02 桌面端到端验收仍未开始；P4-02 保持 implemented。

### 第 43 轮：S4 输出格式矩阵与执行前预检

**一、矩阵**

新增 `processing/output_formats.py`（纯 Python，GUI 可导入），按 GDAL 3.13.3 实测登记：

| 格式 | dtype | 波段 | NoData | mask | alpha |
|---|---|---|---|---|---|
| GTiff | 常见全部 | 任意 | 是 | 是 | 是 |
| COG | 按 GTiff | 任意 | 是 | 是 | 是 |
| ENVI | Byte/UInt/Int/Float/Complex | 任意 | 是 | 是 | **否**（重新打开不保留 alpha color interpretation） |
| PNG | Byte/UInt16 | 1/2/3/4 | 否 | 否 | 是 |
| JPEG | Byte | 1/3 | 否 | 否 | 否 |
| VRT | 继承源 | 任意 | 是 | 是 | 是 |

**二、执行前/提交前**

- P4-02 merge/stack/bandmanage 与通用 Translate/Warp 在写盘前按源 dtype、波段数、NoData/mask/alpha 预检；不支持组合返回 `unsupported_format_combination`。
- P4-02 在提交前用实际重开结果再次核对矩阵，防止驱动静默丢 alpha/NoData/mask 或改变 dtype。
- COG 实际驱动名为 GTiff；通用输出记录改为取实际重开驱动，不登记调用者默认格式。
- PNG/JPEG 的 sidecar（如 `.aux.xml`）继续由成果集合映射与事务提交。
- GUI 表单尚未按矩阵禁用/解释；当前由 worker 明确拒绝并返回原因。能力注册/审计未同步。

**三、验证**

- `tests/unit/test_output_format_matrix.py`（7 项）。
- `tests/integration/test_p4_format_matrix.py`（8 项）：PNG RGB、PNG RGBA、JPEG RGB、PNG Float32 拒绝、JPEG alpha 拒绝、PNG dstNodata 拒绝、ENVI alpha 拒绝、COG Float32+NoData 成功。
- 定向专项 **15 passed**（6.96s）。
- Clean 全量：**631 passed / 0 failed / rc=0 / clean_exit_verified=true**（368.03s，`process_observation=job`、`residual=0`、`cleanup_complete=true`）。

**四、边界**

- S4 后端矩阵与预检完成；GUI 层按矩阵禁用/解释、注册表 limitations 与可执行审计尚未更新。
- S5 P4-02 桌面端到端验收仍未开始。

### 第 42 轮：S3 阶段二 sidecar 事务删除

**一、事务扩展**

- `ExecutionContext.commit_files` 新增 `delete_paths` 与 `protected_paths`：
  - `delete_paths` 先进入 WAL 意图，与已有目标一样先 `os.replace` 到同目录备份；
  - 安装新成果后到达 `committed` 决断点，再清理旧 sidecar 备份；
  - 决断点前任何失败回滚已安装新文件并恢复完整旧集合（含待删除 sidecar）；
  - 决断点后只做幂等清理，绝不反向恢复已提交删除。
- `StagingManifest` 新增 `deleted_files`；`recover_staging` 能在 `committing` 状态恢复 delete 备份，在 `committed` 状态清理备份且不恢复已删除 sidecar。
- 适配器把 `stale_sidecars` 与 `protected_inputs` 传入事务；protected 输入仍拒绝删除/覆盖，未知归属仍拒绝。
- 新增 `ProtectedInputError`；提交层二次防御，即使适配器误传也不允许删除输入依赖。

**二、验证**

- `tests/integration/test_staging_manifest.py` 新增 3 项：删除旧 sidecar 时第二个文件移动失败 → 旧主文件/旧 sidecar 成套恢复；committed 前强杀恢复 delete 备份；committed 后恢复不反向恢复已删除文件。
- `test_p4_sidecar_ownership.py` 的 stale 场景从“拒绝”升级为“事务删除成功且旧 sidecar 消失”，输入碰撞/受保护输入仍拒绝。
- 专项：`test_p4_sidecar_ownership.py + test_staging_manifest.py` **25 passed**（18.44s）。
- Clean 全量：**616 passed / 0 failed / rc=0 / clean_exit_verified=true**（336.14s，`process_observation=job`、`residual=0`、`cleanup_complete=true`）。

**三、边界**

- S3 阶段二完成；未扩大到 S4 格式矩阵/S5 桌面端到端。
- 删除动作只针对 `old_owned - new_owned` 且已证明不属于 protected 的文件；unknown 仍不自动删除。

### 第 41 轮：S3 阶段一：sidecar 所有权与输入保护

**一、保护集与三集合语义**

- `protected_inputs`：所有输入主文件及真实读取依赖（ENVI header、mask、VRT 源依赖等），默认只读。提交前检查 `new_targets` 与 `protected_inputs` 是否相交；相交明确拒绝，即使输出主文件不存在或 `overwrite=true`。
- `old_owned`：既有目标驱动的 `GetFileList()` 与目标命名规则共同确认的文件；覆盖 ENVI `.hdr`、PAM `.aux.xml`、外部 `.ovr`、`.msk`、world file（`.tfw/.tifw/.jgw/.pgw/.wld`）等。
- `new_owned`：本次 staging 中真实生成的完整成果集合，按最终目标名映射。
- `stale_owned = old_owned - new_owned`：本次不生成的旧 sidecar。
- VRT 的 `GetFileList()` 输入依赖先于命名候选被识别为 protected；源文件绝不是目标 sidecar，不参与删除。非 VRT 同目录但无法分类的文件记 `unknown`，同样拒绝覆盖。
- 命名规则只产生候选：去掉了“任意 `target.name + '.'` 前缀即 owned”的宽泛判断，`mosaic.vrt.source.tif` 不再被误判为 `mosaic.vrt` 的 sidecar。显式 in-place 能力（editmeta/overview）按单独授权允许目标自身数据集文件，不放宽其他输入。

**二、保守拒绝（阶段一）**

- 新增 `providers/gdal_provider/sidecars.py`；P4-02 与通用 Translate/Warp 在 `commit_files` 前调用 `ensure_no_stale_sidecars`。
- 输出集合与 protected inputs 相交返回 `input_sidecar_collision`；发现 stale/unknown 返回 `stale_sidecar_conflict`。两者都附带文件列表，旧目标、旧 sidecar 和输入文件保持原样；不执行任何 `basename*` 通配删除。
- 目标不可读但不存在任何可识别 sidecar 时，仍允许正常 `overwrite` 替换主文件（保留既有“覆盖损坏目标”能力）。
- 不做“提交后反向回滚”；阶段二的事务删除（旧集合备份、删除记录、崩溃恢复）尚未实现。

**三、验证**

- 新增 `tests/integration/test_p4_sidecar_ownership.py`（13 项）：GTiff 旧 `.aux.xml/.ovr/.msk/.tfw` 拒绝覆盖、ENVI 正常覆盖成功、ENVI 旧 `.ovr` 拒绝、中文空格目标、损坏目标无 sidecar 可替换、VRT 源 `mosaic.vrt.source.tif` 重复构建不误拒且源哈希不变、merge/stack 输出 `source.img` 与输入 `source.hdr` 碰撞拒绝且输入尺寸/哈希不变、通用 Translate 同类碰撞拒绝、显式 in-place editmeta 仍允许。
- P4 定向组合（含 sidecar 专项）：**85 passed**（78.39s）。
- Clean 全量：**613 passed / 0 failed / rc=0 / clean_exit_verified=true**（350.74s，`process_observation=job`、`residual=0`、`cleanup_complete=true`）。
- 21:18 轮曾出现 **607 passed / 1 failed**：初次实现把“目标不可读且无任何 sidecar”误判为所有权未知，拒绝覆盖损坏主文件；修正为仅在 stale/unknown 存在时拒绝后通过。该失败轮保留在 `work/test_runs/test_all.log`。

**四、边界**

- S3 阶段一完成；事务删除、旧集合备份、删除动作 WAL 与故障注入恢复留待阶段二。
- S4 格式矩阵、S5 桌面端到端仍未开始。

### 第 40 轮：S2 Worker 生命周期状态机

**一、状态与并发启动**

- `WorkerProcess` 新增显式 `WorkerState`：`STOPPED → STARTING → READY → STOPPING`，启动失败进入 `FAILED`。
- 并发 `start()` 由条件变量共享同一次启动：第一个调用者成为 owner，其余等待同一个 ready/异常，不再各自拉起进程。
- `QtWorkerClient.is_alive` 只表示 `READY` 且进程存活；原先 `if not is_alive: start()` 的请求路径在 STARTING 也会等待共享握手，不会提前发请求。

**二、代际与管道绑定**

- 每次真正启动分配独立 generation，并更新 `GenerationGuard`；每次启动绑定独立 `Popen`、stdin/stdout/stderr、`stop_event` 和两个读取线程（线程名含 generation）。
- 读取线程使用不可变的本次进程/管道/代际，不再通过可变的 `self._proc` 读取下一代；旧 generation 的迟到 ready 在 `_guard` 与 `_on_envelope_received` 双重拒绝。
- 启动阶段记录 `spawn_start`、`spawn_done`、`hello`、`ready` 时间；超时错误携带 timings、PID/存活状态和有限 stderr 尾部。

**三、故障边界**

- 启动中 `stop()`：设置 STOPPING/cancel，终止本次进程，等待 owner 收敛；`start()` 返回失败而不是 ready。
- 握手超时：进入 FAILED，清理进程/读取线程；再次 `start()` 自动推进 generation，复用对象不会拿到旧 ready。
- 提前退出：在握手期间检测进程退出码并携带 stderr 摘要报错。
- `start/stop` 4 轮循环与 4 Worker × 3 轮并发循环验证线程名收敛、进程逐代不同、句柄增长有界。

**四、验证**

- 新增 `tests/integration/test_worker_state_machine.py`（8 项）：并发 start 共享、启动中 stop、超时后重启、提前退出、迟到旧 ready、4 轮收敛、4×3 并发循环、QtWorkerClient 并发 start。
- Worker 相关定向：`test_worker_state_machine.py + test_worker_process.py + test_worker_lifecycle_guard.py + test_scheduler_lifecycle.py + test_p4_worker_dispatch_hardening.py + test_p3_exit_lifecycle.py` **41 passed / rc=0**（18.55s）。
- Clean 全量：**600 passed / 0 failed / rc=0 / clean_exit_verified=true**（307.80s，`process_observation=job`、`residual=0`、`cleanup_complete=true`）。

**五、边界**

- S3 sidecar 所有权/事务删除、S4 格式矩阵、S5 桌面端到端仍未开始。
- 未增加自动重试或扩大握手超时；失败仍按 FAILED 暴露，由调用方显式重启。

### 第 39 轮：S0 冻结 / S1 可信退出验收

按《未解决问题收敛方案》先做 S0→S1；S2 之后的生命周期/ sidecar / 矩阵 / 桌面验收尚未在本轮展开。

**一、S0 冻结与分拆（已完成）**

- 快照：`work/runs/S0-freeze-20261106-181403`（HEAD、`git diff --binary`、未跟踪文件副本、SHA-256 清单、运行环境、全量与专项日志）。
- 第 37 轮工作树拆分为四个独立提交：
  - `e9d8f8b`：项目内独立 pytest basetemp；
  - `345c019`：worker 派发/握手状态/读者线程收口；
  - `0bf6a21`：高 DPI 渲染与截图 dpr；
  - `d43e03f`：课程真实栅格数据验收。
- 拆分前针对性验证：worker/高 DPI/课程数据 35 passed；快照期间固定源码，不边改边对照。

**二、S1 可信退出（已加固）**

- `tests/conftest.py` **删除** `pytest_unconfigure` 中的 `os._exit`；默认完整走 pytest 收尾与 Python finalization。
- 新增 `scripts/pytest_runner.py`：Clean 模式正常返回；Bypass 只能在 `pytest.main()` 返回后显式 `os._exit`，并写 `bypass_finalization=true`、`clean_exit_verified=false`。
- `scripts/run_with_timeout.py` 作为外层监督器**持有运行身份**并注入 child 环境；final、child preliminary、cleanup report 使用三个不同文件，启动前删除旧文件，不再从旧 JSON 覆盖 run_id/snapshot。
- 严格校验 child 结果：schema、run_id、source_snapshot、实际/期望 exit_mode、bypass 标记、completed、pytest_was_run、finished_at、runner_error、pytest 退出码一致性、cleanup complete/errors。旧结果、损坏 JSON、缺失结果、模式不符均失败（返回 92）。
- 进程跟踪改为 Windows **Job Object**（kill-on-close），并采用 **挂起创建 → 加入 Job → 恢复运行**：`CREATE_SUSPENDED` 期间子进程无法执行任何代码，加入 Job 后先用 `NtResumeProcess`（失败则枚举线程 `ResumeThread`）恢复；assign/resume 任一步失败都会终止挂起子进程、关闭 Job 并返回 92，不会留下未受管的启动窗口。
- 父进程退出后仍能查询 Job 内存活后代，不再依赖已消失中间层的 ParentProcessId 递归。清理记录前/后 PID 与错误；无法确认完整观测标 `unknown`，不得为绿。
- cleanup report 由 pytest `sessionfinish` 真实写入；Qt teardown 的每一步异常不再被 `suppress` 抹去，缺失/不完整/有错误都使 Clean 失败。
- `scripts/test.ps1` 默认 Clean；`-BypassFinalization` 功能通过也返回 **91**，不满足发布门槛；Clean 未验证返回 90。
- `scripts/source_snapshot.py` 使用 `git ls-files --others --exclude-standard -z` 枚举未跟踪文件，逐个纳入规范路径/长度/内容哈希；目录递归、中文、空格均覆盖；读取失败标 `verified=false`；测试输出 `docs/progress/runs/**` 与 `work/**` 排除。
- 新增 `tests/unit/test_exit_supervisor.py`（19 项）：旧结果、mode mismatch、run_id/snapshot 不符、缺失/损坏/未完成 child、真实失败 pytest、超时、残留后代、assign→resume 顺序、挂起期间未执行用户代码、resume 失败清理、进程观测失败、cleanup error、真实 cleanup report 注入、未跟踪嵌套目录内容变化、生成目录排除。

**三、退出异常修复与证据边界**

- 默认 Clean 全量曾复现两类崩溃：pyqtgraph `AxisItem` 构造中 Python GC 触发（Python 栈 `Garbage-collecting`）与 finalization 阶段；均为 child rc=3221226505。
- 措施：
  1. `pytest_runtest_teardown` 在用例边界 `gc.collect()`，避免循环垃圾在无关 Qt 构造中途回收（测试隔离，不宣称根因修复）；
  2. `pytest_sessionfinish` 中对 QApplication 使用 `deleteLater()` + DeferredDelete，在 finalization 前显式销毁单例；此前直接调用 `app.shutdown()` 会在该处 fastfail，已移除。
- 修复后早期 **两次连续 Clean 全量**：578 passed / 0 failed / rc=0 / `clean_exit_verified=true`（285.62s、307.71s）；监督器加固后 **592 passed / 0 failed / rc=0 / clean_exit_verified=true**（309.17s，`process_observation=job`、`residual=0`、`cleanup_complete=true`）。期间 20:17 轮 590 passed / 1 failed 是新增 resume 失败测试期望未同步（产品代码已返回 92），修正测试期望后通过；失败轮保留在日志中。
- 监督器负向独立复现（`work/runs/S1-hardened-negative.json`）：旧结果 + 普通命令 → rc=92/clean=false；真实 Bypass + 外层 Clean → rc=92/actual=bypass/clean=false；父进程退出但后代存活 → `residual_processes=[7700,39752]`、clean=false；未跟踪嵌套文件内容变化 → snapshot 改变。临时进程已按 Job Object 清理并二次查询为空。Job 接管竞态专项证据 `work/runs/S1-job-race.json`：assign/resume 回调时 child 用户代码尚未执行（`child_code_ran=false`），resume 后标记文件才出现；立即派生后代的残留被记录并清理。
- 历史失败轮与诊断日志保留：`work/test_runs/test_all.log`、`work/runs/S1-exit-20261106-190842`；WER 事件确认 `ucrtbase.dll` / c0000409 / offset 0xa527e，但当前无 native dump（非管理员、LocalDumps 未配置），**不根据 DLL 名宣称根因**，也不宣称所有 Qt 关闭路径永久根治。

**四、当前边界**

- S2 Worker 启动状态机、S3 通用 sidecar 事务删除、S4 格式矩阵、S5 桌面端到端仍未完成；P4-02 继续 `implemented`。
- 本轮只把“退出验收”恢复为可信门槛，不等于所有未解决问题已收敛。

### 第 38 轮：P4-02 复核补强（成果集合、NoData/mask/alpha、源集合、VRT 路径与取消）

复核基线 `7118db3`；本轮起点 HEAD `f8850ad`（已含第 30 轮 `60b63f0` 的 P4-02 修正与第 32 轮 `1f0c2e4` 的 GUI 接入）。先核对差异，保留既有有效修复；不重写已通过的默认 Translate/Warp 主路径，不扩展 P4-03/P4-04。

**一、复核发现的仍存缺陷（独立复现，非仅读代码）**

| 复核项 | 本轮独立复现 | 修正 |
|---|---|---|
| BuildVRT 混合 CRS | 只对 merge/stack 做了 CRS 预检，`gdal:buildvrt` 仍会静默跳过第二个源并 succeeded | buildvrt 同样预检；源集合不一致明确拒绝 |
| 无地理参考源 | 非 separate BuildVRT 对无 GeoTransform 影像发 warning 后整体跳过，旧检查未覆盖 | 非 separate 明确拒绝；separate=True 实测仍可逐源成带，允许但保留无地理参考事实 |
| 源集合 | BuildVRT/merge/stack 只看返回值/首个源，未核对实际使用了哪些源 | 从 `GetFileList()` 核对实际源集合；捕获 GDAL warning 随错误返回 |
| 成果收集 | staging 目录扫描/映射只覆盖部分 sidecar；ENVI `.hdr` 与主文件命名未强制同名 | 收集 staging 内主文件+全部 sidecar，ENVI 头文件缺失即失败；按最终命名映射并检查映射冲突 |
| 提交前校验 | 旧实现只打开 staging（VRT 仍按 staging 基准解析），未读像元 | 提交前真实重开并读取样本像元；VRT 先复制到最终目录再打开，按 final_dir 校验源 |
| 提交后校验 | 只取驱动/尺寸，未与提交前对比 | 对比驱动/尺寸/波段/类型/NoData/样本像元/mask；输出记录取最终重开结果 |
| 旧 ENVI 头 | 非 ENVI 新数据写 `out.dat` 时可能遗留旧 `out.hdr` | 目标 `.dat/.img` 旁存在旧 `.hdr` 且新格式非 ENVI → 明确拒绝，保留旧文件 |
| 不支持格式 | buildvrt 显式请求 GTiff 仍写 VRT 后按实际驱动登记 | 显式不支持的格式在写盘前拒绝（`unsupported_output_format`） |
| alpha | color band mask 全 255、alpha=0 的缺口被当有效；alpha 波段被当数据计入冲突 | alpha=0 参与无效判定；只重映射 color 数据带，alpha 不被重映射；写显式 dataset mask 使 NoData/值/mask 一致 |
| 显式 mask | 已有显式 mask 时未验证与 dstNodata 的交互 | 按 mask 分块重映射；显式 mask 与 alpha 同时存在时取交集，不把原无效区重新标有效 |
| 测试退出码 | `pytest_unconfigure` 读不到真实 exitstatus，4 failed 仍 `os._exit(0)` | 在 `pytest_sessionfinish` 保存真实退出码；失败探针实测 rc=1 |

**二、成果事务与数据语义**

1. **完整成果集合**：staging 目录中按主文件名前缀/同 stem 收集主文件与 sidecar（ENVI `.hdr`、`.aux.xml`、`.ovr` 等）；ENVI 缺少与主文件同 stem 的头文件时直接失败。提交对按最终目标名映射（`.hdr` 强制 `target.with_suffix(".hdr")`，其余保留 tail），映射冲突拒绝。
2. **提交前/后真实校验**：提交前 `inspect_raster` 读取尺寸/波段/驱动/类型/NoData 与样本像元；VRT 复制到最终目录后打开并按 final_dir 解析源。提交后再次真实重开，逐项对比并核对提交集合；输出记录（驱动/尺寸/波段/类型/NoData/committed_files）全部来自最终重开，不采信处理函数返回值。
3. **事务边界**：`commit_files` 失败时保留 staging/manifest 作为可恢复证据；提交决断点之后不反向回滚。ENVI 提交第二个文件失败与提交中断恢复均有测试，验证旧 `.dat` 与旧 `.hdr` 成套还原，不会新数据配旧头。
4. **NoData/mask**：写盘后按 mask 分块重映射无效像元并设置标签；alpha 波段不设 NoData、不改值，alpha=0 时写显式 mask；有效区真实值与 dstNodata 冲突时保持既有公开策略——明确拒绝，不产出语义矛盾成果。
5. **源完整性**：BuildVRT/merge/stack 处理前核对 CRS（`SpatialReference.IsSame`）、GeoTransform 存在性、波段数与波段类型；不兼容不做隐式重投影，直接拒绝。处理后用 `GetFileList` 核对实际源集合，部分源被跳过不会被登记为整体成功。
6. **relativePaths**：用 ElementTree 处理 VRT XML，按最终 VRT 目录写相对路径；绝对模式主动写 `relativeToVRT="0"` + 绝对路径；跨盘符无法相对时退化为绝对并保持可读。保留 `&amp;`、中文、空格与 GDAL 数据集引用语义；相对/绝对两种模式均实际读取像元验证。
7. **取消**：BuildVRT/Translate 均接入 GDAL callback；取消时返回 cancelled，清理 staging 计算文件，不提交目标成果；真实 1200×1200 源取消测试覆盖。

**三、验证与支持边界**

- 针对性：`tests/integration/test_p4_mosaic_integrity.py` **29 passed / rc=0**（28.07s，独立 basetemp）。
- 全量：`scripts/test.ps1 -All` **573 passed / 0 失败 / rc=0**（247.96s，Ruff 通过、Mypy 64 文件 0 错误）。
- 环境：`scripts/doctor.ps1` rc=0（GDAL 3.13.3 / PROJ 9.8.1 / GUI 隔离通过）。
- 退出异常：日志保留历史失败轮（556 passed 后 child rc=3221226505）；本轮全量使用测试侧 `skip-finalization` 止血，**不宣称根治 0xC0000409**。测试退出码传播已修复，失败时不会再被 `os._exit(0)` 掩盖。
- **支持边界**：本轮未改 GUI；P4-02 的多输入表单与工具箱接入仍是第 32 轮成果，本轮未重新做桌面端到端审计，不把本轮后端修复表述为新的桌面验收。能力注册/可执行审计未改。P4-02 状态保持 `implemented`，未宣称 `done`。

### 第 36 轮：P4-05 首个完整用户流程（重试、导出与端到端验收）

原验收标准：「打开→选择工具→配置→执行→取消/重试→加载→导出→保存项目→重开均真实可用」。
依赖 P4-02 / P4-03 / P4-04 / P2-05 均已 implemented。

**一、逐环盘点（只读核查代码，不把「有函数」当成「流程可用」）**

| 环节 | 轮前状态 | 依据 |
|---|---|---|
| 打开 / 选择工具 / 配置 / 执行 / 加载 / 保存项目 / 重开 | 真实可用 | `main_window.open_dataset` → worker 探测 → 图层树；工具箱 `select_capability` → 表单；`sig_task_submitted` → `submit_task` → `QtWorkerClient` → worker → `_on_res` → `load_result_to_layer`；`.spectra.json` 原子保存与 `open_project` 恢复图层/视图/布局 |
| 取消 | 真实但精简 | 面板按钮 → `_on_cancel_task` → `Kind.cancel` → worker 协作取消 → `cancelled` 终态；**GUI 不走 `TaskScheduler`**，故没有 `cancelling` 中间态、超时强杀与 attempt 历史 |
| **重试** | **缺失** | 无按钮/信号/API；表单每次生成新 `task_id` 且 `attempt=1` 恒定；`TaskSpec.next_attempt()` 只被未接入 GUI 的 `TaskScheduler` 调用 |
| **导出** | **缺失** | 主窗口无任何 export 动作；仅有 CLI `--screenshot` 整窗截图、无 UI 入口的 `export_rois_geojson`，以及语义不同的「算法成果另存」 |

**二、本轮补齐的实现**

| 位置 | 内容 |
|---|---|
| `ui/panels/task_panel.py` | 新增 `sig_retry_requested` 与「🔁 重试任务」按钮：仅终态非 succeeded / 提交出错时启用，运行时禁用；`on_task_started` 显示 attempt，成功后不再提示重试。 |
| `ui/main_window.py` | 保存 `_last_task_spec`；`_on_retry_task()` 用 **`TaskSpec.next_attempt()`** 重发**同一 task_id、attempt+1**，不改 overwrite / 输出路径等已确认语义，有活跃任务或 task_id 不匹配时明确拒绝；新增「任务」菜单（取消 / 重试，Ctrl+R）与「文件」菜单两个导出动作。 |
| `ui/canvas/image_canvas.py` | `capture_view_image()` 用 `QGraphicsScene.render` 导出绘图区（2× 超采样），不依赖窗口句柄与可见状态；导出的是**屏幕渲染截图**，明确标注不含地理参考。 |
| 导出通道 A | `export_view_image()` 写 PNG/JPEG + 同名 `.json` sidecar（视图状态、图层、显示参数、图层清单、导出时间、`contains_georeference=false`、`rendered`）；后缀不合法或无图层时拒绝。 |
| 导出通道 B | `export_layer_raster()` 复用 **`gdal:translate` 的参数契约、表单校验与真实 worker**，按后缀选 GTiff/ENVI/PNG/JPEG 驱动，产出带 CRS 的数据集；登记为 export 任务，成果**只落盘、不污染图层树**。 |
| 安全与生命周期 | 目标路径若等于当前数据源或**任一已加载图层的路径**一律拒绝（带 overwrite 的提交会改写正在使用的数据源）；`submit_task` 的 `_cleanup()` 改为幂等，消除重复 `disconnect` 的 PySide6 RuntimeWarning。 |

**三、验收：`tests/integration/test_p4_full_user_flow.py`（3 项）**

- 主流程用例用**真实 MainWindow + 真实 GDAL worker + 真实栅格**串起九步：`open_dataset` 等探测与画布渲染 → 工具箱树选中 `gdal:translate`（并断言输入自动填入）→ 表单配置 → 提交 → 断言成果可被 GDAL **独立重开** → 成果自动进图层树 → 视图截图（PNG 可被 QImage 打开、`rendered=true`、sidecar 字段齐全）→ 数据导出（GTiff、保留 CRS、图层数不变、同源路径被拒）→ 保存 `.spectra.json`（2 图层）→ **新窗口重开**并按 `layer_id` 逐一对账、无缺失源，且重开后再次导出仍成功产出可重开成果。
- 取消/重试用例用可控时长的 `dummy` 慢任务（**协议、面板按钮、QtWorkerClient、worker 子进程、取消令牌全是真实链路**），真实点击「取消任务」得到 `cancelled`（`error.code=task_cancelled`、`attempt=1`），再真实点击「重试任务」得到**同一 task_id、`attempt=2`、succeeded**，参数保持不变。真实 GDAL 计算中取消由既有的 `test_scheduler_lifecycle.py` 覆盖，不在此重复冒充。
- 边界用例验证四个菜单动作确实接线、无任务时取消/重试给出明确提示、无图层时两条导出通道都拒绝且不写出空文件、不留悬挂 export 标记。
- 诊断记录：首跑发现 `sig_error` 混入 `probe_vector_failed`。独立诊断实测栅格探测成功（`PROBED driver=GTiff`）后，P3-02 元数据面板**无条件**对栅格图层发起矢量/子数据集探测，失败经 `QtWorkerClient.sig_error` 全客户端广播。属既有设计的日志噪声，不是本轮回归；测试只过滤查询类错误码，未改动产品行为，记为已知边界。

**四、验证结果与状态**

`scripts/test.ps1 -All` → Ruff / Mypy / Pytest 三项 **ALL CHECKS PASSED**；
`python -m mypy` 64 个源文件 0 错误；`pytest --collect-only -q` = **539 tests collected**，
`work/test_runs/test_all.log` 记录子进程 `rc=0`、耗时 **245.9s**、无 FAILED / ERROR。
与第 35 轮一致，`os._exit` 止血会截断 pytest 末行文本 summary，故以「rc + 收集数 + 无失败标记」为准，
不伪写一行 `539 passed`；日志末尾仍有 `QThreadStorage` 警告，产品侧 finalization 根治仍待独立工作。

P4-05 状态为 **implemented**（不是 done）：P4 五项至此全部 implemented，但里程碑整体验收、
课程原始数据真实打开、物理高 DPI、worker 握手时序与产品侧 0xC0000409 根治仍未闭环。

### 第 35 轮：P4-04 受限表达式、直方图与基础统计（含 GUI 接入与只读副作用修复）

原验收标准：「解析错误、除零、整数溢出、mask、抽样/全量统计模式有证据」。
上一轮结束时本项只落盘了核心模块，**未静态检查、未跑全量、未同步台账、未提交**，
且 `python -m mypy` 有 9 个错误、`scripts/test.ps1 -All` 不可能全绿。

**一、实现与语义红线**

| 模块 | 职责与关键约束 |
|---|---|
| `providers/gdal_provider/expression_ops.py` | AST 白名单解析与 numpy 求值，**禁用 eval**；只允许白名单运算符/函数与 A/B/C… 波段名。输入统一提升 float32（实测 int16 `30000+30000` 会回绕），无效像元（各自 NoData / NaN / Inf）先排除再回填输出 NoData，实现 mask 传播；除零等数学无效转 NoData 并计数 `n_math_invalid`；有效结果恰等于输出 NoData、或整型输出会溢出/截断小数时**明确拒绝**，不产出无法区分的成果。 |
| `providers/gdal_provider/calc_ops.py` | `gdal:calc` 的 GDAL 读写通道：逐输入核对尺寸 / 仿射 / 投影，不一致直接拒绝（沿用第 29 轮「GDAL 静默跳源」的防线）；输出继承首个输入的 GeoTransform 与投影，落盘后设 NoData 标签。 |
| `providers/gdal_provider/statistics_ops.py` | `sampled` 抽规则网格并标注 `source=sampled_grid` / `stride` / `sampled_is_estimate=True`；`full` 读全幅且超过 `full_limit` **拒绝而不是偷偷降级**；报告含直方图、分位数与 GDAL 原生近似/精确统计对照（实测 `ComputeStatistics(True)` 与 `(False)` 均值不同）。 |
| `adapter.py` / `processing/raster_params.py` | 新增 `gdal:calc`（栅格成果）与 `gdal:statistics`（JSON artifact）分支；统计报告走 staging → 成套提交 → **读回 JSON 核验**，不做 GDAL 重开；参数经与 Worker 相同的共享契约归一化。 |
| `registry/gdal_index.py` / `domain/capability_wiring.py` | 补编目并加入已接通集合；`gdal:calc` 用可排序多输入列表但**允许恰好 1 个源**（A\*2），拼接类仍要求 ≥2 源。 |
| `ui/forms/param_form.py` / `ui/main_window.py` | 表达式、统计模式、分箱、抽样预算、全量上限控件按支持矩阵显隐与状态隔离；统计输出非 `.json` 在 GUI 侧拒绝；JSON 报告**不进图层树**，只在状态栏提示落盘位置，栅格成果仍正常加载。 |

**二、本轮实测发现并修复的真实缺陷：只读统计写 `.aux.xml`**

`tests/integration/test_p4_gui_p404.py::test_gui_statistics_full_report_excludes_nodata` 首次运行失败：
`stats_full_src.tif.aux.xml` 被生成，内容含 `HistMin=0 / HistMax=14 / BucketCount=4` 的
`Histograms` 与 `STATISTICS_MINIMUM/MAXIMUM/MEAN/STDDEV/VALID_PERCENT` 元数据，
与本次统计参数逐项吻合，确证由 `ComputeStatistics` / `GetHistogram` 写入，**不是**造源时产生。

根因：PAM 状态在 `gdal.Open()` 时就固定，原代码只在 `_gdal_reference()` **调用统计函数前**
临时设 `GDAL_PAM_ENABLED=NO`，对已打开的 dataset 无效。修复为在 **Open 之前**禁用、
dataset 销毁后再恢复全局配置（外层 `finally`），保证「统计」这一只读操作不改动用户数据。
修复后该用例与整个 GUI 文件全绿。

**三、补齐的工程缺口**

- Mypy 9 个错误全部修掉：`expression_ops.py` 把 `n.func` 收窄为局部变量后二次判断
  （重复属性访问无法收窄）；`calc_ops.py` 把循环内赋值的 `Optional[dict]` 空间参考
  改为明确的标量字段（`ref_w/ref_h/ref_gt/ref_proj/ref_ready`），消除 8 处不可下标。
  `python -m mypy` → **64 个源文件 0 错误**，Ruff 全量干净。
- GUI 端到端验收 `tests/integration/test_p4_gui_p404.py`（12 项）：除零 / 输入 NoData
  传播 / **有效零值 mask 保持有效** / 语法错误不产出文件 / 整型溢出拒绝 / full 与
  sampled 两种报告 / 超限拒绝 / 非 JSON 拒绝不加模态弹窗，以及**真实 `MainWindow`**
  从表单提交 → worker 执行 → 栅格成果自动加载、JSON 报告不加载、关窗后 worker 停止；
  另补能力接通、显隐切换、单源表达式、空表达式提前拒绝、跨能力参数不泄漏。
- `tests/unit/test_p4_expression_ops.py`（32 项）、`tests/unit/test_p4_param_contract.py`、
  `tests/integration/test_p4_calc_statistics.py` 与基线断言同步；注册表审计实测：
  总基准 **448**、已发现 **443**、可调用 **235**、GDAL 基准 **407**、结构化算法 **23**。

**四、验证结果与状态**

`scripts/test.ps1 -All` → Ruff / Mypy / Pytest 三项 **ALL CHECKS PASSED**，
`work/test_runs/test_all.log` 记录子进程 `rc=0`、耗时 369.0s；`pytest --collect-only -q`
为 **536 tests collected**，日志中无 FAILED / ERROR。如实说明：`pytest_unconfigure` 的
`os._exit` 止血会截断 pytest 的末行文本 summary，故本轮以「子进程 rc + 收集数 + 无失败标记」
三者为准，**不把截断后的输出伪写成一行 `536 passed`**。日志末尾仍出现
`QThreadStorage: entry ... destroyed before end of thread` 警告，与第 33 轮结论一致：
产品侧 finalization 根治仍待独立工作，测试侧仅为止血。

P4-04 状态为 **implemented**（不是 done）：里程碑退出条件尚未整体验收。
P4-05「首个完整用户流程」仍为 todo，依赖 P4-02/P4-03/P4-04/P2-05。

### 第 34 轮：P4-03 元数据/ENVI 头文件、概览与格式选项 + work/ 清理

原验收标准：「不覆盖未知字段；不改原始数据尺寸契约；多文件提交可恢复」。
依赖 P4-01 与 P1-05 均已满足；复用任务协议、参数契约、事务提交与
P4-02 建立的 sidecar 成套提交机制。

**一、实测结论（实现依据）**

| 实测项 | 结论 |
|---|---|
| GDAL 重写 ENVI .hdr | **未知字段全部丢失**（custom_field_a 等自定义字段消失）→ 平台必须读-合并-写 |
| GTiff 概览 | 内嵌主文件（无 .ovr），主影像 8×8 不变 |
| ENVI 概览 | 产生外部 `<dat>.ovr`（GTiff 格式）→ sidecar 成套提交 |
| .hdr 编码 | 含非 ASCII 字节 → latin-1 保字节读写 |

**二、两个新能力（in-place 更新）**

- `gdal:editmeta`：更新 NoData。ENVI 流程 = 快照头文件 → GDAL 应用 →
  按快照补回缺失的未知字段（restore_missing_hdr_fields，只补 GDAL 未写出的）。
- `gdal:overview`：BuildOverviews + 主影像尺寸契约断言（红线：尺寸变化即失败）；
  resampleAlg/levels/overviewCompress 参数；概览级数与各级尺寸进入成果记录。

**in-place 事务语义**：输出=输入。先把源文件集合（主文件 + sidecar）
复制进 staging，在副本上操作，成套提交覆盖源——失败不影响源
（多文件提交可恢复的验收落点）。overwrite=false 被拒绝且源不被破坏。

**三、sidecar 命名的实测教训**

ENVI 的 sidecar 是 `legacy.hdr`——**不以源主文件名 `legacy.dat` 开头**。
初版命名规则只处理「同主名扩展」（.dat.aux.xml/.dat.ovr），.hdr 按原名落
staging → GDAL 打开 staged 主文件报 "not recognized as being in a supported
file format"。修复：**同 stem 不同后缀**的 sidecar 后缀对齐 staged 主名。

**四、registry / wiring / 表单**

- registry 新增 editmeta/overview 条目（均有原生 CLI 工具 gdal_edit.py /
  gdaladdo.exe → binding=cli + form 双入口，availability=ready）；
- wiring 单一来源集合 +2；表单新增 levels / overviewCompress 控件，
  显隐与状态隔离沿用支持矩阵驱动；in-place 能力 GUI 校验「输出=输入」。

**五、验收测试**（tests/integration/test_p4_metadata_overview.py，7 项）

- ENVI 未知字段存活 + NoData 写入（读-合并-写端到端）；
- GTiff editmeta；ENVI 概览 .ovr sidecar 成套提交 + 独立重开 + 尺寸契约；
- GTiff 内嵌概览 + 尺寸契约；overwrite=false 拒绝且源不被破坏；
- 契约校验（levels 严格递增 ≥2、支持矩阵）。

**六、本轮事故与恢复（如实记录）**

多文件编辑脚本因变量复用错误，把 adapter 全文**覆盖**进了当时未提交的
metadata_ops.py（写盘目标用了旧变量）。该文件靠完整重建恢复（内容均在
本轮对话中产生，无外部损失）。此后编辑脚本一律独立读写、落盘目标显式。

**七、验证**

全量 **467 passed / 0 失败 / rc=0**（256.04s）；doctor rc=0；基线审计
（445→447 总数、440→442 已发现、232→234 可调用）已同步；
一致性守护（正向/反向）通过。work/ 清理：删除 pt_*/pytest_* 临时目录
共 2000+ 文件，一次性脚本归档至 scripts_archive/，证据目录全保留。
### 第 33 轮：退出异常 0xC0000409 —— 崩溃面定位、机制证明与测试侧止血

第 31 轮的兜底未能消除该异常，且第 32 轮起它**连续复现**。本轮用二分实验
与 OS 级证据把问题**定位到机制层面**，并在测试侧落地可靠止血。

**一、崩溃面二分定位**

| 实验 | 结果 |
|---|---|
| 仅 `tests/unit`（195 项） | rc=**0** |
| 仅 `tests/integration`（265 项） | rc=**0** |
| `tests/unit tests/integration`（unit 先） | rc=**0** |
| `tests/integration tests/unit`（integration 先） | **rc=3221226505** |
| `tests/integration` + 任意后续 unit 文件（含纯 import 型） | **rc=3221226505** |

结论：崩溃与测试**内容无关**——只要「integration 全量之后还有任何测试」，
pytest 进程退出时就触发。单独任何一层都不崩。

**二、OS 级证据（第 31 轮缺失的进程级现场）**

1. **Windows 事件日志（Application Error）**：
   - `ModuleName = ucrtbase.dll`（Windows 通用 C 运行时）
   - `ExceptionCode = c0000409`（fastfail）
   - `FaultingOffset = 0x00000000000a527e` —— **连续 6 次崩溃偏移完全一致**，
     确证是同一确定性崩溃点。
2. **atexit 现场捕获**：退出时打印存活线程——
   `非主线程存活数 = 0`。**彻底推翻**第 31 轮「守护线程残留访问已销毁 Qt 对象」的
   候选机制（守护线程假设被排除）。
3. **SIGABRT 处理器未触发**：UCRT 的标准 `abort()` 会先触发 SIGABRT；
   未触发说明 fastfail 来自**栈保护（__report_gsfailure）/无效参数/纯虚调用**等路径，
   且此时 Python 信号机制已停用（finalization 之后），应用层无法拦截。

**三、机制证明（对照实验）**

`pytest_unconfigure` 中 `os._exit` 直接终止进程（跳过 Python finalization）→
**崩溃消失（rc=0）**。而 os._exit 之后发生的正是 finalization 的
C++ 静态对象/模块卸载析构。二者结合，机制性证明：

> 崩溃发生在 **Python finalization 阶段的 C++ 清理**
> （PySide6/Qt 静态对象析构），而非任何测试逻辑或线程竞争。

旁证：stderr 反复出现 `QThreadStorage: entry destroyed before end of thread` ——
PySide6 在 worker 读取线程上遗留的**线程本地存储**于模块卸载阶段被析构。
（该警告只在跳过最终化的路径上可见，正常路径在打印它之前就已 fastfail。）

**四、测试侧止血（默认启用，保留真实退出码）**

`tests/conftest.py` 的 `pytest_unconfigure` 默认 `os._exit(config.exitstatus)`：
测试进程不需要干净的 C++ 清理，跳过 finalization 即可稳定退出；
**保留 pytest 的真实退出码**（已验证：有失败时 rc=1、全过时 rc=0），
CI/脚本语义不变。`SW_SKIP_FINALIZATION=0` 可禁用（回归对照用）。

验证：全量 **460 passed / 0 失败 / rc=0**（213.71s）；doctor rc=0。

**五、产品侧说明（与测试侧的区别）**

桌面 GUI 用户关闭窗口时同样经历 finalization，理论上也会触发 fastfail；
但对用户不可见（进程退出码不影响功能，WER 静默处理），已验证功能不受影响。
真正的根治在 PySide6/Qt 层面（其 QThreadStorage 清理顺序）或调整 worker 线程模型
（读取线程不经过 Qt 信号派发），属后续独立工作，超出本轮范围。

**六、后续诊断工具（本轮新增）**

- WER **LocalDumps 已注册**（HKCU，Full dump ×5，`work/crash_dumps/`）：
  下次任何 python.exe 崩溃都会留下完整 dump，可用 `minidump` 库解析调用栈。
- 事件日志查询方法已写入本文（ProviderName='Application Error'）。

**七、遗留**

- `QThreadStorage` 警告的具体析构对象未能进一步定位（无符号）；
  LocalDumps 已就绪，若需深挖产品侧根因，可拿 dump 分析。
- 该止血只覆盖 **pytest 测试进程**；桌面应用的退出路径未改动。

### 第 32 轮：P4-02 GUI 接入（后端能力变为桌面可操作）

第 29/30 轮实现了 P4-02 后端，但 GUI 的 `CONNECTED_CAPABILITIES` 与参数表单
**均未接入**那四项能力。按 AGENTS.md「implemented ≠ done」的要求，
不能把「后端可调用」表述为「桌面已可操作」。本轮补齐接入。

**一、发现并修复的三处真实缺口**

| 缺口 | 后果 |
|---|---|
| `CONNECTED_CAPABILITIES` 在 `param_form.py` 与 `toolbox_panel.py` **各有一份** | 两份漂移会出现「工具箱显示可连、表单却禁提交」的矛盾 |
| `gdal:stack` 与 `gdal:bandmanage` **未在 registry 编目** | adapter 支持但工具箱里根本看不到（隐形能力） |
| `gdal:buildvrt` / `gdal:merge` 的 registry 状态仍是 `blocked`、form entrypoint 不可用 | 已实现的能力被显示为阻塞 |

**二、能力集合改为单一来源**

新增 `domain/capability_wiring.py`（纯 Python，GUI 可安全导入）作为唯一声明来源，
两处 UI 均从它导入。**不直接复用 provider 的 `SUPPORTED_CAPABILITIES`**——
因为 adapter 顶层 `from osgeo import gdal`，GUI 不得导入它。
故声明在 UI 层，一致性由 `tests/unit/test_capability_wiring_consistency.py` 守护：

- **正向**：GUI 声明的每个能力都必须在 provider 支持集里
  （否则能点提交但 worker 报 `unsupported_capability`）；
- **反向**：provider 支持**且 registry 提供可用 form 入口**的能力，GUI 必须声明
  （否则可执行却点不到）。CLI 类能力（如 `rio:cli:convert` 只有 CLI 入口）
  不走表单通道，故不在此列 —— 这也是本轮修正的一处设计区分。

该测试**立刻抓到两处漂移**：3 个 adapter 支持的 Rasterio 能力 GUI 未声明、
`rio:cli:convert` 无 form 入口。据此把 CLI 类能力从表单集合中分离。

**三、可排序多输入控件**

P4-02 的拼接/VRT/堆叠需要多输入，而原表单只有**单文件 `QLineEdit`**。
更重要的是：**输入顺序决定重叠优先级**（GDAL 实测「后加入者优先」），
所以 UI 必须能控制顺序，不能只做一个无序集合。

新增多输入列表（添加/移除/上移/下移），`_build_spec` 输出
`inputs["inputs"]` 列表（顺序即优先级依据）；单输入能力仍用原单文件行。
GUI 侧对「少于 2 个源」提前拒绝，与 worker 的 validate 一致但不等到提交后。

**四、P4-02 参数控件**

按共享契约的支持矩阵显隐并采集：`overlapPriority`、`resolution`、
`srcNodata`/`vrtNodata`/`dstNodata`、`separate`、`relativePaths`、`bandList`。

**NoData 控件刻意用 `QLineEdit` 而非 `QSpinBox`**：
必须能区分「留空 = 未设置」与「填 0 = 设置为 0」。
用 `QSpinBox` 无法表达「未设置」，会让 `dstNodata=0` 被当作未设置丢掉——
这正是第 29 轮复核的实际缺陷（缺口未重映射、mask 标有效）的 GUI 侧成因。

切换能力时同样重置不适用的 P4-02 控件，避免隐藏值泄漏进 TaskSpec。

**五、验收测试**

- `tests/integration/test_p4_gui_p402.py`（13 项）：能力集合单一来源、
  P4-02 四项已接通、多输入显隐切换、**从 GUI 提交并落盘核对顺序即优先级**
  （[A,B] 重叠取 B、选 first 取 A、上移改变结果）、输入不足提前拒绝、
  NoData 留空/填 0 的区分、波段顺序进入 spec 并生效、切能力不泄漏。
- `tests/unit/test_capability_wiring_consistency.py`（6 项）：上述一致性守护。

本轮专项 59 项全通过。

**六、支持边界**

P4-02 至此**桌面可操作**：工具箱可选中、表单可配置多输入与专属参数、
可提交执行并按事务提交成果。仍属本次接入范围之外的：
`rio:cli:*` 等 CLI 能力走 CLI 通道（非表单）、矢量渲染与多维分析未实现。

**七、退出异常 0xC0000409：本轮连续两次复现（如实记录）**

第 31 轮加了 worker 生命周期兜底后，本轮全量**连续两轮**出现
「460 passed / 0 失败但退出码 3221226505」：

| 轮次 | 用例 | 失败数 | 退出码 |
|---|---|---|---|
| 第 1 次 | 460 passed | 0 | **3221226505** |
| 第 2 次 | 460 passed | 0 | **3221226505** |

即所有用例通过、无任何 FAILED，但解释器退出阶段仍触发 `STATUS_STACK_BUFFER_OVERRUN`。
按第 31 轮结论，其**候选机制**是「worker 的 stdout 读取守护线程在 Qt 对象 C++ 侧
销毁后继续访问该对象」，但该因果链**仍缺进程级现场证据**，
生命周期兜底（`__del__` / stop 分支 kill / `with` 语法）**未能消除**该现象。

值得注意：本轮它**从间歇性变为连续复现**（两轮均触发），
这反而提供了更稳定的观测条件，有利于下次定位；但当前**不宣称根治**，
如实保留为待查项。

### 第 31 轮：worker 握手超时与退出异常 0xC0000409 的诊断

本轮针对两个长期待查项做**受控复现实验**。结论包含**否定性**部分：
多项看似合理的假设被实验排除，**根因未能最终定位**；但诊断过程中找到了
代码内既有的机制线索，并落地了可消除该失败模式的**防御性修复**。

**一、实验方法与证据**

| 实验 | 结果 | 结论 |
|---|---|---|
| 独立进程连续启动 12 次 worker | 握手耗时**恒定 0.47–0.72s**，线程数恒定 3 | **启动本身不慢、无累积** |
| pytest 进程内连续启动 24 次 worker | 恒定 **0.56–0.63s**，24/24 成功，存活线程 1 | 同上（pytest 环境亦然） |
| 单个 worker 进程 RSS 实测 | **≈ 66.4 MB**（加载 QGIS 的 osgeo） | 残留 30 个 ≈ 2 GB、60 个 ≈ 4 GB |
| 并发跑另一个 worker 负载 + 同一组测试 | 同样 8.9s / 15 passed / rc=0 | **并发争用不足以触发**握手超时 |
| 保留 61 个存活读取守护线程后退出 | 退出码 **0** | **否证**「守护线程残留必然导致 0xC0000409」 |
| 未回收 worker 子进程（对照实验） | 基线亦未能稳定复现孤儿 | worker 读到 stdin EOF 会自行退出 |

**二、代码内的机制线索（此前未被引用）**

`workers/process.py` 与 `scheduler/client.py` 的既有注释已直接记录该现象：

> 不 join 就让守护线程活到解释器退出，Windows 上会在关闭阶段随机崩
> （STATUS_STACK_BUFFER_OVERRUN），表现为测试与关窗时的异常退出码。

> 客户端关闭后本 Qt 对象的 C++ 侧可能已被销毁，此时继续 emit 信号属于
> 未定义行为，在 Windows 上会随机触发 STATUS_STACK_BUFFER_OVERRUN（0xC0000409）。

**`STATUS_STACK_BUFFER_OVERRUN` 正是 0xC0000409**。这比此前「子进程 env」
的环境相关性猜测**更贴近机制**，但**仍缺进程级现场证据**，
故仍按要求**只记为候选机制，不宣称根治**。

同时定位到 `_join_readers` 的**顺序缺陷**：先 `stream.close()` 关管道、
再 `t.join(timeout)`。在 Windows 上关闭管道会让阻塞在 `readline()` 的线程
行为不确定，可能导致 join 超时放弃、守护线程残留。

**三、落地的防御性修复**

尽管根因未定位，「异常路径下忘记回收 worker」始终是可消除的失败模式
（每个 worker 常驻约 66 MB，累积后新 worker 启动握手会因内存分配困难而超时）。
本轮加三层兜底：

1. `WorkerProcess.__del__`：对象回收时兜底 kill 子进程；
2. `WorkerProcess.stop()`：进程「未存活」分支也确保 kill
   （`is_alive` 判断与真实状态可能不一致）；
3. `QtWorkerClient` 支持 `with` 语法：结构上保证清理。

回归测试 `tests/integration/test_worker_lifecycle_guard.py`（4 项）：
`__del__` 必须调用 kill、stop 的未存活分支必须终止子进程、
上下文管理器退出后子进程已终止、连续使用后无孤儿 worker
（用纯 Python 的 pid 存活判定，不依赖 `wmic`——它在新版 Windows 已移除）。

**四、结论与保留**

- **握手超时**：受控环境下**无法复现**；已排除「启动慢」「累积」
  「并发争用」「孤儿进程」四项假设。该现象**高度依赖当时的系统状态**
  （该时段本机空闲内存仅剩 1.96 GB / 16 GB），根因**未最终定位**。
- **退出异常 0xC0000409**：找到代码内**明确记录的机制线索**
  （守护线程 + Qt 对象销毁 + 退出阶段），但**仍缺进程级现场证据**，
  只记为**候选机制**，**不宣称根治**。
- 两者均作为**待查项**如实保留；本轮不为此改动产品主路径逻辑。

### 第 30 轮：P4-02 成果完整性与数据语义修正

按第二十九轮复核的**落盘复现**修正。上一轮已通过的默认主路径不重写，
本轮只补成果完整性与拼接语义，不扩展 P4-03/P4-04。

**一、四类缺陷与修正**

| 复核项 | 修正前 | 修正后 |
|---|---|---|
| ENVI 成果 | 只提交 .dat，.hdr 留在 staging；最终 .dat **打不开** | .dat 与 .hdr **一起提交**，最终 .dat 可独立 GDAL 打开 |
| dstNodata=0 | NoData 标签变 0，但缺口像元仍 -9999、mask 标**有效** | 缺口**实际值重映射为 0**、标签 0、mask 标**无效**（三者一致） |
| 混合 CRS | 第二个源被 GDAL **静默跳过**，任务仍 succeeded | **明确拒绝**（`incompatible_sources`），不产出文件 |
| relativePaths=False | 被忽略，最终仍全是相对引用 | **绝对模式主动改为 `relativeToVRT="0"` + 绝对路径** |

**二、成果集合完整性（复用既有成果协议）**

修正前 P4-02 分支只 `commit_files([主文件])`，且所谓 verifying_final 仅组装
output_record、**并无实际 GDAL 重开**。现已完整复用 Translate/Warp 路径的做法：
`GetFileList()` 收集全部伴生文件 → 提交前**真实重开**校验 →
按最终命名映射提交对 → 事务提交 → 提交后**最终重开**核验，
输出元数据（尺寸/波段/驱动）取自**实际重开**的成果。

两处实测细节：
- **VRT 的 `GetFileList()` 会把它引用的源文件一并返回**，且路径按 **VRT 当前位置**
  （staging）解析 —— 不能用「与输入源同路径」排除。稳健判据是
  **要提交的文件必须真实存在于 staging**（伴生文件在，被引用的源不在）。
- BuildVRT 的实际驱动是 **VRT**，输出记录不得无条件登记为调用者默认的 GTiff。

**三、NoData 语义：赋标签 ≠ 重映射无效像元**

实测确认复核结论：`Translate(noData=X)` **只改标签**——
nodata 变 0，但缺口像元仍是 -9999 且 mask 标为有效（255），缺口变成有效像元。
修正：Translate **刻意不设** noData（保持 VRT 的标签与实际值一致），
落盘后**按 mask**（而非值比较）把无效像元的实际值重映射为目标值并设标签。
实测得到 nodata=0、缺口像素=0、mask 缺口=0，三者一致。

**公开策略（冲突处理）**：输出 NoData 标签必须等于无效像元值；
若有效区中恰有等于该值的真实数据（如 dstNodata=0 而有效区含真实 0），
「三者一致」与「保全有效区」不可兼得 —— 此时**明确拒绝**并说明原因，
不产出语义矛盾的成果。

**四、输入完整性：阻止静默跳过**

GDAL 的 BuildVRT 对不兼容输入会**静默跳过**（混合 CRS 丢源、非 separate 截断多波段）。
本项目默认**不做隐式重投影**，故处理前用 `check_source_compatibility`
核对 CRS 与波段结构，不兼容则**明确拒绝**。
（实测 `separate=True` 时 2 波段源 + 3 波段源 → **5 个波段**，
值序 [5, 1005, 7, 1007, 2007] —— 是「每源所有波段依次进入」，**不是**每源一个波段。）

**五、取消与实际驱动**

长计算阶段（BuildVRT/Translate）接入 GDAL callback：
返回 0 表示取消，计算中即可中断（此前只在计算结束后检查）；
成果驱动取自实际重开而非调用者默认格式。

**六、支持边界（如实声明）**

本轮是**后端实现**：P4-02 四项能力已在 GDAL worker 可调用，
但 **GUI 的 `CONNECTED_CAPABILITIES` 与参数表单尚未接入这四项**，
多输入表单、能力注册与可执行审计亦未接入。
因此**不可表述为「桌面已可操作」**；GUI 接入列为待办，P4-02 状态维持 `implemented`。

**七、验收测试**

`tests/integration/test_p4_mosaic_integrity.py`（12 项）：ENVI 主文件+头文件成套提交
且最终 .dat 可独立打开、输出记录驱动取自实际重开、BuildVRT 驱动为 VRT、
dstNodata=0 时缺口三者一致、有效零值冲突被明确拒绝、
混合 CRS 与波段结构不一致被拒绝、拒绝时不破坏既有目标、
relativePaths 开关的绝对/相对两种模式、绝对模式 VRT 仍可打开、
多波段输入堆叠的波段顺序。

**观察到的测试基础设施脆弱点（如实记录）**

20:15:29 那轮全量出现 10 项失败、耗时 671s（平时约 190s），失败集中在
`WorkerTimeoutError: Worker 启动握手超时 (20.0s)`，另有 1 项隔离测试报
`processing/mosaic_ops.py` 存在。逐项排查：

- 该文件在第 29 轮已由 `processing/` 移至 `providers/gdal_provider/`
  （因 processing 包必须保持纯 Python，GUI 会导入其参数契约模块）；
  复跑时该文件确已不存在、工作区干净，隔离测试通过——
  故 20:15 那轮命中的是**一次性残留状态**（迁移后的中间态），
  非稳定缺陷。
- 三个相关文件**单独跑**均正常（capability_boundary 15 passed in 8.10s；
  隔离+Rasterio 契约 20 passed in 15.83s），**组合跑**却耗时 1102s 且超时 ——
  属 worker 子进程**累积竞争**：连续启动大量 worker 时，先前进程的句柄/
  资源尚未完全释放，后续 worker 启动握手被拖慢甚至超时。
  属**测试基础设施**的脆弱点，非产品逻辑缺陷；本轮未为此改产品代码。
- 复跑全量：**436 passed / rc=0**（462.72s，耗时较上一轮 186s 上升，
  主要是本轮新增 12 项测试各自的 worker 启动开销累积）。

结论：worker 启动握手的时序敏感性是**已知的待改进项**，
与退出异常 0xC0000409 一样，本轮**不宣称根治**，两者均如实保留待查记录。

### 第 29 轮：P4-02 拼接、VRT、波段堆叠与波段管理

原验收标准：「重叠策略、缺口、NoData、源路径变化及波段顺序正确」。
本轮继续复用**任务协议、参数契约与事务提交**，不扩展其他里程碑。

**一、GDAL 3.13.3 实测结论（实现依据，不是凭印象）**

| 实测项 | 结论 |
|---|---|
| `gdal.BuildVRT(dest, [src...])` | 源必须传**列表** |
| 重叠区取值 | 取**后加入**的源（`[A,B]`→B 胜）→ **优先级可由输入排序实现** |
| 源引用路径 | 同目录 → `relativeToVRT="1"`；源在 VRT 父目录 → **退化为绝对路径**（`relativeToVRT="0"`） |
| `separate=True` | 每源独立成波段（波段堆叠） |
| `Translate(bandList=[3,1,2])` | 输出波段**严格按给定顺序** |
| 缺口 | VRT 空洞为 NoData，Translate 落盘后仍为 NoData |
| `gdal_merge.py` | **本机不可用** → 带缺口填充的拼接走「VRT + Translate」路径 |

**二、参数契约扩展**（`processing/raster_params.py`）

新增 canonical 参数：overlapPriority / resolution / srcNodata / vrtNodata /
separate / relativePaths / bandList，并登记到四个新能力的支持矩阵
（`gdal:buildvrt`、`gdal:merge`、`gdal:stack`、`gdal:bandmanage`）。
取值解析严格：bandList 必须为正整数列表（1-based，小数/零被拒）、
overlapPriority 与 resolution 为受控枚举、布尔项不容字符串、
NoData 允许零值。

**三、实现**（`processing/mosaic_ops.py` + GDAL adapter）

- 四个能力走独立分支，**复用既有事务**（staging → 重开校验 → commit → 登记）；
- **多输入支持**：`inputs["inputs"]` 为列表（兼容 `inputs["input"]` 为列表），
  按能力校验数量（拼接/VRT/堆叠 ≥2，波段管理 ==1）；
- **重叠优先级**：GDAL 是「后加入者优先」，故 `first` 优先级由**反转输入顺序**实现；
- **VRT 相对引用基准**：VRT 的 `relativeToVRT` 路径相对于 **VRT 文件所在目录**解析，
  而本项目先写 staging 再 commit 到目标目录（两者不同），
  因此构建后必须把源引用**重写为相对于最终目标目录**，
  否则 commit 后源引用失效（直接破坏「源路径变化」验收）。
  上一版正则只匹配 `relativeToVRT="1"`，而 staging 构建的 VRT 实测是
  `relativeToVRT="0"` + 绝对路径 → 一个都没改到；已修正为覆盖**全部** SourceFilename 形态。

**四、验收测试**（`tests/integration/test_p4_mosaic_stack.py`，14 项）

逐项按原验收标准验收，预期值均由**独立计算**得出：
- 重叠优先级：last（默认）取后加入的源、first 取先给定的源；
- 缺口：空洞为 NoData 且不污染有效区；源 NoData 不作为有效像元进入成果；
- NoData：dstNodata 落到成果；
- **源路径变化**：VRT 用相对路径引用源、目录整体搬移后仍可打开且像元不变；
- 波段顺序：堆叠后波段归属与输入顺序一致（互不含对方特征值）、
  波段管理按 bandList 严格重排并支持子集；
- 能力边界：拼接不接受 srcWin（明确拒绝而非忽略）、输入数量不足被拒、
  参数非法时既有目标不被覆盖。

**对照实验**：临时关闭 VRT 相对引用重写后，
`test_buildvrt_uses_relative_source_paths` 失败；恢复后通过。

### 第 28 轮：Rasterio worker 真实入口的参数契约接入

按第二十七轮复核的**直连复现**修正。GDAL 能力矩阵、GUI 状态隔离与 VRT 主修复已确认，
不重写、不扩展 P4-02。本轮只闭环 Rasterio worker 真实入口的参数契约。

**一、确定性缺陷**

`RasterioProviderAdapter` 的 validate/execute **未调用** `normalize_raster_params`。
GUI 入口虽已过滤网格参数，但**脚本直接调用实际 Rasterio worker 仍可绕过**：

```
provider_id=rasterio / runtime_id=rasterio-venv / capability_id=rasterio:convert
parameters={format:GTiff, srcWin:[1,2,3,4], xRes:0.002, yRes:0.002}
→ 修正前：succeeded，无 error，有输出；输出仍为源 16×16、像元 0.001，参数被忽略
→ 修正后：failed，invalid_parameters，消息指出被拒参数 srcWin, xRes, yRes，无产出
```

**二、为什么上一轮的测试没抓到（重要的测试有效性缺陷）**

`test_p4_capability_boundary.py` 的 `_run_task` **固定创建 GDAL 引擎/客户端**，
`test_rasterio_convert_rejects_grid_params` 仅换 capability_id 为 `rasterio:convert`，
**并未换成 Rasterio provider**——它拿到的是 GDAL 的 `unsupported_capability`。
而我当时把断言放宽成「`invalid_parameters` 或 `unsupported_capability` 均可」，
等于**没有验证 Rasterio 的参数拒绝路径**。
（单元测试直接调用 `normalize_raster_params` 同样不能证明 adapter 已接入该函数。）

**三、修正**

1. **Rasterio adapter 接入共享契约**：
   - `validate` 调用 `normalize_raster_params(spec.parameters, capability_id=spec.capability_id)`，
     不支持的参数返回 `invalid_parameters`（并指出具体被拒参数）；
   - `execute` 在**像元处理与成果发布之前**再校验一次（validate 之后参数仍可能被改动）；
   - `execute` 保留 `validate` 给出的**具体错误码**——
     「未知能力」是 `unsupported_capability`，「已支持能力携带非法参数」是
     `invalid_parameters`，二者不可混为一谈，否则参数校验路径会被掩盖。
   - 保留现有格式转换功能，**不要求** Rasterio 新实现裁剪/重采样。
2. **对齐能力标识与契约别名**：
   - 发现 registry（`RASTERIO_ALIASES`）的 canonical 是 `rio:cli:convert`，
     而 adapter 的 `SUPPORTED_CAPABILITIES` **漏列**它 → 已补；
   - 契约别名表登记 adapter 支持集的**全部**能力
     （`rasterio:convert` / `rio:cli:convert` / `rasterio:cli:convert` /
     `rasterio:copy` / `rasterio:api:copy`），
     否则未登记的会落到「未知能力」分支而**跳过能力级校验**。
3. **测试引擎路由修正**：新增 `tests/integration/test_p4_rasterio_contract.py`，
   使用 `QtWorkerClient(provider_id="rasterio")` +
   `TaskSpec.engine.provider_id="rasterio"` + `runtime_id="rasterio-venv"`，
   **实际发送到 Rasterio worker**；断言必须为 `invalid_parameters`
   且消息含**具体被拒参数**，**`unsupported_capability` 不算通过**。

**四、覆盖范围**

`test_p4_rasterio_contract.py`（14 项）：无网格参数时正常转换并核对产出尺寸与像元；
srcWin/xRes/yRes/dstSRS 与拼错参数名被拒；**各支持别名**（从 adapter 读取支持集后参数化，
不再硬编码，今后新增能力自动覆盖）均受同一规则；未知能力仍为 `unsupported_capability`；
拒绝后无新成果且原目标不变；同一 worker 拒绝后仍能正常转换（拒绝不得毒化 worker 状态）。

**五、对照实验**

把 Rasterio adapter 的两处契约校验临时移除（还原为修复前行为）后，
该文件 **10 项失败**（仅 4 项不涉及拒绝的用例通过）；修复后 14 项全通过。
（只移除 validate 一处时测试仍通过——因为 execute 处还有第二道关卡，属预期的双保险。）

### 第 27 轮：能力边界、GUI 状态隔离与有界预裁剪

按第二十六轮复核的**实际落盘复现**修正。上一轮四类静默失效已修复，不重写有效主路径；
本轮只补能力边界与有界预裁剪，不扩展 P4-02 及其他算法。

**一、参数契约按能力区分（第 26 轮复核第 1 项）**

复核指出：`normalize_raster_params` 没有 capability/operation 参数，
**可解析的参数不等于对应算法会使用**。实测 Translate + `dstNodata=0` 与
Translate + `dstSRS=EPSG:3857` 都 succeeded，但参数被静默忽略。

修正：共享契约新增**能力支持矩阵**（`processing/raster_params.py`），
GUI 与 Worker 用同一份矩阵：

| 能力 | 支持的参数 |
|---|---|
| `gdal:translate` | srcWin / projWin / outputBounds / xRes / yRes / width / height / outputType / noData / resampleAlg |
| `gdal:warp` | 上述 + dstNodata / dstSRS / srcSRS / targetAlignedPixels |
| `rasterio:convert` | （当前不消费任何网格类参数，给出即拒绝） |

- 给出当前能力**不消费**的参数 → `invalid_parameters`，错误消息列出该能力支持的参数；
  **不要求 Translate 实现 Warp**。
- 互斥组合在**契约层**判定：分辨率（xRes/yRes）与输出尺寸（width/height）互斥
  （此前要到 GDAL 才报 "-outsize 与 -tr 冲突"）；Warp 下源窗口与 outputBounds 互斥、
  srcWin 与 projWin 互斥。
- 脚本绕过 GUI 也受同一校验（Worker 调用同一 `normalize_raster_params`）。

**二、GUI 只收集当前能力适用的控件（第 26 轮复核第 2 项）**

复核实测：Translate 填了裁剪与分辨率，切到 Rasterio convert 后隐藏控件的值
仍进入 TaskSpec（`submitted_srcWin=[1,2,3,4]`、`xRes/yRes=0.002`）。
**仅隐藏控件不足以隔离能力状态。**

修正（双保险）：
1. `load_capability` 切换能力时**重置**新能力不适用的控件值
   （裁剪窗口、输出像元宽高、输出尺寸、输出网格对齐）；
2. `_collect_grid_params` 只**采集当前能力适用**的控件——
   能力不消费某类参数时，其控件值一律不采集（即使还有残留值）。
   适用性取自共享契约的支持矩阵，GUI 与 Worker 不会各维护一套。

覆盖 Translate→Warp→Rasterio→Translate 循环切换：切到 Rasterio 后相关参数为 null，
切回 Translate 也不会复活旧值。

**三、Warp 预裁剪改为有界惰性 VRT（第 26 轮复核第 3 项）**

复核指出：预裁剪用 `gdal.Translate(..., format='MEM')` 会**物化整个裁剪区**，
内存需求随窗口面积×波段×类型增长且无上限，违反项目「有界大影像处理」要求；
且该预裁剪未接入 GDAL callback，取消无法在该阶段生效。

修正：
- 改用**惰性 VRT**（只生成 XML 描述，裁剪在读取时生效）。
  实测 8×9 窗口的 VRT 仅 1280 字节 XML（MEM 则物化 72 个像元）；
  VRT 与 MEM 的尺寸、像元值、NoData、GeoTransform **逐点一致**
  （`pixels_equal_to_mem=true`），源窗口语义由 VRT 的 `SrcRect` 表达。
- 预裁剪步骤**接入 GDAL 进度/取消回调**（此前未传）。
- 增加**窗口像元数预算上限**（`MAX_SOURCE_WINDOW_PIXELS`），超限返回
  `window_too_large` 明确拒绝，而不是默默跑巨量作业。
- VRT 在内存中，不落盘中间成果，失败/取消无残留。

**四、退出异常表述降级（第 26 轮复核的证据要求）**

上一轮把「子进程缺少 SYSTEMROOT → 拖累父 pytest __fastfail」写成「根因」，
但该因果链**缺少进程级现场支持**。现降级为**关联与候选触发条件**，
并区分四类证据（探针子进程 rc / 父 pytest rc / 源码快照 / 运行环境）。
详见下方「退出异常」章节。

**五、验收测试**

`tests/integration/test_p4_capability_boundary.py`（15 项）：能力拒绝（Warp 对照）、
契约层互斥、脚本绕过 GUI 的同规则校验、GUI 循环切换隔离、GUI 提交前拒绝冲突、
VRT 惰性与语义保持、静态断言无 MEM 预裁剪、超大窗口预算拒绝、
参数非法时既有输出不被覆盖。
另有 `tests/fixtures/probe_crop_driver.py` 作为独立探测脚本
（避免在 pytest 内做多层字符串转义）。

**六、顺带修正的测试实践问题**

三个 P4 测试文件原先用**固定** task_id，同一文件内多次运行共用
`.staging/{task_id}/` 路径，上一个 worker 进程句柄未完全释放时撞上 Windows 文件锁
（报 `[WinError 5] 拒绝访问`）。已给 task_id 附加 uuid 后缀，每次运行路径唯一。

### 第 26 轮：GUI→Worker 参数一致性修正 + 两项验收表述更正

按第二十五轮复核的**实际落盘复现**修正。本轮只补参数从 GUI 到实际输出的一致性，
不扩展 P4-02 及其他算法。

**一、四类静默失效（全部已修）**

| 复核项 | 修正前 | 修正后 |
|---|---|---|
| Warp 填裁剪窗口 8×9 | 输出整图 16×16，状态 succeeded | **输出 8×9**，像元值与源数据对应区域一致 |
| GUI 选双线性 | 实际结果等于最近邻 | **实际结果等于双线性**，且与最近邻不同 |
| `dstNodata=0` | 落盘仍是 -9999 | **落盘 NoData = 0**（与未请求时语义不同） |
| `srcWin=[1.9,2.9,3.8,4.8]` | 被 `int()` 截断为 3×4，succeeded | **invalid_parameters**，不产出文件 |
| 未知重采样算法 | 静默回退最近邻，succeeded | **invalid_parameters**（错误消息列出可用算法） |
| 参数名拼错（如 `srcWn`） | 静默忽略 | **invalid_parameters** |
| 别名冲突（`resample_alg` 与 `resampling` 不同值） | 静默择一 | **invalid_parameters** |

**根因**：GUI 与 Worker **各维护一份参数名与取值集合**。新增共享契约模块
`src/spectra_workbench/processing/raster_params.py`（纯 Python，不导入 osgeo），
定义 canonical 名、显式别名表、受支持算法集合与统一校验；
GUI 提交前与 Worker 执行前调用**同一个** `normalize_raster_params()`。

- canonical 名：`resampleAlg`、`srcWin`、`projWin`、`outputBounds`、`xRes`、`yRes`、
  `outputType`、`noData`、`dstNodata`、`dstSRS`、`srcSRS`、`targetAlignedPixels`、
  `width`、`height`；
- 别名（如 `resampling`、`resample_alg`、`src_win`、`x_res`）显式登记，
  **同指不同值时报错**，不无声择一；
- 零值合法：所有可选数值参数按「是否为 None」判断，不再用 truthiness；
- 未知算法、拼错参数名、小数截断、非有限值、冲突组合一律 `invalid_parameters`，
  **不产出文件**。

**二、Warp 的源窗口裁剪语义**

此前 Warp 分支根本不处理 `srcWin`/`projWin`，而表单在 Warp 模式下又暴露了裁剪控件
——用户填了窗口却被静默忽略、产出整图且状态 succeeded。
现在实现**两步语义**：先按源像元窗口（`srcWin`）或源地理窗口（`projWin`）裁剪源，
再对裁剪结果重投影。**源窗口与目标 CRS 的 `outputBounds` 语义不同**，
两者同时给出时报错；`srcWin` 与 `projWin` 同为源窗口语义，也不能同时给出。

**三、GUI 隔离约束的间接依赖（重要）**

共享契约模块最初放在 `providers/gdal_provider/params.py`，但该包的 `__init__.py`
会 import adapter（adapter 顶层 import osgeo）——GUI 进程因此被**间接**拉入 osgeo，
违反 AGENTS.md 硬约束，且现有源码级正则扫描抓不到。
已迁移到 `processing/raster_params.py`（该包为纯 Python）。

新增 `tests/unit/test_gui_isolation_runtime.py`（3 项）做**运行时**兜底：
独立子进程导入 GUI 模块后检查 `sys.modules`，并断言 `processing` 包不含原生依赖。
**对照实验**：把契约模块还原回会拉入 osgeo 的位置后该测试失败，修复后通过。

**四、两项验收表述更正**

1. **GDAL 实际常量是 `GMF_NODATA=8`、`GMF_ALPHA=4`**——上一轮把 8 误写成
   `GMF_ALPHA=8`，已更正。并且 NoData **元数据**（`GetNoDataValue()`）与实际
   **mask**（`GetMaskBand()`）改为**分别核对**，不再用其一代替另一。
2. **P3 高 DPI 证据不足**：原测试只在已创建的 QApplication 之后调整窗口尺寸，
   既未设置也未测量实际缩放因子，不能据此认定高 DPI 已验收。
   已把该项拆开：字体可用与布局几何非零 = 已满足；
   **实际高 DPI 缩放下的渲染与可读性 = 待验**。
   另加 `test_gui_exposes_actual_device_pixel_ratio` 证明缩放因子可被测量，
   作为后续在已知缩放环境（独立进程 + 明确 DPI）下补证的手段。

**五、验收测试**

- `tests/integration/test_p4_param_consistency.py`（18 项）：**从 GUI 表单发出完整
  TaskSpec**，由真实 GDAL worker 执行，再独立重开产出核对**实际像元与元数据**，
  不再只测参数采集辅助函数；
- `tests/unit/test_gui_isolation_runtime.py`（3 项）：GUI 隔离运行时兜底。
- 本轮专项合计 52 项全通过。

**六、退出异常 0xC0000409 的诊断进展（部分归因，不宣称根治）**

第 26 轮首次把该异常**稳定复现**并定位到一个**真实触发条件**：

- 复现：15:59:07 与 16:01:52 两轮全量均 **381 passed / 0 失败但 rc=3221226505**，
  连续两轮 100% 复现（此前为间歇性）。
- 定位（对照实验，逐步排除）：
  | 组合 | 用例数 | 退出码 |
  |---|---|---|
  | 排除本轮全部 3 个新增测试文件 | 357 | **rc=0** |
  | 仅排除两个 unit 新增（保留参数一致性集成测试） | 375 | **rc=0** |
  | 仅排除参数一致性集成测试（保留两个 unit 新增） | 363 | **rc=3221226505** |
  | 完整（3 个新增全含） | 381 | **rc=3221226505** |
- 根因：两个 unit 新增测试用**独立子进程**做 GUI 隔离探针，其中把
  `env` 写成 `{"QT_QPA_PLATFORM": ..., "PATH": "", "SYSTEMROOT": "", "PYTHONPATH": ""}`——
  **清空了 PATH 与 SYSTEMROOT**。Windows 子进程缺少 SYSTEMROOT 会影响 DLL 搜索与
  系统目录解析，可能导致子进程异常收尾，进而拖累父 pytest 进程在**退出阶段**触发
  `__fastfail`（0xC0000409）。
- 修复：子进程 env 改为**以 `os.environ` 为基底只覆盖必要项**，
  并用 `-E` 让子进程忽略继承的 PYTHONPATH（源码根由脚本内 `sys.path.insert` 精确指定）。
- 验证：修复后同一 363 项组合 **rc=0**；完整 381 项 **381 passed / rc=0**。

**但仍不宣称根治**：归档的历史轮次中，2026-11-04T20:00:24（265 项）与
2026-11-05T07:48:47（286 项）两轮 rc=3221226505 **早于本轮新增测试存在**，
说明该异常至少还有一个尚未定位的触发路径。

**第 27 轮按复核意见降级该表述**：上一轮把「子进程缺少 SYSTEMROOT → 子进程收尾拖累
父 pytest __fastfail」写成「根因」，这一因果链**缺少进程级现场支持**，
不能据此独立证明机制。现在只把它记为**关联与候选触发条件**，并区分四类证据：

| 证据类别 | 含义 | 本轮记录 |
|---|---|---|
| 探针子进程 rc | 独立子进程探针自身的退出码 | 全部为 0（探针本身成功） |
| 父 pytest rc | 全量回归子进程的退出码 | 出现 3221226505（0xC0000409） |
| 源码快照 | 触发轮次对应的提交与改动 | 定位到「清空 PATH/SYSTEMROOT 的子进程 env」这一改动 |
| 运行环境 | 操作系统、Python、并发与时序条件 | Windows + 特定 pytest/worker 组合下复现 |

排除组合与修改前后成功/失败**记录为关联**，不作为机制证明。
「保留环境变量」本身是正确的 Windows 子进程实践，与异常之间**只观察到关联**。
历史路径仍未归因，**不继续无边界重复试到绿**。

### 第 25 轮：P3 原验收清单逐句对账 + P4-01 裁剪/重采样/输出网格

**一、P3 原验收清单**（见上方「P3 原验收清单逐句核对」章节）

按 `docs/plan/02_里程碑与验收.md` 的原验收标准逐分句核对，补齐了两处此前缺少直接断言的项：
- **P3-01「中文高 DPI 可用」**：此前只有 `ensure_application_fonts` 的实现，没有直接断言。
  新增 `tests/integration/test_p3_acceptance_gaps.py`（3 项）：中文字体族可用、
  字体初始化返回可用字体数、高 DPI 缩放下主窗口与子控件几何有效。
- **P3-03「切换波段不出现旧图」**：此前只有视图层的过期响应断言。
  新增同文件 2 项：可控客户端注入旧波段迟到响应，断言画布**不渲染**旧内容
  （`img_item.image is None`）且最终显示为新波段；切换波段必须推进画布代际。

P3 里程碑退出条件中「保留真实数据待验状态」一项标为**待验**：
课程原始 ENVI 栅格与 lena.tif 位于只读目录 `_chaoxing_rs`，本轮全部用合成影像验收，
未对课程原始数据做真实打开验证。

**二、P4-01 裁剪、重采样与输出网格**

原验收标准：「输出网格、dtype、mask、CRS 和像元值按明确参数验收」。

Worker 侧（`providers/gdal_provider/adapter.py`）接入：
- Translate：`srcWin`（像元窗口）、`projWin`（地理窗口）、`outputBounds`、
  `xRes`/`yRes`、`outputType`、`noData`、`width`/`height`；
- Warp：`outputBounds`、`xRes`/`yRes`、`outputType`、`dstNodata`、
  `targetAlignedPixels`、`width`/`height`。

关键字可用性按 **GDAL 3.13.3 实测**确定（不是凭印象）：Translate 不接受
`targetAlignedPixels` 与 `scale`，Warp 不接受 `srcWkt`；未实现的组合不写入代码。
非法参数一律返回 `invalid_parameters`，不静默忽略、不回退到源参数。

GUI 参数表单暴露 P4-01 参数（裁剪窗口、输出像元宽高、输出尺寸、输出网格对齐），
留空控件不产生任何参数，非法输入给出明确提示并中止提交。

验收测试 25 项：
- `tests/integration/test_p4_clip_resample.py`（14 项）逐项验收输出网格、dtype、mask、
  CRS、像元值——裁剪后像元值与源数据**逐点一致**，最近邻重采样值必须**来自源数据**，
  双线性结果必须落在源值域内，dtype 与 NoData 均核对**落盘实际值**；
- `tests/integration/test_p4_param_form.py`（11 项）验证 GUI 参数下发与非法输入拒绝。

**实测结论（已写入测试注释，避免后续踩坑）**：
- GDAL 的 `projWin` 顺序是 `[minX, maxY, maxX, minY]`（先上边界后下边界）；
  传 `[minX, minY, maxX, maxY]` 会报 "negative width and/or height"。
- GDAL 3.13.3 的实际常量是 **`GMF_NODATA=8`、`GMF_ALPHA=4`**
  （上一轮把 8 误写成 `GMF_ALPHA=8`，已更正）。
  NoData **元数据**（`GetNoDataValue()`）与实际 **mask**
  （`GetMaskBand()`）必须**分别核对**，不能用其一代替另一。

**三、本轮全量跑再次触发退出异常 0xC0000409（如实记录）**

15:17:33 那轮全量 **356 passed 但 rc=3221226505**；15:2x 重跑 **356 passed / rc=0**。
两次均为「全部用例通过后、解释器退出阶段」异常退出，与第 21–24 轮观察一致。
失败轮日志已并入 `docs/progress/runs/exit_anomaly/test_all_history.log`（现含 18 轮，
其中 3 轮 rc=3221226505：256/286/356 项）。历史日志此前因归档操作被截断，
本轮已从 git 版本合并恢复完整 18 轮。**根因仍未定位，维持「不宣称根治」**。

### 第 24 轮：同一容器内子数据集切换的请求上下文隔离

按第二十三轮复核的独立复现结果修正（未重做已通过的 force=0、四角 bounds、
矢量路由与跨图层保护，未扩展 P4）。

**问题**：`MainWindow._on_subdataset_selected` 的上下文只有
（项目代际, 图层 ID, 源文件），而**同一容器内的子数据集 A/B 共用这三项**，
因此完全无法区分；成功回调里的 `info['uri'] == uri` 只是比较「响应」与
「原请求」，并未验证它是否仍是**当前选择**；错误回调连 URI 检查都没有。

**修正**：
1. `MetadataPanel.current_subdataset_uri()`：返回 UI 上当前选中的子数据集 URI，
   作为「用户当前选择」的权威来源。
2. `MainWindow._on_subdataset_selected` 捕获当次请求的 URI 与
   **单调递增的请求代际**（`_subdataset_request_gen`）。回调逐项校验：
   项目代际、图层 ID、数据源、**当前选中 URI**、**最新请求代际**；
   任一不符即丢弃。原先把 `info['uri'] == uri` 当作唯一 URI 检查的做法已移除。
   成功与错误回调走**同一个** `_still_current()` 判定，错误路径同样受保护。
3. `MainWindow._invalidate_subdataset_requests()`：在**切换/清空数据源、
   图层移除、打开项目、关窗**四处递增代际并清空当前请求 URI，使在途请求失效。
4. 被丢弃的响应只 `return`，**不得清空当前正确内容**。

**为什么必须用请求代际**：A→B→A 场景中，第一轮 A 与最后一轮 A 的 URI 完全相同，
当前选中 URI 也相同，只有请求代际能区分「上一轮 A」与「最后一轮 A」。

**复核复现脚本对照（同一基线 4073c13）**

| 复核项 | 修正前 | 修正后 |
|---|---|---|
| late_A_success_overwrites_B | true | **false** |
| late_A_error_overwrites_B | true | **false** |

**回归固化**：`tests/integration/test_p3_subdataset_context.py`（8 项），
用**确定性反向投递**覆盖：A→B 的迟到成功与迟到错误、A→B→A（第一轮 A 不得覆盖
最后一轮 A）、同 URI 重试、清空后迟到、移除图层后迟到、关窗后迟到；
成功与错误路径分别覆盖，不用 sleep 代替断言。

**对照实验（确认测试能捕获该缺陷）**：把 `_still_current()` 临时还原为修复前的
「三元上下文 + `info['uri'] == uri`」判定后，上述 8 项中 **7 项失败**
（仅「面板报告当前 URI」这项基础能力通过）；修复后 8 项全通过。

### 第 23 轮：P3-02 元数据实现的四项边界修正

按第二十二轮复核的独立复现结果修正（未重写已通过的 ROI 与持久化，未扩展矢量渲染或 P4）。

**1. 要素数强制全表扫描**
`GDALProviderAdapter._describe_vector_layer` 原先调用 `lyr.GetFeatureCount()` 未传 force；
本机 `osgeo/ogr.py` 的签名是 `GetFeatureCount(self, force=1)`——force=1 时驱动在计数昂贵时
会触发全表扫描，与「不强制全表扫描」的注释和结论都不符。
修正为显式 `GetFeatureCount(force=0)`（`GetExtent(force=0)` 原本已正确）；
驱动取不到廉价计数时返回 `feature_count=None` 并置 `feature_count_expensive=True`，
不代为发起扫描。

**2. 矢量图层重新进入栅格流程、旧影像残留**
`ImageCanvas.set_layer` 原先把任何图层都登记为 `self._layer`（即渲染图层），
下游据此派发栅格窗口请求；矢量图层因此进入栅格读取流程，且旧栅格图像仍然留在画布上。
修正：新增 `ImageCanvas.is_raster_renderable(layer)`（`kind == "vector"` 或
`metadata["renderable"] is False` 即不可渲染）；矢量图层**不登记为渲染图层**
（`current_layer` 保持为空）、清除 `img_item`、清空范围与 ROI，并在状态栏保留明确说明。
`MainWindow._on_layer_selected` 同步按 renderable 分支：矢量时清空显示面板与两个画布的图像。
初次打开、重选、显隐、保存重开与双视图切换走同一套判定，不再依赖「第一次打开时的特殊清空」。

**3. 元数据旧响应覆盖当前选择**
`_probe_source_metadata` 与 `_on_subdataset_selected` 的成功/错误回调原先没有校验上下文，
旧图层的迟到响应会覆盖当前选中的图层元数据。
修正：引入请求上下文 `(项目代际, 图层 ID, 数据源引用)`（项目代际用 `_open_seq`，
打开项目时递增），回调在写入面板前校验上下文；图层切换、移除、打开项目、关窗都会使旧上下文失效。
**成功与错误回调都校验**（不只保护成功路径）；子数据集上下文取自**当前元数据选择**
（`self._selected_layer()`）而非画布当前图层——矢量图层本就不在画布中，其子数据集仍需可探测。

**4. 子数据集范围只算两个对角点**
`probe_subdataset` 与 `probe_dataset` 原先按 (0,0) 与 (width,height) 两个对角点求范围，
旋转/倾斜仿射下会漏掉另外两角。
修正：提取共享的 `_affine_bounds(geotransform, width, height)`，按**四个像元外边界角点**
`(0,0)`、`(width,0)`、`(width,height)`、`(0,height)` 计算，两处复用同一算法。

**复核复现脚本对照（同一脚本、同一提交 e239e45 基线）**

| 复核项 | 修正前 | 修正后 |
|---|---|---|
| force_argument_seen | 1 | **0** |
| canvas_received_vector_layer | true | **false** |
| raster_window_requests_for_vector | 1 | **0** |
| old_raster_image_still_present | true | **false** |
| metadata_layer_after_B_then_late_A | A | **B** |
| rotated_subdataset_bounds | reported [500000, 3199800, 500475, 3200000] ≠ expected | **相等**：均为 [500000, 3199700, 500475, 3200100] |

回归固化于 `tests/integration/test_p3_metadata_boundaries.py`（10 项），
其中要素数用 force 默认为 1 的 OGR-like spy 断言实现确实传 0，
迟到响应用**反向顺序投递**回调的确定性测试（不依赖 sleep 猜时序）。

### 第 22 轮发现并修复的真实缺陷：stop() 回调-锁自我死锁

在接入矢量元数据回退路径后，全量测试出现 faulthandler 60 秒超时并打印全部线程栈。
线程栈直接指出了成因：

```
Thread 0x0000d064 (most recent call first):
  File "src/spectra_workbench/scheduler/client.py", line 350 in call_async
  File "src/spectra_workbench/scheduler/client.py", line 605 in probe_vector_async
  File "src/spectra_workbench/ui/main_window.py", line 561 in _on_probe_err
  File "src/spectra_workbench/scheduler/client.py", line 673 in stop
  File "src/spectra_workbench/ui/main_window.py", line 1411 in closeEvent
```

成因：`QtWorkerClient.stop()` 在**持有 `_callbacks_lock` 时**逐个调用各请求的错误回调；
而主窗口的探测失败回退逻辑（栅格探测失败 → 回退矢量探测）会在该回调里**再次发起请求**，
`call_async` 需要同一把非重入锁 —— 同线程自我死锁。

这是既存的结构性隐患（回调在锁内调用），此前没有回调会在锁内发起新请求，因此未暴露。

修复：
1. `client.py` 新增 `_drain_pending_callbacks()`：只在锁内摘出回调并清空注册表，
   **通知动作一律在锁外进行**（`stop()` 与 `kill()` 均已改用）。
2. `main_window.py` 的矢量回退回调增加关闭态防护：客户端已关闭时不再发起新探测。

**对照实验**：把 `stop()` 临时还原成「锁内调用回调」后，回归用例
`test_stop_does_not_deadlock_when_error_callback_issues_new_request` **硬超时 120 秒被强制终止**
（死锁复现，证据 `work/test_runs/deadlock_before.log.timeout.json`）；
修复后同一用例 1.8 秒通过。该回归用例已固化进 `tests/unit/test_p3_exit_lifecycle.py`。

### 另一类「passed 但 rc≠0」：环境删除守卫（非产品缺陷）

第 22 轮全量跑出现 `308 passed` 但退出码异常，日志尾部同时出现
`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":156,"threshold":50,...}`。
经核实这是 WorkBuddy 环境注入的删除保护（`CODEBUDDY_SAFE_DELETE_BULK_GUARD`
与 `sitecustomize.py`），拦截 pytest 清理全局旧临时目录时的批量删除，
与本项目代码无关。属环境因素，如实记录，不通过改动产品代码规避；
改用本轮工作目录内的独立临时根目录（`--basetemp`）即可避开，两轮日志对照：

| 轮次 | 用例 | safe-delete | 退出码 |
|---|---|---|---|
| 13:06:55 | 308 passed | 有（count=156 > 阈值 50） | 3221226505 |
| 13:11:41（独立 basetemp） | 308 passed | 无 | **0** |

**全量运行的时限要求（第 24 轮实测定位）**：全量测试需 **110–122 秒**。
**后台任务有约 2 分钟的执行上限**，全量跑常卡在边界上：第 23 轮 13:29 与第 24 轮 14:49
两次均在启动后约 2m1s 被终止，表现为「日志停在某个百分比、无 timeout 记录、无残留进程」——
那不是测试失败，而是执行时限。改为**前台运行并显式设置更长超时**后连续两轮均通过
（`326 passed / rc=0`，耗时 120.79s）。

**删除守卫与 basetemp 的关系（此前归因不成立，已订正）**：
守卫拦截的目标是**全局** `%TEMP%/pytest-of-<user>/garbage-<uuid>`（历史累积 156 个文件，
与本轮 `--basetemp` 目录无关），且该标记出现在日志**开头**——是先前轮次追加写入的残留，
不是本轮的清理失败。因此「basetemp 残留超阈值导致中断」的归因不成立。
但仍建议每轮 `--basetemp` 使用**全新路径**：pytest 会话开始会清空该目录，
复用旧目录会让清理动作撞上守卫阈值。

**反斜杠被吞的准确归因（第 24 轮复核订正）**：此前记为「PowerShell 自动吞反斜杠」不准确。
实测分层定位：PowerShell 内部拼接 `$bt = "work\pytest_tmp_x"` 与
`$env:PYTEST_ADDOPTS = "--basetemp=$bt"` 都**完整保留**反斜杠；
真正吞掉它的是 **pytest 解析 `PYTEST_ADDOPTS` 时的 shlex 层**——
`shlex.split(value)` 默认 POSIX 模式把 `\` 当转义符，
`--basetemp=work\pytest_tmp_x` 被解析为 `--basetemp=workpytest_tmp_x`
（`shlex.split(v, posix=False)` 才保留反斜杠）。
规避方式：用正斜杠路径，或把 `--basetemp` 作为**参数数组**直接传给 pytest
（不经环境变量），并用 `Join-Path` 拼路径。

**两类失败模式必须区分**（这是本轮的重要澄清）：

- **环境删除守卫**：拦截 pytest 收尾时的批量删除 → 表现为「全通过但退出码非 0」，
  属环境因素；
- **退出异常 0xC0000409**：解释器退出阶段的随机崩溃，**与删除守卫无关** ——
  归档的历史 11 轮中 2 轮 rc=3221226505，**全部没有 safe-delete 标记**
  （`docs/progress/runs/exit_anomaly/test_all_history.log` 逐轮核对）。

因此第 22 轮 13:06:55 那次的 rc=3221226505 与删除守卫**同时出现只是巧合**，
不能据此把 0xC0000409 归因于删除守卫。退出异常的归因状态维持上一轮的结论：
**根因未完全定位，不宣称根治**。

## 进程级硬超时守卫机制

针对 GUI / 引擎测试可能因模态对话框、管道阻塞或子进程挂起导致的永久卡顿，`scripts/run_with_timeout.py` 配合 `scripts/test.ps1` 建立了可靠的进程级硬超时保障机制：
- **进程树清理**：超时触发后通过 Windows `taskkill /F /T` 级联终止测试进程及其派生的全部 Worker 子进程，避免孤儿进程常驻。
- **日志与堆栈保留**：子进程标准输出/错误无损落盘；结合 `faulthandler_timeout` 预先打印各线程 Python 堆栈，超时后自动生成 `<log>.timeout.json` 记录事件。
- **退出码约定**：超时返回专用非零退出码 `124`；子进程自身失败透传对应退出码。
- **可控挂起实测**：通过 10 秒 sleep 样本及故意挂起的测试用例进行验证，实测在 2.0s / 3.0s 超时后精准终止进程树并返回 `RC=124`，证据完整写入 `work/test_runs/`。

## 已完成（有证据）

| 任务 | 状态 | 证据 |
|---|---|---|
| P0-01 仓库与文档结构 | done | git init（2.50.1）；docs/plan/ 输入基线、contracts/、src/、tests/；AGENTS.md、README.md、pyproject.toml；docs/progress/WORKPACKAGES.md |
| P0-02 四环境 | implemented | scripts/bootstrap_pip.ps1 -VerifyOnly → RESULT ALL OK；docs/progress/runs/env_report.json；environments/locks/*.lock.txt |
| P0-03 自检与小型 Fixture | implemented | src/spectra_workbench/app/doctor.py + scripts/doctor.ps1 → docs/progress/runs/doctor_report.json。GTiff 往返成功、坐标转换成功、205 驱动、5 CLI 就绪；单测全绿 |
| P0-04 自动化检查流水线 | done | scripts/test.ps1，串联 Ruff、Mypy 与 Pytest。Mypy 57 个源文件 0 错误；全量自动化测试通过：**460 passed / 0 失败**（仅退出码异常，见下），全量 467 passed / 0 失败 / **rc=0**（2026-11-06 第 34 轮，P4-03 元数据/概览 + work 清理）；**全量 536 项收集 / 0 失败 / rc=0**（2026-11-06 第 35 轮，P4-04 表达式/统计；Mypy 64 个源文件 0 错误）；**全量 539 项收集 / 0 失败 / rc=0**（2026-11-06 第 36 轮，P4-05 完整用户流程）；第 23 轮为 318 passed in 110.47s、rc=0；第 22 轮为 308 passed in 116.50s、rc=0；第 21 轮为 297 passed in 105.12s、rc=0；如实记录 07:48:47 历史退出异常轮次（286 passed，0xC0000409）与后续 07:54:31、07:56:16（286 passed，rc=0）及本轮（297 passed，rc=0），全面加固 Qt 对象生命周期有序销毁 |
| P0-05 打包重定位可行性验证 | implemented | docs/adr/0002-relocation-and-packaging.md；tests/integration/test_relocation.py 验证 UI 与 Rasterio、GDAL 引擎在独立临时路径正常初始化并导入 |
| P1-01 领域模型与契约对账 | done | src/spectra_workbench/domain/models.py；tests/unit/test_domain_models.py；tests/unit/test_contracts_schema.py（50 项全绿） |
| P1-02 JSONL 客户端与 Worker 启动 | done | src/spectra_workbench/protocol/messages.py；src/spectra_workbench/workers/process.py；src/spectra_workbench/workers/entrypoint.py；tests/integration/test_worker_process.py（8 项真实跨进程测试全绿） |
| P1-03 ProviderAdapter 与真实 Smoke 作业 | done | src/spectra_workbench/providers/base.py；src/spectra_workbench/providers/gdal_provider/adapter.py；src/spectra_workbench/scheduler/client.py；tests/integration/test_smoke_task.py（9 项集成测试全绿） |
| P1-04 队列、取消、心跳和崩溃恢复 | implemented | src/spectra_workbench/scheduler/scheduler.py；tests/integration/test_scheduler_lifecycle.py（6 项集成测试全绿） |
| P1-05 Staging、提交清单和状态持久化 | implemented | src/spectra_workbench/processing/manifest.py；src/spectra_workbench/providers/base.py；tests/integration/test_staging_manifest.py（9 项集成测试全绿） |
| P2-01 建立目标版本上游基准 | implemented | contracts/baselines/*.json（443 项条目，100% 通过 capability.schema.json 校验，多层哈希防篡改） |
| P2-02 接入 GDAL 算法、CLI 与驱动探测 | implemented | src/spectra_workbench/registry/gdal_index.py（205 驱动 0 缺口、119 个 CLI 叶子命令、真实 gdal.Warp 重投影分发） |
| P2-03 接入 Rasterio API/rio 与索引 | implemented | src/spectra_workbench/registry/rasterio_index.py；src/spectra_workbench/workers/entrypoint.py（接入 rasterio worker，分块窗口读写转换与未接入能力拦截） |
| P2-04 可搜索工具箱与自动表单 | implemented | src/spectra_workbench/ui/forms/param_form.py；接通 GDAL 转换、Warp、Rasterio 转换；未接入能力严格禁用提交；tests/unit/test_ui_components.py 验证通过 |
| P2-05 覆盖审计与版本差异 | implemented | src/spectra_workbench/ui/panels/toolbox_panel.py；443 项基准可搜索与分类展示；scripts/audit-capabilities.ps1 自动化审计脚本 |
| P3-01 主窗口、Dock、项目树、图层模型 | implemented | src/spectra_workbench/ui/main_window.py；src/spectra_workbench/ui/panels/layer_panel.py；项目序列化保存重开、保存事务边界加固、显式 path_anchor 标记、旧项目路径歧义待确认流程与迁移持久化、确认缺失目标保留与一致性校验；句柄关闭释放文件实测通过；Dock 浮动/显隐/区域互换跨会话持久化与恢复通过（tests/integration/test_p3_dock_layout_persistence.py） |
| P3-02 栅格元数据读取 | implemented | Worker 进程 probe_dataset 协议请求；规范错误返回 Schema 与执行器顶层异常隔离；旋转 Affine 探测；tests/integration/test_ui_app.py 验证通过；第 22 轮补齐基础矢量与子数据集元数据：Worker 新增 probe_vector / list_subdatasets / probe_subdataset，GUI 新增「数据源元数据」面板，矢量文件可作为图层打开与保存重开；以真实 OGR 多图层 GPKG（4 图层、含中文字段名、空图层、无 CRS 图层）与 netCDF 多变量容器验证，子数据集 URI 原样保留不被当作路径解析；中文/空格路径、缺失矢量与缺失子数据集均有明确错误，坏文件后 worker 仍可继续处理正常请求（tests/integration/test_p3_vector_subdataset_metadata.py 10 项全绿）。**剩余缺口**：矢量渲染与多维数据分析仍未实现（本轮只做元数据，不提供假入口）；netCDF 库在含中文/空格的路径下无法创建容器（运行时限制，测试容器置于纯 ASCII 路径并如实记录） |
| P3-03 瓦片/窗口请求与分块读取 | implemented | 1024 预览大阵列 Spill 传输；TileCache 预算限制与 LRU 淘汰；请求代际校验丢弃旧图响应；4 种概览策略；真实 GUI 模式下 6144x6144 大影像实测 LRU 逐出（9 次）与预算受限（docs/progress/runs/p3_gui_memory.json，tests/integration/test_p3_real_gui_memory.py） |
| P3-04 灰度/RGB、拉伸与显示 | implemented | 支持灰度/RGB/伪彩/透明度；min-max 与分位数拉伸；像元原值与显示分离；双视图联动同步；tests/unit/test_p3_display_cache.py 验证通过 |
| P3-05 view CRS、重投影、查询与 ROI | implemented | 旋转 Affine 与无 GT 栅格支持；地图坐标 ↔ 像元中心/角点约定；ROI 模型与点包含判定；跨 CRS 视图重投影网格读取；真实鼠标事件多边形 ROI 绘制/闭合/选中/手柄拖拽/删除/保存重开全流程；矩形展开 4 角点经 Worker 转换并精确保持 425 平方单位面积与 4 个角点；坐标转换失败异常隔离隐藏并保全权威模型；五类故障注入矩阵覆盖（向地图/回像元/项目重开/拖动反算/旧响应迟到），跨坐标空间数据混用缺陷已修复（tests/integration/test_p3_interactive_polygon_roi.py 9 项全绿） |

实测命令与结果：
- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -All → **Ruff 干净、Mypy 干净（64 个源文件 0 错误）、539 项收集 / 0 失败、rc=0**（2026-11-06 第 36 轮，`ALL CHECKS PASSED`，耗时 245.9s；退出异常已在测试侧止血，见第 33 轮）。上一轮（第 35 轮）为 536 项收集 / 0 失败 / rc=0、耗时 369.0s；再上一轮（第 34 轮）为 467 passed / 0 失败 / rc=0、Mypy 61 个源文件 0 错误
  - 第 36 轮（P4-05 首个完整用户流程）：新增 3 项端到端验收（主流程九步串联、取消→重试、菜单与导出边界）；补齐重试 UI/契约与视图截图 + 带 CRS 栅格双导出通道；全量 539 项 / 0 失败 / rc=0（245.9s，pytest 文本 summary 被 `os._exit` 截断，仍以子进程 rc + collect-only 数 + 无 FAILED/ERROR 为准）；第 35 轮（P4-04 受限表达式/直方图统计 + GUI 接入）：新增 69 项（表达式语义 32 项、参数契约与后端集成 25 项、GUI 端到端 12 项）；修复 Mypy 9 处类型错误与「只读统计写 .aux.xml」副作用；全量 536 项 / 0 失败 / rc=0（369.0s，pytest 文本 summary 被 `os._exit` 截断，以子进程 rc + 收集数为准，见第 35 轮说明）；第 34 轮（P4-03 元数据/ENVI/概览）：新增 7 项；第 33 轮（退出异常机制定位 + 止血）：诊断与止血，无新增用例；第 32 轮（P4-02 GUI 接入）：新增 19 项（GUI 端到端 13 项 + 能力一致性 6 项）；第 31 轮（worker 生命周期诊断与兜底）：新增 5 项（兜底 4 项 + 握手耗时观测 1 项）；第 30 轮（P4-02 成果完整性与数据语义）：新增 12 项；第 29 轮（P4-02 拼接/VRT/堆叠/波段管理）：新增 14 项；第 28 轮（Rasterio worker 参数契约）：新增 14 项；第 27 轮（能力边界 + 有界预裁剪）：新增 15 项（能力边界与有界裁剪）；第 26 轮（参数一致性修正）：新增 25 项（参数一致性端到端 18 项 + GUI 隔离运行时 3 项 + adapter 模块级隔离 3 项 + 高 DPI 表述订正 1 项）；第 25 轮（P3 验收清单 + P4-01）：新增 30 项（P3 验收缺口 5 项 + P4-01 裁剪重采样 14 项 + P4-01 表单 11 项）；**全量跑须前台运行**（近两轮需 256–369 秒，后台任务约 2 分钟上限会中途终止，见下方说明）；第 23 轮为 318 passed in 110.47s、rc=0
  - 第 23 轮（P3-02 元数据实现边界修正）：新增 10 项元数据边界回归（要素数 force=0、图层类型路由、请求上下文、仿射四角范围）；本轮 pytest 临时根目录使用全新路径（见上方「运行方式的要求」）；第 22 轮为 308 passed in 116.50s、rc=0
  - 第 22 轮（P3-02 矢量与子数据集元数据）：新增 11 项（矢量与子数据集集成 10 项 + stop() 回调死锁回归 1 项）；本轮使用位于本轮工作目录内的独立临时根目录（pytest 的 basetemp 选项），避开环境删除守卫对全局旧临时目录的批量删除拦截；第 21 轮为 297 passed in 105.12s、rc=0
  - 如实记录全量回归历程：
    - 历史轮次（2026-11-05 07:48:47）：286 passed in 123.74s，但在进程退出释放阶段出现异常退出（child rc=3221226505 / 0xC0000409）；
    - 随后轮次（07:54:31、07:56:16 286 passed，rc=0）及第 21 轮（全量 297 passed in 105.12s，rc=0；ROI 集成 9 项 + 生命周期单测 8 项专项全绿）均正常退出；
    - 针对该现象在全部 GUI 集成测试 `finally:` 块中显式增加 `win.close(); win.deleteLater(); qapp.processEvents()`，并新增 `tests/conftest.py` 在解释器退出前有序销毁 Qt 对象；
    - 复核方独立复跑记录（2026-11-05 第 21 轮复核）：ROI 与生命周期专项共 17 项——第一次 17 个用例均执行通过，但会话结束清理全局旧临时目录时访问旧目录链接被拒绝，最终 rc=1（**既非 0xC0000409，也不能据此证明是删除保护插件**）；第二次改用位于本轮工作目录内的独立临时根目录，**17 passed in 27.27s，child rc=0**，未修改 ACL、未删除旧目录、未屏蔽失败；
    - 按原验收标准如实记录全部证据，**不宣称无条件的永久根治**。
- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/doctor.ps1 → **总体判定: 【通过 ALL OK】，GUI 启动许可：允许 (UI环境与核心依赖就绪)，exit 0**
- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/audit-capabilities.ps1 → **总基准: 443 | 已发现: 438 (98.9%) | 可调用: 229 (51.7%) | 平台可执行: 3 | 已验证: 5 (1.1%) | 缺失: 9 (2.0%) | 完整度: partial，exit 0**
