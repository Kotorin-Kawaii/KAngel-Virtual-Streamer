# 部署指南

KAngel Virtual Streamer 当前推荐部署形态是：单 Python 进程 + SQLite + 反向代理。不要用多个 Uvicorn worker 跑同一个数据目录。

## 1. 准备环境

```bash
git clone https://github.com/Kotorin-Kawaii/KAngel-Virtual-Streamer.git
cd KAngel-Virtual-Streamer
uv sync
cp .env.example .env
```

编辑 `.env`，至少设置：

```dotenv
AI__BASE_URL=https://api.example.com/v1
AI__API_KEY=replace-me
AI__DEFAULT_MODEL=replace-with-your-reply-model
CORS__ALLOWED_ORIGINS=["https://your-frontend.example.com"]
```

## 2. 启动服务

开发或小规模自用：

```bash
uv run python main.py
```

生产环境建议交给 systemd、supervisor、Docker 或你熟悉的进程管理器托管，并确保同一 SQLite 数据目录只有一个应用进程写入。

## 3. 反向代理

反向代理需要支持 WebSocket Upgrade，并转发真实客户端 IP。公网建议只暴露 HTTPS。

需要代理的主要入口：

- `GET /`
- `GET /docs`
- `POST /auth/**`
- `GET/POST /sc`
- `GET/POST /emotes/**`
- `WS /danmaku`

完整接口见 [前端接口契约](api/FRONTEND.md)。

## 4. CORS 与登录 Cookie

如果前端和后端跨域：

- 后端 `CORS__ALLOWED_ORIGINS` 必须写前端精确 Origin；
- 前端请求需要带 credentials；
- 生产 HTTPS 下跨站 Cookie 应使用 `Secure; SameSite=None`；
- 本地 HTTP 调试不要混用生产 Cookie 配置。

## 5. 入口层保护

应用内已有基础限流、有界队列和背压，但这不是 DDoS 防护。公网部署建议额外配置：

- CDN/WAF；
- 源站防火墙；
- 每 IP 连接数限制；
- 反向代理请求速率限制；
- 日志与告警。

## 6. 数据与备份

SQLite 中可能包含账号、昵称历史、SC、关系状态和长期记忆。请定期备份，并避免把数据库复制到公开仓库。

建议备份：

- SQLite 数据库文件；
- `.env` 的非密钥配置；
- 自定义 Prompt / QA / 主题 / 活动配置。

不建议备份到公开位置：

- API Key；
- 用户数据；
- 运行日志；
- 管理员密钥。
