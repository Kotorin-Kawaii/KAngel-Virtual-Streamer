"""WebSocket 可信来源解析、组合限流与幂等保护。"""

from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from .rate_limiter import InMemoryRateLimiter, RateLimitPolicy
from .security_metrics import security_metrics


def normalize_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address((value or "").strip())
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return address.compressed
    except ValueError:
        return "unknown"


def ip_subnet_key(value: str) -> str:
    """生成内部限流网段键：IPv4 /24，IPv6 /64。"""
    normalized = normalize_ip(value)
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return "unknown"
    prefix = 24 if isinstance(address, ipaddress.IPv4Address) else 64
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def resolve_client_ip(
    peer_host: str,
    forwarded_for: str,
    trusted_proxy_cidrs: Iterable[str],
) -> str:
    """仅当直连来源可信时，从 XFF 右向左取首个非可信地址。"""
    peer = normalize_ip(peer_host)
    try:
        networks = [ipaddress.ip_network(cidr, strict=False) for cidr in trusted_proxy_cidrs]
        peer_address = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_address in network for network in networks):
        return peer

    chain = [normalize_ip(part) for part in (forwarded_for or "").split(",")]
    chain = [value for value in chain if value != "unknown"] + [peer]
    for value in reversed(chain):
        address = ipaddress.ip_address(value)
        if not any(address in network for network in networks):
            return address.compressed
    return chain[0] if chain else peer


@dataclass(frozen=True)
class ProtectionDecision:
    allowed: bool
    scope: str = ""
    retry_after_seconds: int = 0
    action: str = "cooldown"
    code: str = "rate_limited"


class ExpiringDeduplicator:
    def __init__(self, max_keys: int = 50000):
        self.max_keys = max_keys
        self._expires_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, key: str, ttl_seconds: int, *, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            expiry = self._expires_at.get(key, 0.0)
            if expiry > current:
                return False
            if len(self._expires_at) >= self.max_keys:
                self._expires_at = {
                    item: expires for item, expires in self._expires_at.items()
                    if expires > current
                }
                if len(self._expires_at) >= self.max_keys:
                    oldest = min(self._expires_at, key=self._expires_at.get)
                    self._expires_at.pop(oldest, None)
            self._expires_at[key] = current + max(1, ttl_seconds)
            return True

    def clear(self) -> None:
        with self._lock:
            self._expires_at.clear()


class WebSocketRateGuard:
    def __init__(self, limiter: Optional[InMemoryRateLimiter] = None):
        self.limiter = limiter or InMemoryRateLimiter()

    @staticmethod
    def _policy(rate: float, burst: int, cooldown: int) -> RateLimitPolicy:
        return RateLimitPolicy(rate, burst, cooldown)

    def check_handshake(self, ip: str, account_id: str, config) -> ProtectionDecision:
        if not config.enabled:
            return ProtectionDecision(True)
        checks = [
            (f"ws:handshake:ip:{ip}", self._policy(
                config.ws_handshake_ip_rate_per_minute,
                config.ws_handshake_ip_burst,
                config.rejection_cooldown_seconds,
            ), 1.0),
            ("ws:handshake:global", self._policy(
                config.ws_handshake_global_rate_per_minute,
                config.ws_handshake_global_burst,
                config.rejection_cooldown_seconds,
            ), 1.0),
        ]
        if account_id:
            checks.append((f"ws:handshake:account:{account_id}", self._policy(
                config.ws_handshake_account_rate_per_minute,
                config.ws_handshake_account_burst,
                config.rejection_cooldown_seconds,
            ), 1.0))
        decision = self.limiter.check_many(checks)
        security_metrics.record(
            "ws_handshake", "allowed" if decision.allowed else "rejected"
        )
        return ProtectionDecision(
            decision.allowed,
            scope="ws_handshake",
            retry_after_seconds=decision.retry_after_seconds,
            action="disconnect",
        )

    def check_message(
        self,
        connection_id: str,
        ip: str,
        account_id: str,
        config,
    ) -> ProtectionDecision:
        if not config.enabled:
            return ProtectionDecision(True)
        checks = [
            (f"ws:message:connection:{connection_id}", self._policy(
                config.ws_message_connection_rate_per_minute,
                config.ws_message_connection_burst,
                config.rejection_cooldown_seconds,
            ), 1.0),
            (f"ws:message:ip:{ip}", self._policy(
                config.ws_message_ip_rate_per_minute,
                config.ws_message_ip_burst,
                config.rejection_cooldown_seconds,
            ), 1.0),
            ("ws:message:global", self._policy(
                config.ws_message_global_rate_per_minute,
                config.ws_message_global_burst,
                config.rejection_cooldown_seconds,
            ), 1.0),
        ]
        if account_id:
            checks.append((f"ws:message:account:{account_id}", self._policy(
                config.ws_message_account_rate_per_minute,
                config.ws_message_account_burst,
                config.rejection_cooldown_seconds,
            ), 1.0))
        decision = self.limiter.check_many(checks)
        security_metrics.record(
            "danmaku_send", "allowed" if decision.allowed else "cooldown"
        )
        return ProtectionDecision(
            decision.allowed,
            scope="danmaku_send",
            retry_after_seconds=decision.retry_after_seconds,
            action="cooldown",
        )


websocket_rate_guard = WebSocketRateGuard()
danmaku_deduplicator = ExpiringDeduplicator()
