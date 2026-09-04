"""Account-only, read-only history retrieval for Deep Reflection.

This deliberately does not use the live-memory recall methods: archived but
retained conversations are useful here. SQL projects only allowed fields and
returns at most `limit` rows per category, including when history is very large.
No retrieval counters, access timestamps or relationship facts are changed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database import DatabaseManager


class ImpressionMemoryDisabled(ValueError):
    expected_business_error = True


# All SQL identifiers are fixed here, never supplied by a caller/model.
_CATEGORIES = {
    "conversation_fragments": (
        "account_conversation_fragments", "id", "created_at", "importance",
        "topic_label", "session_scope_id", "",
        "id, viewer_message, streamer_reply, topic_label, transition, "
        "resolved_reference, sentiment, importance, created_at, session_scope_id, archived",
    ),
    "topic_memories": (
        "account_topic_memories", "id", "last_seen_at", "importance",
        "topic_label", "topic_label", "",
        "id, topic_label, summary, source_count, importance, first_seen_at, last_seen_at",
    ),
    "episodic_memories": (
        "stream_episodic_memories", "memory_id", "occurred_at", "salience",
        "topic", "stream_session_id", "AND scope = 'account' AND archived = 0",
        "memory_id, event_type, topic, summary, why_notable, emotional_mark, "
        "follow_up_hint, salience, occurred_at",
    ),
}


class ImpressionCandidateReader:
    def __init__(self, database: DatabaseManager):
        self.database = database

    @staticmethod
    def _history(conn, category: str, account_id: str, cutoff: str,
                 previous_cutoff: str | None, limit: int) -> list[dict[str, Any]]:
        table, key, timestamp, score, topic, session, extra, fields = _CATEGORIES[category]
        # Six balanced rankings: time strata, importance, recent delta, distinct
        # sessions, distinct topics, and recency. The oldest and newest retained
        # events are anchors. A low-score historical event cannot be pushed out
        # merely by hundreds of recent messages. If count <= cap, all rows win.
        rows = conn.execute(f"""
            WITH eligible AS (
                SELECT {fields}, {session} AS diversity_session,
                       {topic} AS diversity_topic,
                       julianday({timestamp}) AS event_time
                FROM {table}
                WHERE account_id = ? AND julianday(expires_at) > julianday(?)
                  AND julianday({timestamp}) <= julianday(?) {extra}
            ), strata AS (
                SELECT *, ntile(?) OVER (ORDER BY event_time, {key}) AS bucket
                FROM eligible
            ), ranked AS (
                SELECT *,
                    row_number() OVER (ORDER BY event_time, {key}) AS oldest,
                    row_number() OVER (ORDER BY event_time DESC, {key}) AS newest,
                    row_number() OVER (ORDER BY {score} DESC, event_time DESC, {key}) AS important,
                    row_number() OVER (
                        ORDER BY CASE WHEN event_time > julianday(?) THEN 0 ELSE 1 END,
                                 {score} DESC, event_time DESC, {key}
                    ) AS delta_rank,
                    row_number() OVER (
                        PARTITION BY bucket ORDER BY {score} DESC, event_time, {key}
                    ) AS bucket_rank,
                    row_number() OVER (
                        PARTITION BY diversity_session ORDER BY {score} DESC, event_time, {key}
                    ) AS session_rank,
                    row_number() OVER (
                        PARTITION BY diversity_topic ORDER BY {score} DESC, event_time, {key}
                    ) AS topic_rank
                FROM strata
            ), diverse AS (
                SELECT *,
                    row_number() OVER (ORDER BY bucket_rank, bucket, {key}) AS span_rank,
                    row_number() OVER (ORDER BY session_rank, diversity_session, {key}) AS session_order,
                    row_number() OVER (ORDER BY topic_rank, diversity_topic, {key}) AS topic_order
                FROM ranked
            )
            SELECT {fields} FROM diverse
            ORDER BY CASE WHEN oldest = 1 THEN -2 WHEN newest = 1 THEN -1
                ELSE min(span_rank * 6, important * 6 + 1, delta_rank * 6 + 2,
                         session_order * 6 + 3, topic_order * 6 + 4, newest * 6 + 5) END,
                event_time, {key}
            LIMIT ?
        """, (account_id, cutoff, cutoff, limit, previous_cutoff or cutoff, limit)).fetchall()
        return sorted((dict(row) for row in rows), key=lambda row: (row[timestamp], str(row[key])))

    def read(self, account_id: str, *, cutoff: str, fragment_limit: int = 500,
             topic_limit: int = 100, episodic_limit: int = 100,
             nickname_limit: int = 50) -> dict[str, Any]:
        limits = (fragment_limit, topic_limit, episodic_limit, nickname_limit)
        if any(type(value) is not int or value < 1 or value > cap
               for value, cap in zip(limits, (2000, 2000, 2000, 500))):
            raise ValueError("invalid candidate limits")
        with self.database._get_connection() as conn:
            # One SQLite read snapshot across preference, history and cutoff.
            # Task insertion performs the authoritative opt-in check again.
            conn.execute("BEGIN")
            preference = conn.execute(
                "SELECT long_term_memory_enabled FROM account_memory_preferences WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if not preference or not preference[0]:
                raise ImpressionMemoryDisabled("memory_disabled")
            previous = conn.execute(
                "SELECT evidence_cutoff_at FROM account_viewer_impressions WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            previous_cutoff = previous[0] if previous else None
            result: dict[str, Any] = {"previous_cutoff_at": previous_cutoff}
            epoch = conn.execute("SELECT epoch FROM account_viewer_impression_epochs WHERE account_id = ?", (account_id,)).fetchone()
            result["privacy_epoch"] = int(epoch[0] if epoch else 0)
            for category, limit in zip(_CATEGORIES, limits):
                result[category] = self._history(conn, category, account_id, cutoff, previous_cutoff, limit)
            row = conn.execute("""
                SELECT first_seen_at, last_seen_at, interaction_count, reply_count, recent_topics
                FROM account_audience_relationships WHERE account_id = ?
            """, (account_id,)).fetchone()
            relationship = dict(row) if row else {}
            try:
                recent_topics = json.loads(relationship.get("recent_topics") or "[]")
            except (TypeError, ValueError):
                recent_topics = []
            relationship["recent_topics"] = [v for v in recent_topics[:50] if isinstance(v, str)] if isinstance(recent_topics, list) else []
            result["relationship"] = relationship
            # Rank across retained versions, not the last N names. Physically
            # deleted versions cannot be reconstructed from fragment nicknames.
            names = conn.execute("""
                WITH history AS (
                    SELECT version, nickname, started_at, ended_at, is_current,
                           ntile(?) OVER (ORDER BY version) AS bucket
                    FROM account_nickname_history
                    WHERE account_id = ? AND julianday(started_at) <= julianday(?)
                ), ranked AS (
                    SELECT *, row_number() OVER (
                        PARTITION BY bucket ORDER BY is_current DESC, version
                    ) AS position
                    FROM history
                )
                SELECT version, nickname, started_at, ended_at, is_current FROM ranked
                ORDER BY position, is_current DESC, bucket LIMIT ?
            """, (nickname_limit, account_id, cutoff, nickname_limit)).fetchall()
            result["nickname_history"] = [dict(row) for row in names]
            return result
