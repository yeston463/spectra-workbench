# 执行状态

更新：2026-11-04（第 12 轮 · P3 最小桌面 GUI 稳定性加固：主线程回调、溢出传输、单次订阅与视口局部读取全面闭环）

## 当前里程碑

- **P0 基础设施与自动化验证闭环**：P0-01、P0-04 为 done；P0-02、P0-03、P0-05 为 implemented。
- **P1 核心作业生命周期全面闭环**：P1-01、P1-02、P1-03 为 done；P1-04、P1-05 为 implemented。
- **P2 能力基准、注册表与动态表单**：
  - **P2-01 建立目标版本上游基准**：implemented。锁定 GDAL 3.13.3 与 Rasterio 1.5.2，全量 443 项条目通过模式校验。
  - **P2-02 接入 GDAL 结构化算法、传统命令与驱动探测**：implemented。205 运行时驱动 + 1 缺失 HDF5 驱动（缺口为 0）；119 个 gdal --json-usage 叶子命令；接入真实 gdal.Warp 重投影。
  - **P2-03 接入 Rasterio API/rio 与索引**：implemented。接入 rasterio worker，分块窗口读写转换与未接入能力拦截。
  - **P2-04 建立可搜索工具箱与自动表单**：implemented。从注册表动态构建参数表单控件 `CapabilityFormWidget`，完整接通 GDAL 格式转换、GDAL Warp 重投影、Rasterio 格式转换；未接入能力严格展示状态徽标与限制说明，禁用提交按钮；实现输入输出路径选择器、同源冲突校验与规范 TaskSpec 组装。
  - **P2-05 建立覆盖审计与版本差异**：implemented。实现 `ToolboxPanel` 可搜索工具箱，收录全部 443 项基准能力，支持实时模糊搜索与引擎筛选；优先高亮平台已接通核心算法；`scripts/audit-capabilities.ps1` 持续提供四维状态审计。
- **P3 主窗口、数据与影像查看**：
  - **P3-01 主窗口、Dock、图层模型**：implemented。实现 `MainWindow`（菜单栏、工具栏、Dock 停靠窗口布局）、`LayerPanel`（图层显隐、移除、元数据展示）；单任务订阅管理（生命周期覆盖 success/error/cancel/close），彻底消除重复结果加载；GUI 进程严格保证零原生地理库导入。
  - **P3-02 栅格元数据读取**：implemented。Worker 进程扩展 `probe_dataset` 协议请求，跨进程提取尺寸、波段数、驱动、数据类型、CRS、GeoTransform、地理范围与 NoData；统一异常隔离与 `retryable` 标准错误 Schema，坏文件探测不中断执行线程。
  - **P3-03 瓦片/窗口请求与分块读取**：implemented。Worker 进程扩展 `read_window` 协议请求；1024 灰度与 512 RGB 预览超限数据通过 Spill 溢出文件透明传输；`ImageCanvas` 依据 `ViewBox.viewRange()` 动态计算视口像元窗口，局部放大无损读取 1:1 原始细节，大视口有界降采样，结合请求代际与图层 ID 校验丢弃过期响应。
  - **P3-04 灰度/RGB、拉伸与显示**：implemented。支持单波段灰度与 3 波段 RGB 合成切换，支持自定义波段映射；内置局部有效像元 min-max 对比度线性拉伸；自动注册离屏 Windows 字体（消除方框/tofu），基于 `sig_view_changed` 就绪信号保障高质量截图。

## 已完成（有证据）

| 任务 | 状态 | 证据 |
|---|---|---|
| P0-01 仓库与文档结构 | done | git init（2.50.1）；docs/plan/ 输入基线、contracts/、src/、tests/；AGENTS.md、README.md、pyproject.toml；docs/progress/WORKPACKAGES.md |
| P0-02 四环境 | implemented | scripts/bootstrap_pip.ps1 -VerifyOnly → RESULT ALL OK；docs/progress/runs/env_report.json；environments/locks/*.lock.txt |
| P0-03 自检与小型 Fixture | implemented | src/spectra_workbench/app/doctor.py + scripts/doctor.ps1 → docs/progress/runs/doctor_report.json。GTiff 往返成功、坐标转换成功、205 驱动、5 CLI 就绪；单测全绿 |
| P0-04 自动化检查流水线 | done | scripts/test.ps1，串联 Ruff、Mypy 与 Pytest。Mypy 50 个源文件 0 错误；全量自动化测试通过：**176 passed in 40.55s** |
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
| P3-01 主窗口、Dock、图层模型 | implemented | src/spectra_workbench/ui/main_window.py；src/spectra_workbench/ui/panels/layer_panel.py；按任务 ID 隔离单次订阅与生命周期注销；GUI 零原生依赖隔离测试全绿 |
| P3-02 栅格元数据读取 | implemented | Worker 进程 probe_dataset 协议请求；规范错误返回 Schema 与执行器顶层异常隔离；tests/integration/test_ui_app.py 验证通过 |
| P3-03 瓦片/窗口请求与分块读取 | implemented | 1024 预览大阵列 Spill 传输；`ImageCanvas._compute_viewport_window` 视口局部采样与 1:1 原始细节读取；代际校验防旧结果覆盖；tests/integration/test_ui_app.py 验证通过 |
| P3-04 灰度/RGB、拉伸与显示 | implemented | 支持单波段灰度与 3 波段 RGB 合成切换；min-max 对比度拉伸；离屏字体初始化与就绪信号截图；tests/integration/test_ui_app.py 验证通过 |

实测命令与结果：
- powershell -ExecutionPolicy Bypass -File scripts/test.ps1 -All → **Ruff 干净、Mypy 干净（50 个源文件 0 错误）、176 passed in 40.55s，exit 0**
- python Antigravity桌面界面复核-第十一轮.py → **离屏字体数=6、回调归属主线程=True、预览传输=True、坏文件恢复=True、连续提交加载=1次、视口动态重读=True，exit 0**
- powershell -ExecutionPolicy Bypass -File scripts/doctor.ps1 → **ALL OK，GUI 启动许可：允许 (UI环境与核心依赖就绪)，exit 0**
- powershell -ExecutionPolicy Bypass -File scripts/audit-capabilities.ps1 → **总基准: 443 | 已发现: 438 (98.9%) | 可调用: 229 (51.7%) | 平台可执行: 3 | 已验证: 5 (1.1%) | 缺失: 9 (2.0%) | 完整度: partial，exit 0**
