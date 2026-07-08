<div align="center">

# KAngel Virtual Streamer

### 让 AI 主播拥有连续人格、观众记忆与自己的直播节奏

`FastAPI` · `WebSocket` · `SQLite` · `OpenAI-compatible API`

[在线体验](https://kotorin-kawaii.github.io/KangelAI/) · [接口文档](docs/FRONTEND_API.md) · [思维流程](docs/AI_STREAMER_FLOW.md)

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white&style=flat-square)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-WebSocket-009688?logo=fastapi&logoColor=white&style=flat-square)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)


</div>

KAngel Server 是一个 AI 虚拟主播后端。它不只生成单条回复，还会持续维护主播情绪、内部状态、直播主题、当前活动、观众关系与登录用户长期记忆，让回复在长时间直播中保持上下文和人格连续性。

## 在线体验

完整前端已通过 GitHub Pages 部署：

> [https://kotorin-kawaii.github.io/KangelAI/](https://kotorin-kawaii.github.io/KangelAI/)

在线页面连接作者部署的服务。自行部署后端或二次开发前端时，需要修改前端 API/WebSocket 地址，并把前端 Origin 加入后端 CORS 精确白名单。

## 功能

| 模块 | 能力 |
| :--- | :--- |
| AI 回复 | 普通弹幕选择、同用户上下文承接、QA 与情感分析并行、结构化情绪与分句输出 |
| 主播人格 | 持续维护 mood、stress、darkness 以及依恋、自信、疲劳等内部状态 |
| 直播状态 | 按时区和排期控制开播，轮换每日主题，并维护“主播现在正在做什么” |
| 观众系统 | 游客临时身份、账号注册登录、昵称历史、关系状态和可清除的长期记忆 |
| SC | 注册用户专用、独立持久化 FIFO、账号冷却、拥堵时优先于普通弹幕 |
| 表情互动 | 仅广播表情 ID，不进入弹幕池、人格、记忆或任何 AI 调用 |
| 公网保护 | HTTP/WS 限流、AI 有界队列、逐连接发送队列、背压与可识别错误 |
| 扩展 | 插件系统、完整 HTTP/WebSocket 契约、OpenAI-compatible 模型服务 |

## 运行要求

- Python 3.11+
- 支持 OpenAI Chat Completions 格式的模型 API
- 单 Python 进程；持久化使用 SQLite，不依赖 Redis

## 快速开始

```bash
git clone https://github.com/Kotorin-Kawaii/KAngel-Virtual-Streamer.git
cd KAngel-Virtual-Streamer
```

推荐使用 uv：

```bash
uv sync
cp .env.example .env           # Windows 可手动复制
uv run python main.py
```

也可以使用传统 pip：

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env           # Windows 可手动复制
python main.py
```

启动后可访问：

| 地址 | 用途 |
| :--- | :--- |
| `http://127.0.0.1:8000/` | 服务状态 |
| `http://127.0.0.1:8000/docs` | OpenAPI 调试页面 |
| `ws://127.0.0.1:8000/danmaku` | 弹幕 WebSocket |

## 最小配置

编辑 `.env`：

```dotenv
AI__BASE_URL=https://api.example.com/v1
AI__API_KEY=replace-me
AI__DEFAULT_MODEL=replace-with-your-reply-model

# 简单任务推荐使用更快、更便宜的模型
AI__QA_SELECTOR_MODEL=replace-with-your-fast-model
AI__DANMAKU_SELECTOR_MODEL=replace-with-your-fast-model
AI__IMPACT_ANALYSIS_MODEL=replace-with-your-balanced-model
AI__PARALLEL_CONTEXT_ANALYSIS=True
```

服务端统一请求 `{AI__BASE_URL}/chat/completions`。SiliconFlow、OpenAI、兼容网关或本地模型服务，只要实现相同的请求与响应结构即可使用；项目不维护供应商专属 SDK 分支。

完整配置示例见 [.env.example](.env.example)。最终回复模型决定人设与上下文质量，QA、候选选择等分类任务可以使用更轻量的模型降低延迟和费用。

## 自定义主播

只修改昵称不会自动替换完整人格。创建新角色时建议按下表逐层调整：

| 想修改的内容 | 修改位置 | 说明 |
| :--- | :--- | :--- |
| 名称、房间主题、初始情绪 | `.env` / `config/settings.py` 的 `PersonaConfig` | 修改 `PERSONA__STREAMER_NAME`、`PERSONA__THEME`、初始 mood/stress/darkness |
| 核心身份、性格、口癖与偏好 | `utils/streamer_prompt_generator.py` → `_build_system_prompt()` | 最终回复与情感分析共享的核心人格；新角色必须完整替换硬编码设定 |
| 人物事实与典型问答 | `utils/streamer_prompt_generator.py` → `QA_DATA` | 使用稳定且唯一的 `Qxx` 编号，避免互相冲突或过时的事实 |
| 数值状态如何影响表达 | `utils/streamer_prompt_generator.py` → `_build_persona_influence_description()` | 定义不同 mood/stress/darkness 区间的语言表现 |
| 状态衰减、边界与关系权重 | `core/persona_dynamics.py` | 控制长期人格变化，修改后应重点回归测试 |
| 模型可输出的情绪动作 | `config/emotion_catalog.py` | 前端必须提供对应动作或资源映射 |
| 每日主题与当前活动 | `.env` 中 `STREAM__DAILY_THEMES`、`STREAM__ACTIVITY_CANDIDATES` | 默认结构位于 `config/settings.py` 的 `StreamConfig` |
| 立绘、动画、音频和表情资源 | 前端项目 | 后端只发送情绪、文本、活动和表情 ID |

建议保留核心语义约束：**当前弹幕与上一轮形成的直接问答，永远优先于每日主题、当前活动和长期记忆。** 否则模型很容易机械复述背景资料。

角色更换后，建议在测试环境使用新的 SQLite 数据库，避免旧角色状态、关系和长期记忆混入新角色。正式迁移前请先备份，不能直接删除仍需保留的用户数据。

## 直播排期示例

```dotenv
STREAM__TIMEZONE=Asia/Shanghai
STREAM__WEEKLY_SCHEDULE={"monday":[{"start":"06:00","end":"03:00"}],"tuesday":[{"start":"06:00","end":"03:00"}]}
STREAM__DAILY_THEMES=[{"id":"chat","name":"互联网杂谈","prompt_hint":"偶尔聊聊网络见闻。"},{"id":"game","name":"游戏闲聊"}]
```

结束时间早于开始时间表示跨越午夜。未配置的星期默认不开播。

## 项目结构

```text
KAngel-Virtual-Streamer/
├── api/          # HTTP 与 WebSocket 路由
├── config/       # 配置模型、情绪动作目录
├── core/         # 人格、记忆、SC、活动、限流等业务逻辑
├── models/       # Pydantic 数据结构
├── plugins/      # 插件系统与示例
├── services/     # OpenAI-compatible AI 客户端
├── utils/        # Prompt、QA 与日志工具
├── docs/         # 接口与机制文档
├── main.py       # 应用入口
└── .env.example  # 脱敏配置模板
```

## 部署

同一数据目录只运行一个应用进程，不要使用多个 Uvicorn worker。业务持久化依赖 SQLite，应用内限流和并发计数保存在单进程内存中。

公网部署建议同时配置：

- HTTPS 反向代理或 CDN/WAF；
- 源站防火墙、连接数和入口层速率限制；
- 精确的 `CORS__ALLOWED_ORIGINS`；
- 跨站登录所需的 `Secure; SameSite=None` Cookie；
- 独立且足够强的管理员密钥，或保持管理接口关闭。

应用内保护不能替代入口层 DDoS 防护。

## 文档

- [完整 HTTP/WebSocket 接口契约](docs/FRONTEND_API.md)
- [AI 主播状态与思维流程](docs/AI_STREAMER_FLOW.md)
- [系统架构说明](docs/ARCHITECTURE.md)
- [部署指南](docs/DEPLOYMENT.md)
- [登录观众长期记忆](docs/LONG_TERM_MEMORY.md)
- [记忆隐私与用户控制](docs/MEMORY_PRIVACY.md)
- [插件开发指南](docs/PLUGIN_GUIDE.md)
- [贡献指南](CONTRIBUTING.md)
- [安全策略](SECURITY.md)
- [更新日志](CHANGELOG.md)

## 常见问题

### 为什么不能启动多个 worker？

当前正式支持形态是“单 Python 进程 + SQLite”。多 worker 会拆分 WebSocket 连接、应用内限流和队列状态，并让同一数据库承受不必要的并发写入。

### 为什么回复仍然需要几秒？

一轮回复包含上下文、QA、情感影响和主回复模型。QA 与情感分析已经并行，但最终延迟仍受最慢前置模型和主回复模型影响。可通过为分类任务配置轻量模型降低等待时间。

### 表情会影响主播心情吗？

不会。观众表情是纯展示旁路，不进入弹幕池、人格、关系、记忆、活动或 AI 链路。

### 登录用户的记忆可以删除吗？

可以。系统提供查询、导出、清除和退出长期记忆的接口；游客不会因为昵称相同而共享账号记忆。

## 贡献

欢迎提交 Issue 和 Pull Request。涉及协议字段时，请同步更新 `docs/FRONTEND_API.md`；涉及人格、记忆或状态动力学时，请说明行为变化和回归方式。请勿提交真实 `.env`、API Key、SQLite 数据库、日志或用户数据。

## License

[MIT](LICENSE)

# *愿天使的光芒照耀所有宅宅✨*
