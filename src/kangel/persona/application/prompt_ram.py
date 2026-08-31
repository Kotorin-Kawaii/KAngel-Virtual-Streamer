"""P30 prompt RAM 服务：主播短时工作记忆的进程内存储与注入装配。

设计取舍（与 ``intent_state.py`` / ``intent_shadow.py`` 一致）：

- **不加锁**。所有方法同步、内部无 ``await``，单线程事件循环即可保证原子性。
- **不落库**。RAM 是易失的：重启清空，换场次由 ``expire_other_sessions`` 清空。
- **绝不影响回复**。采集与注入全部 ``try/except``，失败只递增计数器。
- **身份不可伪造**。模型给出的 ``target`` 只是昵称文本，绑定对象一律用
  服务端已核验的 ``ViewerIdentity.subject_id``；对不上就不绑定对象。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, Iterable, Optional
import uuid

from config import settings
from kangel.persona.domain.prompt_ram import (
    ParsedThought,
    RamEntry,
    RamKind,
    RamState,
    sanitize_nickname,
    sanitize_note,
)
from kangel.shared.logging import logger


class PromptRamService:
    """有界、带 TTL 的念头存储；键为 ``entry_id``，插入顺序即淘汰顺序。"""

    def __init__(self, *, clock: Optional[Callable[[], float]] = None):
        self._clock: Callable[[], float] = clock or monotonic
        self._entries: dict[str, RamEntry] = {}
        self._metrics: Counter = Counter()
        self._last_purge_at: float = 0.0
        self._last_harvest_at: Optional[str] = None

    # ---------------------------------------------------------------- 基础设施

    @property
    def _config(self):
        return settings.prompt_ram

    def _now(self) -> float:
        return self._clock()

    def _ttl_for(self, kind: RamKind) -> float:
        config = self._config
        if kind is RamKind.AWAITING_VIEWER:
            return float(config.awaiting_ttl_seconds)
        if kind is RamKind.OWED_FOLLOWUP:
            return float(config.followup_ttl_seconds)
        return float(config.idea_ttl_seconds)

    def expire_other_sessions(self, current_session_id: str) -> int:
        """换场次即清空：念头永远不跨场复活。"""
        stale = [
            entry_id
            for entry_id, entry in self._entries.items()
            if entry.stream_session_id != current_session_id
        ]
        for entry_id in stale:
            self._entries.pop(entry_id, None)
            self._metrics["expired"] += 1
        return len(stale)

    def purge_expired(self, *, force: bool = False) -> int:
        """机会式清理；默认按 ``purge_interval_seconds`` 节流。"""
        now = self._now()
        if not force and (now - self._last_purge_at) < float(
            self._config.purge_interval_seconds
        ):
            return 0
        self._last_purge_at = now
        dead = [
            entry_id
            for entry_id, entry in self._entries.items()
            if not entry.is_active(now)
        ]
        for entry_id in dead:
            self._entries.pop(entry_id, None)
            self._metrics["expired"] += 1
        return len(dead)

    def _bounded_put(self, entry: RamEntry) -> None:
        limit = int(self._config.max_entries)
        while len(self._entries) >= limit and entry.entry_id not in self._entries:
            self._entries.pop(next(iter(self._entries)))
            self._metrics["evicted"] += 1
        self._entries[entry.entry_id] = entry

    def _prepare(self, stream_session_id: str) -> float:
        """每次读写前的公共前置：清跨场次、清过期，返回当前时钟。"""
        if stream_session_id:
            self.expire_other_sessions(stream_session_id)
        self.purge_expired()
        return self._now()

    def _live_entries(self, stream_session_id: str, now: float) -> list[RamEntry]:
        return [
            entry
            for entry in self._entries.values()
            if entry.stream_session_id == stream_session_id and entry.is_active(now)
        ]

    # ------------------------------------------------------------------ 生命周期

    def resolve_incoming(
        self,
        *,
        subject_id: Optional[str],
        stream_session_id: str,
    ) -> list[RamEntry]:
        """观众开口了：把等他回话的条目置 ``FULFILLED`` 并转入宽限期。"""
        if not self._config.enabled:
            return []
        try:
            now = self._prepare(stream_session_id)
            if not subject_id:
                return []
            grace = float(self._config.fulfilled_grace_seconds)
            fulfilled: list[RamEntry] = []
            for entry_id, entry in list(self._entries.items()):
                if (
                    entry.stream_session_id != stream_session_id
                    or entry.kind is not RamKind.AWAITING_VIEWER
                    or entry.state is not RamState.OPEN
                    or entry.target_subject_id != subject_id
                    or not entry.is_active(now)
                ):
                    continue
                updated = entry.with_state(
                    RamState.FULFILLED, expires_at_monotonic=now + grace
                )
                self._entries[entry_id] = updated
                self._metrics["fulfilled"] += 1
                fulfilled.append(updated)
            return fulfilled
        except Exception as exc:  # pragma: no cover - 防御性
            self._metrics["errors"] += 1
            logger.debug(f"[PromptRAM] resolve_incoming 失败: {exc}")
            return []

    # -------------------------------------------------------------------- 注入

    def build_for_reply(
        self,
        *,
        subject_id: Optional[str],
        stream_session_id: str,
    ) -> dict[str, Any]:
        """回复模型用：本人相关 → 无对象 → 他人相关，最多 ``inject_max_entries`` 条。"""
        if not self._config.enabled:
            return {}
        try:
            now = self._prepare(stream_session_id)
            live = self._live_entries(stream_session_id, now)
            if not live:
                return {}

            def rank(entry: RamEntry) -> int:
                if subject_id and entry.target_subject_id == subject_id:
                    return 0
                if entry.target_subject_id is None:
                    return 1
                return 2

            live.sort(key=lambda entry: (rank(entry), -entry.expires_at_monotonic))
            picked = live[: int(self._config.inject_max_entries)]
            return {
                "entries": [
                    {
                        "kind": entry.kind.value,
                        "state": entry.state.value,
                        "note": entry.note,
                        "target_nickname": entry.target_nickname,
                        "is_current_viewer": bool(
                            subject_id and entry.target_subject_id == subject_id
                        ),
                    }
                    for entry in picked
                ],
                "fulfilled_for_current_viewer": any(
                    entry.state is RamState.FULFILLED
                    and subject_id
                    and entry.target_subject_id == subject_id
                    for entry in picked
                ),
            }
        except Exception as exc:  # pragma: no cover - 防御性
            self._metrics["errors"] += 1
            logger.debug(f"[PromptRAM] build_for_reply 失败: {exc}")
            return {}

    def build_for_selector(self, stream_session_id: str) -> dict[str, str]:
        """弹幕筛选用：``subject_id -> note``，供按身份精确匹配候选。"""
        if not self._config.enabled:
            return {}
        try:
            now = self._prepare(stream_session_id)
            awaiting: dict[str, str] = {}
            for entry in self._live_entries(stream_session_id, now):
                if (
                    entry.kind is RamKind.AWAITING_VIEWER
                    and entry.state is RamState.OPEN
                    and entry.target_subject_id
                ):
                    awaiting[entry.target_subject_id] = entry.note
            return awaiting
        except Exception as exc:  # pragma: no cover - 防御性
            self._metrics["errors"] += 1
            logger.debug(f"[PromptRAM] build_for_selector 失败: {exc}")
            return {}

    def awaiting_current_viewer(
        self,
        *,
        subject_id: Optional[str],
        stream_session_id: str,
    ) -> bool:
        """情绪分析用：只给一个服务端可验证的布尔量，不外传任何文本。"""
        if not self._config.enabled or not subject_id:
            return False
        try:
            now = self._prepare(stream_session_id)
            return any(
                entry.kind is RamKind.AWAITING_VIEWER
                and entry.target_subject_id == subject_id
                for entry in self._live_entries(stream_session_id, now)
            )
        except Exception as exc:  # pragma: no cover - 防御性
            self._metrics["errors"] += 1
            logger.debug(f"[PromptRAM] awaiting_current_viewer 失败: {exc}")
            return False

    # -------------------------------------------------------------------- 采集

    def parse_thoughts(self, raw: Any) -> list[ParsedThought]:
        """容错解析模型输出的 ``thoughts``；全程不抛异常。"""
        try:
            if not isinstance(raw, list):
                return []
            config = self._config
            parsed: list[ParsedThought] = []
            for item in raw:
                if len(parsed) >= int(config.harvest_max_per_reply):
                    break
                if not isinstance(item, dict):
                    self._metrics["rejected"] += 1
                    continue
                note = sanitize_note(
                    item.get("note"), max_chars=int(config.note_max_chars)
                )
                if not note:
                    self._metrics["rejected"] += 1
                    continue
                raw_kind = item.get("kind")
                try:
                    kind = RamKind(raw_kind)
                except (ValueError, TypeError):
                    # 未知 kind 降级为无对象的念头，而不是整条丢掉。
                    kind = RamKind.STANDING_IDEA
                parsed.append(
                    ParsedThought(
                        kind=kind,
                        note=note,
                        claimed_target=sanitize_nickname(item.get("target")),
                    )
                )
            return parsed
        except Exception as exc:  # pragma: no cover - 防御性
            self._metrics["errors"] += 1
            logger.debug(f"[PromptRAM] parse_thoughts 失败: {exc}")
            return []

    def harvest(
        self,
        raw_thoughts: Any,
        *,
        subject_id: Optional[str],
        nickname: str,
        stream_session_id: str,
        danmaku_id: str,
    ) -> list[RamEntry]:
        """回复已核验可展示之后写入念头；失败只计数，绝不抛。"""
        if not self._config.enabled:
            return []
        try:
            now = self._prepare(stream_session_id)
            thoughts = self.parse_thoughts(raw_thoughts)
            if not thoughts:
                return []

            safe_nickname = sanitize_nickname(nickname)
            created: list[RamEntry] = []
            for thought in thoughts:
                kind = thought.kind
                bound_subject: Optional[str] = None
                bound_nickname = ""
                if kind in (RamKind.AWAITING_VIEWER, RamKind.OWED_FOLLOWUP):
                    # ``claimed_target`` 是昵称文本，不可信：只允许命中本轮作者。
                    if (
                        subject_id
                        and safe_nickname
                        and thought.claimed_target == safe_nickname
                    ):
                        bound_subject = subject_id
                        bound_nickname = safe_nickname
                    else:
                        # 想法永远无法挂到别人身上。
                        kind = RamKind.STANDING_IDEA

                if kind is RamKind.AWAITING_VIEWER and bound_subject:
                    self._supersede_awaiting(bound_subject, stream_session_id)

                entry = RamEntry(
                    entry_id=uuid.uuid4().hex,
                    kind=kind,
                    state=RamState.OPEN,
                    note=thought.note,
                    target_subject_id=bound_subject,
                    target_nickname=bound_nickname,
                    stream_session_id=stream_session_id,
                    source_danmaku_id=str(danmaku_id or ""),
                    created_at=datetime.now(timezone.utc).isoformat(),
                    expires_at_monotonic=now + self._ttl_for(kind),
                )
                self._bounded_put(entry)
                self._metrics["harvested"] += 1
                created.append(entry)

            if created:
                self._last_harvest_at = datetime.now(timezone.utc).isoformat()
            return created
        except Exception as exc:
            self._metrics["errors"] += 1
            logger.debug(f"[PromptRAM] harvest 失败: {exc}")
            return []

    def _supersede_awaiting(self, subject_id: str, stream_session_id: str) -> None:
        """每位观众最多 1 条「等回话」，否则同一人身上会堆出互相矛盾的期待。"""
        for entry_id, entry in list(self._entries.items()):
            if (
                entry.stream_session_id == stream_session_id
                and entry.kind is RamKind.AWAITING_VIEWER
                and entry.target_subject_id == subject_id
            ):
                self._entries.pop(entry_id, None)
                self._metrics["superseded"] += 1

    # -------------------------------------------------------------------- 观测

    def snapshot(self) -> list[dict]:
        now = self._now()
        return [
            entry.to_admin_dict(now)
            for entry in self._entries.values()
            if entry.is_active(now)
        ]

    def get_stats(self) -> dict:
        now = self._now()
        live: Iterable[RamEntry] = [
            entry for entry in self._entries.values() if entry.is_active(now)
        ]
        return {
            "harvested": self._metrics["harvested"],
            "rejected": self._metrics["rejected"],
            "fulfilled": self._metrics["fulfilled"],
            "superseded": self._metrics["superseded"],
            "expired": self._metrics["expired"],
            "evicted": self._metrics["evicted"],
            "errors": self._metrics["errors"],
            "open_entries": sum(
                1 for entry in live if entry.state is RamState.OPEN
            ),
            "total_entries": len(self._entries),
            "last_harvest_at": self._last_harvest_at,
        }

    def reset(self) -> None:
        """仅供测试与运维；生产路径不调用。"""
        self._entries.clear()
        self._metrics.clear()
        self._last_purge_at = 0.0
        self._last_harvest_at = None


prompt_ram_service = PromptRamService()
