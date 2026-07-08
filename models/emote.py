"""观众表情公开配置契约。"""

from pydantic import BaseModel


class EmoteConfigResponse(BaseModel):
    allowed_ids: list[str]
    cooldown_seconds: int
