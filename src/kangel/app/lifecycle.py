"""后台任务生命周期的统一装配边界。"""

# P1 只收口入口，现有生命周期实现将在对应领域迁移时逐项拆出。
from kangel.transport.http.routes import lifespan

__all__ = ["lifespan"]
