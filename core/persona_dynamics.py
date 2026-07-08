"""
人格状态动力学模块。

把模型给出的语义影响值转换成更稳定的直播状态变化：带惯性、边界衰减、
自然恢复、重复话题降权和直播间气氛修正。
"""

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import settings
from models.persona import EmotionDelta, PersonaState
from utils.logger import logger


@dataclass
class DynamicsContext:
    """一次状态更新的直播间上下文。"""

    danmaku_rate: int = 0
    audience_sentiment: float = 0.0
    topic_heat: float = 0.0
    repeated_topic: bool = False
    active_users: int = 0
    total_danmaku: int = 0
    viewer_familiarity: float = 0.0
    viewer_affinity: float = 0.5
    viewer_trust: float = 0.5
    conversation_transition: str = "new"
    retrieved_personal_fragments: int = 0
    seconds_since_last_update: float = 0.0
    source: str = "danmaku"


@dataclass
class DynamicsSnapshot:
    """调试用的最近一次动力学处理记录。"""

    raw_delta: Dict[str, float]
    recovery_delta: Dict[str, float]
    adjusted_delta: Dict[str, float]
    context: Dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class PersonaDynamics:
    """人格数值动力学。"""

    def __init__(self):
        self.baseline = PersonaState(
            mood=settings.persona.initial_mood,
            stress=settings.persona.initial_stress,
            darkness=settings.persona.initial_darkness,
        )
        self.max_step = {
            "mood": 0.08,
            "stress": 0.10,
            "darkness": 0.07,
        }
        self.recovery_rate = {
            "mood": 0.012,
            "stress": 0.018,
            "darkness": 0.010,
        }
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
        hot_topics = memory_context.get("hot_topics", []) or []
        audience_sentiment = self._average_sentiment(recent_danmaku)
        topic_heat = self._max_topic_heat(hot_topics)
        repeated_topic = self._is_repeated_topic(danmaku_message, recent_danmaku, hot_topics)
        relationship = memory_context.get("viewer_relationship", {}) or {}
        long_term = memory_context.get("viewer_long_term_memory", {}) or {}

        return DynamicsContext(
            danmaku_rate=int(memory_context.get("danmaku_rate", len(recent_danmaku)) or 0),
            audience_sentiment=audience_sentiment,
            topic_heat=topic_heat,
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
        adjusted, recovery = self._calculate_adjusted_delta(state, raw_delta, context)

        self._last_update_at = datetime.now()
        self._last_snapshot = DynamicsSnapshot(
            raw_delta=raw_delta.model_dump(),
            recovery_delta=recovery.model_dump(),
            adjusted_delta=adjusted.model_dump(),
            context=asdict(context),
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
        adjusted, _ = self._calculate_adjusted_delta(state, raw_delta, preview_context)
        return adjusted

    def project_state(self, state: PersonaState, delta: EmotionDelta) -> PersonaState:
        """将变化投影成临时状态，不修改传入状态。"""
        return PersonaState(
            mood=max(0.0, min(1.0, state.mood + delta.mood)),
            stress=max(0.0, min(1.0, state.stress + delta.stress)),
            darkness=max(0.0, min(1.0, state.darkness + delta.darkness)),
        )

    def _calculate_adjusted_delta(
        self,
        state: PersonaState,
        raw_delta: EmotionDelta,
        context: DynamicsContext,
    ) -> tuple[EmotionDelta, EmotionDelta]:
        recovery = self.recovery_delta(state, context.seconds_since_last_update)
        merged = EmotionDelta(
            mood=raw_delta.mood + recovery.mood,
            stress=raw_delta.stress + recovery.stress,
            darkness=raw_delta.darkness + recovery.darkness,
        )
        adjusted = EmotionDelta(
            mood=self._shape_axis("mood", state.mood, merged.mood),
            stress=self._shape_axis("stress", state.stress, merged.stress),
            darkness=self._shape_axis("darkness", state.darkness, merged.darkness),
        )
        adjusted = self._apply_atmosphere(adjusted, context)
        return EmotionDelta(
            mood=self._clamp_delta(adjusted.mood, self.max_step["mood"]),
            stress=self._clamp_delta(adjusted.stress, self.max_step["stress"]),
            darkness=self._clamp_delta(adjusted.darkness, self.max_step["darkness"]),
        ), recovery

    def recovery_delta(self, state: PersonaState, seconds_since_last_update: float = 0.0) -> EmotionDelta:
        """让状态缓慢回到基准值，避免长期卡在极端。"""
        time_factor = self._time_factor(seconds_since_last_update)
        return EmotionDelta(
            mood=(self.baseline.mood - state.mood) * self.recovery_rate["mood"] * time_factor,
            stress=(self.baseline.stress - state.stress) * self.recovery_rate["stress"] * time_factor,
            darkness=(self.baseline.darkness - state.darkness) * self.recovery_rate["darkness"] * time_factor,
        )

    def get_debug_info(self) -> dict:
        """获取动力学调试信息。"""
        return {
            "baseline": self.baseline.model_dump(),
            "max_step": self.max_step,
            "recovery_rate": self.recovery_rate,
            "last_update_at": self._last_update_at.isoformat() if self._last_update_at else None,
            "last_snapshot": asdict(self._last_snapshot) if self._last_snapshot else None,
        }

    def _apply_atmosphere(self, delta: EmotionDelta, context: DynamicsContext) -> EmotionDelta:
        intensity = 1.0

        if context.repeated_topic:
            intensity *= 0.65

        if context.danmaku_rate >= 30:
            intensity += 0.12
            delta.stress += 0.015
        elif context.danmaku_rate <= 2 and context.total_danmaku > 0:
            delta.stress -= 0.006

        if context.topic_heat >= 0.65:
            delta.stress += 0.012

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

        room = max(0.0, 1.0 - current) if delta > 0 else max(0.0, current)
        boundary_factor = 0.35 + 0.65 * room
        shaped = delta * boundary_factor

        mid = 0.5
        deviation = abs(current - mid)
        if deviation > 0.25:
            moving_back_to_mid = (current > mid and shaped < 0) or (current < mid and shaped > 0)
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
        return max(0.0, (datetime.now() - self._last_update_at).total_seconds())

    def _time_factor(self, seconds: float) -> float:
        if seconds <= 0:
            return 1.0
        return max(1.0, min(3.0, seconds / 30.0))

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

    def _max_topic_heat(self, hot_topics: List[dict]) -> float:
        heats = [
            float(item.get("heat", 0.0) or 0.0)
            for item in hot_topics
            if isinstance(item, dict)
        ]
        return max(heats) if heats else 0.0

    def _is_repeated_topic(
        self,
        danmaku_message: str,
        recent_danmaku: List[dict],
        hot_topics: List[dict],
    ) -> bool:
        if not danmaku_message:
            return False

        recent_same = 0
        for item in recent_danmaku[:8]:
            content = str(item.get("content", "")) if isinstance(item, dict) else ""
            if content and (content in danmaku_message or danmaku_message in content):
                recent_same += 1

        if recent_same >= 2:
            return True

        for item in hot_topics[:3]:
            topic = str(item.get("topic", "")) if isinstance(item, dict) else ""
            heat = float(item.get("heat", 0.0) or 0.0) if isinstance(item, dict) else 0.0
            if topic and topic in danmaku_message and heat >= 0.75:
                return True

        return False


persona_dynamics = PersonaDynamics()
