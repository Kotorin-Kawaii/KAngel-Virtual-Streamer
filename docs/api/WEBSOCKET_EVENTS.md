# WebSocket 前端事件清单

对应后端枚举 [`src/kangel/transport/websocket/protocol.py`](../../src/kangel/transport/websocket/protocol.py)，更新于 2026-08-31。连接地址为 `/danmaku`；所有事件使用 UTF-8 JSON 文本帧。除 `history_batch`、`confirmation` 和 `error` 外，事件均使用顶层 `{ "type", "data" }` 包装。

这是当前版本前端可消费事件的完整清单。详细字段类型与 HTTP 契约见 [FRONTEND.md](FRONTEND.md)。未知 `type` 必须安全忽略并记录开发日志，不能因此断开连接。

| 事件 | 接收者 | 用途与前端处理 |
|---|---|---|
| `history_batch` | 新连接；仅有历史时 | 有界直播间展示历史，顶层为 `messages`/`count`；按 `danmakuID` 去重。`messages[].type` 可能为 `normal` 或 `sc`。|
| `danmaku_realtime` | 全房 | 新普通弹幕或已接受 SC 的公共展示，`data` 为弹幕对象；不能把 `type="sc"` 当成已回复。|
| `confirmation` | 发送该弹幕的连接 | 弹幕已接受的本地确认，含 `timestamp`/`danmaku_rate`，不含 `danmakuID`；不得据此再次插入聊天历史。|
| `danmaku_selected` | 触发当前选择循环的连接 | 某条弹幕被普通回复链选中；它未必来自当前连接，只可作可选展示。|
| `ai_reply` | 全房 | 主播回复；`data.reply` 是可展示的 `AIReply`。SC 回复另有 `data.source="sc"` 与 `sc_id`，按回复 ID 去重。|
| `sc_status` | 全房 | SC 的 `processing`、`replied` 或 `failed` 状态。`replied` 会携带 `reply`，用于断线后补齐；`failed` 不得渲染为成功。|
| `streamer_moderation` | 触发用户连接 | 主播管理状态；warning/timeout/admin_review 使用安全裁剪字段，前端只更新当前用户的提示和禁言倒计时。|
| `viewer_emote` | 全房 | 纯展示表情，按 `(viewer_id, client_event_id)` 去重；不写入弹幕历史，也不参与 AI/关系/记忆。|
| `mood_update` | 已订阅连接 | 主播三轴状态及展示行为；连接后有快照，随后周期更新。|
| `stream_metadata` | 已订阅连接 | 完整直播快照；连接后有快照，断线重连后以它恢复直播状态、主题、活动和待机状态。|
| `viewer_count_update` | 已订阅连接 | 在线人数及累计进出人数的增量；完整数值仍以 `stream_metadata` 为准。|
| `user_activity` | 已订阅连接 | 用户进出房间展示事件。`data.extra` 可能含隐私信息，公开前端必须忽略。|
| `stream_status` | 已订阅连接 | 排期开播/下播边界的状态快照；`is_live` 由后端排期决定，不代表进程是否运行。|
| `streamer_activity` | 已订阅连接 | 公开活动切换演出；按 `(stream_session_id, version)` 去重。先更新 `current`，再展示 `sentences`；漏事件后用 `stream_metadata.current_activity` 恢复。|
| `streamer_idle_state` | 已订阅连接 | 主播待机外显状态；按 `version` 去重。`idle_text` 仅作 UI 文案，不能伪装为主播回复。|
| `streamer_beat` | 已订阅连接 | 低优先级、可丢弃的主播微动作；按 `(stream_session_id, version)` 去重。断线或拥塞期间不补播，不能写入弹幕/SC/回复历史。|
| `rate_limited` | 受限连接 | 操作尚未完成或回复链未接纳。按 `data.scope` 冷却对应控件，按 `action` 决定是否等待关闭；不得自动重试风暴。|
| `error` | 发生错误的连接 | 协议或业务错误。可展示 `message`；对 `duplicate_danmaku`/`duplicate_emote` 不得生成新 ID 自动补发。|

## 断线恢复与关闭码

1. 重连后等待 `mood_update` 与 `stream_metadata` 快照；按需用认证 `GET /sc` 恢复当前账号的 SC 状态。
2. 不补造 `streamer_activity`、`streamer_idle_state`、`streamer_beat`、`viewer_emote` 或 `danmaku_selected`；它们是增量展示，不是可回放事实。
3. 失效令牌不会触发专用认证关闭码，而是降级为游客连接；`1009` 表示帧过大，修正负载后再连接；`1013` 表示容量/频率限制，使用带抖动退避。

## 客户端可发送的消息

普通弹幕必须包含 `nickname`、`message`、`danmakuID`，可选 `type` 和 `sender_level`。已登录连接会由服务端覆盖 `nickname`。观众表情使用独立 `{ "type": "viewer_emote", "emote_id", "client_event_id" }` 结构；完整字段约束见 [FRONTEND.md](FRONTEND.md)。
