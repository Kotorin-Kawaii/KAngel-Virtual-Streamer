"""人格领域事件。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from uuid import uuid4

from .dynamics import DynamicsContext
from .state import EmotionDelta, InternalStateDelta


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PersonaEventType(str, Enum):
    DANMAKU_RECEIVED = "danmaku_received"
    SEMANTIC_IMPACT_ANALYZED = "semantic_impact_analyzed"
    GIFT_RECEIVED = "gift_received"
    MODERATION_ACTION = "moderation_action"
    SILENCE_TICK = "silence_tick"
    STREAM_LIFECYCLE = "stream_lifecycle"
    AUDIENCE_ATMOSPHERE_TICK = "audience_atmosphere_tick"


@dataclass(frozen=True, kw_only=True)
class PersonaEvent:
    event_id: str = field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = field(default_factory=utc_now)
    source: str = "backend"
    source_event_id: Optional[str] = None
    platform_message_id: Optional[str] = None


@dataclass(frozen=True, kw_only=True)
class DanmakuReceivedEvent(PersonaEvent):
    nickname: str
    message: str
    sentiment: float = 0.0
    topics: tuple[str, ...] = ()
    danmaku_rate: int = 0
    event_type: PersonaEventType = field(default=PersonaEventType.DANMAKU_RECEIVED, init=False)


@dataclass(frozen=True, kw_only=True)
class SemanticImpactAnalyzedEvent(PersonaEvent):
    danmaku_id: str
    raw_delta: EmotionDelta
    internal_delta: InternalStateDelta
    dynamics_context: DynamicsContext
    event_type: PersonaEventType = field(default=PersonaEventType.SEMANTIC_IMPACT_ANALYZED, init=False)


@dataclass(frozen=True, kw_only=True)
class GiftReceivedEvent(PersonaEvent):
    nickname: str
    gift_name: str
    value: float = 0.0
    message: str = ""
    event_type: PersonaEventType = field(default=PersonaEventType.GIFT_RECEIVED, init=False)


@dataclass(frozen=True, kw_only=True)
class ModerationActionEvent(PersonaEvent):
    action: str
    target: str = ""
    reason: str = ""
    severity: float = 0.5
    event_type: PersonaEventType = field(default=PersonaEventType.MODERATION_ACTION, init=False)


@dataclass(frozen=True, kw_only=True)
class SilenceTickEvent(PersonaEvent):
    seconds_since_activity: float
    event_type: PersonaEventType = field(default=PersonaEventType.SILENCE_TICK, init=False)


@dataclass(frozen=True, kw_only=True)
class StreamLifecycleEvent(PersonaEvent):
    phase: str
    event_type: PersonaEventType = field(default=PersonaEventType.STREAM_LIFECYCLE, init=False)


@dataclass(frozen=True, kw_only=True)
class AudienceAtmosphereTickEvent(PersonaEvent):
    danmaku_rate: int = 0
    audience_sentiment: float = 0.0
    active_users: int = 0
    event_type: PersonaEventType = field(default=PersonaEventType.AUDIENCE_ATMOSPHERE_TICK, init=False)
