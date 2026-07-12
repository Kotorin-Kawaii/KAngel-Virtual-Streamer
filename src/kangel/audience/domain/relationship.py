"""观众关系领域模型。"""

from datetime import datetime, timezone
from typing import List

from pydantic import BaseModel, Field


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AudienceRelationship(BaseModel):
    viewer_key: str
    nickname: str
    familiarity: float = Field(default=0.05, ge=0.0, le=1.0)
    affinity: float = Field(default=0.5, ge=0.0, le=1.0)
    trust: float = Field(default=0.5, ge=0.0, le=1.0)
    boundary_strikes: int = Field(default=0, ge=0)
    interaction_count: int = Field(default=0, ge=0)
    reply_count: int = Field(default=0, ge=0)
    recent_topics: List[str] = Field(default_factory=list)
    last_message: str = ""
    first_seen_at: str = Field(default_factory=utc_iso)
    last_seen_at: str = Field(default_factory=utc_iso)


__all__ = ["AudienceRelationship"]
