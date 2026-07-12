"""记忆、SC、表情和弹幕的纯 HTTP/WS 传输 Schema。"""

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class MemoryPreferenceUpdateRequest(BaseModel):
    long_term_memory_enabled: bool


class MemoryPreferenceResponse(BaseModel):
    account_id: str
    long_term_memory_enabled: bool
    updated_at: Optional[str] = None


class AccountMemoryResponse(BaseModel):
    account_id: str
    long_term_memory_enabled: bool
    retention_days: int
    relationship: Optional[dict[str, Any]] = None
    recent_conversations: list[dict[str, Any]] = Field(default_factory=list)
    topic_summaries: list[dict[str, Any]] = Field(default_factory=list)


class AccountMemoryExportResponse(AccountMemoryResponse):
    nickname_history: list[dict[str, Any]]
    sc_history: list[dict[str, Any]] = Field(default_factory=list)
    exported_at: str


_SC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


class SCSubmitRequest(BaseModel):
    sc_id: str = Field(min_length=8, max_length=128)
    content: str = Field(min_length=1, max_length=5000)

    @field_validator("sc_id")
    @classmethod
    def validate_sc_id(cls, value: str) -> str:
        value = value.strip()
        if not _SC_ID.fullmatch(value):
            raise ValueError("sc_id 只能包含字母、数字、下划线和连字符")
        return value

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        value = " ".join(value.strip().split())
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("SC 内容不能为空或包含控制字符")
        return value


class SCStatusResponse(BaseModel):
    sc_id: str
    status: Literal["accepted", "pending", "processing", "replied", "rejected", "failed"]
    nickname: str
    content: str
    accepted_at: str
    queue_position: Optional[int] = None
    retry_after_seconds: Optional[int] = None
    next_submit_at: Optional[str] = None
    failure_code: Optional[str] = None
    processing_started_at: Optional[str] = None
    completed_at: Optional[str] = None
    estimated_wait_seconds: Optional[int] = None
    reply: Optional[dict[str, Any]] = None


class SCSubmitResponse(SCStatusResponse):
    pass


class SCConfigResponse(BaseModel):
    cooldown_seconds: int
    max_content_chars: int
    max_content_bytes: int


class EmoteConfigResponse(BaseModel):
    allowed_ids: list[str]
    cooldown_seconds: int


class DanmakuResponse(BaseModel):
    nickname: str
    message: str
    type: str
    timestamp: str
    danmakuID: str


class DanmakuBroadcast(BaseModel):
    type: str = "danmaku_realtime"
    data: DanmakuResponse
