"""Memory 应用服务公共入口（延迟加载）。"""

_MODULES = {
    "AccountMemoryGovernanceService": "governance",
    "ConversationContinuityAnalyzer": "long_term_memory",
    "ConversationTransition": "long_term_memory",
    "LongTermMemoryManager": "long_term_memory",
}
__all__ = list(_MODULES)

def __getattr__(name: str):
    if module_name := _MODULES.get(name):
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
