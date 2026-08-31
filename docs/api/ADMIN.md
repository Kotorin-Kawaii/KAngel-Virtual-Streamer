# 管理后台与管理接口（P29）

面向运维者，不是公开契约。观众前端的公开接口写在
[FRONTEND.md](FRONTEND.md)，管理面刻意不出现在那份文档里。

## 1. 开启方式

管理面整体默认关闭。未开启时**所有** `/admin/*` 与全部带管理门禁的接口
（含 `/config`、`/plugins`、`/persona/*` 等）一律返回 `404`——不是 `401`/`403`，
以免向外暴露「这里有管理接口」。

```bash
ADMIN__ENABLED=true
ADMIN__API_KEY=<强随机值>
ADMIN__RATE_PER_MINUTE=120
ADMIN__BURST=40
ADMIN__CONCURRENCY=2
```

密钥自己生成，别复用任何账号口令：

```bash
python -c "import secrets;print(secrets.token_urlsafe(32))"
```

后台地址：`http://<host>:<port>/admin/ui`

## 2. 鉴权

管理密钥与普通用户登录令牌**完全分离**：不读 Cookie、不接受账号 token，
管理员身份只由密钥决定。两种写法等价：

```bash
curl -H "x-admin-key: $ADMIN_KEY" http://localhost:8000/admin/tokens/daily
curl -H "Authorization: Bearer $ADMIN_KEY" http://localhost:8000/admin/tokens/daily
```

校验顺序（`_validate_admin_request`）：

| 顺序 | 条件 | 响应 |
| --- | --- | --- |
| 1 | `admin.enabled` 为假 | `404` |
| 2 | 缺密钥 / 密钥不匹配（`secrets.compare_digest` 定长比较） | `403` |
| 3 | 超出 `admin:ip:<ip>` 的令牌桶 | `429` + `Retry-After` |
| 4 | 并发超过 `admin.concurrency` | `429` |

限流按客户端 IP 分桶，且**进程内共享**——多标签页开后台会互相挤占同一个桶。

`GET /admin/ui` 是唯一不校验密钥的路径：它只是一个不含任何数据与凭据的空壳。
要求它校验密钥，就必须把密钥放进 URL，而 URL 会进访问日志和浏览器历史；
真正的数据请求全部带 `x-admin-key`。它仍受 `admin.enabled` 门禁（关闭时 404）。

## 3. 后台单页

- 单文件自包含：HTML + 内联 CSS/JS，**零外部请求**，无框架、无 CDN、无图表库。
  页面里唯一的 `http://` 字面量是 `createElementNS` 需要的 SVG 命名空间常量。
- 页面以 Python 常量 `ADMIN_UI_HTML` 的形式存在（`transport/http/admin_ui.py`），
  因为打包配置里没有 package-data，独立 `.html` 不会随包分发。
- 密钥由顶部输入框粘贴，只存 `sessionStorage`（关标签即失效）：不写
  `localStorage`、不写 Cookie、不进 URL。
- 响应头：`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、
  `Referrer-Policy: no-referrer`，以及
  `default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; form-action 'none'; frame-ancestors 'none'`。
- **请求纪律**（迁就 admin 桶）：客户端 fetch 队列并发上限 2；面板懒加载，
  只在展开时才拉；默认不自动刷新，可选最小 30s；收到 `429` 读 `Retry-After`
  退避并提示，不重试轰炸。

## 4. Token 审计

### 4.1 口径

- 埋点在唯一收口 `AIService._call_model`，覆盖全部 AI 调用。
- 维度：**日期 × role × provider × model**。不做账号级或单条弹幕级归因。
- **逐次明细**保留 `TOKEN_AUDIT__DETAIL_RETENTION_DAYS` 天（默认 14）；
  **每日聚合永久保留**。清理只删明细，永不动聚合。
- 一次带回退的调用产生**多行**（每次尝试一行），重试成本可直接看出来。
- 供应商没返回 `usage` 时记 `usage_reported=false` 并显示「未上报」，**不猜数字**。
- 自然日按 `STREAM__TIMEZONE` 划分，与主播作息一致；时区无效时回退 UTC。

### 4.2 花费

金额在**读取时**按当前 `AI__PRICING` 折算，不落库——补配或改价后历史曲线自动跟着变。

```bash
AI__PRICING=[{"model":"gpt-4o-mini","input_per_1m":2.0,"output_per_1m":8.0,"cached_input_per_1m":0.4,"currency":"CNY"},{"model":"*","input_per_1m":1.0,"output_per_1m":4.0,"currency":"CNY"}]
```

- 单价是**每 100 万 token**。`"*"` 是兜底项，只在精确匹配（大小写不敏感）未命中时用。
- `cached_input_per_1m` 不配时，缓存 token 按输入原价计。
- 同一份配置里 `currency` 必须一致：混币无法求和，宁可启动就报错，也不要在后台画出错的曲线。
- 未配价的模型 `cost_amount` 为 `null`、`priced=false`，其 token 数汇总进
  `unpriced_tokens`，那一天/那一行标 `fully_priced=false`。**不用兜底价假装算出了钱。**

### 4.3 隐私

明细表里没有任何 PII：没有 prompt、回复正文、`message_id`、昵称、账号或 IP。
失败行只记异常**类名**（`error_kind`），不记异常消息——消息可能带 URL 里的密钥或
prompt 片段。契约测试断言了明细的字段集合，新增列会让测试失败。

### 4.4 接口

| 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/admin/tokens/daily` | `days`（1–180，默认 14） | 每天一行；缺数据的日子补零 |
| GET | `/admin/tokens/breakdown` | `start`/`end`（`YYYY-MM-DD`）、`days`（1–180） | 一次返回 `by_role` / `by_provider` / `by_model` |
| GET | `/admin/tokens/records` | `day`、`role`（≤64）、`status`（`success`\|`failed`）、`limit`（1–500，默认 100）、`offset` | 逐次调用明细 |
| GET | `/admin/tokens/stats` | — | 记账器健康度、存储行数、价目覆盖 |
| GET | `/admin/overview` | — | 常用只读快照 + 近 7 天 token |

