"""人格模块运行时实例的唯一装配位置。"""

from config import settings
from kangel.infrastructure.database import db_manager

from ..domain.dynamics import PersonaDynamics
from ..domain.internal_state import InternalStateDynamics
from ..domain.reducer import PersonaEventReducer
from ..domain.state import PersonaState
from ..infrastructure import DatabasePersonaEventLog
from .pipeline import PersonaEventPipeline


baseline = PersonaState(
    mood=settings.persona.initial_mood,
    stress=settings.persona.initial_stress,
    darkness=settings.persona.initial_darkness,
)
persona_dynamics = PersonaDynamics(baseline=baseline)
internal_state_dynamics = InternalStateDynamics()
persona_event_pipeline = PersonaEventPipeline(
    reducer=PersonaEventReducer(baseline=baseline),
    event_log=DatabasePersonaEventLog(db_manager),
)

__all__ = [
    "baseline",
    "internal_state_dynamics",
    "persona_dynamics",
    "persona_event_pipeline",
]
