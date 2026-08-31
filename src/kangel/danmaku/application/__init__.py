"""Danmaku 应用服务公共入口（延迟加载）。"""

_MODULES = {
    "ChineseTextAnalyzer": "text_analyzer",
    "DanmakuItem": "pool",
    "DanmakuLoadProfile": "load_tracker",
    "DanmakuMemoryManager": "memory",
    "DanmakuPool": "pool",
    "DanmakuSelector": "selector",
    "DanmakuStatus": "pool",
    "SelectionResult": "selector",
    "resolve_danmaku_load": "load_tracker",
}
__all__ = list(_MODULES)

def __getattr__(name: str):
    if module_name := _MODULES.get(name):
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
"""Danmaku application services public entry points."""

_MODULES = {
    "LanguageDetection": "language",
    "LanguageDetector": "language",
    "ReplyLanguagePolicy": "language",
    "EnglishSurpriseJokeService": "language",
}
__all__ = list(_MODULES)


def __getattr__(name: str):
    if module_name := _MODULES.get(name):
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{module_name}"), name)
    raise AttributeError(name)
