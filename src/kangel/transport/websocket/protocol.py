"""发送给前端的 WebSocket 事件类型唯一清单。"""

from enum import Enum


class WebSocketEventType(str, Enum):
    ERROR = "error"
    RATE_LIMITED = "rate_limited"
    CONFIRMATION = "confirmation"
    HISTORY_BATCH = "history_batch"
    DANMAKU_REALTIME = "danmaku_realtime"
    DANMAKU_SELECTED = "danmaku_selected"
    AI_REPLY = "ai_reply"
    VIEWER_EMOTE = "viewer_emote"
    MOOD_UPDATE = "mood_update"
    STREAM_METADATA = "stream_metadata"
    VIEWER_COUNT_UPDATE = "viewer_count_update"
    USER_ACTIVITY = "user_activity"
    STREAM_STATUS = "stream_status"
    STREAMER_ACTIVITY = "streamer_activity"
    STREAMER_BEAT = "streamer_beat"
    STREAM_MAINLINE_BEAT = "stream_mainline_beat"
    STREAMER_IDLE_STATE = "streamer_idle_state"
    SC_STATUS = "sc_status"
    STREAMER_MODERATION = "streamer_moderation"


__all__ = ["WebSocketEventType"]
