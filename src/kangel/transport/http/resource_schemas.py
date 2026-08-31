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
    episodic_memories: list[dict[str, Any]] = Field(default_factory=list)


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


class SponsorConfigResponse(BaseModel):
    """页面底部赞助入口的展示元数据；不含任何平台凭据。"""

    enabled: bool
    list_enabled: bool
    platform_name: str
    platform_url: str
    notice_text: str


class SponsorEntry(BaseModel):
    display_name: str


class SponsorListResponse(BaseModel):
    """感谢墙：仅昵称，无排序，无金额，无平台 ID。"""

    enabled: bool
    total_count: int
    updated_at: Optional[str] = None
    sponsors: list[SponsorEntry]


class SponsorSyncStatsResponse(BaseModel):
    """仅管理端可见的同步健康度；不返回凭据与单人金额。"""

    enabled: bool
    sync_enabled: bool
    credentials_configured: bool
    sponsor_count: int
    hidden_count: int
    anonymous_count: int
    synced_count: int
    consecutive_failures: int
    last_success_at: Optional[str] = None
    last_attempt_at: Optional[str] = None
    last_error_code: Optional[str] = None


class DanmakuResponse(BaseModel):
    nickname: str
    message: str
    type: str
    timestamp: str
    danmakuID: str


class DanmakuBroadcast(BaseModel):
    type: str = "danmaku_realtime"
    data: DanmakuResponse
