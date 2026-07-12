# 模块边界

| 模块 | 拥有的事实 | 不负责 |
|---|---|---|
| `app` | 应用装配、启动关闭、依赖生命周期 | 领域规则、协议 Schema |
| `persona` | 人格状态、事件、动力学、情绪动作 | WebSocket 广播、SQLite SQL |
| `audience` | 稳定身份、关系、presence、昵称与表情规则 | 生成主播回复 |
| `memory` | 片段、摘要、检索、保留和删除策略 | 账号认证、HTTP 响应 |
| `danmaku` | 弹幕实体、池、选择、负载与文本理解 | SC 持久队列、连接管理 |
| `stream` | 排期、主题、活动事实、待机状态与元数据 | AI 供应商调用 |
| `integrations` | 外部 AI、SuperChat 与平台网关 | 核心人格状态归约 |
| `transport` | HTTP/WS 输入输出和协议错误 | 业务状态决策 |
| `infrastructure` | SQLite、安全、限流、事件与并发实现 | 定义领域策略 |
| `plugins` | 受控扩展接口和插件上下文 | 暴露内部单例或数据库 |
| `config` | 配置类型、校验和加载 | 动态业务状态 |
| `shared` | 时钟、ID、错误、类型、基础日志 | 可归属任一领域的规则 |

## 跨领域协作

跨领域行为通过 application 用例或显式事件编排。一个领域不得为了“方便”直接修改另一个领域的仓储或内部状态。需要同步返回结果时使用公开服务接口；不要求同步结果时使用带来源 ID 的领域事件。

## 当前物理边界

Audience、Memory、Danmaku、Stream、SuperChat、AI gateway 及 Persona 的唯一实现均
位于 `src/kangel`。SQLite、认证、事件总线、安全限流、过载保护和并发闸门位于
`kangel.infrastructure`；HTTP/WebSocket 协议位于 `kangel.transport`。

根目录旧 `core/models/api/services/utils` 包已经删除。架构测试会同时阻止这些目录
重新出现以及规范源码重新导入同名包。`config/` 是正式配置实现，根 `plugins/` 是
运行时插件发现目录，两者均不属于旧架构。

插件只能从 `kangel.plugins` 获取 `BasePlugin`、`PluginContext` 与管理能力。
`PluginContext` 只暴露事件发布、人格快照、直播快照和日志能力，不提供数据库或
内部单例。新增 WebSocket 事件必须登记到 `WebSocketEventType`。

默认测试按 unit/contract/replay 分套执行；真实 AI 和手工完整链路分别位于
integration/ai 与 e2e。
