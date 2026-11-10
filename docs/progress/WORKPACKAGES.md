# 子智能体分工与合并顺序

更新：2026-11-03。协调者（主智能体）负责共享文件、跨包接口合并、验收判定与账本一致性。

## 归属表

| 工作包 | 负责任务 | 拥有路径 | 状态 |
|---|---|---|---|
| 协调者 | P0-01 | `AGENTS.md`、`README.md`、`pyproject.toml`、`.gitignore`、`docs/plan/`、`contracts/`、`docs/progress/STATUS.md`、`docs/progress/WORKPACKAGES.md`、BACKLOG 全量裁定 | 骨架已建 |
| ENV | P0-02（为 P0-03/P0-05 供环境） | `environments/`、`scripts/bootstrap.ps1`、`.tooling/`、`docs/adr/0001-*`、`docs/progress/runs/ENV.md`、`env_report.json` | 运行中 |
| DOMAIN | P1-01、P1-02 | `src/spectra_workbench/domain/`、`protocol/`、`tests/unit/test_domain_*`、`test_protocol_*`、`runs/DOMAIN.md` | 运行中 |
| UI / PROC | P11-01 (交互查看: 卷帘/波段播放/光谱剖面/测距) | `src/spectra_workbench/ui/`, `rendering/` | 排期建账 (todo) |
| PROC / EXT | P11-02 (分类整链: ISODATA/监督分类/精度混淆矩阵) | `src/spectra_workbench/processing/`, `plugins/` | 排期建账 (todo, ext:sklearn_classify 插件接入) |
| PROC | P11-03 (镶嵌高级化: 直方图匹配/羽化/接缝线) | `src/spectra_workbench/processing/` | 排期建账 (todo) |
| PROC / UI | P11-04 (FFT 滤波器羽化过渡与交互编辑) | `src/spectra_workbench/processing/`, `ui/` | 排期建账 (todo) |
| DOMAIN / UI | P11-05 (光谱分析链: 端元采集/水吸收/光谱库) | `src/spectra_workbench/ui/`, `domain/` | 排期建账 (todo) |
| PROC / UI | P11-06 (空间统计: 掩膜分地类统计/密度分割) | `src/spectra_workbench/processing/`, `ui/` | 排期建账 (todo) |
| PROC / UI | P11-07 (几何裁剪管线桥: ROI/矢量多边形掩膜裁剪) | `src/spectra_workbench/processing/`, `ui/` | 排期建账 (todo) |
| DOMAIN / UI | P11-08 (多维高光谱坏波段标注与元数据读取) | `src/spectra_workbench/domain/`, `ui/` | 排期建账 (todo) |
| PROC | P11-09 (次级分析: 矢量缓冲区/DEM采样/PC散点图) | `src/spectra_workbench/processing/` | 排期建账 (todo) |
| DOCTOR | P0-03、P0-04 | `scripts/doctor.ps1`、`scripts/test.ps1`、`app/doctor*`、`tests/fixtures/`、`work/`、`runs/DOCTOR.md`、`doctor.json`、`test_summary.txt` | 运行中 |

并行前提：三者写路径互不相交。`docs/progress/BACKLOG.json` 例外——每个包只允许改自己的任务条目，协调者合并时检查是否越界。

## 关键接口（跨包契约）

1. `docs/progress/runs/env_report.json`（ENV 产出）→ DOCTOR、SCHED、REGISTRY 消费。字段：每个环境的解释器绝对路径、包版本、链接 GDAL/PROJ、CLI 路径、驱动摘要、锁文件路径、磁盘占用。ENV 需保证字段名稳定。
2. `contracts/*.schema.json` → DOMAIN 必须通过校验；分歧只登记不擅改，由协调者裁定是否升 schema 版本。
3. `domain`/`protocol` 模型（DOMAIN 产出）→ SCHED（P1-02 worker 侧、P1-04 队列）与 REGISTRY（P2）消费。

## 轮次计划

- 轮 1（当前）：P0-01 骨架 + P0-02 环境 + P0-03/P0-04 doctor 与流水线 + P1-01/P1-02 模型协议。
- 轮 2：P1-03 ProviderAdapter 与真实 smoke 作业（Qt→worker→成果闭环）、P1-04 队列取消崩溃恢复、P1-05 staging 提交清单。此轮起需要 ENV 的真实解释器路径。
- 轮 3：P2-01…P2-05 能力基准与自动表单；P0-05 打包重定位提前验证。
- 之后按 `docs/plan/02_里程碑与验收.md` 的依赖顺序推进 P3–P10。

## 验收裁定规则

- 子智能体自报的 `done` 一律由协调者复跑命令后确认；复跑不过降为 `implemented` 或 `blocked` 并记录原因。
- 引擎相关检查在 ENV 未落地前只能是 `awaiting_env`，不得计入通过。
- 任何"环境已锁但没实际创建/没导入验证"的交付视为未满足 P0-02。
