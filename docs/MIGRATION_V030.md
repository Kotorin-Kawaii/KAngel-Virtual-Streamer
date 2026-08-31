# 从 v0.2.0 快照升级到 v0.3.0

v0.3.0 是一次跨多个内部开发阶段的追赶发布。它把公开仓库从“分层架构快照”推进到
具有连续人格、直播主线、观众记忆、管理与可观测能力的运行时，但不会默认开启所有实验
系统。升级前请先备份 `.env` 和 SQLite 数据库，并在测试目录验证自定义 Prompt 与前端。

## 快速升级

```bash
git pull --ff-only
uv sync --frozen
cp .env.example .env.example.v030
# 将已有 .env 与模板逐项对照，保留自己的凭据和主题
uv run python main.py
```

从旧公开版本升级时，应用启动会幂等创建新增表、索引和兼容字段，**No manual migration
required**。不要把 `.env.example` 当成生产配置覆盖现有 `.env`。如果数据库来自生产环境，
先复制副本再启动新版本；服务应保持单进程写入一个 SQLite 数据目录。

## Python 与依赖

- 支持 Python 3.11/3.12；
- 推荐 `uv sync --frozen`，也可使用 `pip install -r requirements.txt`；
- 仍只需要 SQLite 和一个 OpenAI Chat Completions 兼容模型服务，不需要 Redis；
- `pyproject.toml` 的项目版本统一为 `0.3.0`，命令行入口 `kangel-server` 保持不变。

## 新增配置

所有新增配置都可以不填，代码会使用安全默认值；修改配置后通常需要重启。

| 配置族 | 作用与默认 | 升级动作 |
| --- | --- | --- |
| `PERSONA__DYNAMICS__*` | 三轴动力学、锚点、余波和恢复参数；`enhanced` 默认 | 不填即可保持默认，调参前先回放 |
| `AI__PROVIDERS` | 按角色和时段选择 Provider，失败自动回退 | 单供应商旧字段仍有效；JSON 必须保持合法 |
| `AI__*` reasoning/role | 为 QA、选择、情绪、管理、情景记忆和 Director 分配模型/超时 | 留空时回退默认模型；Provider 不支持 reasoning 时不会强行发送 |
| `AI__EVENT_APPRAISAL_ENABLED`、`AI__PARALLEL_CONTEXT_ANALYSIS` | 结构化事件评价与 QA/情绪并行 | 默认开启；故障时按兼容路径放行 |
| `AI__INTENT_SHADOW_*` | 非阻塞意图候选及受控应用 | 默认关闭，需验证后灰度 |
| `EPISODIC_MEMORY__*` | 主播情景记忆候选、总结、重试、保留和提示词预算 | 捕获默认开启，外部 AI 总结默认关闭 |
| `PROMPT_RAM__*` | 场次内易失的未闭合互动意图 | 默认关闭，不写数据库、不跨重启恢复 |
| `STREAM__MAINLINE_*`、`STREAM__DIRECTOR_*` | 场次主线快照、待机和 Director 灰度 | Mainline 事实层默认开启；Prompt 注入和 Director 默认关闭 |
| `MODERATION__*` | LLM 语义建议、违规累计、提醒/禁言/管理员复核 | 默认启用后端安全链路；硬规则不能被关系宽容绕过 |
| `TOKEN_AUDIT__*`、`ADMIN__*` | token 记账及管理接口 | 审计按配置运行；管理接口默认关闭且必须独立密钥 |
| `SC__*`、`EMOTES__*`、`SPONSOR__*` | SC、表情和自愿赞助感谢墙 | 默认冷却/展示安全；赞助不授予任何功能权益 |

模板中的 Provider key、管理员 key 和爱发电凭据全部是占位符。真实值只能放在部署机的
`.env`，不能提交 Git，也不能写入前端。

## 数据库与记忆变化

新增的场次事实、情景记忆候选/任务/反思、赞助者和 token 审计表会自动创建；已有账号、
昵称历史、关系、长期人物记忆、SC 队列和人格状态不会被重写。情景记忆候选不复制完整
弹幕，最终召回受相关性与字符预算限制。游客仍不能因为昵称相同而继承账号记忆。

启用历史情景记忆回填属于维护操作，不是普通升级步骤。请只在数据库副本上执行私有维护
工具，先只读审计、确认直播日边界和账号关联，再决定是否应用；回填不会重演当前人格状态
或生成处罚。记忆删除仍不清除 moderation 安全状态。

## HTTP / WebSocket 兼容

核心旧接口继续可用，新增字段和事件采用增量形式：

- `/stream/metadata` 增加开播状态、主题、当前活动、主线和待机信息；旧客户端可忽略未知字段；
- `/sponsor/config`、`/sponsors` 提供赞助入口和仅昵称感谢墙；
- `/moderation/status` 可用于登录用户刷新禁言状态；
- WebSocket 增加 `streamer_beat`、`streamer_idle_state`、`stream_mainline_beat`、
  `streamer_moderation` 等事件，普通 `ai_reply` 增加可选 `source`；
- 新事件不会要求旧前端必须处理。前端接入细节以
  [`docs/api/FRONTEND.md`](api/FRONTEND.md) 和 [`WEBSOCKET_EVENTS.md`](api/WEBSOCKET_EVENTS.md)
  为准。

认证仍可使用 Bearer 或 HttpOnly Cookie。跨站浏览器必须配置精确 CORS Origin，并按 HTTPS
场景启用 `Secure; SameSite=None` Cookie；不要把 token 持久化到浏览器存储。

## 情绪目录与前端资源

情绪动作目录已扩展并清理重复标签；后端发送稳定的 emotion ID，前端负责把 ID 映射到
自己的立绘/动画资源。旧客户端应对未知 ID 使用静态默认动作；升级前检查自定义前端的
fallback，不要根据中文显示名做唯一判断。

## 已退役或不再公开的内容

- 旧的扁平架构目录不再作为运行时入口；请使用 `src/kangel` 和 `main.py`；
- 话题热度不再作为主播人格或弹幕选择的主要驱动，普通短期上下文仍保留；
- 私有生产部署脚本、回放工具、历史数据库回填和运营审计资料不在公开发布包中；
- 没有删除核心 HTTP/WS 入口，旧客户端无需因本次升级强制重写。

## 回滚与检查

升级前保存：

1. 旧源码或 Git commit；
2. `.env`；
3. `data/stream_data.db`，以及服务未完全停止时的 `-wal`/`-shm`。

如果新版本启动失败，停止新进程，恢复旧源码和配置，再恢复数据库备份。不要把新版本
已经写入的新增表当作旧版本可识别；最稳妥的回滚方式是同时恢复升级前数据库副本。

升级完成后检查：

```bash
curl -fsS http://127.0.0.1:8000/status
curl -fsS http://127.0.0.1:8000/stream/metadata
```

若只使用基础弹幕和 AI 回复，通常不需要额外改动；若使用长期记忆、自己开发前端、多个
Provider 或自定义情绪资源，请重点阅读接口文档和上面的配置/兼容说明。
