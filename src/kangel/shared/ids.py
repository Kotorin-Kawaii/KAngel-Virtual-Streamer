"""内部稳定标识生成器。"""

from uuid import uuid4


def new_event_id() -> str:
    return str(uuid4())


__all__ = ["new_event_id"]
