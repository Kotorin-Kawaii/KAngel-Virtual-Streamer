"""人格领域模型与纯动力学公共入口。"""

from .dynamics import DynamicsContext, DynamicsSnapshot, PersonaDynamics, clamp, toward
from .events import (
    AudienceAtmosphereTickEvent,
    DanmakuReceivedEvent,
    GiftReceivedEvent,
    ModerationActionEvent,
    PersonaEvent,
    PersonaEventType,
    SemanticImpactAnalyzedEvent,
    SilenceTickEvent,
    StreamLifecycleEvent,
)
from .internal_state import InternalStateDynamics, InternalStateSnapshot
from .mutations import PersonaMutation
from .reducer import PersonaEventReducer
from .state import (
    AIReply,
    EmotionDelta,
    InternalPersonaState,
    InternalStateDelta,
    PersonaBehavior,
    PersonaDecision,
    PersonaState,
    SentenceWithEmotion,
)

__all__ = [
    "AIReply",
    "AudienceAtmosphereTickEvent",
    "DanmakuReceivedEvent",
    "DynamicsContext",
    "DynamicsSnapshot",
    "EmotionDelta",
    "GiftReceivedEvent",
    "InternalPersonaState",
    "InternalStateDelta",
    "InternalStateDynamics",
    "InternalStateSnapshot",
    "ModerationActionEvent",
    "PersonaBehavior",
    "PersonaDecision",
    "PersonaDynamics",
    "PersonaEvent",
    "PersonaEventReducer",
    "PersonaEventType",
    "PersonaMutation",
    "PersonaState",
    "SemanticImpactAnalyzedEvent",
    "SentenceWithEmotion",
    "SilenceTickEvent",
    "StreamLifecycleEvent",
    "clamp",
    "toward",
]
