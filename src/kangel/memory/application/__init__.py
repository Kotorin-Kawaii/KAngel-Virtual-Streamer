"""Memory 应用服务公共入口（延迟加载）。"""

_MODULES = {
    "AccountMemoryGovernanceService": "governance",
    "ConversationContinuityAnalyzer": "long_term_memory",
    "ConversationTransition": "long_term_memory",
    "LongTermMemoryManager": "long_term_memory",
    "EpisodicMemoryManager": "episodic",
    "EpisodicMemoryConsumer": "episodic",
    "episodic_memory_manager": "episodic",
    "episodic_memory_consumer": "episodic",
    "ViewerImpressionError": "viewer_impression",
    "ViewerImpressionValidationError": "viewer_impression",
    "ViewerImpressionPromptBuilder": "viewer_impression",
    "ViewerImpressionValidator": "viewer_impression",
    "ViewerImpressionService": "viewer_impression",
    "ViewerImpressionWorker": "viewer_impression",
    "viewer_impression_service": "viewer_impression",
    "viewer_impression_worker": "viewer_impression",
}
__all__ = list(_MODULES)

def __getattr__(name: str):
    if module_name := _MODULES.get(name):
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