`/admin/overview` 不是锦上添花而是必需：后台开屏若逐个拉 40 多个接口，
按默认 `burst=10` 第 11 个请求就会 429。所有 DB 读取走 `asyncio.to_thread`。

`daily` 响应形状：

```json
{
  "start_day": "2026-08-19", "end_day": "2026-08-25",
  "timezone": "Asia/Shanghai", "currency": "CNY", "pricing_configured": true,
  "days": [{"day": "2026-08-19", "calls": 132, "failed_calls": 1,
            "usage_missing_calls": 0, "input_tokens": 84120,
            "output_tokens": 9310, "cached_input_tokens": 41000,
            "total_tokens": 93430, "latency_ms_sum": 107184,
            "cost_amount": 0.2413, "unpriced_tokens": 0,
            "fully_priced": true}],
  "totals": {"...": "同上字段的区间合计", "distinct_models": 3}
}
```

`latency_ms_sum` 是延迟总和，除以 `calls` 得平均延迟；`breakdown` 的分组行直接
给算好的 `avg_latency_ms`。

`stats` 里的 `pricing.models_without_price` 列出**有量却没配价**的模型名
（回看 `lookback_days` 天），是补价目表时唯一需要看的清单。`recorder` 含
`enabled/running/queued/recorded/flushed/dropped/retries/errors/purged/last_flush_at/last_error_kind`
与当前保留策略；`storage` 给两张表的行数与最早/最晚日期。

