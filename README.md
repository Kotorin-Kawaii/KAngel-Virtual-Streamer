# KAngel Virtual Streamer

KAngel Virtual Streamer 是一个面向直播间的 AI 主播后端运行时。它通过
WebSocket 接收弹幕并广播事件，同时在 SQLite 中持续保存主播人格状态、登录观众关系与
可治理的长期记忆。回复不是一次性的“问答接口”：当前对话、主播情绪、直播活动、观众
上下文和安全策略共同决定下一轮表现。

项目使用 FastAPI、WebSocket、SQLite 和 OpenAI Chat Completions 兼容接口。除模型服务外，
运行时不要求 Redis 或其他必需的外部服务；正式部署建议保持单 Python 进程写入一个 SQLite
数据目录。

在线体验：<[https://kangelai.kotorin.cn/](http://kangelai.kotorin.cn/)>（该页面连接维护者部署的服务，
自行部署时请替换前端 API/WebSocket 地址）。

本仓库只发布服务端 Runtime、HTTP/WebSocket API、任务与配置能力，不包含维护者的私有
`Kangel-Webpage` 前端。Viewer Impression 提供的是供兼容前端调用的认证 API。

## 核心能力

- 连续人格：维护 `mood`、`stress`、`darkness` 及依恋、自信、疲劳等内部状态；情绪影响
  经过结构化事件评价和后端受限投影，避免单次模型异常直接改写人格。
- 直播上下文：按 IANA 时区和每周排期控制开播状态，轮换每日主题，维护当前活动和主线
  事实；这些背景不会压过当前观众的直接问题。
- 观众连续性：游客只使用当前连接/直播上下文；注册用户按不可变 `account_id` 关联昵称
  历史、关系、人物记忆和情景记忆，支持导出与删除。
- Viewer Impression：注册且开启长期记忆的用户可低频请求一份异步生成、持久保存、受
  隐私治理的私人留言；使用独立 AI role，默认关闭且不会写回人格或记忆状态。
- 互动与安全：SC 使用独立持久化队列和冷却，优先于普通弹幕；表情只广播稳定 ID，不进入
  人格或 AI 链路；主播管理系统结合语义分析、违规累计和直播策略生成提醒或限时禁言。
- AI 基础设施：不同角色可使用不同模型和 Provider，支持失败回退、并行前置分析、超时、
  有界并发、幂等回复和受控 token 用量审计。
- Sponsor Fund Transparency：可从爱发电成功订单同步匿名财务收入、登记公开资金用途，
  只对外提供聚合金额；与 nickname-only 感谢墙相互独立，默认关闭。
- 扩展性：插件目录、完整 HTTP/WebSocket 契约、每日场次总结、赞助者感谢墙等旁路能力。

## 实验功能（默认保守）

代码中包含若干可灰度验证的能力，但不会因为“存在于仓库”就自动接管生产行为：

| 功能 | 默认状态 | 说明 |
| --- | --- | --- |
| Prompt RAM | 关闭 | 场次内易失的未闭合互动意图；重启后清空 |
| Stream Director / AI Director | 关闭 | 当前主线事实层可启用，Director 尚不作为生产权威 |
| Persona Catalog | legacy、rollout 0% | 结构化人格知识可 shadow/灰度，生产仍使用 legacy Prompt |
| Catalog exemplar | 关闭 | 不默认注入风格示例 |
| 下播 AI 场次总结 | 关闭 | 候选会捕获，外部模型总结需显式开启 |
| Episodic Memory AI worker | 关闭 | 低优先级、可重试的情景记忆总结 |
| Viewer Impression | 关闭 | 注册观众的异步私人留言；必须显式配置专用 AI role |
| Sponsor Transparency / Finance Sync | 关闭 | 聚合资金公开与爱发电订单财务同步，独立于感谢墙 |
| Mainline Prompt injection | 关闭 | 公开元数据仍可使用主线事实 |
| Admin API / Dashboard | 关闭 | 需要独立管理员密钥，不能复用普通账号令牌 |

## 运行要求

- Python 3.11 或 3.12；
- 一个实现 OpenAI Chat Completions 请求/响应格式的模型服务；
- 单进程 SQLite 数据目录；不要求 Redis。

## 快速开始

```bash
git clone https://github.com/Kotorin-Kawaii/KAngel-Virtual-Streamer.git
cd KAngel-Virtual-Streamer
```

推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
cp .env.example .env       # Windows 请手动复制
uv run python main.py
```

传统 pip 方式：

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

至少在 `.env` 中填写一个模型服务：

```dotenv
AI__BASE_URL=https://api.example.com/v1
AI__API_KEY=replace-me
AI__DEFAULT_MODEL=replace-with-your-reply-model
AI__QA_SELECTOR_MODEL=replace-with-your-fast-model
AI__DANMAKU_SELECTOR_MODEL=replace-with-your-fast-model
AI__IMPACT_ANALYSIS_MODEL=replace-with-your-balanced-model
# 留空时 Viewer Impression 保持 unavailable，绝不回退普通回复模型
AI__VIEWER_IMPRESSION_MODEL=
```

服务端统一请求 `{AI__BASE_URL}/chat/completions`。OpenAI、SiliconFlow、兼容网关或本地
模型只要提供相同格式即可；多 Provider、角色映射和 fallback 见 `.env.example` 与接口文档。

启动后：

| 地址 | 用途 |
| --- | --- |
| `http://127.0.0.1:8000/status` | 服务健康与连接数 |
| `http://127.0.0.1:8000/docs` | OpenAPI 调试页面 |
| `ws://127.0.0.1:8000/danmaku` | 弹幕 WebSocket |
| `http://127.0.0.1:8000/stream/metadata` | 开播状态、主题、活动和主线元数据 |
| `GET /auth/profile/impression` | 登录用户读取自己的持久留言 |
| `POST /auth/profile/impression/generate` | 异步申请生成私人留言 |
| `GET /sponsor/transparency` | 公开聚合赞助收入、支出、结余与月度用途 |

## 配置与自定义人格

`.env.example` 是脱敏模板，不是生产配置副本。新增配置通常在进程启动时读取，修改后需要
重启；不要提交真实 API key、Cookie、管理员密钥或数据库。

| 想修改的内容 | 位置 |
| --- | --- |
| 名称、初始情绪、昵称 | `.env` 的 `PERSONA__*` |
| 核心身份、性格、口癖、事实问答 | `src/kangel/integrations/ai/prompts.py` 的人格 Prompt 与 QA 数据 |
| mood/stress/darkness 的变化形状 | `config/settings.py` 的 `PERSONA__DYNAMICS__*` 与 `src/kangel/persona/domain/` |
| 可输出情绪和动作 ID | `config/emotion_catalog.py`；前端负责静态资源映射 |
| 排期、时区、每日主题、活动 | `.env` 的 `STREAM__*` |
| 观众记忆保留与提示词预算 | `.env` 的 `MEMORY__*`、`EPISODIC_MEMORY__*` |
| SC、表情、管理和限流 | `.env` 的 `SC__*`、`EMOTES__*`、`MODERATION__*`、`RATE_LIMIT__*` |
| Viewer Impression | `.env` 的 `VIEWER_IMPRESSION__*` 与专用 `viewer_impression` AI role |
| 自愿赞助、感谢墙与资金透明 | `.env` 的 `SPONSOR__*`；凭据只放服务端 |

保留这条语义约束很重要：**当前观众的直接问题和已建立的对话语义，永远优先于每日
主题、当前活动、长期记忆和其他背景。** 角色更换后，建议使用新的测试 SQLite，避免旧
人格状态、关系和记忆混入新角色。

## 直播排期示例

```dotenv
STREAM__TIMEZONE=Asia/Shanghai
STREAM__WEEKLY_SCHEDULE={"monday":[{"start":"06:00","end":"03:00"}],"tuesday":[{"start":"06:00","end":"03:00"}]}
STREAM__DAILY_THEMES=[{"id":"chat","name":"互联网杂谈","prompt_hint":"偶尔聊聊网络见闻。"},{"id":"game","name":"游戏实况"}]
```

结束时间早于开始时间表示跨越午夜；未配置的星期默认不开播。特殊日期主题只作为人格
bias 和前端元数据，不会强行覆盖核心人格。

## 项目结构

```text
KAngel-Virtual-Streamer/
├── src/kangel/       # persona、audience、memory、danmaku、stream、transport 等分层源码
├── config/            # 配置模型与情绪动作目录
├── plugins/           # 运行时插件发现目录
├── docs/              # 架构、API、概念、插件和部署文档
├── main.py            # 便捷启动入口
└── .env.example       # 脱敏配置模板
```

## 部署与安全

部署细节见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。同一数据目录只运行一个应用进程，
不要使用多个 Uvicorn worker。公网部署还应使用 HTTPS/WSS、精确 CORS 白名单、反向代理
WebSocket Upgrade、WAF/防火墙和入口层限流；应用内限流不能替代 DDoS 防护。

## 文档入口

- [HTTP/WebSocket 完整接口契约](docs/api/FRONTEND.md)
- [WebSocket 事件清单](docs/api/WEBSOCKET_EVENTS.md)
- [AI 主播状态与思维流程](docs/concepts/AI_STREAMER_FLOW.md)
- [系统架构说明](docs/architecture/OVERVIEW.md)
- [登录观众长期记忆](docs/concepts/LONG_TERM_MEMORY.md)
- [主播情景记忆](docs/concepts/EPISODIC_MEMORY.md)
- [部署指南](docs/DEPLOYMENT.md)
- [插件开发指南](docs/plugins/DEVELOPMENT.md)
- [v0.3.0 迁移指南](docs/MIGRATION_V030.md)
- [v0.4.0 迁移指南](docs/MIGRATION_V040.md)
- [贡献指南](CONTRIBUTING.md) · [安全策略](SECURITY.md) · [更新日志](CHANGELOG.md)

## 贡献

欢迎提交 Issue 和 Pull Request。修改 HTTP/WebSocket 字段时请同步接口文档；修改人格、
记忆、活动或 Prompt 组装时请说明行为变化和验证方式。请勿提交真实配置、数据库、日志、
用户数据或版权素材。

## License

[MIT](LICENSE)
