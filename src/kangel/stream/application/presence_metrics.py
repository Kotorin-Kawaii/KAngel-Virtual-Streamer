"""P32-A Live Presence 低基数运行指标。"""

from __future__ import annotations

from collections import Counter, deque
from threading import Lock
from typing import Any

from kangel.stream.domain.live_presence import (
    LIVE_ACT_REASON_CODES,
    LiveActDecision,
    LiveActType,
)


class LivePresenceMetrics:
    """进程内聚合指标。

    这里不保留输入上下文、正文或身份；只有固定动作和原因桶，适合直接
    放进管理端诊断快照。实例可在测试中独立创建。
    """

    _ACTION_KEYS = tuple(item.value for item in LiveActType)
    _REASON_KEYS = tuple(sorted(LIVE_ACT_REASON_CODES))
    _THREAD_OPERATIONS = ("open", "resume", "advance")
    _THREAD_GATE_REASONS = frozenset({
        "runtime_disabled", "work_in_flight", "generation_in_flight",
        "no_live_context", "semantic_context_missing", "presence_missing", "presence_inactive",
        "active_interruption", "retry_backoff", "pending_reply_work",
        "reply_ai_active", "reply_ai_waiting", "sc_active", "slow_subscriber",
        "busy_unclassified", "no_viewers", "no_seed", "thread_not_visible",
        "thread_interval", "opener_disabled", "resume_active_disabled",
        "opening_or_closing_beat", "quiet_context_unavailable", "quiet_sc_pending",
        "quiet_ai_busy", "quiet_danmaku_active", "quiet_threshold",
        "quiet_speak_cooldown", "live_presence_disabled", "director_disabled",
        "director_unavailable", "director_performance_disabled",
        "director_startup_cooldown", "director_public_cooldown", "director_busy",
        "director_public_quota", "director_speak_quota", "director_speak_cooldown",
        "generation_preempted", "invalid_public_reply", "unsafe_context_after_generation",
        "activity_fact_mismatch", "session_changed", "beat_changed", "activity_changed",
        "presence_version_stale", "thread_expired", "thread_incompatible",
        "attempt_reserve_conflict", "prebroadcast_preempted", "delivery_guard_blocked",
        "delivery_zero",
    })
    _THREAD_OUTCOMES = frozenset({
        "attempt_scheduled", "template_ready", "generation_started", "generation_valid",
        "generation_invalid_output", "generation_timeout", "generation_error",
        "preempted", "activity_fact_rejected", "stale_session", "stale_beat",
        "stale_activity", "stale_presence_version", "attempt_reserve_conflict",
        "prebroadcast_preempted", "delivery_guard_blocked", "delivery_zero",
        "broadcast_exception", "delivery_commit_success", "delivery_commit_conflict",
        "known_undelivered", "stage_error", "validation_non_string", "validation_invalid_json",
        "validation_not_object", "validation_invalid_emotions", "validation_invalid_sentences",
        "validation_invalid_sentence_shape", "validation_invalid_sentence_emotion",
        "validation_invalid_sentence_text", "validation_reply_too_long",
        "validation_emotion_mismatch",
    })
    _SEMANTIC_HINT_OUTCOMES = frozenset({
        "generation_started", "missing", "invalid_shape", "invalid_action",
        "action_missing", "action_wrong_type", "action_unknown", "action_operation_mismatch",
        "extra_fields", "invalid_topic", "invalid_unfinished_point", "not_grounded",
        "valid_open", "valid_advance", "valid_complete", "committed_open",
        "committed_advance", "committed_complete", "committed_seed_fallback",
        "committed_deterministic_complete",
        "committed_step_limit",
    })

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: Counter[str] = Counter()
        self._actions: Counter[str] = Counter()
        self._reasons: Counter[str] = Counter()
        self._active_actions: Counter[str] = Counter()
        self._active_reasons: Counter[str] = Counter()
        self._recent_active_decisions: deque[dict[str, Any]] = deque(maxlen=32)
        self._thread_gates: Counter[tuple[str, str]] = Counter()
        self._thread_outcomes: Counter[tuple[str, str]] = Counter()
        self._semantic_hint_outcomes: Counter[tuple[str, str]] = Counter()

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] += int(amount)

    def record_evaluation(
        self,
        decision: LiveActDecision | None,
        *,
        legacy_action: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        with self._lock:
            self._counts["evaluations"] += 1
            if decision is None:
                if reason_code:
                    self._reasons[str(reason_code)] += 1
                return
            action = decision.action.value
            self._actions[action] += 1
            self._counts[f"shadow_{action.casefold()}"] += 1
            self._reasons[decision.reason_code] += 1
            if legacy_action is not None:
                if str(legacy_action) == action:
                    self._counts["legacy_match"] += 1
                else:
                    self._counts["legacy_mismatch"] += 1

    def record_observation(self, *, matched: bool) -> None:
        with self._lock:
            key = "legacy_match" if matched else "legacy_mismatch"
            self._counts[key] += 1

    def record_state_created(self) -> None:
        self.increment("state_created")

    def record_state_restored(self) -> None:
        self.increment("state_restored")

    def record_state_updated(self) -> None:
        self.increment("state_updated")

    def record_error(self) -> None:
        self.increment("errors")

    def record_invalid_state(self) -> None:
        self.increment("invalid_state")

    def record_version_conflict(self) -> None:
        self.increment("version_conflicts")

    def record_read(self) -> None:
        """记录 Attention 已选中并完成 READ 提交的次数。"""
        self.increment("read_total")

    def record_active_decision(
        self, *, action: LiveActType, reason_code: str, occurred_at: str
    ) -> None:
        """记录 P32-B 正式动作；只保存低基数动作/原因，不保存弹幕信息。"""
        with self._lock:
            action_value = action.value if isinstance(action, LiveActType) else str(action)
            self._counts[f"active_{action_value.casefold()}"] += 1
            self._active_actions[action_value] += 1
            self._active_reasons[str(reason_code)] += 1
            self._recent_active_decisions.append({
                "action": action_value,
                "reason_code": str(reason_code),
                "occurred_at": str(occurred_at),
            })

    def record_forced_answer(self, reason_code: str) -> None:
        mapping = {
            "FORCED_SC": "forced_answer_sc",
            "FORCED_FOLLOWUP": "forced_answer_followup",
            "FORCED_BOUNDARY": "forced_answer_boundary",
        }
        key = mapping.get(str(reason_code))
        if key:
            self.increment(key)

    def record_ignore_funnel(self, *, eligible: bool, gate_hit: bool) -> None:
        """Record the low-cardinality P32-B6 eligibility/gate funnel."""
        if not eligible:
            return
        self.increment("ignore_eligible")
        self.increment("ignore_gate_hit" if gate_hit else "ignore_gate_miss")

    def record_invalid_active_action(self) -> None:
        self.increment("invalid_active_action")

    def record_active_decision_error(self) -> None:
        self.increment("active_decision_errors")

    def record_consumption_write_error(self) -> None:
        self.increment("consumption_write_errors")

    def record_self_talk_performed(self) -> None:
        self.increment("active_self_talk_performed")

    def record_self_talk_suppressed(self) -> None:
        self.increment("active_self_talk_suppressed")

    def record_thread_gate(self, operation: str, reason: str) -> None:
        """Count a fixed, low-cardinality reason that blocked a stage operation."""
        if operation not in self._THREAD_OPERATIONS or reason not in self._THREAD_GATE_REASONS:
            return
        with self._lock:
            self._thread_gates[(operation, reason)] += 1

    def record_thread_outcome(self, operation: str, outcome: str) -> None:
        if operation not in self._THREAD_OPERATIONS or outcome not in self._THREAD_OUTCOMES:
            return
        with self._lock:
            self._thread_outcomes[(operation, outcome)] += 1

    def record_semantic_hint(self, operation: str, outcome: str) -> None:
        if operation not in self._THREAD_OPERATIONS or outcome not in self._SEMANTIC_HINT_OUTCOMES:
            return
        with self._lock:
            self._semantic_hint_outcomes[(operation, outcome)] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts = dict(self._counts)
            actions = {
                action: int(self._actions.get(action, 0))
                for action in self._ACTION_KEYS
            }
            reasons = {
                reason: int(self._reasons.get(reason, 0))
                for reason in self._REASON_KEYS
            }
            matched = int(counts.get("legacy_match", 0))
            mismatched = int(counts.get("legacy_mismatch", 0))
            recent_active_decisions = list(self._recent_active_decisions)
            thread_gates = dict(self._thread_gates)
            thread_outcomes = dict(self._thread_outcomes)
            semantic_hint_outcomes = dict(self._semantic_hint_outcomes)
            active_actions = {
                action: int(self._active_actions.get(action, 0))
                for action in self._ACTION_KEYS
            }
            active_reasons = {
                reason: int(self._active_reasons.get(reason, 0))
                for reason in self._REASON_KEYS
            }
        total = matched + mismatched
        return {
            "threads": {
                **{name: int(counts.get("thread_" + name, 0)) for name in (
                    "opened", "suspended", "resume_requested", "resumed", "advance",
                    "advance_requested", "resume_suppressed", "shadow_resume", "shadow_advance",
                    "completed", "resumed_completed", "ended", "ended_step_limit",
                    "abandoned", "errors", "review_errors", "invalid_hint_atomic",
                    "abandoned_ttl", "abandoned_activity_change", "abandoned_mainline_change",
                    "abandoned_replaced", "abandoned_uncertain_delivery", "abandoned_invalid_hint",
                    "abandoned_interruption_delivery_unknown",
                    "abandoned_interrupt_limit", "abandoned_unknown_seed", "abandoned_session_end",
                    "restore_success", "restore_expired", "version_conflicts", "delivery_commit_failed",
                )},
                "gate_blocks": {
                    f"{operation}:{reason}": int(count)
                    for (operation, reason), count in sorted(thread_gates.items())
                },
                "stage_outcomes": {
                    operation: {
                        name: int(count) for (op, name), count in sorted(thread_outcomes.items())
                        if op == operation
                    }
                    for operation in self._THREAD_OPERATIONS
                    if any(op == operation for op, _ in thread_outcomes)
                },
                "semantic_hints": {
                    operation: {
                        name: int(count) for (op, name), count in sorted(semantic_hint_outcomes.items())
                        if op == operation
                    }
                    for operation in self._THREAD_OPERATIONS
                    if any(op == operation for op, _ in semantic_hint_outcomes)
                },
                "gate_block_semantics": "blocked scheduler-check samples; not unique threads",
                "resume_suppressed_deprecated": True,
                "completed_semantics": "validated model complete hint, not a quality assessment; ended_step_limit is not completion evidence",
                "resume_rate": (counts.get("thread_resumed", 0) / counts["thread_suspended"]
                                if counts.get("thread_suspended") else None),
                "successful_continuity_rate": (counts.get("thread_resumed_completed", 0) / counts["thread_opened"]
                                if counts.get("thread_opened") else None),
                "scope": "process_since_start",
            },
            "evaluations": int(counts.get("evaluations", 0)),
            "state_created": int(counts.get("state_created", 0)),
            "state_restored": int(counts.get("state_restored", 0)),
            "state_updated": int(counts.get("state_updated", 0)),
            "shadow_answer_danmaku": int(counts.get("shadow_answer_danmaku", 0)),
            "shadow_continue_thread": int(counts.get("shadow_continue_thread", 0)),
            "shadow_ignore_after_read": int(counts.get("shadow_ignore_after_read", 0)),
            "shadow_self_talk": int(counts.get("shadow_self_talk", 0)),
            "legacy_match": matched,
            "legacy_mismatch": mismatched,
            "errors": int(counts.get("errors", 0)),
            "invalid_state": int(counts.get("invalid_state", 0)),
            "version_conflicts": int(counts.get("version_conflicts", 0)),
            "read_total": int(counts.get("read_total", 0)),
            "active_answer_danmaku": int(counts.get("active_answer_danmaku", 0)),
            "active_ignore_after_read": int(counts.get("active_ignore_after_read", 0)),
            "active_self_talk": int(counts.get("active_self_talk", 0)),
            "active_self_talk_performed": int(counts.get("active_self_talk_performed", 0)),
            "active_self_talk_suppressed": int(counts.get("active_self_talk_suppressed", 0)),
            "ignore_eligible": int(counts.get("ignore_eligible", 0)),
            "ignore_gate_hit": int(counts.get("ignore_gate_hit", 0)),
            "ignore_gate_miss": int(counts.get("ignore_gate_miss", 0)),
            "forced_answer_sc": int(counts.get("forced_answer_sc", 0)),
            "forced_answer_followup": int(counts.get("forced_answer_followup", 0)),
            "forced_answer_boundary": int(counts.get("forced_answer_boundary", 0)),
            "invalid_active_action": int(counts.get("invalid_active_action", 0)),
            "active_decision_errors": int(counts.get("active_decision_errors", 0)),
            "consumption_write_errors": int(counts.get("consumption_write_errors", 0)),
            "ignore_after_read_rate": (
                int(counts.get("active_ignore_after_read", 0))
                / max(
                    1,
                    int(counts.get("active_answer_danmaku", 0))
                    + int(counts.get("active_ignore_after_read", 0)),
                )
                if (
                    int(counts.get("active_answer_danmaku", 0))
                    + int(counts.get("active_ignore_after_read", 0))
                )
                else None
            ),
            "recent_active_decisions": recent_active_decisions,
            "active_actions": active_actions,
            "active_reason_codes": active_reasons,
            # These nested projections make the Admin response easy to consume
            # while retaining the fixed low-cardinality counters above.
            "shadow_actions": actions,
            "reason_codes": reasons,
            "match_rate": (matched / total) if total else None,
        }

    get_stats = snapshot


live_presence_metrics = LivePresenceMetrics()

__all__ = ["LivePresenceMetrics", "live_presence_metrics"]
