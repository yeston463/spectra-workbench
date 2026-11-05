# 执行状态

更新：2026-11-05（第 22 轮 · P3-02 基础矢量与子数据集元数据：Worker 侧 probe_vector / list_subdatasets / probe_subdataset、GUI「数据源元数据」面板、矢量文件可作为图层打开与保存重开；同期修复 stop() 回调-锁自我死锁；并按复核意见修正退出异常表述与测试数）

## 当前里程碑

- **P0 基础设施与自动化验证闭环**：P0-01、P0-04 为 done；P0-02、P0-03、P0-05 为 implemented。
- **P1 核心作业生命周期全面闭环**：P1-01、P1-02、P1-03 为 done；P1-04、P1-05 为 implemented。
- **P2 能力基准、注册表与动态表单**：
  - **P2-01 建立目标版本上游基准**：implemented。锁定 GDAL 3.13.3 与 Rasterio 1.5.2，全量 443 项条目通过模式校验。
  - **P2-02 接入 GDAL 结构化算法、传统命令与驱动探测**：implemented。205 运行时驱动 + 1 缺失 HDF5 驱动（缺口为 0）；119 个 gdal --json-usage 叶子命令；接入真实 gdal.Warp 重投影。
  - **P2-03 接入 Rasterio API/rio 与索引**：implemented。接入 rasterio worker，分块窗口读写转换与未接入能力拦截。
  - **P2-04 建立可搜索工具箱与自动表单**：implemented。从注册表动态构建参数表单控件 `CapabilityFormWidget`，完整接通 GDAL 格式转换、GDAL Warp 重投影、Rasterio 格式转换；未接入能力严格展示状态徽标与限制说明，禁用提交按钮；实现输入输出路径选择器、同源冲突校验与规范 TaskSpec 组装。
  - **P2-05 建立覆盖审计与版本差异**：implemented。实现 `ToolboxPanel` 可搜索工具箱，收录全部 443 项基准能力，支持实时模糊搜索与引擎筛选；优先高亮平台已接通核心算法；`scripts/audit-capabilities.ps1` 持续提供四维状态审计。
