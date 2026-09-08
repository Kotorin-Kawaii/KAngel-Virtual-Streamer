"""单进程应用内令牌桶限流器。"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class RateLimitPolicy:
    rate_per_minute: float
    burst: int
    cooldown_seconds: int = 0

    def __post_init__(self):
        if self.rate_per_minute <= 0 or self.burst < 1 or self.cooldown_seconds < 0:
            raise ValueError("限流策略参数无效")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int = 0


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    blocked_until: float = 0.0


class InMemoryRateLimiter:
    """线程安全令牌桶；仅适用于单进程或开发环境。"""

    def __init__(self, max_buckets: int = 10000):
        self.max_buckets = max_buckets
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(
        self,
        key: str,
        policy: RateLimitPolicy,
        *,
        cost: float = 1.0,
        now: Optional[float] = None,
    ) -> RateLimitDecision:
        return self.check_many([(key, policy, cost)], now=now)

    def check_many(
        self,
        checks: Iterable[tuple[str, RateLimitPolicy, float]],
        *,
        now: Optional[float] = None,
    ) -> RateLimitDecision:
        """原子检查组合桶；任一拒绝时不消耗其他桶令牌。"""
        items = list(checks)
        if not items or any(not key or cost <= 0 for key, _, cost in items):
            raise ValueError("限流组合检查必须有效")
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            prepared = []
            for key, policy, cost in items:
                bucket = self._buckets.get(key)
                if bucket is None:
                    self._evict_if_needed(current)
                    bucket = _Bucket(float(policy.burst), current)
                    self._buckets[key] = bucket
                rate_per_second = policy.rate_per_minute / 60.0
                elapsed = max(0.0, current - bucket.updated_at)
                bucket.tokens = min(
                    float(policy.burst), bucket.tokens + elapsed * rate_per_second
                )
                bucket.updated_at = current
                prepared.append((bucket, policy, cost, rate_per_second))

            for bucket, policy, cost, rate_per_second in prepared:
                if current < bucket.blocked_until:
                    return RateLimitDecision(
                        False, max(1, math.ceil(bucket.blocked_until - current))
                    )
                if bucket.tokens < cost:
                    refill_wait = (cost - bucket.tokens) / rate_per_second
                    retry_after = max(refill_wait, float(policy.cooldown_seconds))
                    if policy.cooldown_seconds:
                        bucket.blocked_until = current + policy.cooldown_seconds
                    return RateLimitDecision(False, max(1, math.ceil(retry_after)))

            for bucket, _, cost, _ in prepared:
                bucket.tokens -= cost
            return RateLimitDecision(True)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()

    def _evict_if_needed(self, now: float) -> None:
        if len(self._buckets) < self.max_buckets:
            return
        stale_before = now - 3600.0
        stale = [
            key for key, bucket in self._buckets.items()
            if bucket.updated_at < stale_before and bucket.blocked_until <= now
        ]
        for key in stale:
            self._buckets.pop(key, None)
        if len(self._buckets) >= self.max_buckets:
            oldest = min(self._buckets, key=lambda key: self._buckets[key].updated_at)
            self._buckets.pop(oldest, None)


class ConcurrencyLease:
    def __init__(self, gate: "ConcurrencyGate", scope: str):
        self._gate = gate
        self.scope = scope
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._gate._release(self.scope)
            self._released = True


class ConcurrencyGate:
    """非阻塞并发闸门，避免昂贵工作在服务内无限排队。"""

    def __init__(self):
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, scope: str, limit: int) -> Optional[ConcurrencyLease]:
        if not scope or limit < 1:
            raise ValueError("并发闸门参数无效")
        with self._lock:
            current = self._counts.get(scope, 0)
            if current >= limit:
                return None
            self._counts[scope] = current + 1
        return ConcurrencyLease(self, scope)

    def _release(self, scope: str) -> None:
        with self._lock:
            current = self._counts.get(scope, 0)
            if current <= 1:
                self._counts.pop(scope, None)
            else:
                self._counts[scope] = current - 1

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()


@dataclass
class _FailureState:
    failures: int
    blocked_until: float
    updated_at: float


class ProgressiveCooldown:
    """按连续失败次数短暂递增冷却；成功后清除且永不永久锁定。"""

    def __init__(self):
        self._states: dict[str, _FailureState] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, now: Optional[float] = None) -> RateLimitDecision:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            state = self._states.get(key)
            if state and current < state.blocked_until:
                return RateLimitDecision(
                    False, max(1, math.ceil(state.blocked_until - current))
                )
        return RateLimitDecision(True)

    def record_failure(
        self,
        key: str,
        *,
        threshold: int,
        base_seconds: int,
        max_seconds: int,
        now: Optional[float] = None,
    ) -> int:
        if threshold < 1 or base_seconds < 1 or max_seconds < base_seconds:
            raise ValueError("渐进冷却参数无效")
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            previous = self._states.get(key)
            failures = (previous.failures if previous else 0) + 1
            exponent = max(0, failures - threshold)
            cooldown = 0 if failures < threshold else min(
                max_seconds, base_seconds * (2 ** exponent)
            )
            self._states[key] = _FailureState(
                failures=failures,
                blocked_until=current + cooldown,
                updated_at=current,
            )
        return cooldown

    def clear(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._states.clear()
            else:
                self._states.pop(key, None)


rate_limiter = InMemoryRateLimiter()
concurrency_gate = ConcurrencyGate()
login_failure_cooldown = ProgressiveCooldown()
password_change_failure_cooldown = ProgressiveCooldown()
