"""账号本人可见的人物记忆查询、导出、删除与退出服务。"""

from datetime import datetime, timezone

from config import settings
from kangel.infrastructure.database import db_manager
from .runtime import account_memory_policy
from kangel.integrations.superchat.service import SCService


class AccountMemoryGovernanceService:
    def __init__(self, database=None):
        self._database = database

    @property
    def database(self):
        return self._database or db_manager

    def get_snapshot(self, account_id: str) -> dict:
        self.database.purge_expired_account_relationships(
            account_memory_policy.retention_cutoff().isoformat()
        )
        preference = self.database.get_account_memory_preference(account_id)
        if not preference["long_term_memory_enabled"]:
            # 即使偏好由迁移或内部工具直接关闭，也保持“关闭即无数据”的不变量。
            self.database.delete_account_persona_memory(account_id)
        relationship = (
            self.database.get_account_audience_relationship(account_id)
            if preference["long_term_memory_enabled"] else None
        )
        if relationship and relationship.get("last_message"):
            relationship["last_message"] = (
                account_memory_policy.prepare_text(relationship["last_message"]) or ""
            )
        now = datetime.now(timezone.utc).isoformat()
        self.database.purge_expired_account_long_term_memory(
            now, settings.memory.max_archived_fragments
        )
        fragments = self.database.list_account_conversation_fragments(
            account_id, limit=20
        )
        summaries = self.database.list_account_topic_memories(account_id, limit=20)
        episodic = self.database.list_account_episodic_memories(account_id, limit=100)
        return {
            "account_id": account_id,
            "long_term_memory_enabled": preference["long_term_memory_enabled"],
            "retention_days": settings.memory.retention_days,
            "relationship": relationship,
            "recent_conversations": [self._public_fragment(item) for item in fragments],
            "topic_summaries": [self._public_topic(item) for item in summaries],
            "episodic_memories": [self._public_episodic(item) for item in episodic],
        }

    def export(self, account_id: str) -> dict:
        snapshot = self.get_snapshot(account_id)
        snapshot.update({
            "nickname_history": self.database.list_account_nickname_history(account_id),
            "sc_history": SCService(self.database).list_for_account(account_id, limit=1000),
            "exported_at": datetime.now(timezone.utc).isoformat(),
        })
        all_fragments = self.database.list_account_conversation_fragments(
            account_id,
            limit=settings.memory.max_archived_fragments + settings.memory.recent_fragment_limit,
            include_archived=True,
        )
        snapshot["recent_conversations"] = [
            self._public_fragment(item) for item in all_fragments
        ]
        snapshot["topic_summaries"] = [
            self._public_topic(item)
            for item in self.database.list_account_topic_memories(account_id, limit=1000)
        ]
        snapshot["episodic_memories"] = [
            self._public_episodic(item)
            for item in self.database.list_account_episodic_memories(account_id, limit=1000)
        ]
        snapshot["viewer_impression"] = self.database.list_account_viewer_impression_export(account_id)
        for item in snapshot["nickname_history"]:
            item["is_current"] = bool(item["is_current"])
            item.pop("mention_presented_at", None)
            item.pop("account_id", None)
        return snapshot

    def set_enabled(self, account_id: str, enabled: bool) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        preference = self.database.set_account_memory_preference(
            account_id, enabled, now
        )
        if not enabled:
            self.database.delete_account_persona_memory(account_id)
        return preference

    def delete(self, account_id: str) -> None:
        self.database.delete_account_persona_memory(account_id)

    def _public_fragment(self, item: dict) -> dict:
        return {
            key: item.get(key) for key in (
                "id", "session_scope_id", "danmaku_id", "nickname",
                "nickname_version", "viewer_message", "streamer_reply",
                "reply_payload",
                "topic_label", "transition", "resolved_reference", "sentiment",
                "importance", "created_at", "last_accessed_at", "access_count",
                "archived", "expires_at",
            )
        }

    def _public_topic(self, item: dict) -> dict:
        return {
            key: item.get(key) for key in (
                "id", "topic_label", "summary", "source_count", "importance",
                "first_seen_at", "last_seen_at", "last_accessed_at",
                "access_count", "expires_at",
            )
        }

    def _public_episodic(self, item: dict) -> dict:
        """只导出用户可理解的记忆，不暴露候选、账号 ID 或安全评分。"""
        return {
            key: item.get(key) for key in (
                "memory_id", "stream_session_id", "event_type", "topic", "summary",
                "why_notable", "emotional_mark", "follow_up_hint", "salience",
                "occurred_at", "created_at", "expires_at",
            )
        }


account_memory_governance_service = AccountMemoryGovernanceService()
