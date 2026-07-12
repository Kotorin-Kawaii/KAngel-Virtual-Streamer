"""持久化的观众关系记忆。"""

import asyncio
from datetime import datetime, timezone
from typing import Any, List, Optional

from kangel.infrastructure.database import db_manager
from ..domain.identity import ViewerIdentity, ViewerIdentityType
from ..domain.relationship import AudienceRelationship
from kangel.memory.application.runtime import account_memory_policy


class AudienceRelationshipManager:
    """按身份类型路由账号持久关系、游客临时关系和旧版昵称关系。"""

    _boundary_words = (
        "垃圾", "废物", "恶心", "去死", "闭嘴", "滚", "傻逼", "骗子"
    )

    def __init__(self, database=None):
        self._lock = asyncio.Lock()
        self._database = database
        self._guest_relationships: dict[str, AudienceRelationship] = {}

    @property
    def database(self):
        return self._database or db_manager

    def normalize_viewer_key(self, nickname: str) -> str:
        normalized = " ".join((nickname or "匿名宅宅").strip().casefold().split())
        return normalized or "匿名宅宅"

    async def get(
        self,
        nickname: str,
        identity: Optional[ViewerIdentity] = None,
    ) -> AudienceRelationship:
        if identity is not None:
            if identity.identity_type == ViewerIdentityType.GUEST:
                stored_guest = self._guest_relationships.get(identity.subject_id)
                if stored_guest:
                    return stored_guest.model_copy(deep=True)
                return AudienceRelationship(
                    viewer_key=identity.subject_id,
                    nickname=identity.current_nickname,
                )

            stored_account = self.database.get_account_audience_relationship(
                identity.account_id
            )
            preference = self.database.get_account_memory_preference(identity.account_id)
            if not preference["long_term_memory_enabled"]:
                stored_account = None
            if stored_account and account_memory_policy.is_expired(
                stored_account.get("last_seen_at", "")
            ):
                self.database.delete_account_persona_memory(identity.account_id)
                stored_account = None
            if stored_account:
                return AudienceRelationship(**stored_account)
            return AudienceRelationship(
                viewer_key=identity.subject_id,
                nickname=identity.current_nickname,
            )

        # 仅为旧调用保留兼容路径；不得自动迁移到账号关系。
        viewer_key = self.normalize_viewer_key(nickname)
        stored = self.database.get_audience_relationship(viewer_key)
        if stored:
            return AudienceRelationship(**stored)
        return AudienceRelationship(viewer_key=viewer_key, nickname=nickname or "匿名宅宅")

    async def observe_danmaku(
        self,
        nickname: str,
        message: str,
        sentiment: float = 0.0,
        topics: Optional[List[str]] = None,
        identity: Optional[ViewerIdentity] = None,
    ) -> AudienceRelationship:
        async with self._lock:
            relationship = await self.get(nickname, identity=identity)
            relationship.nickname = nickname or relationship.nickname
            relationship.interaction_count += 1
            relationship.familiarity = self._clamp(
                relationship.familiarity + 0.025 * (1.0 - relationship.familiarity)
            )
            relationship.affinity = self._clamp(
                relationship.affinity + max(-0.035, min(0.025, sentiment * 0.025))
            )
            relationship.trust = self._clamp(
                relationship.trust + (
                    0.008 if sentiment > 0.35 else -0.015 if sentiment < -0.35 else 0.001
                )
            )
            if sentiment < -0.35 and any(word in message for word in self._boundary_words):
                relationship.boundary_strikes += 1

            safe_topics = [
                safe for topic in (topics or [])
                if (safe := account_memory_policy.prepare_text(str(topic)))
            ]
            merged_topics = safe_topics + relationship.recent_topics
            relationship.recent_topics = list(dict.fromkeys(merged_topics))[:8]
            safe_message = account_memory_policy.prepare_text(message)
            if safe_message is not None:
                relationship.last_message = safe_message
            relationship.last_seen_at = datetime.now(timezone.utc).isoformat()
            self._save(relationship, identity)
            return relationship

    async def record_reply(
        self,
        nickname: str,
        analysis: Any = None,
        identity: Optional[ViewerIdentity] = None,
        conversation_transition: Optional[str] = None,
    ) -> AudienceRelationship:
        async with self._lock:
            relationship = await self.get(nickname, identity=identity)
            relationship.reply_count += 1
            relationship.familiarity = self._clamp(
                relationship.familiarity + (
                    0.024 if conversation_transition in {
                        "continuation", "contrast", "supplement"
                    } else 0.018
                ) * (1.0 - relationship.familiarity)
            )
            tone = getattr(analysis, "emotional_tone", "neutral")
            if tone == "positive":
                relationship.affinity = self._clamp(relationship.affinity + 0.006)
            elif tone == "negative":
                relationship.affinity = self._clamp(relationship.affinity - 0.004)
            if conversation_transition in {"continuation", "contrast", "supplement"}:
                relationship.trust = self._clamp(relationship.trust + 0.003)
            relationship.last_seen_at = datetime.now(timezone.utc).isoformat()
            self._save(relationship, identity)
            return relationship

    async def forget_guest(self, identity: Optional[ViewerIdentity]) -> None:
        if not identity or identity.identity_type != ViewerIdentityType.GUEST:
            return
        async with self._lock:
            self._guest_relationships.pop(identity.subject_id, None)

    def _save(
        self,
        relationship: AudienceRelationship,
        identity: Optional[ViewerIdentity],
    ) -> None:
        if identity is None:
            self.database.upsert_audience_relationship(relationship.model_dump())
        elif identity.identity_type == ViewerIdentityType.GUEST:
            self._guest_relationships[identity.subject_id] = relationship.model_copy(deep=True)
        else:
            preference = self.database.get_account_memory_preference(identity.account_id)
            if not preference["long_term_memory_enabled"]:
                return
            payload = relationship.model_dump()
            payload["account_id"] = identity.account_id
            self.database.upsert_account_audience_relationship(payload)

    def _clamp(self, value: float) -> float:
        return max(0.0, min(1.0, value))


audience_relationship_manager = AudienceRelationshipManager()
