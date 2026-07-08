"""连接级身份解析与服务端信任边界。"""

import re
from dataclasses import dataclass
from typing import Optional

from models.viewer import ViewerIdentity, ViewerIdentityType


_OPAQUE_ID_PATTERN = re.compile(r"^[^\s\x00-\x1f\x7f]{1,128}$")


def _validate_opaque_id(value: str, field_name: str) -> str:
    normalized = (value or "").strip()
    if not _OPAQUE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field_name} 必须是 1-128 位且不包含空白或控制字符")
    return normalized


def _normalize_nickname(nickname: str) -> str:
    normalized = " ".join((nickname or "匿名宅宅").strip().split())
    return normalized[:100] or "匿名宅宅"


@dataclass(frozen=True)
class VerifiedAccountPrincipal:
    """
    上层认证成功后交给连接层的可信主体。

    弹幕消息中的任意 account_id 字段都不能构造该对象；未来认证中间件应在
    校验 token/session 后调用 from_authentication。
    """

    account_id: str
    issuer: str
    nickname: str = "匿名宅宅"
    nickname_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _validate_opaque_id(self.account_id, "account_id"))
        object.__setattr__(self, "issuer", _validate_opaque_id(self.issuer, "issuer"))
        object.__setattr__(self, "nickname", _normalize_nickname(self.nickname))
        if self.nickname_version < 1:
            raise ValueError("nickname_version 必须大于等于 1")

    @classmethod
    def from_authentication(
        cls, account_id: str, issuer: str, nickname: str = "匿名宅宅",
        nickname_version: int = 1,
    ) -> "VerifiedAccountPrincipal":
        return cls(
            account_id=_validate_opaque_id(account_id, "account_id"),
            issuer=_validate_opaque_id(issuer, "issuer"),
            nickname=_normalize_nickname(nickname),
            nickname_version=nickname_version,
        )


class ViewerIdentityResolver:
    """把可信认证主体或连接 ID 转换为统一内部身份。"""

    def resolve_for_connection(
        self,
        *,
        connection_id: str,
        nickname: str,
        principal: Optional[VerifiedAccountPrincipal] = None,
    ) -> ViewerIdentity:
        connection_id = _validate_opaque_id(connection_id, "connection_id")
        if principal is not None:
            if not isinstance(principal, VerifiedAccountPrincipal):
                raise TypeError("登录身份只能来自 VerifiedAccountPrincipal")
            account_id = _validate_opaque_id(principal.account_id, "account_id")
            display_name = principal.nickname
            return ViewerIdentity(
                identity_type=ViewerIdentityType.AUTHENTICATED,
                subject_id=f"account:{account_id}",
                account_id=account_id,
                nickname_version=principal.nickname_version,
                current_nickname=display_name,
                session_scope_id=connection_id,
            )
        display_name = _normalize_nickname(nickname)
        return ViewerIdentity(
            identity_type=ViewerIdentityType.GUEST,
            subject_id=f"guest:{connection_id}",
            guest_id=connection_id,
            current_nickname=display_name,
            session_scope_id=connection_id,
        )


viewer_identity_resolver = ViewerIdentityResolver()
