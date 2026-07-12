"""账号注册、登录、令牌与昵称历史 HTTP Schema。"""

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    nickname: str = Field(min_length=1, max_length=100)

    @field_validator("username", "nickname")
    @classmethod
    def reject_control_characters(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("不能包含控制字符")
        return normalized


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class AccountResponse(BaseModel):
    account_id: str
    username: str
    nickname: str
    nickname_version: int = 1
    created_at: str


class AuthTokenResponse(BaseModel):
    account: AccountResponse
    access_token: str
    token_type: str = "bearer"
    expires_at: str


class RateLimitErrorResponse(BaseModel):
    code: str = "rate_limited"
    message: str
    retry_after_seconds: int = Field(ge=1)
    scope: str
    request_id: str


class NicknameUpdateRequest(BaseModel):
    nickname: str = Field(min_length=1, max_length=100)

    @field_validator("nickname")
    @classmethod
    def validate_nickname(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("昵称不能包含控制字符")
        return normalized


class NicknameHistoryEntry(BaseModel):
    version: int
    nickname: str
    started_at: str
    ended_at: Optional[str] = None
    is_current: bool


class NicknameHistoryResponse(BaseModel):
    account_id: str
    history: list[NicknameHistoryEntry]
