# P24 主播情景记忆

P24 与 P21 的公开场次总结分离。P21 只说明“本场播了什么、房间整体怎样”；P24 保存主播值得在意的具体事件，例如高重要性倾诉、支持互动、共同玩梗、SC、活动里程碑和边界事件。P24 的候选冻结不依赖 P21 开关；P21 关闭或重启恢复时，仍会按候选场次 ID 独立收口。

实时回复只写结构化候选，不增加额外模型调用。候选引用已有回复、账号片段、SC、moderation 或活动记录；下播后由独立的 `session_memory` worker 在 SC 和普通回复空闲时调用 OpenAI Chat Completions 兼容模型，严格校验 JSON 后写入情景记忆和本场反思。任务表只保存候选 ID 和运行状态，私人反思保存在独立表中。

候选本身不复制完整原文，并在最长人物记忆保留期内保留来源引用，便于删除竞态、失败重试和运维审计；最终提示词只注入经过压缩的记忆层，默认总预算为 500 个字符。

登录且开启长期记忆的观众按不可变 `account_id` 关联。游客只保留匿名房间事件，不能按昵称跨场次归属。记忆删除、退出和保留期清理遵循人物记忆治理，但不会删除 moderation 安全状态。

默认配置关闭外部 AI worker：

```dotenv
EPISODIC_MEMORY__ENABLED=True
EPISODIC_MEMORY__AI_ENABLED=False
AI__SESSION_MEMORY_MODEL=deepseek-v4-flash
```

## Reliability v1

P24 任务现在按 bounded batch 执行，默认每次模型调用最多 8 个候选；一场直播最多 48 个候选，批次之间保留独立的 reflection fragment，最终合并为整场反思。`stream_memory_tasks` 的状态包括 `pending`、`processing`、`failed_retryable`、`completed` 和终态 `failed`；`stream_memory_candidates` 使用 `pending`、`claimed`、`summarized`、`discarded`。`pending` 只表示仍有真实消费路径，明确未选中的候选会写入 `discarded` 及 `resolution_code`。

每次 claim 都有 SQLite 事务、lease 和 execution token。启动与 worker wake 会有限度地执行 reconciliation：过期 lease 释放回 pending，旧的 failed 任务转为 retryable，冻结场次中缺少 task 的候选会幂等补建/补入 task，active 场次不会被提前消费。Provider timeout、HTTP、空响应、JSON、schema 和未知错误分别写入 `last_error_code`，详情经过截断和敏感信息过滤；retryable 错误使用指数 backoff，不会因 `max_attempts` 耗尽而永久丢失整场记忆。

只有配置的批次数上限、明确的数据治理/删除竞态等确定性情况才进入 task 终态 `failed`；进入终态前所有 task 输入候选都会写入 `discarded` 和原因，绝不会留下不可解释的 pending。

新增配置：

```dotenv
EPISODIC_MEMORY__AI_ENABLED=True
EPISODIC_MEMORY__BATCH_SIZE=8
EPISODIC_MEMORY__MAX_BATCHES_PER_TASK=32
EPISODIC_MEMORY__RETRY_BACKOFF_SECONDS=30
EPISODIC_MEMORY__RETRY_BACKOFF_MAX_SECONDS=1800
```

迁移和副本回放：

```bash
.venv/bin/python scripts/migrate_stream_memory_reliability.py --db /path/to/copy.db --direction upgrade --apply
.venv/bin/python scripts/migrate_stream_memory_reliability.py --db /path/to/copy.db --direction downgrade --apply
.venv/bin/python scripts/migrate_stream_memory_reliability.py --db /path/to/copy.db --direction upgrade --apply
.venv/bin/python scripts/verify_stream_memory_recovery_v1.py --db /path/to/stream_data.db
```

最后一个命令会复制输入数据库，在副本上用确定性 fake provider 回放 08/13 至 08/16 的历史任务，同时确认 08/17 active 场次仍保持 pending，并比较输入数据库 SHA-256。

开启前应先通过只读回填审计：

```bash
.venv/bin/python scripts/backfill_episodic_memory.py --db /path/to/stream_data.db
# 等价显式只读模式
.venv/bin/python scripts/backfill_episodic_memory.py --db /path/to/stream_data.db --dry-run
```

确认候选范围后，才显式执行 `--apply`。默认仅把 `2026-07-30` 及之后、且通过真实
`account_conversation_fragments.danmaku_id` 关联的片段归属到账号；更早数据即使有昵称或旧片段也降级为匿名房间事件。回填按 `Asia/Shanghai` 与每日 06:00 至次日 03:00 直播日边界归档。候选和任务使用唯一键与场次状态，重复 `--apply` 会复用已有记录，进程中断后再次执行即可续跑。

管理员可通过 `/memory/episodic/stats` 查看不含身份和原文的低基数任务状态。
