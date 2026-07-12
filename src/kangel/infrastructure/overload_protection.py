"""单进程过载快照与分层准入判定。"""

from dataclasses import dataclass
import os
import resource

from config import settings
from .security_metrics import security_metrics


@dataclass(frozen=True)
class OverloadDecision:
    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 1


class OverloadProtector:
    @staticmethod
    def _rss_mb() -> float:
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS 为 bytes，Linux 为 KiB。
        return value / (1024 * 1024) if value > 10_000_000 else value / 1024

    def snapshot(self, *, connections: int, ai_active: int, ai_waiting: int) -> dict:
        try:
            load = os.getloadavg()[0] / max(1, os.cpu_count() or 1)
        except (AttributeError, OSError):
            load = 0.0
        return {
            "connections": connections,
            "ai_active": ai_active,
            "ai_waiting": ai_waiting,
            "cpu_load_per_core": round(load, 3),
            "rss_mb": round(self._rss_mb(), 1),
        }

    def admit(self, *, expensive: bool, snapshot: dict) -> OverloadDecision:
        config = settings.rate_limit
        if not config.overload_enabled or not expensive:
            return OverloadDecision(True)
        reason = ""
        if snapshot["connections"] >= config.overload_max_connections:
            reason = "connections"
        elif snapshot["ai_waiting"] >= config.overload_max_ai_waiters:
            reason = "ai_queue"
        elif (
            config.overload_max_rss_mb > 0
            and snapshot["rss_mb"] >= config.overload_max_rss_mb
        ):
            reason = "memory"
        elif (
            config.overload_max_cpu_load_per_core > 0
            and snapshot["cpu_load_per_core"] >= config.overload_max_cpu_load_per_core
        ):
            reason = "cpu"
        if reason:
            security_metrics.record("overload", "rejected")
            return OverloadDecision(False, reason, config.overload_retry_after_seconds)
        security_metrics.record("overload", "allowed")
        return OverloadDecision(True)


overload_protector = OverloadProtector()
