"""SuperChat 集成（延迟加载）。"""

_MODULES = {
    "SCConsumer": "consumer",
    "SCService": "service",
    "sc_consumer": "consumer",
    "sc_service": "service",
}
__all__ = list(_MODULES)

def __getattr__(name: str):
    if module_name := _MODULES.get(name):
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
