"""服务状态与配置 HTTP Schema。"""

from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class ServerStatus(BaseModel):
    """服务器状态"""
    status: str
    active_connections: int
    message_history_count: int
    server_time: str


class RootResponse(BaseModel):
    """根路径响应"""
    status: str
    connections: int
    history_count: int


class ConfigUpdateRequest(BaseModel):
    """配置更新请求"""
    key: str
    value: dict


class ConfigResponse(BaseModel):
    """配置响应"""
    success: bool
    config: Optional[dict] = None
    message: Optional[str] = None
