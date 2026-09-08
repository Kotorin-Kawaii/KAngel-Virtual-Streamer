"""有界、低基数的应用安全与过载指标。"""

from collections import Counter
import threading


_KNOWN_SCOPES = {
    "http", "auth_register", "auth_login", "auth_password_change", "account_profile",
    "ws_handshake", "danmaku_send", "ai_reply", "connection",
    "broadcast", "database", "sc", "viewer_emote", "overload", "moderation",
}
_KNOWN_OUTCOMES = {
    "allowed", "rejected", "cooldown", "queue_full", "timeout",
    "connected", "closed", "send_failed", "error",
}


class SecurityMetrics:
    def __init__(self):
        self._counts = Counter()
        self._lock = threading.Lock()

    def record(self, scope: str, outcome: str, count: int = 1) -> None:
        safe_scope = scope if scope in _KNOWN_SCOPES else "other"
        safe_outcome = outcome if outcome in _KNOWN_OUTCOMES else "other"
        with self._lock:
            self._counts[(safe_scope, safe_outcome)] += max(0, int(count))

    def snapshot(self) -> dict:
        with self._lock:
            rows = {
                f"{scope}:{outcome}": value
                for (scope, outcome), value in sorted(self._counts.items())
            }
        return {"counters": rows, "label_policy": "fixed_low_cardinality"}

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()


security_metrics = SecurityMetrics()
