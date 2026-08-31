"""HTTP/WebSocket 认证令牌解析与 FastAPI 依赖。"""

import asyncio

from fastapi import HTTPException, Request, Response, WebSocket

from config import settings
from kangel.infrastructure.auth import auth_service


def _build_set_cookie_header(
    name: str, value: str, *, max_age: int, path: str
) -> str:
    """手动构建 Set-Cookie 头，确保 Partitioned 等属性可靠附加。

    Starlette 0.27 的 set_cookie 不支持 partitioned 参数，
    且 raw_headers 在 set_cookie 后未必包含 Set-Cookie 条目，
    因此旧 _append_partitioned 方案会静默丢弃 Partitioned。
    """
    parts = [f"{name}={value}", f"Path={path}", f"Max-Age={max_age}", "HttpOnly"]
    if settings.auth.cookie_secure:
        parts.append("Secure")
    if settings.auth.cookie_samesite:
        parts.append(f"SameSite={settings.auth.cookie_samesite}")
    if settings.auth.cookie_domain:
        parts.append(f"Domain={settings.auth.cookie_domain}")
    if settings.auth.cookie_partitioned:
        parts.append("Partitioned")
    return "; ".join(parts)


def set_auth_cookie(response: Response, auth_result: dict) -> None:
    response.headers.append(
        "set-cookie",
        _build_set_cookie_header(
            settings.auth.cookie_name,
            auth_result["access_token"],
            max_age=settings.auth.access_token_ttl_hours * 3600,
            path="/",
        ),
    )
    refresh_token = auth_result.get("refresh_token")
    if not refresh_token:
        return
    response.headers.append(
        "set-cookie",
        _build_set_cookie_header(
            settings.auth.refresh_cookie_name,
            refresh_token,
            max_age=settings.auth.refresh_token_ttl_hours * 3600,
            path="/auth/refresh",
        ),
    )


def http_access_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.casefold() == "bearer" and token:
        return token
    return request.cookies.get(settings.auth.cookie_name, "")


async def require_http_principal(request: Request, authentication_service=auth_service):
    principal = await asyncio.to_thread(
        authentication_service.authenticate_access_token, http_access_token(request)
    )
    if principal is None:
        raise HTTPException(status_code=401, detail="访问令牌无效或已过期")
    return principal


def websocket_access_token(websocket: WebSocket, query_token: str = None) -> str:
    if query_token:
        return query_token
    cookie_token = websocket.cookies.get(settings.auth.cookie_name)
    if cookie_token:
        return cookie_token
    authorization = websocket.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    return token if scheme.casefold() == "bearer" else ""