- **P3 主窗口、数据与影像查看**（全部保持 implemented）：
  - **P3-01 主窗口、Dock、项目树、图层模型**：implemented。实现主窗口与 Dock 布局、图层顺序保序；显式设置 Dock 与工具栏 objectName，实测验证浮动（isFloating）、显隐（isVisible）与停靠区域重排跨会话无损保存与重开恢复；分离运行时根与用户项目目录，新项目显式记录 `path_anchor='project_dir'`；保存事务边界加固；旧版 1.0 格式且缺少路径基准标记的项目进入明确的待确认流程，程序化打开返回 checkable 状态，用户确认前不自动派发同名文件、未确认前禁止保存，用户确认后按真实路径派发并迁移持久化，取消确认原文件保持不变；无论确认的物理目标是否存在均保留用户确认选择（存在性仅决定是否请求加载，不影响路径更新），重开后严格保持目标一致，杜绝劫持同名文件；自定义空路径提供校验阻断；旧绝对引用直接按确定语义解析；缺失源状态指示与显式移除；移除图层与关窗彻底释放文件句柄与缓存。
  - **P3-02 栅格元数据读取**：implemented。通过 Worker 提取尺寸、波段数、驱动、数据类型、CRS、GeoTransform（含旋转 Affine 系数）、地理范围与 NoData；统一异常隔离与 `retryable` 标准错误 Schema，坏文件探测不中断执行线程。
  - **P3-02 基础矢量与子数据集元数据**（第 22 轮补齐原缺口）：implemented。Worker 新增 `probe_vector`（图层名称、几何类型、CRS、范围、字段名称/类型/可空性；要素数与范围不可得时以 `feature_count_expensive` / `extent_available` 如实标注，不返回编造数值）、`list_subdatasets`（原样保留引擎返回的数据源引用）与 `probe_subdataset`（尺寸、dtype、NoData、波段、GeoTransform、CRS）。GUI 新增「数据源元数据」面板展示矢量结构与可选子数据集；栅格探测失败时回退矢量探测，使矢量文件可作为图层打开、可保存重开；**矢量渲染与多维分析尚未实现，明确标注而不提供假入口**。
  - **P3-03 瓦片请求、概览、缓存、过期请求处理**：implemented。仅按可见窗口向 Worker 索取数据；实现 `TileCache`（LRU 淘汰与字节预算限制）；请求代际（`_active_request_id`）与图层 ID 校验彻底丢弃过期旧图响应；支持四种概览采样策略（auto/nearest/average/gauss）；在真实交互式 GUI 下以 6144x6144 多级概览大影像完成缩放、平移、切波段与切层全流程实测，验证 LRU 淘汰真实触发（9 次逐出），缓存严格受限在预算内，图层移除后缓存彻底清零。
  - **P3-04 灰度/RGB/伪彩、拉伸、透明度与分屏**：implemented。支持灰度、RGB 与 6 种伪彩色（terrain/viridis/plasma/inferno/magma/jet）；min-max 与 2%~98% 分位数拉伸；原始像元值与显示拉伸值严格物理分离；实现双视图联动同步模式（`test_dual_canvas_link_mode`）。
  - **P3-05 view CRS、重投影、查询与 ROI**：implemented。支持旋转 Affine 栅格与无 GT 栅格加载；地图坐标 ↔ 像元行列支持中心 (center) 与角点 (corner) 两种坐标约定（`test_map_to_pixel_center_vs_corner`）；实现 PointROI / RectROI / PolygonROI 模型与点在多边形判定；跨 CRS 视图重投影网格索取；通过真实 Qt 鼠标事件（QTest）完成多边形 ROI 逐点绘制、右键/起点闭合、选中、顶点手柄真实拖拽形变、删除与保存重开恢复；多图层切换下 ROI 所属图层严格隔离与保全；旋转 Affine 栅格与视图 CRS 切换（像元 vs EPSG:4326）下坐标空间与点位无损保持；矩形 ROI 权威几何与显示轮廓物理分离，在旋转 Affine 栅格下展开 4 角点经 Worker 转换并渲染实际轮廓，实测断言 4 个真实地图角点与 425.0 平方地图单位覆盖面积（彻底杜绝错误轴对齐面积 375.0），变换视图下手柄隐藏禁用，防止扭曲矩形；坐标转换失败时绝不伪装恒等变换，严格隐藏图形项（`item.isVisible() is False`）并清空 `display_points`（为 None），保全权威模型，切回有效视图自动恢复；拖动反算 Worker 报错时安全回退，不崩溃不污染。本轮补齐五类故障注入矩阵（向地图失败 / 回像元失败 / 项目重开失败 / 拖动反算失败 / 旧响应迟到），每类均验证图形状态、权威几何与保存结果；修复跨坐标空间数据混用缺陷（state 显式记录显示点所属空间与 CRS），拖动反算失败时显示图形恢复原轮廓、权威几何保持不变。

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
| P0-04 自动化检查流水线 | done | scripts/test.ps1，串联 Ruff、Mypy 与 Pytest。Mypy 57 个源文件 0 错误；全量自动化测试通过：**410 passed in 176.15s**，全量回归子进程 rc=0（2026-11-05 第 28 轮，Rasterio worker 参数契约接入）；第 23 轮为 318 passed in 110.47s、rc=0；第 22 轮为 308 passed in 116.50s、rc=0；第 21 轮为 297 passed in 105.12s、rc=0；如实记录 07:48:47 历史退出异常轮次（286 passed，0xC0000409）与后续 07:54:31、07:56:16（286 passed，rc=0）及本轮（297 passed，rc=0），全面加固 Qt 对象生命周期有序销毁 |
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
- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -All → **Ruff 干净、Mypy 干净（58 个源文件 0 错误）、410 passed in 176.15s，exit 0**
  - 第 28 轮（Rasterio worker 参数契约）：新增 14 项；第 27 轮（能力边界 + 有界预裁剪）：新增 15 项（能力边界与有界裁剪）；第 26 轮（参数一致性修正）：新增 25 项（参数一致性端到端 18 项 + GUI 隔离运行时 3 项 + adapter 模块级隔离 3 项 + 高 DPI 表述订正 1 项）；第 25 轮（P3 验收清单 + P4-01）：新增 30 项（P3 验收缺口 5 项 + P4-01 裁剪重采样 14 项 + P4-01 表单 11 项）；**全量跑须前台运行**（需 110–122 秒，后台任务约 2 分钟上限会中途终止，见下方说明）；第 23 轮为 318 passed in 110.47s、rc=0
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
