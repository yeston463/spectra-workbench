# 执行状态

更新：2026-11-04（第 18 轮 · P3 实际交互验收：真实鼠标事件多边形 ROI 全流程交互、Dock 布局跨会话几何与显隐恢复、真实 GUI 大影像 LRU 淘汰与常驻内存实测、全量 283 项回归全绿）

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
  - **P3-03 瓦片请求、概览、缓存、过期请求处理**：implemented。仅按可见窗口向 Worker 索取数据；实现 `TileCache`（LRU 淘汰与字节预算限制）；请求代际（`_active_request_id`）与图层 ID 校验彻底丢弃过期旧图响应；支持四种概览采样策略（auto/nearest/average/gauss）；在真实交互式 GUI 下以 6144x6144 多级概览大影像完成缩放、平移、切波段与切层全流程实测，验证 LRU 淘汰真实触发（9 次逐出），缓存严格受限在预算内，图层移除后缓存彻底清零。
  - **P3-04 灰度/RGB/伪彩、拉伸、透明度与分屏**：implemented。支持灰度、RGB 与 6 种伪彩色（terrain/viridis/plasma/inferno/magma/jet）；min-max 与 2%~98% 分位数拉伸；原始像元值与显示拉伸值严格物理分离；实现双视图联动同步模式（`test_dual_canvas_link_mode`）。
  - **P3-05 view CRS、重投影、查询与 ROI**：implemented。支持旋转 Affine 栅格与无 GT 栅格加载；地图坐标 ↔ 像元行列支持中心 (center) 与角点 (corner) 两种坐标约定（`test_map_to_pixel_center_vs_corner`）；实现 PointROI / RectROI / PolygonROI 模型与点在多边形判定；跨 CRS 视图重投影网格索取；通过真实 Qt 鼠标事件（QTest）完成多边形 ROI 逐点绘制、右键/起点闭合、选中、顶点手柄真实拖拽形变、删除与保存重开恢复；多图层切换下 ROI 所属图层严格隔离与保全；旋转 Affine 栅格与视图 CRS 切换（像元 vs EPSG:4326）下坐标空间与点位无损保持。

## 原 P3 验收标准逐项对账与剩余缺口

