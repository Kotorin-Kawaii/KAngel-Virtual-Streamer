# 系统架构说明

本文描述 KAngel Virtual Streamer 的后端运行思路。更偏“数字人思维链路”的说明见 [AI_STREAMER_FLOW.md](AI_STREAMER_FLOW.md)，更偏前端字段的说明见 [FRONTEND_API.md](FRONTEND_API.md)。

## 顶层结构

```text
Client / Frontend
  ├─ HTTP: auth, profile, SC, emote, config/query
  └─ WebSocket: danmaku, AI reply, mood, metadata, activity, emote

FastAPI
  ├─ api/                路由与协议入口
  ├─ core/               人格、记忆、活动、SC、限流、连接管理
  ├─ services/           OpenAI-compatible AI client
  ├─ models/             Pydantic 数据结构
  ├─ config/             配置与情绪动作目录
  ├─ plugins/            插件加载与事件扩展
  └─ utils/              Prompt、QA、日志等工具
```

## 主事件线：弹幕到回复

1. WebSocket 收到观众弹幕。
2. 连接层解析游客/登录用户身份。
3. 限流、过载保护与基础校验。
4. 弹幕进入上下文记忆与弹幕池。
5. 选择器挑选值得回复的普通弹幕；SC 走独立优先队列。
6. 回复生成前，系统组装：
   - 当前弹幕；
   - 同用户短上下文；
   - 登录用户长期记忆摘要；
   - 主播 mood/stress/darkness；
   - 主播内部状态；
   - 当日主题；
   - 当前活动；
   - 相关 QA；
   - SC 高优先级上下文。
7. QA 选择与情感影响分析可并行执行，以降低关键路径延迟。
8. 主回复模型输出结构化情绪与分句文本。
9. 后端广播回复，并持久化必要的关系与记忆数据。

核心约束：当前弹幕与上一轮形成的直接问答，永远优先于冲突的每日主题、当前活动和长期记忆。

## 并行状态线

除弹幕回复外，系统还维护几条独立状态线：

- 直播排期：根据配置时区和时间段计算当前是否开播。
- 每日主题：按日期轮换主题，并随元数据推送给前端。
- 当前活动：维护“主播现在正在干什么”，可静默变化，也可主动广播活动变化。
- 心情推送：周期性向前端推送 mood/stress/darkness 等状态。
- SC 消费：独立队列，优先于普通弹幕。
- 表情互动：只广播 emote id，不进入人格、记忆或 AI 链路。
- 公网保护：HTTP/WS 限流、有界队列、发送背压与安全指标。

## 持久化

默认持久化使用 SQLite，主要保存：

- 账号与登录身份；
- 昵称历史；
- 观众关系；
- 长期记忆；
- SC 队列与历史；
- 必要的运行状态。

当前目标是“单 Python 进程即可运行”，因此不把 Redis、消息队列或外部数据库作为必需依赖。

## AI 服务

后端统一调用 OpenAI Chat Completions 兼容接口：

```text
{AI__BASE_URL}/chat/completions
```

不同任务可以配置不同模型：

- 主回复模型：质量优先；
- QA 选择：轻量、快速；
- 弹幕选择：轻量、快速；
- 情感影响分析：平衡速度与准确性。

这样可以降低端到端延迟，同时避免把供应商 SDK 写死进核心逻辑。

## 插件

插件位于 `plugins/`，通过 `plugins.plugin_manager` 加载。插件可以参与弹幕接收、广播、AI 回复后处理或自定义事件，但应避免阻塞主事件循环。
