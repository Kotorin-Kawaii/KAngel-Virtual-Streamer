"""纯展示观众表情旁路：校验、冷却、幂等和聚合指标。"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter

from config import settings
from kangel.infrastructure.rate_limiter import InMemoryRateLimiter, RateLimitPolicy
from ..domain.emote import EmoteDecision


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


class ViewerEmoteService:
    def __init__(self, limiter=None, clock=None):
        self.limiter = limiter or InMemoryRateLimiter(max_buckets=20000)
        self.clock = clock or time.monotonic
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._metrics = Counter()

    def process(
        self, *, emote_id: str, client_event_id: str, connection_id: str,
        client_ip: str, identity,
    ) -> EmoteDecision:
        config = settings.emotes
        if (
            not isinstance(emote_id, str) or not _ID.fullmatch(emote_id)
            or emote_id not in config.allowed_ids
        ):
            self._metric("unknown_emote")
            return EmoteDecision(False, "invalid_emote")
        if not isinstance(client_event_id, str) or not _EVENT_ID.fullmatch(client_event_id):
            self._metric("invalid_event_id")
            return EmoteDecision(False, "invalid_emote_event")

        subject = (
            f"account:{identity.account_id}"
            if identity and identity.is_authenticated
            else f"guest:{connection_id}"
        )
        dedup_key = f"{subject}:{client_event_id}"
        now = self.clock()
        with self._lock:
            self._purge_seen(now)
            if self._seen.get(dedup_key, 0) > now:
                self._metrics["deduplicated"] += 1
                return EmoteDecision(False, "duplicate_emote")

        cooldown_rate = 60.0 / config.cooldown_seconds
        checks = [
            (f"emote:cooldown:{subject}", RateLimitPolicy(cooldown_rate, 1), 1.0),
            (f"emote:connection:{connection_id}", RateLimitPolicy(
                config.connection_rate_per_minute, config.connection_burst
            ), 1.0),
            (f"emote:ip:{client_ip or 'unknown'}", RateLimitPolicy(
                config.ip_rate_per_minute, config.ip_burst
            ), 1.0),
            ("emote:global", RateLimitPolicy(
                config.global_rate_per_minute, config.global_burst
            ), 1.0),
        ]
        decision = self.limiter.check_many(checks, now=now)
        if not decision.allowed:
            self._metric("rate_limited")
            return EmoteDecision(
                False, "rate_limited", decision.retry_after_seconds
            )

        with self._lock:
            if self._seen.get(dedup_key, 0) > now:
                self._metrics["deduplicated"] += 1
                return EmoteDecision(False, "duplicate_emote")
            self._seen[dedup_key] = now + config.dedup_ttl_seconds
            self._metrics["allowed"] += 1
            self._metrics[f"emote:{emote_id}"] += 1
        nickname = identity.current_nickname if identity else "匿名宅宅"
        return EmoteDecision(True, payload={
            "emote_id": emote_id,
            "nickname": nickname,
            "viewer_id": connection_id,
            "client_event_id": client_event_id,
        })

    def get_metrics(self) -> dict:
        with self._lock:
            return {
                "allowed": self._metrics["allowed"],
                "rate_limited": self._metrics["rate_limited"],
                "unknown_emote": self._metrics["unknown_emote"],
                "invalid_event_id": self._metrics["invalid_event_id"],
                "deduplicated": self._metrics["deduplicated"],
                "broadcast_failures": self._metrics["broadcast_failures"],
                "by_emote": {
                    key.removeprefix("emote:"): value
                    for key, value in self._metrics.items()
                    if key.startswith("emote:")
                },
            }

    def record_broadcast_failures(self, count: int) -> None:
        if count > 0:
            with self._lock:
                self._metrics["broadcast_failures"] += int(count)

    def clear(self) -> None:
        self.limiter.clear()
        with self._lock:
            self._seen.clear()
            self._metrics.clear()

    def _metric(self, name: str) -> None:
        with self._lock:
            self._metrics[name] += 1

    def _purge_seen(self, now: float) -> None:
        if len(self._seen) < 10000:
            return
        self._seen = {key: expiry for key, expiry in self._seen.items() if expiry > now}


viewer_emote_service = ViewerEmoteService()
