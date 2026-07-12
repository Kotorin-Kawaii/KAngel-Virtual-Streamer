"""Danmaku 稳定公共 API。"""

from .domain import *
from .domain import __all__ as _domain_all

__all__ = [*_domain_all, "ChineseTextAnalyzer", "DanmakuItem", "DanmakuLoadProfile", "DanmakuMemoryManager", "DanmakuPool", "DanmakuSelector", "DanmakuStatus", "SelectionResult", "resolve_danmaku_load"]

def __getattr__(name: str):
    if name in set(__all__) - set(_domain_all):
        from . import application
        return getattr(application, name)
    raise AttributeError(name)
