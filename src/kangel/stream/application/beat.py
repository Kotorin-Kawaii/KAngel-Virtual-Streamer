"""低频主播节拍：只消费已确认的直播事实，不调用模型也不参与弹幕调度。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from config import settings
from kangel.infrastructure.database import DatabaseManager


@dataclass(frozen=True)
class StreamerBeat:
    """可以公开展示的一次微动作；字段刻意不包含用户或内部人格数据。"""

    stream_session_id: str
    version: int
    activity_version: int
    beat_type: str
    display_text: str
    occurred_at: str

    def to_dict(self) -> dict:
        return asdict(self)


# 文案只陈述已确认活动或主播自身的短动作，禁止虚构观众、战果或新活动事实。
_MICRO_ACTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "game": (
        ("activity_progress", "这个环节还在继续，我再认真一点。"),
        ("glance_chat", "我先扫一眼弹幕，再继续。"),
        ("short_pause", "等我整理一下节奏。"),
        ("invite_participation", "想说什么可以慢慢发出来。"),
        ("natural_close", "这个小环节先收个尾。"),
    ),
    "music": (
        ("activity_progress", "这段再听一会儿。"),
        ("glance_chat", "我先看看大家在聊什么。"),
        ("short_pause", "让我缓一小下。"),
        ("compose_mood", "我调整一下状态，继续陪你们。"),
        ("natural_close", "这一段先告一段落。"),
    ),
    "chat": (
        ("glance_chat", "我先扫一眼弹幕。"),
        ("short_pause", "等我想一下怎么说。"),
        ("compose_mood", "我整理一下心情再继续聊。"),
        ("invite_participation", "有想聊的可以慢慢说。"),
        ("natural_close", "这个话题先轻轻收一下。"),
    ),
    "variety": (
        ("activity_progress", "这个环节继续推进一下。"),
        ("glance_chat", "我先看看弹幕的反应。"),
        ("short_pause", "让我整理一下接下来的节奏。"),
        ("invite_participation", "想参与的话慢慢告诉我。"),
        ("natural_close", "这个小环节先收个尾。"),
    ),
}
_FALLBACK_ACTIONS = _MICRO_ACTIONS["chat"]


class StreamerBeatScheduler:
    """保守的、持久化去重的主播微动作调度器。"""

    def __init__(self, database: DatabaseManager, *, clock: Callable[[], datetime] | None = None):
        self.database = database
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_evaluated_at = 0.0
        self._stats = {
            "evaluations": 0,
            "emitted": 0,
            "suppressed_disabled": 0,
            "suppressed_not_live": 0,
            "suppressed_busy": 0,
            "suppressed_deduplicated": 0,
            "suppressed_quota": 0,
        }

    async def tick(self, context: dict[str, Any]) -> StreamerBeat | None:
        """至多生成一次节拍；不满足前提时直接丢弃，不建立待处理队列。"""
        now_monotonic = asyncio.get_running_loop().time()
        if now_monotonic - self._last_evaluated_at < settings.stream.beat_evaluation_interval_seconds:
            return None
        self._last_evaluated_at = now_monotonic
        self._stats["evaluations"] += 1

        if not settings.stream.beat_enabled:
            self._stats["suppressed_disabled"] += 1
            return None
        if not context.get("is_live") or not context.get("stream_session_id") or not context.get("activity"):
            self._stats["suppressed_not_live"] += 1
            return None
        if (
            context.get("sc_pending")
            or context.get("ai_waiting")
            or context.get("slow_consumer")
            or context.get("activity_switch_recent")
            or int(context.get("danmaku_rate", 0)) > settings.stream.beat_low_activity_max_rate
        ):
            self._stats["suppressed_busy"] += 1
            return None

        activity = context["activity"]
        candidate = self._choose_candidate(str(context["stream_session_id"]), activity)
        if candidate is None:
            return None
        beat_type, display_text = candidate
        claimed = await asyncio.to_thread(
            self._claim,
            stream_session_id=str(context["stream_session_id"]),
            activity_version=int(activity["version"]),
            beat_type=beat_type,
            display_text=display_text,
            occurred_at=self.clock().isoformat(),
        )
        if claimed is None:
            return None
        self._stats["emitted"] += 1
        return claimed

    @staticmethod
    def _choose_candidate(
        stream_session_id: str, activity: dict[str, Any]
    ) -> tuple[str, str] | None:
        try:
            activity_version = int(activity["version"])
        except (KeyError, TypeError, ValueError):
            return None
        candidates = _MICRO_ACTIONS.get(str(activity.get("category", "")), _FALLBACK_ACTIONS)
        digest = hashlib.sha256(
            f"kangel-streamer-beat:{stream_session_id}:{activity_version}".encode()
        ).digest()
        return candidates[int.from_bytes(digest[:4], "big") % len(candidates)]

    def _claim(
        self,
        *,
        stream_session_id: str,
        activity_version: int,
        beat_type: str,
        display_text: str,
        occurred_at: str,
    ) -> StreamerBeat | None:
        """在 SQLite 中原子领取配额、时间窗与事实版本，重启后仍不重复。"""
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""
                SELECT version, occurred_at FROM streamer_beat_events
                WHERE stream_session_id = ? ORDER BY version DESC LIMIT 1
            """, (stream_session_id,)).fetchone()
            count = conn.execute("""
                SELECT COUNT(*) FROM streamer_beat_events WHERE stream_session_id = ?
            """, (stream_session_id,)).fetchone()[0]
            if count >= settings.stream.beat_max_per_stream:
                self._stats["suppressed_quota"] += 1
                return None
            if row:
                previous = datetime.fromisoformat(row["occurred_at"])
                current = datetime.fromisoformat(occurred_at)
                if (current - previous).total_seconds() < settings.stream.beat_min_interval_seconds:
                    self._stats["suppressed_quota"] += 1
                    return None
            version = (int(row["version"]) + 1) if row else 1
            try:
                conn.execute("""
                    INSERT INTO streamer_beat_events (
                        stream_session_id, activity_version, version, beat_type,
                        display_text, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    stream_session_id, activity_version, version, beat_type,
                    display_text, occurred_at,
                ))
            except Exception as exc:
                if "UNIQUE constraint failed" in str(exc):
                    self._stats["suppressed_deduplicated"] += 1
                    return None
                raise
        return StreamerBeat(
            stream_session_id=stream_session_id,
            version=version,
            activity_version=activity_version,
            beat_type=beat_type,
            display_text=display_text,
            occurred_at=occurred_at,
        )

    def get_stats(self) -> dict:
        return dict(self._stats)


__all__ = ["StreamerBeat", "StreamerBeatScheduler"]
