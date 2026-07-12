"""Memory 模块配置化运行时实例。"""

from config import settings
from ..domain.policy import AccountMemoryPolicy


account_memory_policy = AccountMemoryPolicy(
    max_text_length=settings.memory.max_text_length,
    retention_days=settings.memory.retention_days,
)

__all__ = ["account_memory_policy"]
