"""账号人物记忆治理接口模型。"""

from typing import Any, Optional

from pydantic import BaseModel, Field


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
