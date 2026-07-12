from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class DanmakuMessage(BaseModel):
    """弹幕消息数据模型"""
    nickname: str = Field(..., description="用户昵称")
    message: str = Field(..., description="弹幕内容")
    type: str = Field(default="normal", description="消息类型")
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat(), description="时间戳")


class DanmakuResponse(BaseModel):
    """弹幕响应数据模型"""
    nickname: str
    message: str
    type: str
    timestamp: str
    danmakuID: str


class DanmakuBroadcast(BaseModel):
    """弹幕广播数据"""
    type: str = "danmaku_realtime"
    data: DanmakuResponse


class HistoryBatch(BaseModel):
    """历史弹幕批量推送"""
    type: str = "history_batch"
    messages: list[dict]
    count: int


class Confirmation(BaseModel):
    """确认消息"""
    type: str = "confirmation"
    message: str
    timestamp: str


class ErrorResponse(BaseModel):
    """错误响应"""
    type: str = "error"
    message: str
