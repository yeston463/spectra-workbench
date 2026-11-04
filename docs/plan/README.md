# 遥感与 GIS 工作台 · Qoder 开发任务包

版本：1.0；编写日期：2026-11-03。本文档包描述待开发软件，并非软件安装包。

## 使用方式

1. 在 Qoder 打开独立的开发目录，例如 `C:\Users\赵航\Desktop\studio\spectra-workbench`。先检查目录和已有仓库，再创建所需文件，不覆盖无关项目。
2. 将本任务包完整复制到项目的 `docs/plan/`。
3. 将 `03_QODER_PROMPT.md` 的“首次执行提示词”粘贴给 Qoder。
4. Qoder 应按里程碑持续实现、运行与验收，将进度写入项目 `docs/progress/`。`04_BACKLOG.json` 是初始任务账本，不是已完成记录。

当前已知资料位置：`C:\Users\赵航\Desktop\studio\_chaoxing_rs`。它是课程资料来源，保持只读。其他机器若无此路径，先使用合成测试数据推进开发，真实课程验收标为等待数据。

## 文件索引

| 文件 | 内容 |
|---|---|
| `01_完整开发方案.md` | 产品范围、技术栈、架构、界面、计算与显示规则、环境与发布 |
| `02_里程碑与验收.md` | 分阶段交付物、具体验收用例、性能目标及发布门槛 |
| `03_QODER_PROMPT.md` | 首次执行、后续继续执行、最终审计提示词 |
| `04_BACKLOG.json` | 机器可读任务 ID、依赖、验收标准与初始状态 |
| `contracts/capability.schema.json` | 单项能力注册记录的 JSON Schema |
| `contracts/task.schema.json` | 单项处理任务的 JSON Schema |
| `contracts/message.schema.json` | JSONL 通信封套与基础事件的 JSON Schema |
| `contracts/examples/` | 合法但尚未验证可执行的能力与任务样例 |
| `05_官方依据与资料说明.md` | 官方技术依据、课程资料盘点和已知缺口 |
| `06_交付检查表.md` | 防漏项、自测及交接要求 |
| `07_任务包自检.json` | 本任务包的结构、Schema、样例和依赖校验结果；不代表软件已通过测试 |

本方案优先级高于此前“课程专用版／FFT 单独作为第一版”的开发安排。课程是流程模板，产品是可扩展的通用遥感与 GIS 桌面工作台。

ZIP 内直接包含本目录文件，无额外顶层目录。解压到项目 `docs/plan/` 后应能直接看到 `docs/plan/README.md`。
