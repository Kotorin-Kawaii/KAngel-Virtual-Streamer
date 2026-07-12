"""WebSocket 连接与前端事件协议。"""

from .connection_manager import ConnectionManager, WebSocketConnection, connection_manager
from .protocol import WebSocketEventType

__all__ = [
    "ConnectionManager", "WebSocketConnection", "WebSocketEventType",
    "connection_manager",
]
