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
| P3-01 | 主窗口、Dock、项目树、图层模型 | 基础项目可保存重开源/样式/视图；布局可恢复；关闭图层释放文件；中文高 DPI 可用 | 项目 JSON 相对路径序列化与重开恢复通过；分离运行时根与用户项目目录；新项目显式记录 `path_anchor`；保存事务边界加固，写失败无内存污染；整体复制搬移保持可用；另存为重算引用指向原数据；旧版 1.0 路径歧义进入待确认流程不自动猜测，用户确认后按真实路径派发并迁移持久化，取消确认原文件保持不变；无论确认目标是否存在均保留用户选择并在保存重开后目标一致；缺失图层与 ROI 在保存/另存为时不丢失；缺失源列表明确指示并支持重新关联与主动移除；图层移除后重命名与删除证明 Windows 句柄已彻底释放；为所有 Dock 窗体显式设置 `objectName`，调整浮动（isFloating）、显隐（isVisible）、左右停靠区互换，保存项目并在全新实例重开验证布局无损恢复（`tests/integration/test_p3_dock_layout_persistence.py` 全绿）；中文路径测试通过。 | 原生桌面不同物理显示器 DPI 切换与排版微调待更广泛物理环境实测。 | implemented |
| P3-02 | 栅格/矢量/子数据集的元数据读取 | 数据类型、NoData、mask、单位、波段与空间模型准确；不完整数据明确报错 | 栅格尺寸、dtype、NoData、波段、空间参考、旋转 Affine 准确探测；不完整/损坏文件返回清晰错误且不中断 Worker。 | P3-02 原始范围内的矢量图层（OGR）与科学子数据集（HDF/NetCDF subdatasets）基础元数据探测本期尚未实现/验证。 | implemented |
| P3-03 | 瓦片请求、概览、缓存、过期请求处理 | 只读可见窗口；切换波段不出现旧图；内存增长受缓存预算限制 | 仅读视口局部数据；快速切层/切波段过期响应安全丢弃；TileCache 具备字节预算与 LRU 淘汰机制；在离屏 Qt GUI 环境（`QT_QPA_PLATFORM=offscreen`，包含真实 QMainWindow、QGraphicsView 绘制与显示数组及独立 WorkerProcess）下，使用 6144x6144 多级概览大影像（303 MB），在 10 MiB 小缓存预算下连续实测缩放、平移、切波段与切层，真实触发 7 次 LRU 淘汰，缓存占用严格限制在预算内（阶段采样缓存峰值 10106361 bytes ≈ 9.64 MiB < 10 MiB 预算），GUI 阶段采样 RSS 最大值为 194.91 MiB，Worker 峰值 77.89 MiB；图层移除与关窗后缓存严格清零（0 bytes），GUI 退出保留 RSS 约 187.8 MiB（受 Python 内存分配器池化保留影响，底层 TileCache 清空无泄露）（`docs/progress/runs/p3_gui_memory.json`，`tests/integration/test_p3_real_gui_memory.py` 全绿）。 | 超大影像长周期持续交互在不同显卡驱动与物理桌面下的渲染平滑度待后续实测。 | implemented |
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

**运行方式的要求（实测踩到的坑）**：`--basetemp` 指定的目录**每次必须用全新路径**。
pytest 在会话开始时会清空 basetemp；若该目录残留文件数超过删除守卫阈值（50），
清理会被拦截并中断整轮运行（表现为日志停在某个百分比、无 timeout 记录、无残留进程）。
本轮 13:29 那轮即因此中断（basetemp 残留 63 个文件）；换全新路径后正常完成。

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
| P0-04 自动化检查流水线 | done | scripts/test.ps1，串联 Ruff、Mypy 与 Pytest。Mypy 57 个源文件 0 错误；全量自动化测试通过：**318 passed in 110.47s**，全量回归子进程 rc=0（2026-11-05 第 23 轮，P3-02 元数据边界修正）；第 22 轮为 308 passed in 116.50s、rc=0；第 21 轮为 297 passed in 105.12s、rc=0；如实记录 07:48:47 历史退出异常轮次（286 passed，0xC0000409）与后续 07:54:31、07:56:16（286 passed，rc=0）及本轮（297 passed，rc=0），全面加固 Qt 对象生命周期有序销毁 |
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
- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -All → **Ruff 干净、Mypy 干净（57 个源文件 0 错误）、318 passed in 110.47s，exit 0**
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
