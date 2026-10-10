"""单场直播的舞台即时状态模型。

P32-A 只提供一个可持久化、可恢复且不参与正式回复决策的 Shadow
状态层。这里的对象刻意不包含弹幕正文、观众身份或长期记忆；SpeechThread
的 topic 是后续阶段生成的主播话题摘要，而不是用户消息的存储位置。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from enum import Enum
from typing import Any
from uuid import UUID


class LiveActType(str, Enum):
    """P32-A 唯一允许的舞台建议。"""

    ANSWER_DANMAKU = "ANSWER_DANMAKU"
    CONTINUE_THREAD = "CONTINUE_THREAD"
    IGNORE_AFTER_READ = "IGNORE_AFTER_READ"
    SELF_TALK = "SELF_TALK"


class SpeechThreadStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


SPEECH_THREAD_SOURCES = frozenset({
    "director", "activity", "self_talk", "reply", "manual_test",
})
LIVE_ACT_REASON_CODES = frozenset({
    "SC_PRIORITY", "DANMAKU_AVAILABLE", "DANMAKU_INTERRUPT_ALLOWED",
    "THREAD_ACTIVE", "THREAD_RESUMABLE", "ROOM_QUIET", "BUSY",
    "NOT_LIVE", "NO_ACTION", "HIGH_CHAT_PRESSURE",
    "RECENT_REPLY_SATURATION", "LOW_RESPONSE_NECESSITY",
    "FORCED_SC", "FORCED_FOLLOWUP", "FORCED_BOUNDARY",
    "FALLBACK_POLICY_ERROR", "ACTIVE_DISABLED",
})

ACTIVE_P32B_ACTIONS = frozenset({
    LiveActType.ANSWER_DANMAKU,
    LiveActType.IGNORE_AFTER_READ,
})


def _bounded_text(value: Any, *, field: str, maximum: int, allow_empty: bool = True) -> str:
    text = str(value or "").strip()
    if not allow_empty and not text:
        raise ValueError(f"{field} 不能为空")
    if len(text) > maximum:
        raise ValueError(f"{field} 长度不能超过 {maximum}")
    # 原始控制字符没有出现在任何正常话题摘要中，也避免把换行/日志内容
    # 作为状态的一部分带入管理端响应。
    if any(ord(char) < 32 and char not in "\t" for char in text):
        raise ValueError(f"{field} 包含非法控制字符")
    return text


@dataclass(frozen=True)
class SpeechThread:
    """主播连续话题的最小摘要；不携带用户身份或原始消息。"""

    thread_id: str
    topic: str
    unfinished_point: str | None
    status: SpeechThreadStatus
    source: str
    importance: float
    started_at: str
    last_touched_at: str
    version: int = 1
    expires_at: str | None = None
    opened_by_speech_id: str | None = None
    last_speech_id: str | None = None
    mainline_beat_version: int | None = None
    activity_version: int | None = None
    activity_id: str | None = None
    activity_category: str | None = None
    beat_id: str | None = None
    beat_kind: str | None = None
    plan_profile_id: str | None = None
    allow_same_category: bool = False
    interrupt_count: int = 0
    resume_count: int = 0
    step_count: int = 1
    seed_id: str | None = None
    interruption_id: str | None = None
    interruption_source: str | None = None
    interruption_visible: bool = False
    # Only validated, delivered stage speech; never viewer text or model thoughts.
    last_public_text: str | None = None

    def __post_init__(self) -> None:
        normalized_id = str(self.thread_id or "").strip()
        try:
            UUID(normalized_id)
        except (ValueError, AttributeError):
            raise ValueError("thread_id 必须是 UUID") from None
        object.__setattr__(self, "thread_id", normalized_id)
        object.__setattr__(
            self, "topic", _bounded_text(self.topic, field="topic", maximum=120, allow_empty=False)
        )
        if self.unfinished_point is not None:
            object.__setattr__(
                self,
                "unfinished_point",
                _bounded_text(self.unfinished_point, field="unfinished_point", maximum=160),
            )
        if self.last_public_text is not None:
            object.__setattr__(self, "last_public_text", _bounded_text(
                self.last_public_text, field="last_public_text", maximum=160, allow_empty=False))
        if not isinstance(self.status, SpeechThreadStatus):
            try:
                object.__setattr__(self, "status", SpeechThreadStatus(str(self.status)))
            except ValueError:
                raise ValueError("SpeechThread.status 无效") from None
        source = str(self.source or "").strip()
        if source not in SPEECH_THREAD_SOURCES:
            raise ValueError("SpeechThread.source 不在允许列表")
        object.__setattr__(self, "source", source)
        importance = float(self.importance)
        if not 0.0 <= importance <= 1.0:
            raise ValueError("SpeechThread.importance 必须在 0..1")
        object.__setattr__(self, "importance", importance)
        for field in ("version", "interrupt_count", "resume_count", "step_count"):
            value = getattr(self, field)
            if type(value) is not int or value < (1 if field in {"version", "step_count"} else 0):
                raise ValueError(f"SpeechThread.{field} 无效")
        for field in ("expires_at", "opened_by_speech_id", "last_speech_id",
                      "activity_id", "activity_category", "beat_id", "beat_kind",
                      "plan_profile_id", "seed_id", "interruption_id", "interruption_source"):
            value = getattr(self, field)
            if value is not None:
                _bounded_text(value, field=field, maximum=160)
        for field in ("started_at", "last_touched_at"):
            value = str(getattr(self, field) or "").strip()
            if not value:
                raise ValueError(f"SpeechThread.{field} 不能为空")
            object.__setattr__(self, field, value)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, value: Any) -> "SpeechThread":
        if not isinstance(value, dict):
            raise ValueError("SpeechThread 必须是对象")
        allowed = {field.name for field in fields(cls)}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"SpeechThread 包含未知字段: {', '.join(sorted(unknown))}")
        data = dict(value)
        if isinstance(data.get("unfinished_point"), str):
            data["unfinished_point"] = data["unfinished_point"][:160]
        return cls(**data)


@dataclass(frozen=True)
class LiveActSnapshot:
    action: LiveActType
    reason_code: str
    occurred_at: str
    source: str = "shadow_policy"

    def __post_init__(self) -> None:
        if not isinstance(self.action, LiveActType):
            try:
                object.__setattr__(self, "action", LiveActType(str(self.action)))
            except ValueError:
                raise ValueError("LiveActSnapshot.action 无效") from None
        reason = str(self.reason_code or "").strip()
        if reason not in LIVE_ACT_REASON_CODES:
            raise ValueError("LiveActSnapshot.reason_code 不在允许列表")
        object.__setattr__(self, "reason_code", reason)
        occurred_at = str(self.occurred_at or "").strip()
        if not occurred_at:
            raise ValueError("LiveActSnapshot.occurred_at 不能为空")
        object.__setattr__(self, "occurred_at", occurred_at)
        if str(self.source) != "shadow_policy":
            raise ValueError("LiveActSnapshot.source 无效")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        return data

    @classmethod
    def from_dict(cls, value: Any) -> "LiveActSnapshot":
        if not isinstance(value, dict):
            raise ValueError("LiveActSnapshot 必须是对象")
        allowed = {"action", "reason_code", "occurred_at", "source"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(
                f"LiveActSnapshot 包含未知字段: {', '.join(sorted(unknown))}"
            )
        return cls(
            action=value.get("action"),
            reason_code=value.get("reason_code"),
            occurred_at=value.get("occurred_at"),
            source=value.get("source", "shadow_policy"),
        )


@dataclass(frozen=True)
class LiveActDecision:
    """一次 Shadow Policy 结果；不包含弹幕 ID。"""

    action: LiveActType
    reason_code: str
    stream_session_id: str
    presence_version: int
    mainline_beat_version: int | None
    activity_version: int | None
    source: str = "shadow_policy"
    schema_version: str = "live-act-decision-v1"
    target_thread_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, LiveActType):
            try:
                object.__setattr__(self, "action", LiveActType(str(self.action)))
            except ValueError:
                raise ValueError("LiveActDecision.action 无效") from None
        reason = str(self.reason_code or "").strip()
        if reason not in LIVE_ACT_REASON_CODES:
            raise ValueError("LiveActDecision.reason_code 不在允许列表")
        object.__setattr__(self, "reason_code", reason)
        session_id = str(self.stream_session_id or "").strip()
        if not session_id:
            raise ValueError("stream_session_id 不能为空")
        object.__setattr__(self, "stream_session_id", session_id)
        if int(self.presence_version) < 1:
            raise ValueError("presence_version 必须 >= 1")
        object.__setattr__(self, "presence_version", int(self.presence_version))
        for field in ("mainline_beat_version", "activity_version"):
            raw = getattr(self, field)
            if raw is not None and int(raw) < 1:
                raise ValueError(f"{field} 必须为正整数")
            if raw is not None:
                object.__setattr__(self, field, int(raw))
        if self.source not in {"shadow_policy", "active_policy"}:
            raise ValueError("LiveActDecision.source 无效")
        if self.schema_version != "live-act-decision-v1":
            raise ValueError("LiveActDecision.schema_version 无效")
        if self.target_thread_id is not None:
            try:
                UUID(str(self.target_thread_id))
            except (ValueError, AttributeError):
                raise ValueError("target_thread_id 必须是 UUID") from None
            object.__setattr__(self, "target_thread_id", str(self.target_thread_id))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        return data


@dataclass(frozen=True)
class LivePresenceState:
    stream_session_id: str
    version: int
    active_thread: SpeechThread | None
    suspended_threads: tuple[SpeechThread, ...]
    last_act: LiveActSnapshot | None
    last_speech_at: str | None
    mainline_beat_version: int | None
    activity_version: int | None
    created_at: str
    updated_at: str
    status: str = "active"
    schema_version: str = "live-presence-state-v2"
    pending_speech_attempt: dict[str, Any] | None = None

    MAX_SUSPENDED_THREADS = 3

    def __post_init__(self) -> None:
        session_id = str(self.stream_session_id or "").strip()
        if not session_id:
            raise ValueError("stream_session_id 不能为空")
        object.__setattr__(self, "stream_session_id", session_id)
        version = int(self.version)
        if version < 1:
            raise ValueError("presence version 必须 >= 1")
        object.__setattr__(self, "version", version)
        suspended = tuple(self.suspended_threads or ())
        if len(suspended) > self.MAX_SUSPENDED_THREADS:
            raise ValueError("suspended_threads 超过上限")
        if any(not isinstance(item, SpeechThread) for item in suspended):
            raise ValueError("suspended_threads 包含非法条目")
        if self.active_thread is not None and not isinstance(self.active_thread, SpeechThread):
            raise ValueError("active_thread 类型无效")
        if self.active_thread is not None and self.active_thread.status is not SpeechThreadStatus.ACTIVE:
            raise ValueError("active_thread.status 必须为 active")
        object.__setattr__(self, "suspended_threads", suspended)
        if any(item.status is not SpeechThreadStatus.SUSPENDED for item in suspended):
            raise ValueError("suspended_threads.status 必须为 suspended")
        thread_ids = [item.thread_id for item in suspended]
        if self.active_thread is not None:
            thread_ids.append(self.active_thread.thread_id)
        if len(thread_ids) != len(set(thread_ids)):
            raise ValueError("LivePresenceState 不允许重复 thread_id")
        if self.last_act is not None and not isinstance(self.last_act, LiveActSnapshot):
            raise ValueError("last_act 类型无效")
        for field in ("mainline_beat_version", "activity_version"):
            raw = getattr(self, field)
            if raw is not None and int(raw) < 1:
                raise ValueError(f"{field} 必须为正整数")
            if raw is not None:
                object.__setattr__(self, field, int(raw))
        status = str(self.status or "active")
        if status not in {"active", "completed", "archived"}:
            raise ValueError("LivePresenceState.status 无效")
        object.__setattr__(self, "status", status)
        if self.schema_version not in {"live-presence-state-v1", "live-presence-state-v2"}:
            raise ValueError("LivePresenceState.schema_version 无效")
        if self.pending_speech_attempt is not None:
            attempt = self.pending_speech_attempt
            if not isinstance(attempt, dict) or set(attempt) != {
                "attempt_id", "operation", "thread_id", "thread_version", "started_at"
            }:
                raise ValueError("invalid speech attempt")
            UUID(attempt["attempt_id"])
            if attempt["operation"] not in {"open", "advance", "resume"}:
                raise ValueError("invalid speech operation")
            if attempt["thread_id"] is not None:
                UUID(attempt["thread_id"])
            if type(attempt["thread_version"]) is not int or attempt["thread_version"] < 0:
                raise ValueError("invalid thread version")
            _bounded_text(attempt["started_at"], field="started_at", maximum=40)
        for field in ("created_at", "updated_at"):
            value = str(getattr(self, field) or "").strip()
            if not value:
                raise ValueError(f"{field} 不能为空")
            object.__setattr__(self, field, value)
        if self.last_speech_at is not None:
            object.__setattr__(self, "last_speech_at", str(self.last_speech_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stream_session_id": self.stream_session_id,
            "version": self.version,
            "active_thread": self.active_thread.to_dict() if self.active_thread else None,
            "suspended_threads": [item.to_dict() for item in self.suspended_threads],
            "last_act": self.last_act.to_dict() if self.last_act else None,
            "last_speech_at": self.last_speech_at,
            "mainline_beat_version": self.mainline_beat_version,
            "activity_version": self.activity_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "pending_speech_attempt": self.pending_speech_attempt,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "LivePresenceState":
        if not isinstance(value, dict):
            raise ValueError("LivePresenceState 必须是对象")
        if value.get("schema_version") not in {"live-presence-state-v1", "live-presence-state-v2"}:
            raise ValueError("未知 LivePresenceState schema")
        allowed = {
            "schema_version", "stream_session_id", "version", "active_thread",
            "suspended_threads", "last_act", "last_speech_at",
            "mainline_beat_version", "activity_version", "created_at",
            "updated_at", "status", "pending_speech_attempt",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"LivePresenceState 包含未知字段: {', '.join(sorted(unknown))}")
        raw_suspended = value.get("suspended_threads", [])
        if not isinstance(raw_suspended, list):
            raise ValueError("suspended_threads 必须是数组")
        return cls(
            stream_session_id=value.get("stream_session_id"),
            version=value.get("version"),
            pending_speech_attempt=value.get("pending_speech_attempt"),
            active_thread=(
                SpeechThread.from_dict(value["active_thread"])
                if value.get("active_thread") is not None else None
            ),
            suspended_threads=tuple(SpeechThread.from_dict(item) for item in raw_suspended),
            last_act=(
                LiveActSnapshot.from_dict(value["last_act"])
                if value.get("last_act") is not None else None
            ),
            last_speech_at=value.get("last_speech_at"),
            mainline_beat_version=value.get("mainline_beat_version"),
            activity_version=value.get("activity_version"),
            created_at=value.get("created_at"),
            updated_at=value.get("updated_at"),
            status=value.get("status", "active"),
            schema_version=value.get("schema_version"),
        )


@dataclass(frozen=True)
class LivePresenceContext:
    """Shadow Policy 的显式输入；禁止用无限扩展的 dict 替代。"""

    stream_session_id: str
    is_live: bool
    has_selected_danmaku: bool
    has_active_thread: bool
    has_suspended_thread: bool
    seconds_since_last_speech: float | None
    danmaku_rate_60s: int
    silence_seconds: float
    mainline_beat_version: int | None
    activity_version: int | None
    ai_waiting: bool
    sc_pending: bool
    # P32-B 仅使用已经存在的聚合信号；这些字段不携带正文、昵称或身份。
    recent_reply_count: int = 0
    seconds_since_last_reply: float | None = None
    is_sc_danmaku: bool = False
    is_verified_follow_up: bool = False
    requires_boundary: bool = False
    # 由调用方对 session + danmaku id 生成的低基数稳定 bucket。
    # 它只用于通过响应必要性检查后的保守 gate，不会持久化。
    response_gate_bucket: int | None = None

    def __post_init__(self) -> None:
        if not str(self.stream_session_id or "").strip():
            raise ValueError("stream_session_id 不能为空")
        object.__setattr__(self, "stream_session_id", str(self.stream_session_id).strip())
        object.__setattr__(self, "danmaku_rate_60s", max(0, int(self.danmaku_rate_60s)))
        object.__setattr__(self, "silence_seconds", max(0.0, float(self.silence_seconds)))
        if self.seconds_since_last_speech is not None:
            object.__setattr__(
                self,
                "seconds_since_last_speech",
                max(0.0, float(self.seconds_since_last_speech)),
            )
        object.__setattr__(self, "recent_reply_count", max(0, int(self.recent_reply_count)))
        if self.seconds_since_last_reply is not None:
            object.__setattr__(
                self,
                "seconds_since_last_reply",
                max(0.0, float(self.seconds_since_last_reply)),
            )
        for field in ("is_sc_danmaku", "is_verified_follow_up", "requires_boundary"):
            object.__setattr__(self, field, bool(getattr(self, field)))
        if self.response_gate_bucket is not None:
            bucket = int(self.response_gate_bucket)
            if not 0 <= bucket <= 255:
                raise ValueError("response_gate_bucket 必须在 0..255")
            object.__setattr__(self, "response_gate_bucket", bucket)
