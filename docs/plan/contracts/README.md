# 初始协议契约

JSON Schema 版本：Draft 2020-12。三份 schema 是开发起点，Qoder 实现 Pydantic 模型时应保持兼容并写契约测试。后续不兼容修改需提高协议/schema 版本并记录迁移。

- `capability.schema.json`：单项能力记录。
- `task.schema.json`：单个任务尝试的规格。
- `message.schema.json`：JSONL 封套和各类事件的基础字段约束；特定 action 的参数还需要对应领域模型校验。
- `examples/`：只验证结构的模板，均标记 `template_only: true`，未绑定真实环境和数据，执行器必须拒绝直接运行。
- 正式能力的 descriptor_revision 应采用规范化参数/引擎契约的内容哈希；不能使用示例文本。状态/验证时间等可变字段不要使每次扫描都改变执行描述哈希。
- 正式基准需要非空版本和 snapshot_sha256；模板中为 null/占位文字是为了明确尚未解析。
- `project_root` 为绝对路径；相对数据/输出/工作目录按此根解析；URI 由对应 provider 解释。路径解析与安全检查不能仅靠 schema。
- `inputs`、`parameters`、`outputs` 分开：适配器把统一字段映射到实际引擎参数，不能把 output_uri 等平台字段直接塞给原生 API。
- schema 校验之外，还必须检查能力参数类型、互斥、NoData/CRS、来源/目标冲突、驱动读写、资源预算、实际环境版本、描述哈希和副作用。
- 领域模型为数据集提供更完整的空间模型、元数据与掩膜结构；此处 DatasetRef 仅用于任务引用，避免复制巨大元数据。

IPC 的具体 action（例如读取瓦片、查询像元和运行任务）在 P1 以主方案第 5 节为依据补充类型化 payload；当前封套不替代所有 provider 的动作定义。ratio=null 表示当前阶段无法估算百分比，UI 显示不定进度；不能自行捏造计算比例。
