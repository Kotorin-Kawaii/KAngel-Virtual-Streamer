# 前端更新建议

## 2026-08-10：主播管理事件与本站禁言状态

- 新增定向 WebSocket 事件 `streamer_moderation`。只对被处理连接发送，前端可用
  `muted`、`mute_until`、`retry_after_seconds` 和 `message` 更新当前用户的禁言提示与
  倒计时；不要展示 `moderation_id` 以外的内部评分或理由（这些字段不会下发）。
- 主播的公开设界回复仍是 `ai_reply`，但 `data.source === "moderation"`，应作为主播
  回复展示，不要把它当成普通弹幕，也不要依赖 `original_message` 关联严重违规原文。
- 登录用户重连后调用认证 `GET /moderation/status` 恢复本站禁言状态；游客状态只在
  当前 WebSocket 连接存在期间有效。禁言时后端会拒绝新弹幕并返回定向
  `streamer_moderation` 状态事件。

## 2026-07-22：事件清单收口

- 以 `docs/api/WEBSOCKET_EVENTS.md` 作为当前版本的服务端事件清单；它覆盖所有可接收事件、发送范围、是否可丢弃及断线恢复来源。
- `streamer_idle_state` 和 `streamer_beat` 已纳入 `ServerWebSocketEvent` 联合类型。前端对未知 `type` 必须安全忽略；不得把增量演出事件转换成弹幕、SC 或 `ai_reply`。

## 2026-07-16：独立主播微动作 `streamer_beat`

- WebSocket 新增可选事件 `streamer_beat`，数据为 `{ stream_session_id, version, activity_version, beat_type, display_text, occurred_at }`；完整类型见 `docs/api/FRONTEND.md`。
- 以 `(stream_session_id, version)` 去重，作为低优先级、可丢弃的独立演出展示；断线恢复后不要向后端索取或本地补播。
- 不得把 `display_text` 写进弹幕历史、`ai_reply`、SC 状态或聊天室消息列表；它不代表模型对任何用户的回复。

## 2026-07-15：特殊日期主题元数据

- 直播元数据与 `stream_status.data` 新增 `special_date_theme`，其为 `null` 或 `{ id, name, title, frontend_theme, date }`。
- 前端只按 `frontend_theme` 映射装饰资源；未知 ID、空值或不支持的资源必须安全回退普通每日主题，不得自行按客户端日期判断节日。
- 不要把 `title` 当作主播当前活动，也不要把特殊日期主题覆盖 `current_activity` 或弹幕/SC 回复。

## 2026-07-15：主播待机状态

- 直播元数据与 `stream_status.data` 新增 `streamer_idle_state`；实时变更另行推送 `streamer_idle_state`。
- 前端按 `version` 去重，使用 `frontend_animation` 映射演出资源；未知值回退静态默认状态。
- `idle_text` 仅用于界面说明，不要显示成主播发送的弹幕或与 `ai_reply` 合并。

## 2026-07-15：多语言回复展示

- 后端会根据弹幕文本选择回复表达语言，`ai_reply` 结构和字段不变；前端应按原文展示，不要自行翻译、二次检测或覆盖回复文本。
- 语言策略、置信度、英文互动梗条件均为后端内部决策，前端不得将其展示为系统提示或用户状态。

## 2026-07-15：SC 进入直播间公共历史

- `danmaku_realtime.data.type` 现在可能为 `sc`。收到后应立即以 SC 样式展示原文，不必等待 `ai_reply`。
- 新连接收到的 `history_batch.messages` 也可能包含 `type="sc"`，其 `danmakuID` 等于 `sc_id`。
- 使用 `danmakuID` 去重，避免页面已有实时 SC 后又因本地状态恢复重复插入。
- SC 是否排队、处理中、已回复或失败仍以 `sc_status` 为准；不要把 `danmaku_realtime` 当作“已回复”。
- `history_batch` 是当前直播服务进程的有界历史，不应作为永久订单或付费记录；当前账号自己的业务记录继续通过认证 `GET /sc` 获取。

## P24 主播情景记忆（追加字段）

认证接口 `GET /auth/profile/memory` 与 `GET /auth/profile/memory/export` 新增可选字段 `episodic_memories`。这是账号本人可见的下播情景记忆列表，不需要新增 WebSocket 事件。

```json
{
  "episodic_memories": [
    {
      "memory_id": "string",
      "stream_session_id": "string",
      "event_type": "personal_disclosure | affection_or_support | shared_joke_or_callback | promise_or_open_thread | sc_highlight | boundary_incident | room_incident | activity_milestone",
      "topic": "string",
      "summary": "string",
      "why_notable": "string",
      "emotional_mark": "string",
      "follow_up_hint": "string",
      "salience": 0.82,
      "occurred_at": "ISO-8601",
      "created_at": "ISO-8601",
      "expires_at": "ISO-8601"
    }
  ]
}
```

前端可以在记忆管理页面展示、导出和删除这些条目。字段是追加兼容的，旧客户端可以忽略。服务端不会通过 WebSocket 广播账号情景记忆，也不会返回候选 ID、账号 ID、原始弹幕、LLM 评分或安全审计字段。

管理员诊断接口 `/memory/episodic/stats` 仅供后端运维使用，不属于普通前端用户契约；它只返回低基数任务/候选/记忆数量，不返回账号、昵称、候选 ID 或原文。
