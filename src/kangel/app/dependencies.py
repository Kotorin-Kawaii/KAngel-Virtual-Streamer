"""应用依赖访问边界。

新代码应通过显式构造参数接收依赖。此快照仅描述迁移期仍由旧模块维护的实例，
避免在 P1 创建第二套全局单例。
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApplicationDependencies:
    """装配层服务实例的只读集合。"""

    persona_engine: Any
    database: Any
    connection_manager: Any


def load_dependencies() -> ApplicationDependencies:
    """延迟读取规范单例，防止仅导入模块时重复初始化服务。"""
    from kangel.transport.websocket.connection_manager import connection_manager
    from kangel.infrastructure.database import db_manager
    from kangel.persona.application.engine import persona_engine

    return ApplicationDependencies(
        persona_engine=persona_engine,
        database=db_manager,
        connection_manager=connection_manager,
    )


__all__ = ["ApplicationDependencies", "load_dependencies"]