| ID | 任务 | 原始验收标准 | 当前已满足项与实测证据 | 剩余缺口与待验条件 | 当前状态 |
|---|---|---|---|---|---|
| P3-01 | 主窗口、Dock、项目树、图层模型 | 基础项目可保存重开源/样式/视图；布局可恢复；关闭图层释放文件；中文高 DPI 可用 | 项目 JSON 相对路径序列化与重开恢复通过；分离运行时根与用户项目目录；新项目显式记录 `path_anchor`；保存事务边界加固，写失败无内存污染；整体复制搬移保持可用；另存为重算引用指向原数据；旧版 1.0 路径歧义进入待确认流程不自动猜测，用户确认后按真实路径派发并迁移持久化，取消确认原文件保持不变；无论确认目标是否存在均保留用户选择并在保存重开后目标一致；缺失图层与 ROI 在保存/另存为时不丢失；缺失源列表明确指示并支持重新关联与主动移除；图层移除后重命名与删除证明 Windows 句柄已彻底释放；为所有 Dock 窗体显式设置 `objectName`，调整浮动（isFloating）、显隐（isVisible）、左右停靠区互换，保存项目并在全新实例重开验证布局无损恢复（`tests/integration/test_p3_dock_layout_persistence.py` 全绿）；中文路径测试通过。 | 复杂多层嵌套分组图层树（如 QGIS 风格多级容器组）尚未展开（本轮严格按原验收标准验收，不扩展 P4）。 | implemented |
| P3-02 | 栅格/矢量/子数据集的元数据读取 | 数据类型、NoData、mask、单位、波段与空间模型准确；不完整数据明确报错 | 栅格尺寸、dtype、NoData、波段、空间参考、旋转 Affine 准确探测；不完整/损坏文件返回清晰错误且不中断 Worker。 | P3-02 原始范围内的矢量图层（OGR）与科学子数据集（HDF/NetCDF subdatasets）基础元数据探测本期尚未实现/验证。 | implemented |
| P3-03 | 瓦片请求、概览、缓存、过期请求处理 | 只读可见窗口；切换波段不出现旧图；内存增长受缓存预算限制 | 仅读视口局部数据；快速切层/切波段过期响应安全丢弃；TileCache 具备字节预算与 LRU 淘汰机制；在带真实 Qt 绘制与显示数组的 GUI 环境下，使用 6144x6144 多级概览大影像（303 MB），在 10 MiB 小缓存预算下连续实测缩放、平移、切波段与切层，真实触发 9 次 LRU 淘汰，缓存占用严格限制在预算内（峰值 9.99 MiB），峰值 GUI RSS 189.9 MiB，Worker 77.6 MiB，图层移除后缓存清空（`docs/progress/runs/p3_gui_memory.json`，`tests/integration/test_p3_real_gui_memory.py` 全绿）。 | 真实生产环境下超大逻辑阵列（>10GB）的长周期长时间交互表现仍待更广泛场景检验。 | implemented |
| P3-04 | 灰度/RGB/伪彩、拉伸、透明度与分屏 | 显示改变不改原始像元；抽样统计有标识；跨视图同步模式正确 | 灰度/RGB/伪彩/透明度完整支持；显示拉伸与原始像元查询严格解耦；双视图联动同步测试通过。 | 极值与分位数拉伸在特殊极端动态范围数据下的精细边界表现待进一步实测验证。 | implemented |
| P3-05 | view CRS、重投影、查询与 ROI | 旋转 affine、不同 CRS、无 CRS 视图、像元中心/角点均有样例 | 旋转 Affine 与无 CRS 栅格支持；地图坐标与像元行列中心/角点换算通过；ROI 模型支持多点、矩形与多边形；视图跨 CRS 重投影网格读取通过；使用真实 Qt 鼠标事件（QTest）完成多边形 ROI 逐点绘制、右键/起点闭合、选中、顶点手柄拖拽形变、删除与保存重开恢复；多图层切换下 ROI 所属图层隔离与恢复；旋转 Affine 栅格与视图 CRS 切换（像元 vs EPSG:4326）下坐标空间与点位无损保持（`tests/integration/test_p3_interactive_polygon_roi.py` 全绿）。 | 更复杂的贝塞尔曲线/自相交多边形拓扑校验待后续专题完善。 | implemented |

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
| P0-04 自动化检查流水线 | done | scripts/test.ps1，串联 Ruff、Mypy 与 Pytest。Mypy 56 个源文件 0 错误；全量自动化测试通过：**283 passed in 86.64s**，全量回归子进程 rc=0 未复现原异常 |
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
| P3-02 栅格元数据读取 | implemented | Worker 进程 probe_dataset 协议请求；规范错误返回 Schema 与执行器顶层异常隔离；旋转 Affine 探测；tests/integration/test_ui_app.py 验证通过 |
| P3-03 瓦片/窗口请求与分块读取 | implemented | 1024 预览大阵列 Spill 传输；TileCache 预算限制与 LRU 淘汰；请求代际校验丢弃旧图响应；4 种概览策略；真实 GUI 模式下 6144x6144 大影像实测 LRU 逐出（9 次）与预算受限（docs/progress/runs/p3_gui_memory.json，tests/integration/test_p3_real_gui_memory.py） |
| P3-04 灰度/RGB、拉伸与显示 | implemented | 支持灰度/RGB/伪彩/透明度；min-max 与分位数拉伸；像元原值与显示分离；双视图联动同步；tests/unit/test_p3_display_cache.py 验证通过 |
| P3-05 view CRS、重投影、查询与 ROI | implemented | 旋转 Affine 与无 GT 栅格支持；地图坐标 ↔ 像元中心/角点约定；ROI 模型与点包含判定；跨 CRS 视图重投影网格读取；真实鼠标事件多边形 ROI 绘制/闭合/选中/手柄拖拽/删除/保存重开全流程（tests/integration/test_p3_interactive_polygon_roi.py） |

实测命令与结果：
- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -All → **Ruff 干净、Mypy 干净（56 个源文件 0 错误）、283 passed in 86.64s，未复现原退出异常，exit 0**
- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/doctor.ps1 → **总体判定: 【通过 ALL OK】，GUI 启动许可：允许 (UI环境与核心依赖就绪)，exit 0**
- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/audit-capabilities.ps1 → **总基准: 443 | 已发现: 438 (98.9%) | 可调用: 229 (51.7%) | 平台可执行: 3 | 已验证: 5 (1.1%) | 缺失: 9 (2.0%) | 完整度: partial，exit 0**
