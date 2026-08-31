"""由稳定直播事实派生的主播待机外显状态，不调用 AI。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class IdleState:
    idle_state: str
    idle_text: str
    frontend_animation: str
    priority: int
    background_music_hint: Optional[str] = None
    reason: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IdleStateResolver:
    """将主题、活动和人格读模型映射为唯一的前端待机状态。"""

    def resolve(
        self,
        *,
        is_live: bool,
        special_date_theme: Optional[dict],
        special_idle_state_hint: Optional[str],
        daily_theme_name: str,
        current_activity: Optional[dict],
        persona_state: Any,
        internal_state: Any,
        audience_sentiment: float = 0.0,
    ) -> IdleState:
        if not is_live:
            return IdleState(
                "offline", "当前未开播。", "idle_offline", 0, reason="offline"
            )

        fatigue = float(getattr(internal_state, "fatigue", 0.0))
        stress = float(getattr(persona_state, "stress", 0.0))
        darkness = float(getattr(persona_state, "darkness", 0.0))
        arousal = float(getattr(internal_state, "arousal", 0.0))
        attachment = float(getattr(internal_state, "attachment", 0.0))

        # 特殊日期只能在显式配置待机 ID 时拥有最高演出优先级。
        if special_date_theme and special_idle_state_hint:
            title = special_date_theme.get("title") or special_date_theme.get("name")
            return self._with_modifier(IdleState(
                special_idle_state_hint,
                f"{title}，正在和大家一起直播。",
                special_idle_state_hint,
                100,
                reason="special_date",
            ), fatigue, stress)

        # 当前活动是持久化事实，优先于会频繁波动的人格读数。
        if current_activity:
            category = str(current_activity.get("category", "chat"))
            activity_name = str(current_activity.get("display_name", daily_theme_name))
            object_name = str(current_activity.get("object_name", ""))
            state, animation = {
                "game": ("gaming", "idle_gaming"),
                "music": ("music_chat", "idle_music"),
                "variety": ("variety", "idle_variety"),
            }.get(category, ("chatting", "idle_chatting"))
            detail = f"{activity_name}：{object_name}" if object_name else activity_name
            return self._with_modifier(IdleState(
                state, f"正在{detail}，偶尔瞄一眼弹幕。", animation, 80,
                reason="current_activity",
            ), fatigue, stress)

        if darkness >= 0.72:
            return IdleState("dark", "灯光有点暗，安静地看着弹幕。", "idle_dark", 70, reason="darkness")
        if stress >= 0.72:
            return IdleState("stressed", "有点焦躁地刷着消息。", "idle_stressed", 70, reason="stress")
        if fatigue >= 0.72:
            return IdleState("tired", "今天有点困，趴在桌上看弹幕。", "idle_tired", 70, reason="fatigue")
        if arousal >= 0.78:
            return IdleState("excited", "精神满满地等着下一条弹幕。", "idle_excited", 70, reason="arousal")
        if attachment >= 0.78 or audience_sentiment >= 0.65:
            return IdleState("chatty", "一直盯着弹幕，想和大家多聊几句。", "idle_chatty", 70, reason="attachment")
        return IdleState("chatting", "轻松杂谈中，等着大家开口。", "idle_chatting", 10, reason="default")

    @staticmethod
    def _with_modifier(state: IdleState, fatigue: float, stress: float) -> IdleState:
        if fatigue >= 0.72:
            return IdleState(**{**state.to_dict(), "idle_text": f"{state.idle_text} 虽然有点困，但还是很努力。"})
        if stress >= 0.72:
            return IdleState(**{**state.to_dict(), "idle_text": f"{state.idle_text} 情绪有点绷紧。"})
        return state
