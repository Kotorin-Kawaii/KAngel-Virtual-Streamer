"""P32-A 确定性 Live Presence Shadow Policy。"""

from __future__ import annotations

from kangel.stream.domain.live_presence import (
    LIVE_ACT_REASON_CODES,
    LiveActDecision,
    LiveActType,
    LivePresenceContext,
)


class DeterministicLivePresencePolicy:
    """保守、无网络 IO 的舞台建议策略。

    返回 ``None`` 表示这次不产生 Shadow Decision。它和
    ``LiveActType.IGNORE_AFTER_READ`` 是两个不同概念：前者表示没有足够
    信号，后者表示未来正式 Presence 层可能会在已读后选择不接话。
    """

    # P32-B6 的正式忽略条件刻意很窄：它不是高压房间限流，而是在
    # 低强度房间里，主播刚刚回复过观众、当前又没有必须承接的交互时，
    # 允许极少数语义上可不回复的弹幕落到 IGNORE_AFTER_READ。
    #
    # 稳定 gate 只在通过资格检查之后起限流作用，不代替资格判断。
    # bucket 由调用方对 session + danmaku id 做单向摘要得到，本策略不读取
    # 弹幕正文、昵称或账号。1/8 是初始上限，不是对所有弹幕的忽略率。
    ACTIVE_LOW_ROOM_MAX_RATE = 3
    ACTIVE_RECENT_REPLY_COUNT = 1
    ACTIVE_REPLY_SATURATION_SECONDS = 60.0
    ACTIVE_IGNORE_GATE_MODULUS = 8

    def __init__(self, *, quiet_seconds: float = 180.0):
        self.quiet_seconds = max(1.0, float(quiet_seconds))

    def evaluate(self, context: LivePresenceContext) -> LiveActDecision | None:
        if not isinstance(context, LivePresenceContext):
            raise TypeError("LivePresencePolicy 需要 LivePresenceContext")
        if not context.is_live:
            return None

        if context.sc_pending:
            return self._decision(context, LiveActType.ANSWER_DANMAKU, "SC_PRIORITY")

        if context.has_selected_danmaku:
            reason = (
                "DANMAKU_INTERRUPT_ALLOWED"
                if context.has_active_thread
                else "DANMAKU_AVAILABLE"
            )
            return self._decision(context, LiveActType.ANSWER_DANMAKU, reason)

        if context.has_active_thread:
            return self._decision(context, LiveActType.CONTINUE_THREAD, "THREAD_ACTIVE")

        if context.has_suspended_thread:
            return self._decision(context, LiveActType.CONTINUE_THREAD, "THREAD_RESUMABLE")

        speech_silence = context.seconds_since_last_speech
        quiet_for = max(
            context.silence_seconds,
            speech_silence if speech_silence is not None else 0.0,
        )
        if (
            not context.ai_waiting
            and context.danmaku_rate_60s <= 0
            and quiet_for >= self.quiet_seconds
        ):
            return self._decision(context, LiveActType.SELF_TALK, "ROOM_QUIET")

        # BUSY is deliberately represented as no decision. The service records
        # the fixed reason in metrics, but no fake LiveAct is persisted.
        return None

    def evaluate_active(self, context: LivePresenceContext) -> LiveActDecision:
        """P32-B 正式决策：只允许 ANSWER 或 IGNORE_AFTER_READ。

        这条路径只接收已经被 Attention 选中的弹幕，且只使用窗口化速率、
        最近回复时间和调用方提供的强制回复标记。它不读取 Persona、关系、
        Prompt RAM 或长期记忆，也不执行任何网络 IO。
        """
        if not isinstance(context, LivePresenceContext):
            raise TypeError("LivePresencePolicy 需要 LivePresenceContext")
        if not context.is_live or not context.has_selected_danmaku:
            return self._active_decision(
                context, LiveActType.ANSWER_DANMAKU, "FALLBACK_POLICY_ERROR"
            )
        if context.is_sc_danmaku or context.sc_pending:
            return self._active_decision(context, LiveActType.ANSWER_DANMAKU, "FORCED_SC")
        if context.is_verified_follow_up:
            return self._active_decision(context, LiveActType.ANSWER_DANMAKU, "FORCED_FOLLOWUP")
        if context.requires_boundary:
            return self._active_decision(context, LiveActType.ANSWER_DANMAKU, "FORCED_BOUNDARY")
        if context.ai_waiting:
            return self._active_decision(context, LiveActType.ANSWER_DANMAKU, "DANMAKU_AVAILABLE")

        if self.ignore_after_read_eligible(context) and self.ignore_gate_passed(context):
            return self._active_decision(
                context, LiveActType.IGNORE_AFTER_READ, "LOW_RESPONSE_NECESSITY"
            )

        return self._active_decision(context, LiveActType.ANSWER_DANMAKU, "DANMAKU_AVAILABLE")

    def ignore_after_read_eligible(self, context: LivePresenceContext) -> bool:
        """Return whether deterministic facts allow a non-reply at all."""
        if not isinstance(context, LivePresenceContext):
            raise TypeError("LivePresencePolicy 需要 LivePresenceContext")
        recent_reply_seconds = context.seconds_since_last_reply
        return bool(
            context.is_live
            and context.has_selected_danmaku
            and not context.is_sc_danmaku
            and not context.sc_pending
            and not context.is_verified_follow_up
            and not context.requires_boundary
            and not context.ai_waiting
            and context.danmaku_rate_60s <= self.ACTIVE_LOW_ROOM_MAX_RATE
            and context.recent_reply_count >= self.ACTIVE_RECENT_REPLY_COUNT
            and recent_reply_seconds is not None
            and recent_reply_seconds <= self.ACTIVE_REPLY_SATURATION_SECONDS
            and not context.has_active_thread
            and not context.has_suspended_thread
        )

    def ignore_gate_passed(self, context: LivePresenceContext) -> bool:
        """Apply the stable gate only after eligibility has been established."""
        return bool(
            context.response_gate_bucket is not None
            and context.response_gate_bucket % self.ACTIVE_IGNORE_GATE_MODULUS == 0
        )

    @staticmethod
    def _decision(
        context: LivePresenceContext, action: LiveActType, reason_code: str
    ) -> LiveActDecision:
        if reason_code not in LIVE_ACT_REASON_CODES:
            raise ValueError("非法 LiveAct reason_code")
        return LiveActDecision(
            action=action,
            reason_code=reason_code,
            stream_session_id=context.stream_session_id,
            # The service replaces this with the state version before storing
            # the decision. A positive placeholder keeps the value model strict
            # for policy-only callers.
            presence_version=1,
            mainline_beat_version=context.mainline_beat_version,
            activity_version=context.activity_version,
        )

    @staticmethod
    def _active_decision(
        context: LivePresenceContext, action: LiveActType, reason_code: str
    ) -> LiveActDecision:
        if reason_code not in LIVE_ACT_REASON_CODES:
            raise ValueError("非法 LiveAct reason_code")
        return LiveActDecision(
            action=action,
            reason_code=reason_code,
            stream_session_id=context.stream_session_id,
            presence_version=1,
            mainline_beat_version=context.mainline_beat_version,
            activity_version=context.activity_version,
            source="active_policy",
        )


__all__ = ["DeterministicLivePresencePolicy"]
