"""人格领域稳定公共 API。"""

from .domain import (
    AIReply,
    AffectAfterglow,
    AudienceAtmosphereTickEvent,
    DanmakuReceivedEvent,
    DynamicsContext,
    DynamicsSnapshot,
    EmotionDelta,
    GiftReceivedEvent,
    InternalPersonaState,
    InternalStateDelta,
    InternalStateDynamics,
    InternalStateSnapshot,
    ModerationActionEvent,
    PersonaBehavior,
    PersonaDecision,
    PersonaDynamics,
    PersonaAffectAnchor,
    PersonaDynamicsTuning,
    PersonaEvent,
    PersonaEventReducer,
    PersonaEventType,
    PersonaMutation,
    PersonaState,
    SemanticImpactAnalyzedEvent,
    SentenceWithEmotion,
    SilenceTickEvent,
    StreamLifecycleEvent,
)

__all__ = [
    "AIReply",
    "AffectAfterglow",
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
    "PersonaAffectAnchor",
    "PersonaDynamicsTuning",
    "PersonaEvent",
    "PersonaEventReducer",
    "PersonaEventType",
    "PersonaMutation",
    "PersonaState",
    "SemanticImpactAnalyzedEvent",
    "SentenceWithEmotion",
    "SilenceTickEvent",
    "StreamLifecycleEvent",
    "EmotionCategory",
    "EmotionManager",
    "ImpactAnalysis",
    "PersonaEngine",
    "PersonaImpactAnalyzer",
]


def __getattr__(name: str):
    """延迟暴露应用服务，避免只导入领域类型时初始化数据库和全局引擎。"""
    if name == "PersonaEngine":
        from .application.engine import PersonaEngine
        return PersonaEngine
    if name in {"ImpactAnalysis", "PersonaImpactAnalyzer"}:
        from .application.impact_analyzer import ImpactAnalysis, PersonaImpactAnalyzer
        return {"ImpactAnalysis": ImpactAnalysis, "PersonaImpactAnalyzer": PersonaImpactAnalyzer}[name]
    if name in {"EmotionCategory", "EmotionManager"}:
        from .application.emotion_manager import EmotionCategory, EmotionManager
        return {"EmotionCategory": EmotionCategory, "EmotionManager": EmotionManager}[name]
    raise AttributeError(name)
