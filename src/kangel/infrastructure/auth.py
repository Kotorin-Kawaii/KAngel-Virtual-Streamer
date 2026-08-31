"""基于 scrypt 密码哈希和不透明会话令牌的本地账号服务。"""

import base64
import hashlib
import hmac
import secrets
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import settings
from .database import db_manager
from kangel.audience.application.identity_service import VerifiedAccountPrincipal


class UsernameAlreadyExistsError(ValueError):
    pass


class InvalidCredentialsError(ValueError):
    pass


class InvalidRefreshTokenError(ValueError):
    pass


class AuthService:
    def __init__(self, database=None):
        self.database = database or db_manager

    def register(self, username: str, password: str, nickname: str) -> dict:
        username_display, username_key = self._normalize_username(username)
        self._validate_password(password)
        nickname = self._normalize_nickname(nickname)
        salt = secrets.token_bytes(16)
        now = self._now()
        account = {
            "account_id": str(uuid.uuid4()),
            "username_key": username_key,
            "username": username_display,
            "password_salt": base64.b64encode(salt).decode("ascii"),
            "password_hash": self._hash_password(password, salt),
            "nickname": nickname,
            "account_type": "regular",
            "login_enabled": True,
            "created_at": now,
        }
        try:
            stored = self.database.create_account(account)
        except sqlite3.IntegrityError as exc:
            raise UsernameAlreadyExistsError("用户名已存在") from exc
        return self._issue_token_pair(stored)

    def login(self, username: str, password: str) -> dict:
        _, username_key = self._normalize_username(username)
        account = self.database.get_account_by_username_key(username_key)
        if not account or not bool(account.get("login_enabled", 1)):
            # 对不存在账号执行一次等成本哈希，降低用户名枚举时序差异。
            self._hash_password(password, bytes(16))
            raise InvalidCredentialsError("用户名或密码错误")
        salt = base64.b64decode(account["password_salt"])
        actual_hash = self._hash_password(password, salt)
        if not hmac.compare_digest(actual_hash, account["password_hash"]):
            raise InvalidCredentialsError("用户名或密码错误")
        return self._issue_token_pair(account)

    def authenticate_access_token(
        self, access_token: str
    ) -> Optional[VerifiedAccountPrincipal]:
        if not access_token or len(access_token) > 512:
            return None
        token_hash = self._hash_token(access_token)
        session = self.database.get_active_auth_session(token_hash, self._now())
        if not session:
            return None
        return VerifiedAccountPrincipal.from_authentication(
            account_id=session["account_id"],
            issuer="local-account",
            nickname=session["nickname"],
            nickname_version=session["nickname_version"],
        )

    def refresh(self, refresh_token: str) -> dict:
        """轮换 refresh token，并为同一账号签发新的短期访问会话。"""
        if not refresh_token or len(refresh_token) > 512:
            raise InvalidRefreshTokenError("刷新令牌无效或已过期")
        created_at = self._now_datetime()
        access_token = secrets.token_urlsafe(32)
        next_refresh_token = secrets.token_urlsafe(48)
        access_expires_at = created_at + timedelta(
            hours=settings.auth.access_token_ttl_hours
        )
        refresh_expires_at = created_at + timedelta(
            hours=settings.auth.refresh_token_ttl_hours
        )
        account = self.database.rotate_auth_refresh_session(
            current_token_hash=self._hash_token(refresh_token),
            new_access_session={
                "token_hash": self._hash_token(access_token),
                "created_at": created_at.isoformat(),
                "expires_at": access_expires_at.isoformat(),
            },
            new_refresh_session={
                "token_hash": self._hash_token(next_refresh_token),
                "created_at": created_at.isoformat(),
                "expires_at": refresh_expires_at.isoformat(),
            },
            now=created_at.isoformat(),
        )
        if not account:
            raise InvalidRefreshTokenError("刷新令牌无效或已过期")
        return {
            "account": self._account_payload(account),
            "access_token": access_token,
            "refresh_token": next_refresh_token,
            "token_type": "bearer",
            "expires_at": access_expires_at.isoformat(),
        }

    def update_nickname(self, account_id: str, nickname: str) -> dict:
        normalized = self._normalize_nickname(nickname)
        account = self.database.update_account_nickname(
            account_id, normalized, self._now()
        )
        if not account:
            raise InvalidCredentialsError("账号不存在")
        return self._account_payload(account)

    def get_account(self, account_id: str) -> Optional[dict]:
        """返回已认证账号的当前展示资料，不暴露令牌或会话信息。"""
        account = self.database.get_account_by_id(account_id)
        return self._account_payload(account) if account else None

    def list_nickname_history(self, account_id: str) -> list[dict]:
        return [
            {
                "version": row["version"],
                "nickname": row["nickname"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "is_current": bool(row["is_current"]),
            }
            for row in self.database.list_account_nickname_history(account_id)
        ]

    def delete_nickname_history(self, account_id: str, version: int) -> bool:
        return self.database.delete_account_nickname_history_version(account_id, version)

    def _issue_token_pair(self, account: dict) -> dict:
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(48)
        created_at = self._now_datetime()
        expires_at = created_at + timedelta(hours=settings.auth.access_token_ttl_hours)
        refresh_expires_at = created_at + timedelta(
            hours=settings.auth.refresh_token_ttl_hours
        )
        self.database.create_auth_session({
            "token_hash": self._hash_token(access_token),
            "account_id": account["account_id"],
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        })
        self.database.create_auth_refresh_session({
            "token_hash": self._hash_token(refresh_token),
            "account_id": account["account_id"],
            "created_at": created_at.isoformat(),
            "expires_at": refresh_expires_at.isoformat(),
        })
        return {
            "account": self._account_payload(account),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_at": expires_at.isoformat(),
        }

    def _account_payload(self, account: dict) -> dict:
        history = self.database.list_account_nickname_history(account["account_id"])
        current = next((row for row in history if row["is_current"]), None)
        return {
            "account_id": account["account_id"],
            "username": account["username"],
            "nickname": account["nickname"],
            "nickname_version": current["version"] if current else 1,
            "created_at": account["created_at"],
        }

    def _normalize_username(self, username: str) -> tuple[str, str]:
        display = unicodedata.normalize("NFKC", username or "").strip()
        if not 3 <= len(display) <= 64 or any(char.isspace() or ord(char) < 32 for char in display):
            raise ValueError("用户名必须为 3-64 位且不能包含空白或控制字符")
        return display, display.casefold()

    def _normalize_nickname(self, nickname: str) -> str:
        normalized = " ".join(unicodedata.normalize("NFKC", nickname or "").strip().split())
        if not normalized or len(normalized) > 100:
            raise ValueError("昵称必须为 1-100 位")
        return normalized

    def _validate_password(self, password: str) -> None:
        if not settings.auth.min_password_length <= len(password) <= 128:
            raise ValueError(
                f"密码长度必须为 {settings.auth.min_password_length}-128 位"
            )

    def _hash_password(self, password: str, salt: bytes) -> str:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        return base64.b64encode(digest).decode("ascii")

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _now_datetime(self) -> datetime:
        return datetime.now(timezone.utc)

    def _now(self) -> str:
        return self._now_datetime().isoformat()


auth_service = AuthService()
