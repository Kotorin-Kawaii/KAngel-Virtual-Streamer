"""P22 场次级心智状态：只保存可审计的短时互动意图。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum


class InteractionMode(str, Enum):
    ANSWER = "answer"
    COMFORT = "comfort"
    PLAYFUL_TEASE = "playful_tease"
    RECEIVE_SC = "receive_sc"
    FOLLOW_UP = "follow_up"
    ACTIVITY_COMMENTARY = "activity_commentary"
    BOUNDARY_SET = "boundary_set"


class PrimaryIntent(str, Enum):
    ANSWER = "answer"
    HOLD_EMOTION = "hold_emotion"
    CONTINUE_CALLBACK = "continue_callback"
    INVITE_PARTICIPATION = "invite_participation"
    ADVANCE_ACTIVITY = "advance_activity"
    SET_BOUNDARY = "set_boundary"
    NATURAL_CLOSE = "natural_close"


@dataclass(frozen=True)
class StreamerIntentState:
    """不含观众标识或原始弹幕的、本场短时可恢复状态。"""

    stream_session_id: str
    interaction_mode: InteractionMode
    primary_intent: PrimaryIntent
    energy_level: float
    attention_target: str
    current_beat: str
    next_beat_hint: str
    last_callback: str
    updated_at: str
    expires_at: str
    version: int

    def to_dict(self) -> dict:
        data = asdict(self)
        data["interaction_mode"] = self.interaction_mode.value
        data["primary_intent"] = self.primary_intent.value
        return data


@dataclass(frozen=True)
class ReplyPlan:
    """一次回复的压缩表达建议；不复制记忆、QA 或原始弹幕。"""

    interaction_mode: InteractionMode
    primary_intent: PrimaryIntent
    energy_level: float
    attention_target: str
    current_beat: str
    next_beat_hint: str = ""
    callback_fact: str = ""
    allow_light_follow_up: bool = False
    activity_anchor: str = ""

    def to_intent_update(self) -> dict:
        return {
            "interaction_mode": self.interaction_mode,
            "primary_intent": self.primary_intent,
            "energy_level": self.energy_level,
            "attention_target": self.attention_target,
            "current_beat": self.current_beat,
            "next_beat_hint": self.next_beat_hint,
            "last_callback": self.callback_fact,
        }