### 4.5 开关与回退

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TOKEN_AUDIT__ENABLED` | `True` | 总开关。关闭后 `record()` 直接返回、flush 任务不启动 |
| `TOKEN_AUDIT__DETAIL_ENABLED` | `True` | 关掉只留每日聚合，不写逐次明细 |
| `TOKEN_AUDIT__DETAIL_RETENTION_DAYS` | `14` | 明细保留天数（1–365）；聚合表永久保留 |
| `TOKEN_AUDIT__FLUSH_INTERVAL_SECONDS` | `5` | 落库间隔（1–300） |
| `TOKEN_AUDIT__FLUSH_BATCH_SIZE` | `200` | 单次事务最多写多少条（1–2000） |
| `TOKEN_AUDIT__QUEUE_CAPACITY` | `2000` | 内存队列上限（100–100000），满了丢最旧并计数 |
| `TOKEN_AUDIT__PURGE_INTERVAL_SECONDS` | `3600` | 明细清理节流（60–86400） |

记账**绝不影响回复**：回复路径上只有一次内存 `deque.append`，落库在后台任务里；
`record()` 整体包在 `try/except` 里，失败只递增 `errors` 计数。写库失败的批次放回
队首重试一次，再失败就丢弃并计数（不无限堆积）。关服前做最后一次 flush。

回退只需改 `.env` 重启，不用回滚代码：`TOKEN_AUDIT__ENABLED=False` 停记账，
`ADMIN__ENABLED=False` 关整个管理面。

## 5. 密钥脱敏

`GET /config` 会在浏览器里渲染，所以凭据在**服务端**就换成固定哨兵 `"***"`：

- `ai.api_key`、`ai.providers[].api_key`、`admin.api_key`；
- `custom` 段里递归匹配键名含 `api_key` / `apikey` / `token` / `secret` / `password` 的项。

空值保持为空，用来区分「没配」和「配了但不给看」。

`sponsor` 与 `token_audit` **不在** `export_config()` 的白名单里：前者含爱发电凭据，
后者的开关通过 `/admin/tokens/stats` 展示，暴露面越小越好。

回灌保护：`update_settings()` 跳过值恰为 `"***"` 的字段并记一条 warning，
所以有人 `GET /config` 再 `PUT` 回来，不会把哨兵写成真密钥。

自查：

```bash
curl -s -H "x-admin-key: $ADMIN_KEY" http://localhost:8000/config | grep -i "api_key\|token"
```

## 6. 既有管理接口清单

全部需要管理密钥，全部在后台里有对应面板。

**概览与配置**：`GET /admin/overview`、`GET /config`、`GET /connections`、
`GET /admin/security/stats`

**Token 审计**：`GET /admin/tokens/{daily,breakdown,records,stats}`

**SC 与审核**：`GET /admin/sc/stats`、`GET /admin/moderation/stats`

**弹幕**：`GET /danmaku/pool`、`GET /danmaku/selector/stats`

**人格与情绪**：`GET /persona/state`、`GET /persona/events/debug`、
`GET /persona/impact/{debug,history}`、`GET /emotion/{stats,randomness}`、
`GET /mood/pusher/stats`、`GET /admin/prompt-ram`

**记忆**：`GET /memory/{stats,context,group-discussion,persona-impact}`、
`GET /memory/episodic/stats`

**直播**：`GET /stream/{activities,metadata/stats}`

**表情**：`GET /admin/emotes/stats`

**赞助**：`GET /admin/sponsor/stats`

**数据库**：`GET /database/{stats,danmaku,replies,export}`

**插件**：`GET /plugins`

### 6.1 后台里的写操作

只保留**可逆低危**动作，每个都要在弹窗里输入 `EXEC` 二次确认：

| 方法 | 路径 |
| --- | --- |
| POST | `/plugins/{plugin_name}/enable` \| `/disable` |
| POST | `/emotion/randomness`（`randomness`） |
| POST | `/emotion/reset-history` |
| POST | `/emotion/select`（`mood`/`stress`/`darkness`/`count`） |
| POST | `/persona/impact/debug-mode`（`enabled`） |
| POST | `/persona/impact/analyze`（JSON body `content`） |

**刻意排除**（后台只以禁用状态列出并注明「请用 curl 执行」）：

- `PUT /config` —— 全量覆盖运行配置，不可逆；
- `POST /persona/reset` —— 清空人格状态，不可逆。

### 6.2 `GET /admin/prompt-ram`（P30 工作记忆）

主播「未闭合意图」的内存快照，纯只读：

```json
{
  "enabled": true,
  "stats": {"harvested": 12, "rejected": 2, "fulfilled": 5, "superseded": 1,
            "expired": 4, "evicted": 0, "errors": 0, "open_entries": 3},
  "entries": [
    {"entry_id": "…", "kind": "awaiting_viewer", "state": "open",
     "note": "问了他推的角色，等他答", "target_nickname": "小明",
     "target_subject_id": "account:1234", "created_at": "…",
     "remaining_seconds": 121.4, "version": 1}
  ]
}
```

`note` 是模型自由生成的念头原文，`target_subject_id` 是身份主键 ——
**两者只允许出现在这个 ADMIN_ONLY 接口里**：不进 WS 广播、不进 SQLite、
不进任何面向观众的响应。这也是调这个功能时唯一的观察窗口（想法质量、
TTL 是否合适、消毒有没有误杀，都看 `stats` 与 `note`）。

`PROMPT_RAM__ENABLED=false` 时接口仍在，返回 `enabled: false` 且
`entries` 为空。契约由 `tests/contract/test_admin_dashboard.py` 钉住顶层
字段集合 `{enabled, stats, entries}`。

## 7. 上线顺序

1. 先只开记账（`TOKEN_AUDIT__ENABLED=true`，`ADMIN__ENABLED` 仍关），观察一天，
   确认 token 量级与供应商账单吻合。
2. 再开 `ADMIN__ENABLED` + 强密钥，用后台看数。
3. 最后补 `AI__PRICING`，金额出现。

任何一步异常都只需改 `.env` 重启回退。

## 8. 数据表

| 表 | 主键 | 保留 |
| --- | --- | --- |
| `ai_token_usage_records` | `record_id` | `detail_retention_days` 天 |
| `ai_token_usage_daily` | `(day, role, provider, model)` | 永久 |

明细写入用 `INSERT OR IGNORE`（同 `record_id` 不重复写），聚合用
`ON CONFLICT ... DO UPDATE SET x = x + excluded.x`，两者在**同一个事务**里完成。
