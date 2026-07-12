"""普通 HTTP 请求的全局兜底边界。"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Optional

from config import settings
from .rate_limiter import InMemoryRateLimiter, RateLimitPolicy, rate_limiter
from .realtime_protection import resolve_client_ip
from .security_metrics import security_metrics


class HttpProtectionMiddleware:
    def __init__(self, app, limiter: Optional[InMemoryRateLimiter] = None):
        self.app = app
        self.limiter = limiter or rate_limiter

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not settings.rate_limit.enabled:
            await self.app(scope, receive, send)
            return

        config = settings.rate_limit
        headers = {
            key.decode("latin-1").casefold(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        header_bytes = sum(
            len(key) + len(value) for key, value in scope.get("headers", [])
        )
        query_bytes = len(scope.get("query_string", b""))
        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError:
            content_length = config.http_max_body_bytes + 1
        if (
            header_bytes > config.http_max_header_bytes
            or query_bytes > config.http_max_query_bytes
            or content_length > config.http_max_body_bytes
        ):
            await self._respond(send, 413, {
                "code": "request_too_large",
                "message": "请求头、查询参数或请求体超过服务端限制",
            })
            return

        try:
            body = await asyncio.wait_for(
                self._read_body(receive, config.http_max_body_bytes),
                timeout=config.http_body_read_timeout_seconds,
            )
        except _RequestBodyTooLarge:
            await self._respond(send, 413, {
                "code": "request_too_large",
                "message": "请求头、查询参数或请求体超过服务端限制",
            })
            return
        except asyncio.TimeoutError:
            await self._respond(send, 408, {
                "code": "request_timeout",
                "message": "请求体上传超时，请稍后重试",
            })
            return

        peer = scope.get("client") or ("unknown", 0)
        client_ip = resolve_client_ip(
            peer[0], headers.get("x-forwarded-for", ""), config.trusted_proxy_cidrs
        )
        path = scope.get("path", "")
        cheap_read = path in {"/", "/status", "/stream/metadata", "/emotion/list"}
        if not cheap_read:
            # 延迟导入避免应用启动时的模块环；控制面保持可用。
            from .bounded_work_gate import ai_reply_work_gate
            from kangel.transport.websocket.connection_manager import connection_manager
            from .overload_protection import overload_protector
            gate = ai_reply_work_gate.snapshot()
            pressure = overload_protector.snapshot(
                connections=connection_manager.get_connection_count(),
                ai_active=gate["active"], ai_waiting=gate["waiting"],
            )
            overload = overload_protector.admit(expensive=True, snapshot=pressure)
            if not overload.allowed:
                await self._respond(send, 503, {
                    "code": "server_overloaded",
                    "message": "服务器繁忙，请稍后重试",
                    "retry_after_seconds": overload.retry_after_seconds,
                    "scope": "server_overload",
                }, extra_headers=[
                    (b"retry-after", str(overload.retry_after_seconds).encode("ascii")),
                ])
                return
        multiplier = 4 if cheap_read else 1
        decision = self.limiter.check_many([
            (f"http:ip:{client_ip}", RateLimitPolicy(
                config.http_ip_rate_per_minute * multiplier,
                config.http_ip_burst * multiplier,
                config.rejection_cooldown_seconds,
            ), 1.0),
            ("http:global", RateLimitPolicy(
                config.http_global_rate_per_minute * multiplier,
                config.http_global_burst * multiplier,
                config.rejection_cooldown_seconds,
            ), 1.0),
        ])
        if not decision.allowed:
            security_metrics.record("http", "rejected")
            request_id = str(uuid.uuid4())
            await self._respond(send, 429, {
                "code": "rate_limited",
                "message": "请求过于频繁，请稍后再试",
                "retry_after_seconds": decision.retry_after_seconds,
                "scope": "http_global",
                "request_id": request_id,
            }, extra_headers=[
                (b"retry-after", str(decision.retry_after_seconds).encode("ascii")),
                (b"x-request-id", request_id.encode("ascii")),
            ])
            return
        security_metrics.record("http", "allowed")
        response_started = False

        async def replay_receive():
            nonlocal body
            if body is None:
                return {"type": "http.disconnect"}
            payload, body = body, None
            return {"type": "http.request", "body": payload, "more_body": False}

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await asyncio.wait_for(
                self.app(scope, replay_receive, tracked_send),
                timeout=config.http_request_timeout_seconds,
            )
        except asyncio.TimeoutError:
            if not response_started:
                await self._respond(send, 504, {
                    "code": "request_timeout",
                    "message": "服务器处理超时，请稍后重试",
                })

    @staticmethod
    async def _read_body(receive, max_bytes: int) -> bytes:
        chunks = []
        total = 0
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                return b""
            if message_type != "http.request":
                continue
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > max_bytes:
                raise _RequestBodyTooLarge
            if chunk:
                chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    @staticmethod
    async def _respond(send, status: int, body: dict, extra_headers=None):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(payload)).encode("ascii")),
        ] + (extra_headers or [])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})


class _RequestBodyTooLarge(Exception):
    pass
