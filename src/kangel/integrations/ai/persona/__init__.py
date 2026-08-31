"""结构化 Persona 资产与灰度运行入口。"""

from .catalog import (
    PersonaCatalog,
    PersonaCatalogEntry,
    PersonaEvidence,
    PersonaExemplar,
    build_persona_catalog,
    load_persona_catalog,
)
from .constitution import build_persona_system_prompt
from .evidence_selector import (
    PersonaEvidenceSelector,
    PersonaSelection,
    persona_prompt_metrics,
    resolve_prompt_mode,
)
from .state_style import PersonaStyleVector, build_style_vector

__all__ = [
    "PersonaCatalog",
    "PersonaCatalogEntry",
    "PersonaEvidence",
    "PersonaExemplar",
    "PersonaEvidenceSelector",
    "PersonaSelection",
    "PersonaStyleVector",
    "build_persona_catalog",
    "load_persona_catalog",
    "build_persona_system_prompt",
    "build_style_vector",
    "persona_prompt_metrics",
    "resolve_prompt_mode",
]
