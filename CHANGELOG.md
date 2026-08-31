# Changelog

此项目仍在快速迭代中。正式版本发布后会按语义化版本维护更详细的更新记录。

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
