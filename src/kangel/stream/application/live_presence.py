"""P32-A Live Presence 状态、Shadow 对照与受控持久化服务。"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from config import settings
from kangel.infrastructure.database import DatabaseManager
from kangel.shared.logging import logger
from kangel.stream.domain.live_presence import (
    ACTIVE_P32B_ACTIONS,
    LIVE_ACT_REASON_CODES,
    LiveActDecision,
    LiveActSnapshot,
    LiveActType,
    LivePresenceContext,
    LivePresenceState,
    SpeechThread,
)

from .presence_metrics import LivePresenceMetrics, live_presence_metrics
from .presence_policy import DeterministicLivePresencePolicy
from .stage_speech_journal import StageSpeechJournal


class LivePresenceVersionConflict(RuntimeError):
    """状态版本已被另一个异步更新抢先推进。"""


_LEGACY_ACTIONS = frozenset({
    "ANSWER_DANMAKU", "SELF_TALK", "NO_ACTION",
})


@dataclass(frozen=True)
class LivePresenceShadowObservation:
    stream_session_id: str
    presence_version: int
    legacy_action: str
    shadow_action: str
    shadow_reason_code: str
    matched: bool
    mainline_beat_version: int | None
    activity_version: int | None
    occurred_at: str

    def __post_init__(self) -> None:
        session_id = str(self.stream_session_id or "").strip()
        if not session_id:
            raise ValueError("stream_session_id 不能为空")
        object.__setattr__(self, "stream_session_id", session_id)
        presence_version = int(self.presence_version)
        if presence_version < 1:
            raise ValueError("presence_version 必须 >= 1")
        object.__setattr__(self, "presence_version", presence_version)
        legacy = str(self.legacy_action or "").strip()
        if legacy not in _LEGACY_ACTIONS:
            raise ValueError("legacy_action 不在允许列表")
        object.__setattr__(self, "legacy_action", legacy)
        shadow = (
            self.shadow_action.value
            if isinstance(self.shadow_action, LiveActType)
            else str(self.shadow_action or "").strip()
        )
        try:
            LiveActType(shadow)
        except ValueError:
            raise ValueError("shadow_action 不在允许列表") from None
        object.__setattr__(self, "shadow_action", shadow)
        reason = str(self.shadow_reason_code or "").strip()
        if reason not in LIVE_ACT_REASON_CODES:
            raise ValueError("shadow_reason_code 不在允许列表")
        object.__setattr__(self, "shadow_reason_code", reason)
        for field in ("mainline_beat_version", "activity_version"):
            raw = getattr(self, field)
            if raw is not None:
                value = int(raw)
                if value < 1:
                    raise ValueError(f"{field} 必须为正整数")
                object.__setattr__(self, field, value)
        timestamp = str(self.occurred_at or "").strip()
        if not timestamp:
            raise ValueError("occurred_at 不能为空")
        object.__setattr__(self, "occurred_at", timestamp)
        if isinstance(self.matched, bool):
            matched = self.matched
        elif isinstance(self.matched, int) and self.matched in (0, 1):
            matched = bool(self.matched)
        else:
            raise ValueError("matched 必须是布尔值")
        object.__setattr__(self, "matched", matched)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_session_id": self.stream_session_id,
            "presence_version": self.presence_version,
            "legacy_action": self.legacy_action,
            "shadow_action": self.shadow_action,
            "reason_code": self.shadow_reason_code,
            "matched": self.matched,
            "mainline_beat_version": self.mainline_beat_version,
            "activity_version": self.activity_version,
            "occurred_at": self.occurred_at,
        }


_UNSET = object()
def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LivePresenceService:
    """单场 Live Presence 服务。

    所有数据库操作都是同步的小事务，异步调用方应使用
    :meth:`observe_legacy_action`（内部交给线程执行），从而不会把 Shadow
    观察放到弹幕回复或 WebSocket 广播的等待链上。
    """

    OBSERVATION_RETENTION_DAYS = 30
    OBSERVATION_MAX_ROWS = 50_000
    CLEANUP_INTERVAL_SECONDS = 3600.0

    def __init__(
        self,
        database: DatabaseManager,
        *,
        policy: DeterministicLivePresencePolicy | None = None,
        metrics: LivePresenceMetrics | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.policy = policy or DeterministicLivePresencePolicy()
        self.metrics = metrics or live_presence_metrics
        self.clock = clock or _utc_now
        self._last_cleanup_at = 0.0
        self._active_session_id: str | None = None
        self.stage_speech_journal = StageSpeechJournal()

    @staticmethod
    def enabled() -> bool:
        return bool(
            settings.stream.live_presence_enabled
            and settings.stream.live_presence_shadow_enabled
        )

    @staticmethod
    def active_enabled() -> bool:
        """P32-B 正式动作开关；不要求 Shadow 也同时开启。"""
        return bool(
            settings.stream.live_presence_enabled
            and settings.stream.live_presence_active_enabled
        )

    @staticmethod
    def self_talk_active_enabled() -> bool:
        return bool(
            settings.stream.live_presence_enabled
            and settings.stream.live_presence_active_enabled
            and settings.stream.live_presence_self_talk_active_enabled
        )

    @staticmethod
    def state_enabled() -> bool:
        return bool(settings.stream.live_presence_enabled)

    def get(self, stream_session_id: str) -> LivePresenceState | None:
        if not self.state_enabled():
            return None
        try:
            row = self.database.get_live_presence_session(stream_session_id)
            if not row:
                return None
            state = self._decode_row(row)
            if state.status == "active":
                self._active_session_id = state.stream_session_id
            elif self._active_session_id == state.stream_session_id:
                self._active_session_id = None
            return state
        except Exception as exc:
            self.metrics.record_invalid_state()
            logger.warning(
                "Live Presence 状态恢复失败，Shadow 本轮跳过: session=%s error=%s",
                stream_session_id,
                exc,
            )
            return None

    def get_or_create(
        self,
        *,
        stream_session_id: str,
        mainline_beat_version: int | None = None,
        activity_version: int | None = None,
        now: datetime | None = None,
    ) -> LivePresenceState | None:
        if not self.state_enabled() or not str(stream_session_id or "").strip():
            return None
        session_id = str(stream_session_id).strip()
        existing = self.get(session_id)
        if existing is not None:
            return existing
        timestamp = self._timestamp(now)
        state = LivePresenceState(
            stream_session_id=session_id,
            version=1,
            active_thread=None,
            suspended_threads=(),
            last_act=None,
            last_speech_at=None,
            mainline_beat_version=mainline_beat_version,
            activity_version=activity_version,
            created_at=timestamp,
            updated_at=timestamp,
        )
        try:
            created = self.database.insert_live_presence_session(
                stream_session_id=session_id,
                version=state.version,
                state_json=self._encode(state),
                created_at=state.created_at,
                updated_at=state.updated_at,
            )
            row = self.database.get_live_presence_session(session_id)
            if not row:
                return None
            restored = self._decode_row(row)
            self._active_session_id = session_id
            if created:
                self.metrics.record_state_created()
            else:
                self.metrics.record_state_restored()
            return restored
        except Exception as exc:
            self.metrics.record_error()
            logger.warning("Live Presence 状态创建失败，Shadow 本轮跳过: %s", exc)
            return None

    def update(
        self,
        current: LivePresenceState,
        *,
        active_thread: SpeechThread | None | object = _UNSET,
        suspended_threads: tuple[SpeechThread, ...] | list[SpeechThread] | object = _UNSET,
        last_act: LiveActSnapshot | None | object = _UNSET,
        last_speech_at: str | None | object = _UNSET,
        mainline_beat_version: int | None | object = _UNSET,
        activity_version: int | None | object = _UNSET,
        status: str | object = _UNSET,
        pending_speech_attempt: dict | None | object = _UNSET,
        now: datetime | None = None,
    ) -> LivePresenceState | None:
        """按 expected version 更新；失败返回 None 并计冲突。"""
        if not self.state_enabled():
            return None
        requested_status = (
            current.status if status is _UNSET else str(status or "")
        )
        if current.status != "active":
            # A completed/archived session is a lifecycle fence.  It may be
            # inspected, but no late task may mutate it back into an active
            # state (or revive its threads for the next session).
            if requested_status != current.status:
                self.metrics.record_error()
            return None
        next_suspended = (
            current.suspended_threads
            if suspended_threads is _UNSET
            else self._bound_suspended(tuple(suspended_threads))
        )
        changed = LivePresenceState(
            stream_session_id=current.stream_session_id,
            version=current.version + 1,
            active_thread=current.active_thread if active_thread is _UNSET else active_thread,
            suspended_threads=next_suspended,
            last_act=current.last_act if last_act is _UNSET else last_act,
            last_speech_at=current.last_speech_at if last_speech_at is _UNSET else last_speech_at,
            mainline_beat_version=(
                current.mainline_beat_version
                if mainline_beat_version is _UNSET else mainline_beat_version
            ),
            activity_version=(
                current.activity_version
                if activity_version is _UNSET else activity_version
            ),
            created_at=current.created_at,
            updated_at=self._timestamp(now),
            status=requested_status,
            pending_speech_attempt=(current.pending_speech_attempt
                if pending_speech_attempt is _UNSET else pending_speech_attempt),
        )
        try:
            updated = self.database.update_live_presence_session(
                stream_session_id=current.stream_session_id,
                expected_version=current.version,
                version=changed.version,
                state_json=self._encode(changed),
                updated_at=changed.updated_at,
            )
            if not updated:
                self.metrics.record_version_conflict()
                return None
            self.metrics.record_state_updated()
            self._active_session_id = changed.stream_session_id
            return changed
        except Exception as exc:
            self.metrics.record_error()
            logger.warning("Live Presence 状态更新失败: %s", exc)
            return None

    def complete(
        self, stream_session_id: str, *, current: LivePresenceState | None = None,
        now: datetime | None = None,
    ) -> LivePresenceState | None:
        current = current or self.get(stream_session_id)
        # A late observation can win the first optimistic race at stream end.
        # Re-read once after a conflict so the lifecycle fence is eventually
        # committed without ever overwriting a newer state.
        for _ in range(2):
            if current is None:
                return None
            if current.status != "active":
                # Completed/archived state is retained for diagnostics but can
                # never be re-entered by a late Shadow task from this session.
                return current
            changed = self.update(current, status="completed", active_thread=None,
                suspended_threads=(), pending_speech_attempt=None, now=now)
            if changed is not None:
                if current.active_thread or current.suspended_threads:
                    self.metrics.increment("thread_abandoned")
                    self.metrics.increment("thread_abandoned_session_end")
                return changed
            current = self.get(stream_session_id)
        return None

    def evaluate_shadow(
        self,
        context: LivePresenceContext,
        *,
        legacy_action: str,
        state: LivePresenceState | None = None,
        now: datetime | None = None,
    ) -> LiveActDecision | None:
        """运行一次本地 Policy 并记录 Shadow 对照。

        ``legacy_action`` 只接受粗粒度动作，不包含 danmaku id、正文或账号。
        关闭时严格返回 None 且不创建状态、不运行 Policy、不写 observation。
        """
        if not self.enabled():
            return None
        if not isinstance(context, LivePresenceContext):
            raise TypeError("evaluate_shadow 需要 LivePresenceContext")
        legacy = str(legacy_action or "NO_ACTION")
        if legacy not in _LEGACY_ACTIONS:
            raise ValueError("legacy_action 不在允许列表")
        # The lifecycle boundary is strict: a stale/offline signal must not
        # create a state row merely because Shadow is enabled.
        if not context.is_live:
            self.metrics.record_evaluation(
                None, legacy_action=legacy, reason_code="NOT_LIVE"
            )
            return None
        current = state or self.get_or_create(
            stream_session_id=context.stream_session_id,
            mainline_beat_version=context.mainline_beat_version,
            activity_version=context.activity_version,
            now=now,
        )
        if current is None:
            return None
        if current.stream_session_id != context.stream_session_id:
            self.metrics.record_error()
            logger.warning(
                "Live Presence 状态与上下文 session 不一致，Shadow 本轮跳过: state=%s context=%s",
                current.stream_session_id,
                context.stream_session_id,
            )
            return None
        if current.status != "active":
            self.metrics.record_evaluation(
                None, legacy_action=legacy, reason_code="NO_ACTION"
            )
            return None
        try:
            raw_decision = self.policy.evaluate(context)
        except Exception as exc:
            self.metrics.record_error()
            logger.warning("Live Presence Shadow Policy 失败: %s", exc)
            return None
        if raw_decision is None:
            # Busy is a useful diagnostic distinction even though both busy
            # and an ordinary no-op leave the Legacy path untouched.
            no_action_reason = "BUSY" if context.ai_waiting else "NO_ACTION"
            self.metrics.record_evaluation(
                None, legacy_action=legacy, reason_code=no_action_reason
            )
            self._maybe_cleanup(self._timestamp(now))
            return None
        # Policy 独立调用时用 version=1；服务在持久化前替换成真实快照版本。
        try:
            decision = LiveActDecision(
                action=raw_decision.action,
                reason_code=raw_decision.reason_code,
                stream_session_id=current.stream_session_id,
                presence_version=current.version,
                mainline_beat_version=context.mainline_beat_version,
                activity_version=context.activity_version,
            )
        except Exception as exc:
            self.metrics.record_error()
            logger.warning("Live Presence Shadow Decision 非法，已丢弃: %s", exc)
            return None
        timestamp = self._timestamp(now)
        next_state = self.update(
            current,
            last_act=LiveActSnapshot(
                action=decision.action,
                reason_code=decision.reason_code,
                occurred_at=timestamp,
            ),
            mainline_beat_version=context.mainline_beat_version,
            activity_version=context.activity_version,
            now=now,
        )
        if next_state is None:
            return None
        observation = {
            "stream_session_id": current.stream_session_id,
            "presence_version": current.version,
            "legacy_action": legacy,
            "shadow_action": decision.action.value,
            "reason_code": decision.reason_code,
            "matched": legacy == decision.action.value,
            "mainline_beat_version": context.mainline_beat_version,
            "activity_version": context.activity_version,
            "occurred_at": timestamp,
        }
        try:
            self.database.insert_live_presence_observation(observation)
            self.metrics.record_evaluation(
                decision, legacy_action=legacy, reason_code=decision.reason_code
            )
            self._maybe_cleanup(timestamp)
            return decision
        except Exception as exc:
            # 状态已经提交时 observation 写入失败也不能回滚或影响 Legacy。
            self.metrics.record_error()
            logger.warning("Live Presence observation 写入失败: %s", exc)
            return decision

    def evaluate_active(
        self,
        context: LivePresenceContext,
        *,
        now: datetime | None = None,
    ) -> LiveActDecision | None:
        """P32-B 正式入口：只处理 READ 之后的 ANSWER/IGNORE 二选一。

        该方法不写 Live Presence state/observation，也不触碰 Persona 或回复
        链；终态由 danmaku persistence 层提交。任何策略异常或非法动作都
        安全回退为 Legacy ANSWER。
        """
        if not self.active_enabled():
            return None
        if not isinstance(context, LivePresenceContext):
            raise TypeError("evaluate_active 需要 LivePresenceContext")
        # P32-B 只服务于已被 Attention 选中的在线弹幕；离线/未选中
        # 输入不是一次需要回退成回答的策略错误，而是没有 active work。
        if not context.is_live or not context.has_selected_danmaku:
            return None
        timestamp = self._timestamp(now)
        try:
            ignore_eligible = self.policy.ignore_after_read_eligible(context)
            ignore_gate_hit = (
                self.policy.ignore_gate_passed(context) if ignore_eligible else False
            )
            raw_decision = self.policy.evaluate_active(context)
            if raw_decision is None:
                raise ValueError("active policy 未返回决策")
            action = raw_decision.action
            if not isinstance(action, LiveActType):
                action = LiveActType(str(action))
            if action not in ACTIVE_P32B_ACTIONS:
                self.metrics.record_invalid_active_action()
                return self._record_active_fallback(context, timestamp)
            reason_code = str(raw_decision.reason_code or "").strip()
            if reason_code not in LIVE_ACT_REASON_CODES:
                self.metrics.record_invalid_active_action()
                return self._record_active_fallback(context, timestamp)
            decision = LiveActDecision(
                action=action,
                reason_code=reason_code,
                stream_session_id=context.stream_session_id,
                presence_version=1,
                mainline_beat_version=context.mainline_beat_version,
                activity_version=context.activity_version,
                source="active_policy",
            )
        except Exception as exc:
            self.metrics.record_active_decision_error()
            logger.warning("Live Presence P32-B 决策失败，回退 Legacy ANSWER: %s", exc)
            return self._record_active_fallback(context, timestamp)

        self.metrics.record_active_decision(
            action=decision.action,
            reason_code=decision.reason_code,
            occurred_at=timestamp,
        )
        self.metrics.record_ignore_funnel(
            eligible=ignore_eligible,
            gate_hit=ignore_gate_hit,
        )
        self.metrics.record_forced_answer(decision.reason_code)
        return decision

    def _record_active_fallback(
        self, context: LivePresenceContext, timestamp: str
    ) -> LiveActDecision:
        decision = LiveActDecision(
            action=LiveActType.ANSWER_DANMAKU,
            reason_code="FALLBACK_POLICY_ERROR",
            stream_session_id=context.stream_session_id,
            presence_version=1,
            mainline_beat_version=context.mainline_beat_version,
            activity_version=context.activity_version,
            source="active_policy",
        )
        self.metrics.record_active_decision(
            action=decision.action,
            reason_code=decision.reason_code,
            occurred_at=timestamp,
        )
        return decision

    async def decide_active(
        self,
        context: LivePresenceContext,
        *,
        now: datetime | None = None,
    ) -> LiveActDecision | None:
        """异步旁路包装；策略本身不产生额外模型调用。"""
        return await asyncio.to_thread(self.evaluate_active, context, now=now)

    def evaluate_active_self_talk(
        self,
        context: LivePresenceContext,
        *,
        now: datetime | None = None,
    ) -> LiveActDecision | None:
        """P32-C0 permission check for one quiet-room SELF_TALK action."""
        if not self.self_talk_active_enabled():
            return None
        if not isinstance(context, LivePresenceContext):
            raise TypeError("evaluate_active_self_talk 需要 LivePresenceContext")
        if (
            not context.is_live
            or context.has_selected_danmaku
            or context.has_active_thread
            or context.has_suspended_thread
            or context.sc_pending
            or context.ai_waiting
            or context.danmaku_rate_60s > 0
            or context.mainline_beat_version is None
            or context.activity_version is None
            or context.silence_seconds < settings.stream.director_quiet_seconds
            or (
                context.seconds_since_last_speech is not None
                and context.seconds_since_last_speech
                < settings.stream.director_speak_cooldown_seconds
            )
        ):
            return None
        try:
            raw = self.policy.evaluate(context)
            if raw is None or raw.action is not LiveActType.SELF_TALK:
                return None
            decision = LiveActDecision(
                action=LiveActType.SELF_TALK,
                reason_code="ROOM_QUIET",
                stream_session_id=context.stream_session_id,
                presence_version=1,
                mainline_beat_version=context.mainline_beat_version,
                activity_version=context.activity_version,
                source="active_policy",
            )
        except Exception as exc:
            self.metrics.record_active_decision_error()
            logger.warning("Live Presence P32-C0 决策失败，保持安静: %s", exc)
            return None
        self.metrics.record_active_decision(
            action=decision.action,
            reason_code=decision.reason_code,
            occurred_at=self._timestamp(now),
        )
        return decision

    async def decide_active_self_talk(
        self, context: LivePresenceContext, *, now: datetime | None = None
    ) -> LiveActDecision | None:
        return await asyncio.to_thread(
            self.evaluate_active_self_talk, context, now=now
        )

    def seconds_since_last_speech(
        self, stream_session_id: str, *, now: datetime | None = None
    ) -> float | None:
        current = self.get(stream_session_id)
        if current is None or not current.last_speech_at:
            return None
        reference = now or self.clock()
        occurred = self._parse_timestamp(current.last_speech_at)
        return max(0.0, (reference.astimezone(timezone.utc) - occurred).total_seconds())

    def commit_active_self_talk(
        self,
        context: LivePresenceContext,
        decision: LiveActDecision,
        *,
        now: datetime | None = None,
    ) -> LivePresenceState | None:
        """Persist only a successfully broadcast SELF_TALK stage fact."""
        if decision.action is not LiveActType.SELF_TALK:
            return None
        changed = self.record_public_speech(
            stream_session_id=context.stream_session_id,
            action=LiveActType.SELF_TALK,
            reason_code="ROOM_QUIET",
            mainline_beat_version=context.mainline_beat_version,
            activity_version=context.activity_version,
            now=now,
        )
        if changed is not None:
            self.metrics.record_self_talk_performed()
        return changed

    def record_public_speech(
        self,
        *,
        stream_session_id: str,
        action: LiveActType,
        reason_code: str | None,
        mainline_beat_version: int | None,
        activity_version: int | None,
        now: datetime | None = None,
    ) -> LivePresenceState | None:
        """Persist the timestamp of a successfully public, user-visible utterance."""
        if not self.state_enabled() or action not in {
            LiveActType.ANSWER_DANMAKU,
            LiveActType.SELF_TALK,
        }:
            return None
        if reason_code is not None and reason_code not in LIVE_ACT_REASON_CODES:
            return None
        timestamp = self._timestamp(now)
        for _ in range(2):
            current = self.get(stream_session_id)
            if current is None or current.status != "active":
                return None
            changed = self.update(
                current,
                last_act=LiveActSnapshot(
                    action=action,
                    reason_code=reason_code,
                    occurred_at=timestamp,
                ) if reason_code is not None else current.last_act,
                last_speech_at=timestamp,
                mainline_beat_version=mainline_beat_version,
                activity_version=activity_version,
                now=now,
            )
            if changed is not None:
                return changed
        return None

    async def observe_legacy_action(
        self,
        context: LivePresenceContext,
        *,
        legacy_action: str,
        state: LivePresenceState | None = None,
        now: datetime | None = None,
    ) -> LiveActDecision | None:
        """异步旁路入口；所有 SQLite 与策略计算在线程中完成。"""
        return await asyncio.to_thread(
            self.evaluate_shadow,
            context,
            legacy_action=legacy_action,
            state=state,
            now=now,
        )

    def record_observation(
        self,
        *,
        context: LivePresenceContext,
        legacy_action: str,
        now: datetime | None = None,
    ) -> LiveActDecision | None:
        """兼容名称：等价于 evaluate_shadow。"""
        return self.evaluate_shadow(context, legacy_action=legacy_action, now=now)

    def snapshot(self, *, limit: int = 20) -> dict[str, Any]:
        """管理端安全投影；不包含 topic、正文、昵称或账号。"""
        enabled = bool(settings.stream.live_presence_enabled)
        shadow_enabled = bool(settings.stream.live_presence_shadow_enabled)
        active_enabled = bool(self.active_enabled())
        current = None
        if enabled:
            state = self._latest_state()
            if state is not None:
                current = self._safe_state_projection(state)
        recent = []
        if enabled and shadow_enabled:
            try:
                recent = self.database.list_live_presence_observations(limit=limit)
            except Exception as exc:
                self.metrics.record_error()
                logger.debug("读取 Live Presence observations 失败: %s", exc)
        return {
            "enabled": enabled,
            "shadow_enabled": shadow_enabled,
            "active_enabled": active_enabled,
            "self_talk_active_enabled": bool(self.self_talk_active_enabled()),
            "current": current,
            "metrics": self.metrics.snapshot(),
            "recent_observations": recent,
        }

    get_stats = snapshot

    def cleanup_observations(self, *, now: datetime | None = None) -> int:
        timestamp = self._timestamp(now)
        cutoff = self._parse_timestamp(timestamp) - timedelta(days=self.OBSERVATION_RETENTION_DAYS)
        try:
            return self.database.cleanup_live_presence_observations(
                cutoff_at=cutoff.isoformat(), max_rows=self.OBSERVATION_MAX_ROWS
            )
        except Exception as exc:
            self.metrics.record_error()
            logger.debug("Live Presence observation 清理失败: %s", exc)
            return 0

    def _maybe_cleanup(self, timestamp: str) -> None:
        now_mono = time.monotonic()
        if now_mono - self._last_cleanup_at < self.CLEANUP_INTERVAL_SECONDS:
            return
        self._last_cleanup_at = now_mono
        self.cleanup_observations(now=self._parse_timestamp(timestamp))

    def _latest_state(self) -> LivePresenceState | None:
        session_id = self._active_session_id
        if session_id:
            state = self.get(session_id)
            if state is not None and state.status == "active":
                return state
        try:
            with self.database._get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM live_presence_sessions ORDER BY updated_at DESC LIMIT 16"
                ).fetchall()
            for row in rows:
                try:
                    state = self._decode_row(dict(row))
                    if state.status == "active":
                        self._active_session_id = state.stream_session_id
                        return state
                except Exception:
                    self.metrics.record_invalid_state()
        except Exception as exc:
            self.metrics.record_error()
            logger.debug("读取最新 Live Presence 状态失败: %s", exc)
        return None

    @staticmethod
    def _safe_state_projection(state: LivePresenceState) -> dict[str, Any]:
        def thread_projection(thread: SpeechThread | None) -> dict[str, Any] | None:
            if thread is None:
                return None
            return {
                "thread_id": thread.thread_id,
                "status": thread.status.value,
                "importance": thread.importance,
                "started_at": thread.started_at,
                "last_touched_at": thread.last_touched_at,
            }

        return {
            "stream_session_id": state.stream_session_id,
            "version": state.version,
            "active_thread": thread_projection(state.active_thread),
            "suspended_thread_count": len(state.suspended_threads),
            "suspended_threads": [
                thread_projection(item) for item in state.suspended_threads
            ],
            "last_act": state.last_act.to_dict() if state.last_act else None,
            "mainline_beat_version": state.mainline_beat_version,
            "activity_version": state.activity_version,
            "updated_at": state.updated_at,
        }

    @staticmethod
    def _bound_suspended(threads: tuple[SpeechThread, ...]) -> tuple[SpeechThread, ...]:
        if len(threads) <= LivePresenceState.MAX_SUSPENDED_THREADS:
            return threads
        # 低 importance 先淘汰；同分时 oldest last_touched 先淘汰。
        ranked = sorted(
            enumerate(threads),
            key=lambda pair: (-pair[1].importance, pair[1].last_touched_at, pair[0]),
        )
        keep = sorted(ranked[: LivePresenceState.MAX_SUSPENDED_THREADS], key=lambda pair: pair[0])
        return tuple(item for _, item in keep)

    @staticmethod
    def _encode(state: LivePresenceState) -> str:
        return json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode_row(row: dict[str, Any]) -> LivePresenceState:
        raw = row.get("state_json")
        if not isinstance(raw, str):
            raise ValueError("state_json 缺失")
        state = LivePresenceState.from_dict(json.loads(raw))
        if state.stream_session_id != row.get("stream_session_id"):
            raise ValueError("state session 与表主键不一致")
        if int(row.get("version")) != state.version:
            raise ValueError("state version 与表字段不一致")
        if row.get("created_at") != state.created_at:
            raise ValueError("state created_at 与表字段不一致")
        if row.get("updated_at") != state.updated_at:
            raise ValueError("state updated_at 与表字段不一致")
        return state

    def _timestamp(self, value: datetime | None) -> str:
        current = value or self.clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# 与其它 Stream 应用服务一致提供一个进程级装配实例；测试和嵌入式调用可
# 显式传入自己的 DatabaseManager/metrics，避免共享状态。
from kangel.infrastructure.database import db_manager

live_presence_service = LivePresenceService(db_manager)

__all__ = [
    "LivePresenceService", "LivePresenceShadowObservation",
    "LivePresenceVersionConflict", "live_presence_service",
]
