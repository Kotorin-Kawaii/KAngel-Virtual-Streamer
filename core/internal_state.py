"""细粒度内部状态及其确定性动力学。"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from core.persona_dynamics import DynamicsContext
from models.persona import InternalPersonaState, InternalStateDelta


@dataclass
class InternalStateSnapshot:
    before: Dict[str, float]
    delta: Dict[str, float]
    after: Dict[str, float]
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class InternalStateDynamics:
    """把语义分析转换为兴奋、疲劳、依恋和自信变化。"""

    def __init__(self):
        self.baseline = InternalPersonaState()
        self.max_step = {
            "arousal": 0.08,
            "fatigue": 0.05,
            "attachment": 0.035,
            "confidence": 0.055,
        }
        self.recovery_rate = {
            "arousal": 0.025,
            "fatigue": 0.012,
            "attachment": 0.004,
            "confidence": 0.010,
        }
        self._last_snapshot: Optional[InternalStateSnapshot] = None

    def derive_delta(
        self,
        state: InternalPersonaState,
        analysis: Any,
        context: Optional[DynamicsContext] = None,
    ) -> InternalStateDelta:
        context = context or DynamicsContext()
        tone = getattr(analysis, "emotional_tone", "neutral")
        intensity = float(getattr(analysis, "content_intensity", 0.5) or 0.5)
        relevance = float(getattr(analysis, "context_relevance", 0.5) or 0.5)
        mood_impact = float(getattr(analysis, "mood_impact", 0.0) or 0.0)
        stress_impact = float(getattr(analysis, "stress_impact", 0.0) or 0.0)
        darkness_impact = float(getattr(analysis, "darkness_impact", 0.0) or 0.0)

        positive = 1.0 if tone == "positive" else 0.0
        negative = 1.0 if tone == "negative" else 0.0
        mixed = 1.0 if tone == "mixed" else 0.0
        room_load = min(context.danmaku_rate / 10.0, 1.0)
        relationship_weight = min(1.0, max(
            0.25,
            0.25 + context.viewer_familiarity * 0.45 + context.viewer_trust * 0.30,
        ))
        continuity = 1.0 if context.conversation_transition in {
            "continuation", "contrast", "supplement"
        } else 0.0
        personal_evidence = min(context.retrieved_personal_fragments / 4.0, 1.0)

        raw = InternalStateDelta(
            arousal=(intensity - 0.35) * 0.055 + abs(stress_impact) * 0.25 + room_load * 0.012,
            fatigue=max(stress_impact, 0.0) * 0.30 + room_load * 0.010 + negative * 0.008 - positive * 0.005,
            attachment=(
                (positive * 0.018 - negative * 0.012 + mixed * 0.003)
                * relevance * relationship_weight
                + continuity * personal_evidence * 0.004
            ),
            confidence=(
                mood_impact * 0.28
                - max(stress_impact, 0.0) * 0.18
                - darkness_impact * 0.08
                + continuity * max(context.viewer_affinity - 0.5, 0.0) * 0.006
            ),
        )

        values = {}
        for axis in self.max_step:
            current = getattr(state, axis)
            recovery = (getattr(self.baseline, axis) - current) * self.recovery_rate[axis]
            value = getattr(raw, axis) + recovery
            room = (1.0 - current) if value > 0 else current
            value *= 0.4 + 0.6 * max(0.0, room)
            values[axis] = max(-self.max_step[axis], min(self.max_step[axis], value))
        return InternalStateDelta(**values)

    def project(
        self,
        state: InternalPersonaState,
        delta: InternalStateDelta,
        *,
        record: bool = False,
    ) -> InternalPersonaState:
        projected = InternalPersonaState(**{
            axis: max(0.0, min(1.0, getattr(state, axis) + getattr(delta, axis)))
            for axis in self.max_step
        })
        if record:
            self._last_snapshot = InternalStateSnapshot(
                before=state.model_dump(),
                delta=delta.model_dump(),
                after=projected.model_dump(),
            )
        return projected

    def get_debug_info(self) -> dict:
        return {
            "baseline": self.baseline.model_dump(),
            "max_step": self.max_step,
            "recovery_rate": self.recovery_rate,
            "last_snapshot": asdict(self._last_snapshot) if self._last_snapshot else None,
        }


internal_state_dynamics = InternalStateDynamics()
