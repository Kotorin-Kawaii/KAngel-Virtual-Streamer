# Live Presence、SpeechThread 与观众问候

本仓库公开后端机制与 API，不含私有网页前端。所有新权限默认关闭；代码完成不等于
真实模型表现已验收。不要一次性开启全部阶段。

## 语义与边界

Persona 约束公开表达，Live Presence 决定舞台时机，冻结的 Theme/Plan、当前 Beat/Activity
提供已提交事实，SpeechThread 只组织本场几十秒到几分钟的话题。不建立人格 Core，
不把偏航写回 Plan，不把 Thread 写成长期记忆，不从模型台词反推活动变化。

Live Presence 与 Active 同时开启且 Mainline 有效时，普通弹幕、SC、舞台发言读取相同
场次语义；旧主线 Prompt/主题投影开关不再阻断它。Active 关闭时恢复旧开关控制。
观众当前问题、SC 和安全边界优先于任何自主演出。

## 分阶段配置

所有下列权限在 settings 与公开示例中均为 False：

| 阶段 | 显式开关 | 作用 |
| --- | --- | --- |
| Foundation | LIVE_PRESENCE_ENABLED | 建立场次状态；需重启 worker |
| Shadow | LIVE_PRESENCE_SHADOW_ENABLED | 只观测策略，不改变正式回复 |
| Read authority | LIVE_PRESENCE_ACTIVE_ENABLED | 允许 ANSWER / 保守 IGNORE_AFTER_READ |
| Atomic self-talk | LIVE_PRESENCE_SELF_TALK_ACTIVE_ENABLED | 安静、无观众任务时单轮发言 |
| Thread C1 | LIVE_PRESENCE_THREAD_ENABLED | 状态维护、TTL、CAS 和延续 Shadow |
| Thread C2 | LIVE_PRESENCE_THREAD_OPENER_ENABLED | 审核素材开题；成功发送后建话题 |
| Thread C3 | LIVE_PRESENCE_THREAD_SUSPEND_ACTIVE_ENABLED | 真正回复开始时暂停，IGNORE 不暂停 |
| Thread C5 | LIVE_PRESENCE_THREAD_RESUME_ACTIVE_ENABLED | 恢复暂停话题或推进 ACTIVE 下一段 |
| Thread C6 | LIVE_PRESENCE_THREAD_SEMANTIC_HINT_ENABLED | 同次舞台生成的可选结构化语义建议 |
| Audience facts | AUDIENCE_PRESENCE_ENABLED | 去重在线感知与进房候选 Shadow |
| Audience greeting | AUDIENCE_GREETING_ACTIVE_ENABLED | 受人格约束的单句公开问候 |

表中开关使用 STREAM__ 前缀。启停 Foundation、Thread 或 Audience worker 需重启；
不要把总开关当作可靠热生命周期管理。正式主动演出还要求 Director 和
DIRECTOR_PERFORMANCE_ENABLED 开启；先验证 Shadow，再逐阶段放行。

Thread 默认 TTL 300 秒、不无限续期，段落间隔 45 秒，最多 3 段、3 次中断，
全场最多一个未结束话题。沿用场次 Speak 配额、冷却和忙碌门禁。
C0 单句仍是 atomic，不自动创建 Thread。问候不设次数预算，但保留个人/全局冷却、
稳定入场时间、候选 TTL、公共舞台间隔和观众抢占；不批量点名所有连接。

## 交付、重启和回滚

至少一个 WebSocket 发送成功才提交开题/推进；这不是客户端播放确认。零发送不推进。
发送结果不明或发送后提交失败，不重发该段，恢复时放弃对应 Thread。
普通弹幕 REPLIED / SC replied 不是交付证据；中断需持久确认 interruption_visible。
不确定中断保守放弃，明确广播前失败的正常回调恢复 ACTIVE。

Thread 状态复用 live_presence_sessions.state_json，CAS 防止陈旧覆盖；读取兼容 v1，
下一次写入 v2。SQLite 幂等建表和记录迁移 marker，无需手工 SQL。
下播/新场隔离，TTL 与不兼容 Activity/Mainline 切换会放弃话题，不重播旧发言。

回滚先关闭 Thread 五个权限、问候和主动行为并重启。v2 快照不保证可供旧二进制读取；
旧版本拒绝读取时安全降级，不要删库或强行改写快照。

## 隐私与观测

鉴权 GET /admin/live-presence 提供开关、版本、当前 Thread、恢复漏斗、
固定低基数 gate_blocks / stage_outcomes / semantic_hints 和最多 32 条成功公开台词
的进程内审查缓存。缓存会在重启时清空，不保存原始弹幕、Prompt、thoughts 或账号身份。
Thread 快照只保留当前摘要及上一段已交付短文本，不建立台词历史表。

Shadow observation 不存弹幕正文、昵称、账号或整段回复，保留 30 天并限制最多 50000 行。
人数来自连接与去重登录账号；在线不代表认真观看，同名不代表同一人。
问候只使用当前公开称呼和粗粒度关系标签，不按赞助金额区别待遇。

观察 opened/suspended/resume_requested/resumed/completed/abandoned、交付提交、
CAS 冲突和抑制原因；计数为进程窗口，不应当作跨重启终身统计。
保留正常 Attention / Reply / SC 延迟指标，并人工回放台词检查自然度与人格一致性。

生产验收需实际观察 opener → ACTIVE → ANSWER → SUSPENDED → CONTINUE_THREAD →
ACTIVE → COMPLETED，同时确认 SC 抢占、下一条回复、无 pending 堆积、重启不重播及新场隔离。
自动测试不证明永不 OOC，也不替代真实交付验收。
