"""观众表情领域决策与公开配置契约。"""

from dataclasses import dataclass
from pydantic import BaseModel


@dataclass(frozen=True)
class EmoteDecision:
    allowed: bool
    code: str = ""
    retry_after_seconds: int = 0
    payload: dict | None = None


class EmoteConfigResponse(BaseModel):
    allowed_ids: list[str]
    cooldown_seconds: int


__all__ = ["EmoteConfigResponse", "EmoteDecision"]
