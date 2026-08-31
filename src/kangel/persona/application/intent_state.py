"""P22 心智状态存储服务；不调用 AI，也不拥有活动/人格事实写权限。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from kangel.infrastructure.database import DatabaseManager
from kangel.persona.domain.intent import (
    InteractionMode, PrimaryIntent, StreamerIntentState,
)


class StreamerIntentStateService:
    """场次级短时意图，使用乐观版本避免并发回复互相覆盖。"""

    def __init__(self, database: DatabaseManager, ttl_minutes: int = 20):
        self.database = database
        self.ttl_minutes = max(1, min(int(ttl_minutes), 240))

    def get_or_create(
        self, stream_session_id: str, *, now: Optional[datetime] = None
    ) -> StreamerIntentState:
        reference = self._reference(now)
        updated_at, expires_at = self._timestamps(reference)
        with self.database._get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO streamer_intent_states (
                    stream_session_id, interaction_mode, primary_intent, energy_level,
                    attention_target, current_beat, next_beat_hint, last_callback,
                    updated_at, expires_at, version
                ) VALUES (?, 'answer', 'answer', 0.5, 'room', 'open', '', '', ?, ?, 1)
            """, (stream_session_id, updated_at, expires_at))
            row = conn.execute(
                "SELECT * FROM streamer_intent_states WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
        return self._from_row(row)

    def get_active(
        self, stream_session_id: str, *, now: Optional[datetime] = None
    ) -> StreamerIntentState | None:
        reference = self._reference(now)
        with self.database._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM streamer_intent_states WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"]) <= reference:
            return None
        return self._from_row(row)

    def commit_after_reply(
        self,
        state: StreamerIntentState,
        *,
        interaction_mode: InteractionMode,
        primary_intent: PrimaryIntent,
        energy_level: float,
        attention_target: str,
        current_beat: str,
        next_beat_hint: str = "",
        last_callback: str = "",
        now: Optional[datetime] = None,
    ) -> StreamerIntentState | None:
        """只允许已验证回复完成后调用；版本冲突安全返回 None。"""
        reference = self._reference(now)
        values = self._validated_values(
            energy_level, attention_target, current_beat, next_beat_hint, last_callback
        )
        updated_at, expires_at = self._timestamps(reference)
        with self.database._get_connection() as conn:
            cursor = conn.execute("""
                UPDATE streamer_intent_states
                SET interaction_mode = ?, primary_intent = ?, energy_level = ?,
                    attention_target = ?, current_beat = ?, next_beat_hint = ?,
                    last_callback = ?, updated_at = ?, expires_at = ?, version = version + 1
                WHERE stream_session_id = ? AND version = ?
            """, (
                interaction_mode.value, primary_intent.value, values[0], values[1],
                values[2], values[3], values[4], updated_at, expires_at,
                state.stream_session_id, state.version,
            ))
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM streamer_intent_states WHERE stream_session_id = ?",
                (state.stream_session_id,),
            ).fetchone()
        return self._from_row(row)

    def purge_expired(self, *, now: Optional[datetime] = None) -> int:
        reference = self._reference(now).isoformat()
        with self.database._get_connection() as conn:
            return conn.execute(
                "DELETE FROM streamer_intent_states WHERE expires_at <= ?", (reference,)
            ).rowcount

    def expire_other_sessions(
        self, current_session_id: str | None, *, now: Optional[datetime] = None
    ) -> int:
        """开播边界和下播时使旧节拍立刻失效，绝不跨场复活。"""
        reference = self._reference(now).isoformat()
        with self.database._get_connection() as conn:
            if current_session_id:
                cursor = conn.execute("""
                    UPDATE streamer_intent_states SET expires_at = ?
                    WHERE stream_session_id != ? AND expires_at > ?
                """, (reference, current_session_id, reference))
            else:
                cursor = conn.execute(
                    "UPDATE streamer_intent_states SET expires_at = ? WHERE expires_at > ?",
                    (reference, reference),
                )
        return cursor.rowcount

    @staticmethod
    def _reference(now: Optional[datetime]) -> datetime:
        reference = now or datetime.now().astimezone()
        return reference if reference.tzinfo else reference.astimezone()

    def _timestamps(self, reference: datetime) -> tuple[str, str]:
        return reference.isoformat(), (reference + timedelta(minutes=self.ttl_minutes)).isoformat()

    @staticmethod
    def _validated_values(
        energy_level: float, attention_target: str, current_beat: str,
        next_beat_hint: str, last_callback: str,
    ) -> tuple[float, str, str, str, str]:
        if not 0.0 <= float(energy_level) <= 1.0:
            raise ValueError("energy_level 必须在 0 到 1 之间")
        compact = tuple(str(value).strip() for value in (
            attention_target, current_beat, next_beat_hint, last_callback,
        ))
        if not compact[0] or len(compact[0]) > 32:
            raise ValueError("attention_target 必须为 1-32 个字符")
        if not compact[1] or len(compact[1]) > 80:
            raise ValueError("current_beat 必须为 1-80 个字符")
        if any(len(value) > 120 for value in compact[2:]):
            raise ValueError("短时提示或回调不能超过 120 个字符")
        return float(energy_level), *compact

    @staticmethod
    def _from_row(row) -> StreamerIntentState:
        return StreamerIntentState(
            stream_session_id=row["stream_session_id"],
            interaction_mode=InteractionMode(row["interaction_mode"]),
            primary_intent=PrimaryIntent(row["primary_intent"]),
            energy_level=float(row["energy_level"]),
            attention_target=row["attention_target"],
            current_beat=row["current_beat"],
            next_beat_hint=row["next_beat_hint"],
            last_callback=row["last_callback"],
            updated_at=row["updated_at"], expires_at=row["expires_at"],
            version=int(row["version"]),
        )
