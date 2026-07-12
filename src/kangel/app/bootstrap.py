"""应用装配入口。

P1 阶段继续复用旧 transport 与 infrastructure；后续领域迁移只需替换这里的装配，
而不再把 FastAPI 创建逻辑散落到仓库根目录。
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from kangel.transport.http.routes import RateLimitExceeded, _rate_limit_response, router
from config import settings
from kangel.infrastructure.http_protection import HttpProtectionMiddleware

from .lifecycle import lifespan


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用。"""
    application = FastAPI(
        title=settings.project_name,
        description="支持实时弹幕收发的WebSocket服务",
        version="2.0.0",
        lifespan=lifespan,
    )
    application.include_router(router)
    application.add_middleware(HttpProtectionMiddleware)
    # 最后添加使 CORS 位于最外层：OPTIONS 预检不进入业务限流或路由。
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allowed_origins,
        allow_credentials=settings.cors.allow_credentials,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
        expose_headers=["Retry-After", "X-Request-ID"],
        max_age=settings.cors.max_age_seconds,
    )

    @application.exception_handler(RateLimitExceeded)
    async def rate_limit_exception_handler(request, exc):
        return _rate_limit_response(exc.scope, exc.retry_after_seconds)

    return application


__all__ = ["create_app"]
