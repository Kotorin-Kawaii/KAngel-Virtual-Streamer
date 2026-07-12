"""按直播场次持久化的主播当前活动事实层。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from kangel.infrastructure.database import DatabaseManager


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_FALLBACK = {
    "id": "free-chat",
    "category": "chat",
    "name": "轻松杂谈",
    "object_name": "和宅宅们聊天",
    "theme_ids": ["*"],
    "min_duration_minutes": 30,
}


@dataclass(frozen=True)
class StreamerActivityState:
    stream_session_id: str
    activity_id: str
    category: str
    display_name: str
    object_name: str
    started_at: str
    min_duration_minutes: int
    version: int
    trigger_source: str
    public_performance: bool
    ended_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class StreamerActivityService:
    def __init__(self, database: DatabaseManager, raw_candidates: Any):
        self.database = database
        self.errors: list[str] = []
        self.candidates = self._parse(raw_candidates)
        if not self.candidates:
            self.errors.append("没有可用活动候选，已使用轻松杂谈兜底")
            self.candidates = [_FALLBACK.copy()]

    def get_or_create(
        self,
        *,
        stream_session_id: str,
        theme_id: str,
        started_at: str,
    ) -> StreamerActivityState:
        """同一场次只初始化一次；并发和重启均读取数据库中的既有事实。"""
        chosen = self._choose(stream_session_id, theme_id)
        now = datetime.now().astimezone().isoformat()
        with self.database._get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO streamer_activity_sessions (
                    stream_session_id, activity_id, category, display_name,
                    object_name, started_at, min_duration_minutes, version,
                    trigger_source, public_performance, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'theme_initialization', 0, ?)
            """, (
                stream_session_id, chosen["id"], chosen["category"], chosen["name"],
                chosen["object_name"], started_at, chosen["min_duration_minutes"], now,
            ))
            row = conn.execute(
                "SELECT * FROM streamer_activity_sessions WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
            conn.execute("""
                INSERT OR IGNORE INTO streamer_activity_transitions (
                    stream_session_id, version, activity_id, display_name,
                    object_name, trigger_source, public_performance, changed_at
                ) VALUES (?, 1, ?, ?, ?, 'theme_initialization', 0, ?)
            """, (
                stream_session_id, row["activity_id"], row["display_name"],
                row["object_name"], row["started_at"],
            ))
        return self._from_row(row)

    def get(self, stream_session_id: str) -> StreamerActivityState | None:
        with self.database._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM streamer_activity_sessions WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def end_other_sessions(
        self, current_session_id: str | None, ended_at: str
    ) -> int:
        """归档不再活跃的场次；当前场次（若有）保持进行中。"""
        with self.database._get_connection() as conn:
            if current_session_id:
                cursor = conn.execute("""
                    UPDATE streamer_activity_sessions
                    SET ended_at = ?, updated_at = ?
                    WHERE ended_at IS NULL AND stream_session_id != ?
                """, (ended_at, ended_at, current_session_id))
            else:
                cursor = conn.execute("""
                    UPDATE streamer_activity_sessions
                    SET ended_at = ?, updated_at = ? WHERE ended_at IS NULL
                """, (ended_at, ended_at))
            return cursor.rowcount

    def evaluate_and_switch(
        self,
        *,
        current: StreamerActivityState,
        theme_id: str,
        now: datetime,
        mood: float,
        stress: float,
        fatigue: float,
        danmaku_rate: int,
        switch_cooldown_minutes: int,
        max_duration_minutes: int,
        busy_rate_threshold: int,
        allow_public_performance: bool = False,
        darkness: float = 0.0,
        arousal: float = 0.5,
        audience_sentiment: float = 0.0,
    ) -> StreamerActivityState | None:
        """确定性评估静默切换；返回 None 表示继续当前活动或版本冲突。"""
        if current.ended_at or danmaku_rate >= busy_rate_threshold:
            return None
        started = datetime.fromisoformat(current.started_at)
        reference = now if now.tzinfo else now.astimezone()
        if started.tzinfo is None:
            started = started.replace(tzinfo=reference.tzinfo)
        elapsed_minutes = max(0.0, (reference - started).total_seconds() / 60)
        minimum = max(current.min_duration_minutes, switch_cooldown_minutes)
        if elapsed_minutes < minimum:
            return None
        reason = None
        if elapsed_minutes >= max_duration_minutes:
            reason = "max_duration"
        elif fatigue >= 0.75:
            reason = "high_fatigue"
        elif stress >= 0.8:
            reason = "high_stress"
        elif mood <= 0.2:
            reason = "low_mood"
        elif darkness >= 0.8:
            reason = "high_darkness"
        elif arousal <= 0.15:
            reason = "low_arousal"
        elif audience_sentiment <= -0.6:
            reason = "negative_room_atmosphere"
        if reason is None:
            return None

        candidates = [
            item for item in self.candidates
            if (theme_id in item["theme_ids"] or "*" in item["theme_ids"])
            and item["id"] != current.activity_id
        ]
        recent_ids = {
            item["activity_id"] for item in self.list_transitions(
                current.stream_session_id, limit=3
            )
        }
        fresh_candidates = [item for item in candidates if item["id"] not in recent_ids]
        if fresh_candidates:
            candidates = fresh_candidates
        if not candidates:
            return None
        ordered = sorted(candidates, key=lambda item: item["id"])
        digest = hashlib.sha256(
            f"kangel-activity-switch:{current.stream_session_id}:{current.version + 1}:{reason}".encode()
        ).digest()
        chosen = ordered[int.from_bytes(digest[:4], "big") % len(ordered)]
        changed_at = reference.isoformat()
        return self.switch_to_candidate(
            current=current,
            candidate=chosen,
            changed_at=changed_at,
            trigger_source=reason,
            public_performance=(reason == "max_duration" and allow_public_performance),
        )

    def switch_to_candidate(
        self, *, current: StreamerActivityState, candidate: dict,
        changed_at: str, trigger_source: str, public_performance: bool,
    ) -> StreamerActivityState | None:
        """统一乐观锁切换入口，并以场次+版本幂等记录历史。"""
        if candidate["id"] == current.activity_id:
            return None
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("""
                UPDATE streamer_activity_sessions
                SET activity_id = ?, category = ?, display_name = ?, object_name = ?,
                    started_at = ?, min_duration_minutes = ?, version = version + 1,
                    trigger_source = ?, public_performance = ?, updated_at = ?
                WHERE stream_session_id = ? AND version = ? AND ended_at IS NULL
            """, (
                candidate["id"], candidate["category"], candidate["name"],
                candidate["object_name"], changed_at, candidate["min_duration_minutes"],
                trigger_source, int(public_performance), changed_at,
                current.stream_session_id, current.version,
            ))
            if cursor.rowcount != 1:
                return None
            conn.execute("""
                INSERT INTO streamer_activity_transitions (
                    stream_session_id, version, previous_activity_id,
                    previous_display_name, previous_object_name, activity_id,
                    display_name, object_name, trigger_source,
                    public_performance, changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                current.stream_session_id, current.version + 1,
                current.activity_id, current.display_name, current.object_name,
                candidate["id"], candidate["name"], candidate["object_name"],
                trigger_source, int(public_performance), changed_at,
            ))
            row = conn.execute(
                "SELECT * FROM streamer_activity_sessions WHERE stream_session_id = ?",
                (current.stream_session_id,),
            ).fetchone()
        return self._from_row(row)

    def suggest_from_danmaku(
        self, *, current: StreamerActivityState, message: str, now: datetime,
        familiarity: float, trust: float, sentiment: float, danmaku_rate: int,
        min_familiarity: float, min_trust: float, switch_cooldown_minutes: int,
        busy_rate_threshold: int, allow_public_performance: bool = False,
    ) -> StreamerActivityState | None:
        """可信关系只能提出目录内明确活动；低关系或繁忙时不改变事实。"""
        if (
            current.ended_at or familiarity < min_familiarity or trust < min_trust
            or sentiment < -0.2 or danmaku_rate >= busy_rate_threshold
        ):
            return None
        normalized = "".join(message.casefold().split())
        if not any(word in normalized for word in ("换", "玩", "聊", "听", "来", "试试", "想看")):
            return None
        matches = [item for item in self.candidates if item["id"] != current.activity_id and any(
            token and "".join(token.casefold().split()) in normalized
            for token in (item["id"], item["name"], item["object_name"])
        )]
        if len(matches) != 1:
            return None
        reference = now if now.tzinfo else now.astimezone()
        started = datetime.fromisoformat(current.started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=reference.tzinfo)
        elapsed = (reference - started).total_seconds() / 60
        if elapsed < max(current.min_duration_minutes, switch_cooldown_minutes):
            return None
        return self.switch_to_candidate(
            current=current, candidate=matches[0], changed_at=reference.isoformat(),
            trigger_source="audience_suggestion",
            public_performance=allow_public_performance,
        )

    def public_performance_allowed(
        self, stream_session_id: str, now: datetime,
        min_interval_minutes: int, max_per_stream: int,
    ) -> bool:
        if max_per_stream <= 0:
            return False
        with self.database._get_connection() as conn:
            rows = conn.execute("""
                SELECT changed_at FROM streamer_activity_transitions
                WHERE stream_session_id = ? AND public_performance = 1
                ORDER BY version DESC
            """, (stream_session_id,)).fetchall()
        if len(rows) >= max_per_stream:
            return False
        if not rows:
            return True
        last = datetime.fromisoformat(rows[0]["changed_at"])
        reference = now if now.tzinfo else now.astimezone()
        if last.tzinfo is None:
            last = last.replace(tzinfo=reference.tzinfo)
        return (reference - last).total_seconds() >= min_interval_minutes * 60

    def list_transitions(self, stream_session_id: str, limit: int = 20) -> list[dict]:
        with self.database._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM streamer_activity_transitions
                WHERE stream_session_id = ? ORDER BY version DESC LIMIT ?
            """, (stream_session_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def _choose(self, session_id: str, theme_id: str) -> dict:
        matching = [
            item for item in self.candidates if theme_id in item["theme_ids"]
        ] or [item for item in self.candidates if "*" in item["theme_ids"]]
        if not matching:
            matching = [_FALLBACK]
        ordered = sorted(matching, key=lambda item: item["id"])
        digest = hashlib.sha256(
            f"kangel-activity:{session_id}:{theme_id}".encode("utf-8")
        ).digest()
        return ordered[int.from_bytes(digest[:4], "big") % len(ordered)]

    def _parse(self, raw: Any) -> list[dict]:
        if not isinstance(raw, list):
            self.errors.append("activity_candidates 必须是数组")
            return []
        parsed, seen = [], set()
        for index, item in enumerate(raw):
            try:
                if not isinstance(item, dict):
                    raise ValueError("候选必须是对象")
                activity_id = str(item.get("id", "")).strip()
                category = str(item.get("category", "")).strip()
                name = " ".join(str(item.get("name", "")).split())
                object_name = " ".join(str(item.get("object_name", "")).split())
                theme_ids = item.get("theme_ids", [])
                duration = int(item.get("min_duration_minutes", 30))
                if not _ID.fullmatch(activity_id) or activity_id in seen:
                    raise ValueError("id 无效或重复")
                if not _ID.fullmatch(category):
                    raise ValueError("category 无效")
                if not 1 <= len(name) <= 80 or not 1 <= len(object_name) <= 120:
                    raise ValueError("名称或具体对象长度无效")
                if not isinstance(theme_ids, list) or not theme_ids:
                    raise ValueError("theme_ids 必须是非空数组")
                normalized_themes = [str(value).strip() for value in theme_ids]
                if any(value != "*" and not _ID.fullmatch(value) for value in normalized_themes):
                    raise ValueError("theme_ids 包含无效 ID")
                if not 1 <= duration <= 1440:
                    raise ValueError("min_duration_minutes 必须为 1..1440")
                parsed.append({
                    "id": activity_id, "category": category, "name": name,
                    "object_name": object_name, "theme_ids": normalized_themes,
                    "min_duration_minutes": duration,
                })
                seen.add(activity_id)
            except (TypeError, ValueError) as exc:
                self.errors.append(f"activity_candidates[{index}] 无效: {exc}")
        return parsed

    @staticmethod
    def _from_row(row) -> StreamerActivityState:
        return StreamerActivityState(
            stream_session_id=row["stream_session_id"],
            activity_id=row["activity_id"],
            category=row["category"],
            display_name=row["display_name"],
            object_name=row["object_name"],
            started_at=row["started_at"],
            min_duration_minutes=row["min_duration_minutes"],
            version=row["version"],
            trigger_source=row["trigger_source"],
            public_performance=bool(row["public_performance"]),
            ended_at=row["ended_at"],
        )
