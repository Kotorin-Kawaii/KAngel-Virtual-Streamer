"""人格事件的无副作用确定性归约器。"""

from .dynamics import DynamicsContext, PersonaDynamicsTuning, clamp, toward
from .events import (
    AudienceAtmosphereTickEvent,
    DanmakuReceivedEvent,
    GiftReceivedEvent,
    ModerationActionEvent,
    PersonaEvent,
    SemanticImpactAnalyzedEvent,
    SilenceTickEvent,
    StreamLifecycleEvent,
)
from .mutations import PersonaMutation
from .state import EmotionDelta, InternalPersonaState, InternalStateDelta, PersonaState


class PersonaEventReducer:
    """模型解释语义，归约器只计算有界状态变化。"""

    def __init__(
        self,
        baseline: PersonaState | None = None,
        tuning: PersonaDynamicsTuning | None = None,
    ):
        self.baseline = baseline or PersonaState()
        self.tuning = tuning or PersonaDynamicsTuning()

    def reduce(
        self,
        event: PersonaEvent,
        state: PersonaState,
        internal_state: InternalPersonaState,
    ) -> PersonaMutation:
        if isinstance(event, SemanticImpactAnalyzedEvent):
            return PersonaMutation(
                emotion_delta=event.raw_delta,
                internal_delta=event.internal_delta,
                dynamics_context=event.dynamics_context,
                reason="模型完成语义理解，后端提交确定性动力学变化",
            )
        if isinstance(event, DanmakuReceivedEvent):
            return self._reduce_danmaku(event)
        if isinstance(event, AudienceAtmosphereTickEvent):
            return self._reduce_atmosphere(event)
        if isinstance(event, GiftReceivedEvent):
            return self._reduce_gift(event)
        if isinstance(event, ModerationActionEvent):
            return self._reduce_moderation(event)
        if isinstance(event, SilenceTickEvent):
            return self._reduce_silence(event, state, internal_state)
        if isinstance(event, StreamLifecycleEvent):
            return self._reduce_lifecycle(event)
        return PersonaMutation(reason="未知事件，不改变状态")

    def _reduce_danmaku(self, event: DanmakuReceivedEvent) -> PersonaMutation:
        # 原始弹幕只更新 pipeline 的聚合信号；被选中弹幕由语义事件作用一次。
        return PersonaMutation(reason="原始弹幕仅进入直播间气氛聚合，不直接修改人格")

    def _reduce_atmosphere(self, event: AudienceAtmosphereTickEvent) -> PersonaMutation:
        load = min(max(event.danmaku_rate, 0) / 30.0, 1.0)
        sentiment = clamp(event.audience_sentiment, -1.0, 1.0)
        return PersonaMutation(
            emotion_delta=EmotionDelta(
                mood=sentiment * 0.002,
                stress=load * 0.0035 + max(0.0, -sentiment) * 0.001,
                darkness=max(0.0, -sentiment) * 0.001,
            ),
            internal_delta=InternalStateDelta(
                arousal=load * 0.005,
                fatigue=load * 0.0025,
            ),
            reason="周期性直播间负载与总体情绪影响",
        )

    def _reduce_gift(self, event: GiftReceivedEvent) -> PersonaMutation:
        strength = min(max(event.value, 0.0) / 1000.0, 1.0)
        return PersonaMutation(
            emotion_delta=EmotionDelta(
                mood=0.008 + strength * 0.025,
                stress=-0.004 + strength * 0.006,
                darkness=-0.003,
            ),
            internal_delta=InternalStateDelta(
                arousal=0.012 + strength * 0.025,
                fatigue=0.002,
                attachment=0.006 + strength * 0.012,
                confidence=0.008 + strength * 0.015,
            ),
            reason="礼物事件预留归约规则",
        )

    def _reduce_moderation(self, event: ModerationActionEvent) -> PersonaMutation:
        severity = clamp(event.severity, 0.0, 1.0)
        return PersonaMutation(
            emotion_delta=EmotionDelta(
                mood=-0.004 * severity,
                stress=0.012 * severity,
                darkness=0.006 * severity,
            ),
            internal_delta=InternalStateDelta(
                arousal=0.010 * severity,
                fatigue=0.004 * severity,
                attachment=-0.003 * severity,
                confidence=-0.004 * severity,
            ),
            reason="房管事件预留归约规则",
        )

    def _reduce_silence(
        self,
        event: SilenceTickEvent,
        state: PersonaState,
        internal_state: InternalPersonaState,
    ) -> PersonaMutation:
        if event.seconds_since_activity < self.tuning.silence_min_activity_seconds:
            return PersonaMutation(reason="静默时间不足，不更新状态")
        factor = min(
            max(event.seconds_since_activity / self.tuning.silence_factor_reference_seconds, 1.0),
            self.tuning.silence_factor_max,
        )
        mood_delta = (self.baseline.mood - state.mood) * self.tuning.silence_recovery_mood * factor
        stress_delta = (self.baseline.stress - state.stress) * self.tuning.silence_recovery_stress * factor
        darkness_delta = (self.baseline.darkness - state.darkness) * self.tuning.silence_recovery_darkness * factor
        if event.seconds_since_activity >= self.tuning.silence_cold_room_seconds:
            mood_delta += self.tuning.silence_cold_room_mood_delta
            stress_delta += self.tuning.silence_cold_room_stress_delta
        return PersonaMutation(
            emotion_delta=EmotionDelta(
                mood=clamp(mood_delta, -self.tuning.silence_max_delta_mood, self.tuning.silence_max_delta_mood),
                stress=clamp(stress_delta, -self.tuning.silence_max_delta_stress, self.tuning.silence_max_delta_stress),
                darkness=clamp(darkness_delta, -self.tuning.silence_max_delta_darkness, self.tuning.silence_max_delta_darkness),
            ),
            internal_delta=InternalStateDelta(
                arousal=toward(internal_state.arousal, 0.35, 0.012 * factor),
                fatigue=toward(internal_state.fatigue, 0.2, 0.010 * factor),
                attachment=-0.0005 if event.seconds_since_activity >= self.tuning.silence_cold_room_seconds else 0.0,
                confidence=toward(internal_state.confidence, 0.65, 0.003 * factor),
            ),
            dynamics_context=DynamicsContext(
                source="silence", silence_seconds=event.seconds_since_activity,
            ),
            reason="直播间静默时执行自然恢复与轻微冷场反应",
        )

    def _reduce_lifecycle(self, event: StreamLifecycleEvent) -> PersonaMutation:
        phase = event.phase.casefold()
        if phase in {"started", "start", "opening"}:
            return PersonaMutation(
                emotion_delta=EmotionDelta(mood=0.006, stress=0.004, darkness=0.0),
                internal_delta=InternalStateDelta(arousal=0.025, confidence=0.006),
                reason="直播开始",
            )
        if phase in {"ended", "end", "stopped"}:
            return PersonaMutation(
                emotion_delta=EmotionDelta(mood=0.0, stress=-0.008, darkness=0.0),
                internal_delta=InternalStateDelta(arousal=-0.018, fatigue=0.008),
                reason="直播结束",
            )
        return PersonaMutation(reason=f"未配置状态变化的直播阶段: {event.phase}")
