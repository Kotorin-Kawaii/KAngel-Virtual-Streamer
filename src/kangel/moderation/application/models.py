"""主播管理系统的受限结构化模型。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


Action = Literal["none", "warning", "timeout", "admin_review"]
AttackType = Literal[
    "none", "personal_attack", "harassment", "spam", "threat",
    "doxxing", "hate", "sexual_harassment", "prompt_injection", "other",
]


class BehaviorAssessment(BaseModel):
    """LLM 只可提交此结构；所有数值和枚举都会在后端再次校验。"""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["none", "warning", "timeout", "admin_review"] = "none"
    toxicity: float = Field(default=0.0, ge=0.0, le=1.0)
    attack_type: AttackType = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    proposed_action: Action = "none"
    reason_code: str = "none"

    @field_validator("reason_code", mode="before")
    @classmethod
    def normalize_reason(cls, value: Any) -> str:
        value = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(value or "none").strip())
        return value[:64] or "none"


class ModerationContext(BaseModel):
    """供 moderation 模型使用的最小上下文快照。"""

    nickname: str
    message: str
    recent_behavior: list[dict[str, Any]] = Field(default_factory=list)
    behavior_state: dict[str, Any] = Field(default_factory=dict)
    viewer_relationship: dict[str, Any] = Field(default_factory=dict)
    direct_context: dict[str, Any] = Field(default_factory=dict)
    stream_context: dict[str, Any] = Field(default_factory=dict)
    persona_state: dict[str, float] = Field(default_factory=dict)
    internal_state: dict[str, float] = Field(default_factory=dict)


class ModerationDecision(BaseModel):
    moderation_id: str
    danmaku_id: str
    subject_key: str
    action: Action
    toxicity: float
    confidence: float
    severity: float
    attack_type: AttackType
    reason_code: str
    mute_until: Optional[str] = None
    reserved: bool = False


class ModerationReaction(BaseModel):
    """主播设界回复的内部结果；不包含原始违规弹幕。"""

    reply_data: dict[str, Any]
    fallback_used: bool = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def message_digest(message: str) -> str:
    """审计只保存摘要，不重复持久化违规原文。"""
    return hashlib.sha256((message or "").encode("utf-8")).hexdigest()[:24]


def parse_json_object(text: str) -> dict[str, Any]:
    """容忍模型包裹 markdown，但不接受自由文本作为决策。"""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned.strip())
    if not isinstance(value, dict):
        raise ValueError("moderation 模型输出必须是 JSON 对象")
    return value
