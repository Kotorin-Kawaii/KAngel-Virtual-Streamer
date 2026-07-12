"""外部系统集成公共边界。"""

__all__ = ["AIService", "SCConsumer", "SCService", "ai_service", "sc_consumer", "sc_service"]

def __getattr__(name: str):
    if name in {"AIService", "ai_service"}:
        from . import ai
        return getattr(ai, name)
    if name in {"SCConsumer", "SCService", "sc_consumer", "sc_service"}:
        from . import superchat
        return getattr(superchat, name)
    raise AttributeError(name)
