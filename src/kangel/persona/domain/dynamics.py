"""
人格状态动力学模块。

把模型给出的语义影响值转换成更稳定的直播状态变化：带惯性、边界衰减、
自然恢复、重复话题降权和直播间气氛修正。
"""

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from .state import EmotionDelta, PersonaState


logger = logging.getLogger(__name__)


def clamp(value: float, minimum: float, maximum: float) -> float:
    """将数值限制在闭区间内。"""
    return max(minimum, min(maximum, value))


def toward(current: float, target: float, rate: float, limit: float = 0.05) -> float:
    """按比例向目标移动，并限制单步变化。"""
    return clamp((target - current) * rate, -limit, limit)


@dataclass
class DynamicsContext:
    """一次状态更新的直播间上下文。"""

    danmaku_rate: int = 0
    audience_sentiment: float = 0.0
    repeated_topic: bool = False
    active_users: int = 0
    total_danmaku: int = 0
    viewer_familiarity: float = 0.0
    viewer_affinity: float = 0.5
    viewer_trust: float = 0.5
    conversation_transition: str = "new"
    retrieved_personal_fragments: int = 0
    seconds_since_last_update: float = 0.0
    silence_seconds: float = 0.0
    source: str = "danmaku"


@dataclass(frozen=True)
class PersonaDynamicsTuning:
    """三轴与静默恢复的受限调参集；档案只在进程启动时选择。"""

    profile: str = "enhanced"
    max_step_mood: float = 0.08
    max_step_stress: float = 0.10
    max_step_darkness: float = 0.07
    recovery_rate_mood: float = 0.012
    recovery_rate_stress: float = 0.018
    recovery_rate_darkness: float = 0.010
    recovery_time_reference_seconds: float = 30.0
    recovery_time_factor_max: float = 3.0
    repeated_topic_multiplier: float = 0.65
    boundary_factor_min: float = 0.35
    mean_reversion_threshold: float = 0.42
    mean_reversion_strength: float = 0.25
    extreme_guard_mood_floor: float = 0.08
    extreme_guard_stress_ceiling: float = 0.92
    extreme_guard_darkness_ceiling: float = 0.92
    extreme_guard_recovery_multiplier: float = 1.08
    silence_min_activity_seconds: float = 30.0
    silence_factor_reference_seconds: float = 30.0
    silence_factor_max: float = 4.0
    silence_recovery_mood: float = 0.006
    silence_recovery_stress: float = 0.009
    silence_recovery_darkness: float = 0.005
    silence_max_delta_mood: float = 0.02
    silence_max_delta_stress: float = 0.025
    silence_max_delta_darkness: float = 0.015
    silence_cold_room_seconds: float = 120.0
    silence_cold_room_mood_delta: float = -0.0015
    silence_cold_room_stress_delta: float = 0.001
    anchor_min_room_samples: int = 6
    anchor_room_mood_max: float = 0.04
    anchor_room_stress_max: float = 0.025
    anchor_room_darkness_max: float = 0.015
    anchor_load_stress_max: float = 0.03
    anchor_load_rate_reference: int = 30
    anchor_update_min_delta: float = 0.01
    anchor_max_updates_per_stream: int = 24
    afterglow_enabled: bool = True
    afterglow_half_life_seconds: float = 180.0
    afterglow_apply_ratio: float = 0.20
    afterglow_capture_ratio: float = 0.40
    afterglow_max_mood: float = 0.05
    afterglow_max_stress: float = 0.06
    afterglow_max_darkness: float = 0.05
    afterglow_positive_relief_multiplier: float = 0.45
    repeated_event_decay: float = 0.15
    repeated_event_min_scale: float = 0.45

    @classmethod
    def from_mapping(cls, value: Any) -> "PersonaDynamicsTuning":
        """从配置模型或字典读取受限字段，领域层不依赖 Pydantic。"""
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if not isinstance(value, dict):
            return cls()
        accepted = {key: value[key] for key in cls.__dataclass_fields__ if key in value}
        return cls(**accepted)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PersonaAffectAnchor:
    """本场人格三轴自然回归的目标；只保存安全、可审计的场景来源。"""

    stream_session_id: str
    mood: float
    stress: float
    darkness: float
    version: int = 1
    sources: Dict[str, str] = field(default_factory=dict)
    updated_at: str = ""

    @classmethod
    def from_mapping(cls, value: Any) -> Optional["PersonaAffectAnchor"]:
        if not isinstance(value, dict) or not value.get("stream_session_id"):
            return None
        try:
            sources = value.get("sources", {})
            return cls(
                stream_session_id=str(value["stream_session_id"]),
                mood=clamp(float(value["mood"]), 0.0, 1.0),
                stress=clamp(float(value["stress"]), 0.0, 1.0),
                darkness=clamp(float(value["darkness"]), 0.0, 1.0),
                version=max(1, int(value.get("version", 1))),
                sources={
                    str(key)[:48]: str(item)[:96]
                    for key, item in (sources.items() if isinstance(sources, dict) else [])
                },
                updated_at=str(value.get("updated_at", ""))[:64],
            )
        except (TypeError, ValueError, KeyError):
            return None

    def as_state(self) -> PersonaState:
        return PersonaState(mood=self.mood, stress=self.stress, darkness=self.darkness)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stream_session_id": self.stream_session_id,
            "mood": self.mood,
            "stress": self.stress,
            "darkness": self.darkness,
            "version": self.version,
            "sources": dict(self.sources),
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class AffectAfterglow:
    """短时负面余波；只保存有界数值和事件类别，不保存原始互动内容。"""

    mood: float = 0.0
    stress: float = 0.0
    darkness: float = 0.0
    last_signature: str = ""
    same_signature_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DynamicsSnapshot:
    """调试用的最近一次动力学处理记录。"""

    raw_delta: Dict[str, float]
    recovery_delta: Dict[str, float]
    adjusted_delta: Dict[str, float]
    context: Dict[str, Any]
    stages: Dict[str, Dict[str, float]] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class PersonaDynamics:
    """人格数值动力学。"""

    def __init__(
        self,
        baseline: Optional[PersonaState] = None,
        tuning: Optional[PersonaDynamicsTuning] = None,
    ):
        self.baseline = baseline or PersonaState()
        self.tuning = tuning or PersonaDynamicsTuning()
        self.max_step = {
            "mood": self.tuning.max_step_mood,
            "stress": self.tuning.max_step_stress,
            "darkness": self.tuning.max_step_darkness,
        }
        self.recovery_rate = {
            "mood": self.tuning.recovery_rate_mood,
            "stress": self.tuning.recovery_rate_stress,
            "darkness": self.tuning.recovery_rate_darkness,
        }
        self._active_anchor: Optional[PersonaAffectAnchor] = None
        self._afterglow = AffectAfterglow()
        self._last_update_at: Optional[datetime] = None
        self._last_snapshot: Optional[DynamicsSnapshot] = None

    def build_context(
        self,
        *,
        memory_context: Optional[dict] = None,
        danmaku_message: str = "",
        source: str = "danmaku",
    ) -> DynamicsContext:
        """根据记忆上下文构造动力学上下文。"""
        if not memory_context:
            return DynamicsContext(source=source)

        recent_danmaku = memory_context.get("recent_danmaku", []) or []
        audience_sentiment = self._average_sentiment(recent_danmaku)
        repeated_topic = self._is_repeated_topic(danmaku_message, recent_danmaku)
        relationship = memory_context.get("viewer_relationship", {}) or {}
        long_term = memory_context.get("viewer_long_term_memory", {}) or {}

        return DynamicsContext(
            danmaku_rate=int(memory_context.get("danmaku_rate", len(recent_danmaku)) or 0),
            audience_sentiment=audience_sentiment,
            repeated_topic=repeated_topic,
            active_users=int(memory_context.get("active_users", 0) or 0),
            total_danmaku=int(memory_context.get("total_danmaku", 0) or 0),
            viewer_familiarity=float(relationship.get("familiarity", 0.0) or 0.0),
            viewer_affinity=float(relationship.get("affinity", 0.5) or 0.5),
            viewer_trust=float(relationship.get("trust", 0.5) or 0.5),
            conversation_transition=str(long_term.get("transition", "new")),
            retrieved_personal_fragments=len(long_term.get("recent_fragments", []) or []),
            source=source,
        )

    def apply(
        self,
        state: PersonaState,
        raw_delta: EmotionDelta,
        context: Optional[DynamicsContext] = None,
    ) -> EmotionDelta:
        """应用动力学修正，返回最终应该写入状态的 delta。"""
        context = context or DynamicsContext()
        context.seconds_since_last_update = self._seconds_since_last_update()
        adjusted, recovery, stages, afterglow = self._calculate_adjusted_delta(
            state, raw_delta, context,
        )

        self._afterglow = afterglow
        self._last_update_at = datetime.now(timezone.utc)
        self._last_snapshot = DynamicsSnapshot(
            raw_delta=raw_delta.model_dump(),
            recovery_delta=recovery.model_dump(),
            adjusted_delta=adjusted.model_dump(),
            context=asdict(context),
            stages=stages,
        )

        logger.debug(
            "人格动力学修正: raw=%s recovery=%s adjusted=%s context=%s",
            raw_delta.model_dump(),
            recovery.model_dump(),
            adjusted.model_dump(),
            asdict(context),
        )
        return adjusted

    def preview(
        self,
        state: PersonaState,
        raw_delta: EmotionDelta,
        context: Optional[DynamicsContext] = None,
    ) -> EmotionDelta:
        """计算本轮即时反应，但不推进时钟或写入调试快照。"""
        preview_context = replace(context) if context else DynamicsContext()
        preview_context.seconds_since_last_update = self._seconds_since_last_update()
        adjusted, _, _, _ = self._calculate_adjusted_delta(state, raw_delta, preview_context)
        return adjusted

    def project_state(self, state: PersonaState, delta: EmotionDelta) -> PersonaState:
        """将变化投影成临时状态，不修改传入状态。"""
        return PersonaState(
            mood=max(0.0, min(1.0, state.mood + delta.mood)),
            stress=max(0.0, min(1.0, state.stress + delta.stress)),
            darkness=max(0.0, min(1.0, state.darkness + delta.darkness)),
        )

    def activate_anchor(self, anchor: PersonaAffectAnchor) -> None:
        """激活已持久化的场次锚点；它只影响后续自然恢复。"""
        if self._active_anchor and self._active_anchor.stream_session_id != anchor.stream_session_id:
            self._afterglow = AffectAfterglow()
        self._active_anchor = anchor

    def clear_anchor(self) -> None:
        """下播或无有效场次时恢复基础人格锚点。"""
        self._active_anchor = None
        self._afterglow = AffectAfterglow()

    def record_silence_afterglow(self, context: DynamicsContext) -> None:
        """冷场只留下很小的余波，不重复执行三轴基线恢复。"""
        if (
            not self._afterglow_enabled()
            or context.silence_seconds < self.tuning.silence_cold_room_seconds
        ):
            return
        previous = self._decayed_afterglow(self._seconds_since_last_update())
        self._afterglow = AffectAfterglow(
            mood=max(-self.tuning.afterglow_max_mood, previous.mood - 0.003),
            stress=min(self.tuning.afterglow_max_stress, previous.stress + 0.004),
            darkness=min(self.tuning.afterglow_max_darkness, previous.darkness + 0.002),
            last_signature="cold_room",
            same_signature_count=(
                previous.same_signature_count + 1
                if previous.last_signature == "cold_room" else 1
            ),
        )
        self._last_update_at = datetime.now(timezone.utc)

    def _calculate_adjusted_delta(
        self,
        state: PersonaState,
        raw_delta: EmotionDelta,
        context: DynamicsContext,
    ) -> tuple[EmotionDelta, EmotionDelta, Dict[str, Dict[str, float]], AffectAfterglow]:
        signature = self._event_signature(raw_delta)
        event_scale = self._event_scale(signature)
        scaled_raw = EmotionDelta(
            mood=raw_delta.mood * event_scale,
            stress=raw_delta.stress * event_scale,
            darkness=raw_delta.darkness * event_scale,
        )
        decayed_afterglow = self._decayed_afterglow(context.seconds_since_last_update)
        scaled_raw = self._limit_positive_relief(scaled_raw, decayed_afterglow)
        recovery = self.recovery_delta(state, context.seconds_since_last_update)
        afterglow_delta = EmotionDelta(
            mood=decayed_afterglow.mood * self.tuning.afterglow_apply_ratio,
            stress=decayed_afterglow.stress * self.tuning.afterglow_apply_ratio,
            darkness=decayed_afterglow.darkness * self.tuning.afterglow_apply_ratio,
        )
        merged = EmotionDelta(
            mood=scaled_raw.mood + recovery.mood + afterglow_delta.mood,
            stress=scaled_raw.stress + recovery.stress + afterglow_delta.stress,
            darkness=scaled_raw.darkness + recovery.darkness + afterglow_delta.darkness,
        )
        shaped = EmotionDelta(
            mood=self._shape_axis("mood", state.mood, merged.mood),
            stress=self._shape_axis("stress", state.stress, merged.stress),
            darkness=self._shape_axis("darkness", state.darkness, merged.darkness),
        )
        atmospheric = self._apply_atmosphere(shaped, context)
        adjusted = EmotionDelta(
            mood=self._clamp_delta(atmospheric.mood, self.max_step["mood"]),
            stress=self._clamp_delta(atmospheric.stress, self.max_step["stress"]),
            darkness=self._clamp_delta(atmospheric.darkness, self.max_step["darkness"]),
        )
        next_afterglow = self._next_afterglow(
            decayed_afterglow, scaled_raw, context, signature,
        )
        return adjusted, recovery, {
            "raw_after_repeated_event_scale": scaled_raw.model_dump(),
            "afterglow_applied": afterglow_delta.model_dump(),
            "merged_raw_recovery_and_afterglow": merged.model_dump(),
            "after_boundary_and_mean_reversion": shaped.model_dump(),
            "after_atmosphere": atmospheric.model_dump(),
            "after_max_step_clamp": adjusted.model_dump(),
        }, next_afterglow

    def recovery_delta(self, state: PersonaState, seconds_since_last_update: float = 0.0) -> EmotionDelta:
        """让状态缓慢回到基准值，避免长期卡在极端。"""
        time_factor = self._time_factor(seconds_since_last_update)
        anchor = (
            self._active_anchor.as_state()
            if self._active_anchor and self.tuning.profile == "enhanced"
            else self.baseline
        )
        return EmotionDelta(
            mood=(anchor.mood - state.mood) * self.recovery_rate["mood"] * time_factor,
            stress=(anchor.stress - state.stress) * self.recovery_rate["stress"] * time_factor,
            darkness=(anchor.darkness - state.darkness) * self.recovery_rate["darkness"] * time_factor,
        )

    def get_debug_info(self) -> dict:
        """获取动力学调试信息。"""
        return {
            "baseline": self.baseline.model_dump(),
            "profile": self.tuning.profile,
            "active_anchor": self._active_anchor.to_dict() if self._active_anchor else None,
            "afterglow": self._afterglow.to_dict(),
            "max_step": self.max_step,
            "recovery_rate": self.recovery_rate,
            "tuning": self.tuning.to_dict(),
            "last_update_at": self._last_update_at.isoformat() if self._last_update_at else None,
            "last_snapshot": asdict(self._last_snapshot) if self._last_snapshot else None,
        }

    def _event_signature(self, delta: EmotionDelta) -> str:
        if delta.stress >= 0.012 or delta.darkness >= 0.008 or delta.mood <= -0.012:
            return "pressure"
        if delta.mood >= 0.012 and delta.stress <= -0.008:
            return "uplift"
        return ""

    def _event_scale(self, signature: str) -> float:
        if self.tuning.profile != "enhanced":
            return 1.0
        if not signature or signature != self._afterglow.last_signature:
            return 1.0
        repeated = max(1, self._afterglow.same_signature_count)
        return max(
            self.tuning.repeated_event_min_scale,
            1.0 - repeated * self.tuning.repeated_event_decay,
        )

    def _decayed_afterglow(self, seconds_since_last_update: float) -> AffectAfterglow:
        if not self._afterglow_enabled():
            return AffectAfterglow()
        seconds = max(0.0, seconds_since_last_update)
        factor = 0.5 ** (seconds / self.tuning.afterglow_half_life_seconds)
        return AffectAfterglow(
            mood=self._afterglow.mood * factor,
            stress=self._afterglow.stress * factor,
            darkness=self._afterglow.darkness * factor,
            last_signature=self._afterglow.last_signature,
            same_signature_count=self._afterglow.same_signature_count,
        )

    def _limit_positive_relief(
        self, delta: EmotionDelta, afterglow: AffectAfterglow,
    ) -> EmotionDelta:
        """正向互动可以安抚，但不能在一轮内抹掉已有压力/阴暗余波。"""
        if not self._afterglow_enabled():
            return delta
        relief = self.tuning.afterglow_positive_relief_multiplier
        return EmotionDelta(
            mood=delta.mood,
            stress=(delta.stress * relief if delta.stress < 0 and afterglow.stress > 0 else delta.stress),
            darkness=(delta.darkness * relief if delta.darkness < 0 and afterglow.darkness > 0 else delta.darkness),
        )

    def _next_afterglow(
        self,
        previous: AffectAfterglow,
        delta: EmotionDelta,
        context: DynamicsContext,
        signature: str,
    ) -> AffectAfterglow:
        if not self._afterglow_enabled():
            return AffectAfterglow()
        capture = self.tuning.afterglow_capture_ratio
        mood = min(0.0, previous.mood + min(0.0, delta.mood) * capture)
        stress = max(0.0, previous.stress + max(0.0, delta.stress) * capture)
        darkness = max(0.0, previous.darkness + max(0.0, delta.darkness) * capture)

        # 房间压力只以聚合上下文小幅补充，避免单条弹幕变成长时人格事实。
        if context.danmaku_rate >= self.tuning.anchor_load_rate_reference:
            stress += 0.006
        if context.audience_sentiment <= -0.35:
            mood -= 0.004
            stress += 0.005
            darkness += 0.003
        if context.danmaku_rate <= 2 and context.total_danmaku > 0:
            mood -= 0.002
            stress += 0.002

        repeated = (
            previous.same_signature_count + 1
            if signature and signature == previous.last_signature else 1 if signature else 0
        )
        return AffectAfterglow(
            mood=max(-self.tuning.afterglow_max_mood, mood),
            stress=min(self.tuning.afterglow_max_stress, stress),
            darkness=min(self.tuning.afterglow_max_darkness, darkness),
            last_signature=signature,
            same_signature_count=repeated,
        )

    def _afterglow_enabled(self) -> bool:
        return self.tuning.profile == "enhanced" and self.tuning.afterglow_enabled

    def _apply_atmosphere(self, delta: EmotionDelta, context: DynamicsContext) -> EmotionDelta:
        intensity = 1.0

        if context.repeated_topic:
            intensity *= self.tuning.repeated_topic_multiplier

        if context.danmaku_rate >= 30:
            intensity += 0.12
            delta.stress += 0.015
        elif context.danmaku_rate <= 2 and context.total_danmaku > 0:
            delta.stress -= 0.006

        if context.audience_sentiment > 0.35:
            delta.mood += 0.012
            delta.stress -= 0.008
        elif context.audience_sentiment < -0.35:
            delta.mood -= 0.014
            delta.stress += 0.018
            delta.darkness += 0.010

        if context.active_users >= 8:
            delta.mood += 0.006
            delta.stress += 0.004

        return EmotionDelta(
            mood=delta.mood * intensity,
            stress=delta.stress * intensity,
            darkness=delta.darkness * intensity,
        )

    def _shape_axis(self, axis: str, current: float, delta: float) -> float:
        if delta == 0:
            return 0.0

        if self.tuning.profile == "legacy":
            return self._shape_axis_legacy(axis, current, delta)

        room = max(0.0, 1.0 - current) if delta > 0 else max(0.0, current)
        boundary_factor = self.tuning.boundary_factor_min + (1.0 - self.tuning.boundary_factor_min) * room
        shaped = delta * boundary_factor

        mid = 0.5
        deviation = abs(current - mid)
        if deviation > self.tuning.mean_reversion_threshold:
            moving_back_to_mid = (current > mid and shaped < 0) or (current < mid and shaped > 0)
            if moving_back_to_mid:
                shaped *= 1.0 + deviation * self.tuning.mean_reversion_strength
            else:
                shaped *= max(0.65, 1.0 - deviation * self.tuning.mean_reversion_strength)

        # 不再在普通低落/高压区间自动乐观复位；只有接近安全边界时才轻微帮助
        # 状态回到可用范围，且动态锚点与事件余波仍可继续主导长期走向。
        if axis == "stress" and current >= self.tuning.extreme_guard_stress_ceiling and shaped < 0:
            shaped *= self.tuning.extreme_guard_recovery_multiplier
        elif axis == "darkness" and current >= self.tuning.extreme_guard_darkness_ceiling and shaped < 0:
            shaped *= self.tuning.extreme_guard_recovery_multiplier
        elif axis == "mood" and current <= self.tuning.extreme_guard_mood_floor and shaped > 0:
            shaped *= self.tuning.extreme_guard_recovery_multiplier

        return shaped

    def _shape_axis_legacy(self, axis: str, current: float, delta: float) -> float:
        """P22.31 前的数值形状，仅供灰度回退与人工对照。"""
        room = max(0.0, 1.0 - current) if delta > 0 else max(0.0, current)
        shaped = delta * (0.35 + 0.65 * room)
        deviation = abs(current - 0.5)
        if deviation > 0.25:
            moving_back_to_mid = (current > 0.5 and shaped < 0) or (
                current < 0.5 and shaped > 0
            )
            if moving_back_to_mid:
                shaped *= 1.0 + deviation * 0.8
            else:
                shaped *= max(0.45, 1.0 - deviation * 0.7)
        if axis == "stress" and current > 0.75 and shaped < 0:
            shaped *= 1.15
        elif axis == "darkness" and current > 0.65 and shaped < 0:
            shaped *= 1.12
        elif axis == "mood" and current < 0.30 and shaped > 0:
            shaped *= 1.12
        return shaped

    def _seconds_since_last_update(self) -> float:
        if not self._last_update_at:
            return 0.0
        return max(
            0.0,
            (datetime.now(timezone.utc) - self._last_update_at).total_seconds(),
        )

    def _time_factor(self, seconds: float) -> float:
        if seconds <= 0:
            return 1.0
        return max(
            1.0,
            min(
                self.tuning.recovery_time_factor_max,
                seconds / self.tuning.recovery_time_reference_seconds,
            ),
        )

    def _clamp_delta(self, value: float, limit: float) -> float:
        return max(-limit, min(limit, value))

    def _average_sentiment(self, recent_danmaku: List[dict]) -> float:
        sentiments = [
            float(item.get("sentiment", 0.0) or 0.0)
            for item in recent_danmaku
            if isinstance(item, dict)
        ]
        if not sentiments:
            return 0.0
        return max(-1.0, min(1.0, sum(sentiments) / len(sentiments)))

    def _is_repeated_topic(
        self,
        danmaku_message: str,
        recent_danmaku: List[dict],
    ) -> bool:
        """只按最近弹幕的重复判定复读；P30 起不再参考话题热度。"""
        if not danmaku_message:
            return False

        recent_same = 0
        for item in recent_danmaku[:8]:
            content = str(item.get("content", "")) if isinstance(item, dict) else ""
            if content and (content in danmaku_message or danmaku_message in content):
                recent_same += 1

        return recent_same >= 2
