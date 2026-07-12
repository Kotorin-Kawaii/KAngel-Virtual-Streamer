"""Memory 稳定公共 API。"""

from .domain import *
from .domain import __all__ as _domain_all

__all__ = [*_domain_all, "AccountMemoryGovernanceService", "ConversationContinuityAnalyzer", "ConversationTransition", "LongTermMemoryManager"]


def __getattr__(name: str):
    if name in {"AccountMemoryGovernanceService", "ConversationContinuityAnalyzer", "ConversationTransition", "LongTermMemoryManager"}:
        from . import application
        return getattr(application, name)
    raise AttributeError(name)
