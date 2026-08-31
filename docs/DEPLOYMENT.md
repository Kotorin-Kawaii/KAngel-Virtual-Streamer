# 部署指南

KAngel Virtual Streamer 的支持形态是“单 Python 进程 + SQLite + 反向代理”。仓库公开内容
不包含生产 `.env`、数据库、模型缓存或供应商凭据；请在自己的部署目录创建这些文件。

## 1. 准备环境

需要 Python 3.11/3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/Kotorin-Kawaii/KAngel-Virtual-Streamer.git
cd KAngel-Virtual-Streamer
uv sync --frozen
cp .env.example .env
```

至少设置：

```dotenv
SERVER__HOST=127.0.0.1
SERVER__PORT=8000
AI__BASE_URL=https://api.example.com/v1
AI__API_KEY=replace-me
AI__DEFAULT_MODEL=replace-with-your-reply-model
CORS__ALLOWED_ORIGINS=["https://your-frontend.example.com"]
```

服务端只调用 `{AI__BASE_URL}/chat/completions`。供应商只要兼容 OpenAI Chat Completions
请求/响应结构即可。多 Provider、角色模型和 reasoning 配置见 `.env.example`；配置了
reasoning 不代表供应商一定支持，只有对应协议明确支持时才会发送该参数。

## 2. 启动与检查

```bash
uv run python main.py
```

检查：

```bash
curl -fsS http://127.0.0.1:8000/status
curl -fsS http://127.0.0.1:8000/stream/metadata
```

启动时会幂等创建新增 SQLite 表和索引。配置文件修改通常需要重启进程；不需要为新增
表手工编写 SQL。

## 3. systemd 示例

将路径替换成实际部署目录；不要使用多个 Uvicorn worker。

```ini
[Unit]
Description=KAngel Virtual Streamer
After=network.target

[Service]
Type=simple
User=kangel
Group=kangel
WorkingDirectory=/opt/kangel
ExecStart=/opt/kangel/.venv/bin/python /opt/kangel/main.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

同一 `data/` 目录只能由一个应用进程写入。进程内的 WebSocket 连接、弹幕池、限流桶和
正在生成的回复不会跨重启恢复；账号、关系、长期记忆、SC 队列和场次事实保存在 SQLite
中。

## 4. 反向代理与跨域

反向代理必须支持 WebSocket Upgrade，并将 `/danmaku` 转发到应用。公网部署应使用
HTTPS/WSS、精确的 `CORS__ALLOWED_ORIGINS`、源站防火墙和入口层限流。应用内限流只能
缓解误用，不能替代 CDN/WAF 或 DDoS 防护。

跨站浏览器登录通常需要：

```dotenv
AUTH__COOKIE_SECURE=True
AUTH__COOKIE_SAMESITE=none
AUTH__COOKIE_PARTITIONED=True
```

前端请求要带 `credentials: "include"`；不要把响应里的 access token 写入
`localStorage`。完整 HTTP/WebSocket 契约见 [docs/api/FRONTEND.md](api/FRONTEND.md)。

## 5. 赞助感谢墙

赞助入口默认关闭。若要启用展示入口：

```dotenv
SPONSOR__ENABLED=True
SPONSOR__PLATFORM_URL=https://your-sponsor-page.example.com
```

若要同步爱发电感谢名单，还需显式启用同步并填写服务端凭据：

```dotenv
SPONSOR__SYNC_ENABLED=True
SPONSOR__AFDIAN_USER_ID=replace-me
SPONSOR__AFDIAN_TOKEN=replace-me
```

凭据只用于服务端请求，不能提交到仓库或返回给前端。赞助不授予权限、SC 额度、徽章或
回复优先级。

## 6. 备份、升级与回滚

升级前先停止服务并备份：

1. `.env`（通过安全渠道保存，不要放进公开位置）；
2. `data/stream_data.db`，若服务未完全停止还要保存 `-wal` 和 `-shm`；
3. 自定义 Prompt、QA、主题和活动配置。

然后更新代码并同步锁定依赖：

```bash
git pull --ff-only
uv sync --frozen
uv run python main.py
```

v0.3.0 的新增表和索引会在启动时自动创建；从旧公开版本升级通常 **No manual migration
required**。若使用自定义数据库副本或需要回填历史情景记忆，请先阅读
[v0.3.0 迁移指南](MIGRATION_V030.md)，并在副本上验证后再操作。升级失败时停止新进程，
恢复原源码、`.env` 和 SQLite 备份，再启动旧版本。

## 7. 运行安全清单

- 不要在公网暴露管理接口；`ADMIN__ENABLED` 默认保持 `False`。
- API key、管理员密钥、Cookie、用户记忆和 SQLite 文件不能进入 Git。
- 默认关闭 Prompt RAM、Director、Persona Catalog rollout 和 AI 总结等实验功能。
- 定期轮换供应商和管理员凭据，观察服务日志与资源使用。
- 只运行一个写入同一 SQLite 目录的应用实例。
