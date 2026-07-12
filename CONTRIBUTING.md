# Contributing

感谢你愿意改进 KAngel Virtual Streamer。这个项目的目标不是只做“会回弹幕的机器人”，而是探索一个拥有连续人格、观众关系、直播节奏和长期记忆的 AI 主播后端。

## 开发环境

推荐使用 uv：

```bash
uv sync
cp .env.example .env
uv run python main.py
```

也可以使用 pip：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## 提交前检查

- 不要提交真实 `.env`、API Key、Cookie、SQLite 数据库、日志或用户数据。
- 修改 HTTP/WebSocket 字段时，同步更新 `docs/api/FRONTEND.md`。
- 修改主播状态、记忆、活动或 Prompt 组装时，同步更新 `docs/concepts/AI_STREAMER_FLOW.md` 或 `docs/architecture/OVERVIEW.md`。
- 修改部署方式时，同步更新 `docs/DEPLOYMENT.md`。
- 修改插件机制时，同步更新 `docs/plugins/DEVELOPMENT.md`。

## 代码风格

- 优先保持单 Python 进程可运行，不引入 Redis 等额外必需服务。
- 持久化默认使用 SQLite。
- AI 服务统一使用 OpenAI Chat Completions 兼容接口，不维护供应商专属 SDK 分支。
- 对会暴露给公网的接口，要考虑速率限制、错误响应和前端可识别提示。

## Issue / PR 建议

提交问题时建议包含：

- 复现步骤；
- 期望行为与实际行为；
- 相关日志，注意脱敏；
- 是否涉及前端字段、配置项或数据库迁移。

提交 PR 时建议说明：

- 改动目的；
- 影响的接口或配置；
- 是否改变主播人格/记忆/状态流；
- 你执行过的测试或本地验证方式。
