# Changelog

此项目仍在快速迭代中。正式版本发布后会按语义化版本维护更详细的更新记录。

## 0.4.0

v0.4.0 聚焦注册观众连续性与自愿赞助资金透明。两个功能都保持默认关闭，维护者需要显式
配置后才会启用。

### Viewer Impression

- 注册并开启长期记忆的用户可通过认证接口低频请求一份持久私人留言；生成任务异步执行，
  不阻塞弹幕、SC 或普通回复链。
- 使用独立 `viewer_impression` AI role，必须显式配置模型，不回退普通回复模型；支持独立
  timeout、reasoning、并发、lease、重试和执行令牌。
- 生成时冻结证据快照并按预算选取长期关系、对话片段、话题摘要和情景记忆；成功后才开始
  默认 7 天冷却，失败不会覆盖已有留言。
- 留言参与账号长期记忆的导出与删除治理，但不会写回长期记忆，也不会改变 Relationship、
  Persona 或 Stream 状态。

### Sponsor Fund Transparency

- 增加爱发电 `query-order` 有界同步、Decimal 金额解析和不含用户身份的订单财务账本。
- 增加月度收入聚合、手工支出登记、编辑与作废，以及独立 Finance Sync Worker。
- `GET /sponsor/transparency` 只公开累计收入、支出、结余、月度汇总和资金用途；不会公开
  单个赞助者金额、订单号、平台用户 ID、留言或支付信息。
- 收入只能来自成功订单同步，管理端不能直接修改收入总额；赞助不授予权限、SC 额度或
  任何排队优先级。

### Fixes / Reliability

- 加固 Viewer Impression 的 Provider 时间窗、证据预算、lease 续期、重试与段落换行处理。
- 加固弹幕 attention read gate，并增加低开销时序诊断；现有回复、感谢墙和直播主链保持兼容。

### Compatibility

- SQLite 在启动时幂等创建新增表，不需要手工数据库迁移。
- Viewer Impression、Sponsor Transparency 和 Finance Sync 均默认关闭；升级步骤见
  [`docs/MIGRATION_V040.md`](docs/MIGRATION_V040.md)。

## 0.3.0

这是自分层架构快照以来的一次集中追赶版本，重点是让主播在连续直播中保持可解释、可
回退的状态与上下文。

### Persona & Affect

- 增加事件评价、确定性人格状态投影、场次情绪锚点、余波和重复事件衰减；情绪恢复与
  边界参数可配置。
- 回复计划和非阻塞意图分析可选启用；直接问答始终优先于主题、活动和记忆背景。
- 情绪动作目录扩展并去重，推荐策略加入语气匹配、冷门加成和陈旧度控制。

### Memory & Continuity

- 增加可靠的场次总结和主播情景记忆候选链路，支持 SQLite lease、重试、恢复和幂等。
- 登录观众继续按不可变账号 ID 关联长期记忆；Prompt RAM 为场次内易失实验功能，默认关闭。

### Stream Runtime

- 直播排期、时区、每日主题、当前活动、主线快照和待机状态统一进入元数据。
- Mainline 事实层默认启用；主线 Prompt 注入、Director 和 AI Director 保持关闭/灰度。

### Interaction & Safety

- 增加主播管理系统：LLM 只做受限语义分析，后端结合累计违规、关系和直播策略决定提醒、
  限时禁言或管理员复核。
- 增加语言检测、英文首次互动策略、SC/表情旁路约束和更严格的回复幂等、队列与背压。

### AI Infrastructure

- 支持多 Provider、按角色选择模型、失败回退和可选 reasoning 参数；QA、弹幕选择、情绪、
  管理、情景记忆与 Director 可分别配置超时和模型。
- 增加 token 用量审计、价目折算和管理接口（管理接口默认关闭）。

### Sponsor

- 增加自愿赞助入口与脱敏感谢墙。赞助不授予权限、SC 额度、排队优先级或任何人格权益，
  爱发电凭据仅在服务端同步时使用。

### Compatibility

- HTTP/WebSocket 新字段与事件保持向后兼容的增量设计；数据库启动时幂等补建新增表。
- 旧的基础弹幕、登录、长期记忆和 SC 使用方式继续有效；升级步骤见
  [`docs/MIGRATION_V030.md`](docs/MIGRATION_V030.md)。

### Experimental Features

- Prompt RAM、Director/AI Director、Persona Catalog rollout、Catalog exemplar、下播 AI
  总结、Mainline Prompt 注入和 Admin Dashboard 均需要显式配置，默认不接管生产行为。

## 0.1.0

初始开源版本：

- FastAPI + WebSocket 弹幕直播后端；
- OpenAI Chat Completions 兼容 AI 服务；
- 持续人格状态、情绪影响分析和结构化回复；
- 登录用户长期记忆、昵称历史和游客隔离；
- SC 弹幕队列、冷却与优先回复；
- 表情旁路广播；
- 每日主题、直播排期和“当前活动”状态；
- HTTP/WS 限流、有界 AI 队列与基础公网保护；
- 插件系统；
- 前端接口契约与 AI 主播思维流程文档。
