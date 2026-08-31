"""Kangel 的规范应用与命令行入口。"""

from .app.bootstrap import create_app

app = create_app()


def run() -> None:
    """使用配置启动 Uvicorn。"""
    import uvicorn

    from config import settings
    from kangel.shared.logging import logger

    logger.info("启动虚拟主播弹幕系统...")
    logger.info(
        "WebSocket 接口: ws://%s:%s/danmaku",
        settings.server.host,
        settings.server.port,
    )
    logger.info(
        "HTTP 接口: http://%s:%s/",
        settings.server.host,
        settings.server.port,
    )
    logger.info(
        "状态接口: http://%s:%s/status",
        settings.server.host,
        settings.server.port,
    )
    uvicorn.run(
        "kangel.main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
    )


__all__ = ["app", "create_app", "run"]
