"""HTTP/WebSocket 认证令牌解析与 FastAPI 依赖。"""

import asyncio

from fastapi import HTTPException, Request, Response, WebSocket

from config import settings
from kangel.infrastructure.auth import auth_service


def set_auth_cookie(response: Response, auth_result: dict) -> None:
    response.set_cookie(
        key=settings.auth.cookie_name,
        value=auth_result["access_token"],
        max_age=settings.auth.access_token_ttl_hours * 3600,
        httponly=True,
        secure=settings.auth.cookie_secure,
        samesite=settings.auth.cookie_samesite,
        domain=settings.auth.cookie_domain,
        path="/",
    )
    if settings.auth.cookie_partitioned:
        response.headers["set-cookie"] += "; Partitioned"


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
