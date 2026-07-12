"""不含业务语义的共享基础能力。"""

from .clock import Clock, SystemClock
from .ids import new_event_id

__all__ = ["Clock", "SystemClock", "new_event_id"]
