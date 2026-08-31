"""P22 确定性回复规划器：排序事实，不生成内容、不调用 AI。"""

from __future__ import annotations

from kangel.persona.domain.intent import InteractionMode, PrimaryIntent, ReplyPlan


class ResponsePlanner:
    _COMFORT_TERMS = ("难过", "好累", "累", "伤心", "焦虑", "安慰", "抱抱", "不开心")

    def plan(
        self, *, message: str, is_sc: bool, conversation_context: dict | None,
        activity: dict | None, internal_state: dict | None,
        language_reliable: bool, requires_boundary: bool = False,
    ) -> ReplyPlan:
        """严格按安全/SC/直接语义/同用户承接/活动背景排序。"""
        text = (message or "").strip()
        energy = self._energy(internal_state)
        activity_anchor = self._activity_anchor(text, activity)
        if requires_boundary:
            return ReplyPlan(InteractionMode.BOUNDARY_SET, PrimaryIntent.SET_BOUNDARY,
                energy, "current_message", "set_boundary")
        if is_sc:
            return ReplyPlan(InteractionMode.RECEIVE_SC, PrimaryIntent.ANSWER,
                energy, "current_message", "receive_sc", activity_anchor=activity_anchor)
        if language_reliable and conversation_context and (
            conversation_context.get("depends_on_previous")
            and conversation_context.get("same_verified_viewer")
        ):
            callback = str(conversation_context.get("resolved_reference") or "")[:80]
            return ReplyPlan(InteractionMode.FOLLOW_UP, PrimaryIntent.CONTINUE_CALLBACK,
                energy, "current_message", "continue_verified_callback",
                next_beat_hint="wait_for_current_reply", callback_fact=callback,
                allow_light_follow_up=True, activity_anchor=activity_anchor)
        if any(term in text for term in self._COMFORT_TERMS):
            return ReplyPlan(InteractionMode.COMFORT, PrimaryIntent.HOLD_EMOTION,
                energy, "current_message", "hold_emotion", allow_light_follow_up=True)
        if activity_anchor:
            return ReplyPlan(InteractionMode.ACTIVITY_COMMENTARY,
                PrimaryIntent.ADVANCE_ACTIVITY, energy, "current_message",
                "answer_activity_question", activity_anchor=activity_anchor)
        return ReplyPlan(InteractionMode.ANSWER, PrimaryIntent.ANSWER, energy,
            "current_message", "answer_current_message")

    @staticmethod
    def _energy(internal_state: dict | None) -> float:
        state = internal_state or {}
        arousal = float(state.get("arousal", .5))
        fatigue = float(state.get("fatigue", .2))
        return round(max(.15, min(.9, arousal * (1 - fatigue * .45))), 3)

    @staticmethod
    def _activity_anchor(message: str, activity: dict | None) -> str:
        if not activity:
            return ""
        object_name = str(activity.get("object_name", "")).strip()
        category = str(activity.get("category", "")).strip()
        compact = "".join(message.casefold().split())
        if object_name and "".join(object_name.casefold().split()) in compact:
            return object_name[:80]
        if category == "game" and any(word in compact for word in ("玩什么", "这关", "游戏")):
            return object_name[:80]
        return ""


response_planner = ResponsePlanner()
