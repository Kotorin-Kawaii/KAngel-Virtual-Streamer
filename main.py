from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import RateLimitExceeded, _rate_limit_response, router, lifespan
from config import settings
from core.http_protection import HttpProtectionMiddleware


def create_app() -> FastAPI:
    """创建并配置FastAPI应用"""
    app = FastAPI(
        title=settings.project_name,
        description="支持实时弹幕收发的WebSocket服务",
        version="2.0.0",
        lifespan=lifespan
    )
    
    app.include_router(router)
    app.add_middleware(HttpProtectionMiddleware)
    # 最后添加使 CORS 位于最外层：OPTIONS 预检不进入业务限流或路由。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allowed_origins,
        allow_credentials=settings.cors.allow_credentials,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
        expose_headers=["Retry-After", "X-Request-ID"],
        max_age=settings.cors.max_age_seconds,
    )

    @app.exception_handler(RateLimitExceeded)
    async def rate_limit_exception_handler(request, exc):
        return _rate_limit_response(exc.scope, exc.retry_after_seconds)
    
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    from utils.logger import logger
    
    logger.info("🎯 启动虚拟主播弹幕系统...")
    logger.info(f"📡 WebSocket 接口: ws://{settings.server.host}:{settings.server.port}/danmaku")
    logger.info(f"🌐 HTTP 接口: http://{settings.server.host}:{settings.server.port}/")
    logger.info(f"📊 状态接口: http://{settings.server.host}:{settings.server.port}/status")
    
    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload
    )
