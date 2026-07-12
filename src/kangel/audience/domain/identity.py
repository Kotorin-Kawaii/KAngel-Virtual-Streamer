"""统一的登录用户与游客身份模型。"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class ViewerIdentityType(str, Enum):
    AUTHENTICATED = "authenticated"
    GUEST = "guest"


class ViewerIdentity(BaseModel):
    """内部身份对象；展示昵称永远不是身份主键。"""

    identity_type: ViewerIdentityType
    subject_id: str = Field(min_length=1, max_length=160)
    current_nickname: str = Field(min_length=1, max_length=100)
    session_scope_id: str = Field(min_length=1, max_length=160)
    account_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    nickname_version: Optional[int] = Field(default=None, ge=1)
    guest_id: Optional[str] = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_identity_shape(self) -> "ViewerIdentity":
        if self.identity_type == ViewerIdentityType.AUTHENTICATED:
            if not self.account_id or self.guest_id is not None:
                raise ValueError("登录身份必须且只能包含 account_id")
            if self.subject_id != f"account:{self.account_id}":
                raise ValueError("登录身份 subject_id 与 account_id 不一致")
        elif self.identity_type == ViewerIdentityType.GUEST:
            if not self.guest_id or self.account_id is not None or self.nickname_version is not None:
                raise ValueError("游客身份必须且只能包含 guest_id")
            if self.subject_id != f"guest:{self.guest_id}":
                raise ValueError("游客身份 subject_id 与 guest_id 不一致")
        return self

    @property
    def is_authenticated(self) -> bool:
        return self.identity_type == ViewerIdentityType.AUTHENTICATED

    def with_nickname(self, nickname: str) -> "ViewerIdentity":
        """只更新展示昵称，不改变稳定身份。"""
        normalized = " ".join((nickname or "匿名宅宅").strip().split())
        return self.model_copy(update={"current_nickname": normalized[:100] or "匿名宅宅"})
