"""Kangel 服务端稳定公共包。"""

from typing import Any

__version__ = "2.0.0"


def create_app() -> Any:
    """延迟创建应用，避免仅导入公共包时初始化旧全局服务。"""
    from .app.bootstrap import create_app as factory

    return factory()


__all__ = ["__version__", "create_app"]
