"""人格状态的确定性变更描述。"""

from dataclasses import dataclass, field
from typing import Optional

from .dynamics import DynamicsContext
from .state import EmotionDelta, InternalStateDelta


@dataclass
class PersonaMutation:
    emotion_delta: EmotionDelta = field(
        default_factory=lambda: EmotionDelta(mood=0.0, stress=0.0, darkness=0.0)
    )
    internal_delta: InternalStateDelta = field(default_factory=InternalStateDelta)
    dynamics_context: Optional[DynamicsContext] = None
    reason: str = ""

    @property
    def has_changes(self) -> bool:
        values = [
            *self.emotion_delta.model_dump().values(),
            *self.internal_delta.model_dump().values(),
        ]
        return any(abs(float(value)) > 1e-9 for value in values)
