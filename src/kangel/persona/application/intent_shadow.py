"""P22.B.1 非阻塞意图候选影子服务；从不影响主回复。"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import json
from time import monotonic

from config import settings
from kangel.infrastructure.bounded_work_gate import BoundedWorkGate
from kangel.infrastructure.bounded_work_gate import ai_reply_work_gate
from kangel.integrations.ai.service import ai_service
from kangel.persona.domain.intent import InteractionMode, PrimaryIntent, ReplyPlan
from kangel.persona.domain.appraisal import EventTriggerClass


@dataclass(frozen=True)
class IntentCandidate:
    interaction_mode: InteractionMode
    primary_intent: PrimaryIntent
    energy_level: float
    callback_category: str
    allow_light_follow_up: bool
    source: str


@dataclass(frozen=True)
class _ReplySuccess:
    stream_session_id: str
    base_intent_version: int
    committed_intent_version: int
    expires_at: float


@dataclass(frozen=True)
class _StagedCandidate:
    candidate: IntentCandidate
    stream_session_id: str
    base_intent_version: int
    expires_at: float


class IntentCandidateShadowService:
    """非阻塞候选的短生命周期协调器。

    所有暂存均为进程内、一次性且带 TTL 的影子数据：它们不会写入人格、
    关系、活动或场次心智状态。下一拍仅在状态版本仍完全吻合时才能观察到候选。
    """

    _LIFECYCLE_TTL_SECONDS = 120
    _LIFECYCLE_MAX_ITEMS = 64

    def __init__(self):
        self._gate = BoundedWorkGate()
        self._metrics = Counter()
        self._stream_attempts = Counter()
        self._reply_successes: dict[tuple[str, str], _ReplySuccess] = {}
        self._waiting_candidates: dict[tuple[str, str], _StagedCandidate] = {}
        self._next_beat_candidates: dict[tuple[str, int], _StagedCandidate] = {}

    def snapshot(self) -> dict:
        self._purge_expired()
        return {
            "outcomes": dict(sorted(self._metrics.items())),
            "gate": self._gate.snapshot(),
            "lifecycle": {
                "waiting_for_reply_success": len(self._waiting_candidates),
                "next_beat_candidates": len(self._next_beat_candidates),
            },
        }

    def mark_reply_success(
        self, *, event_id: str, stream_session_id: str,
        base_intent_version: int | None, committed_intent_version: int | None,
    ) -> None:
        """只在展示校验通过且心智状态成功提交后登记本轮成功。"""
        if not event_id or not stream_session_id:
            return
        if base_intent_version is None or committed_intent_version is None:
            return
        self._purge_expired()
        key = (stream_session_id, event_id)
        success = _ReplySuccess(
            stream_session_id=stream_session_id,
            base_intent_version=base_intent_version,
            committed_intent_version=committed_intent_version,
            expires_at=monotonic() + self._LIFECYCLE_TTL_SECONDS,
        )
        self._reply_successes[key] = success
        waiting = self._waiting_candidates.pop(key, None)
        if waiting is None:
            self._metrics["reply_success_waiting_candidate"] += 1
            return
        self._activate_next_beat_candidate(key, waiting, success)

    def consume_next_beat_candidate(self, intent_state) -> IntentCandidate | None:
        """取出与当前场次状态版本精确匹配的一次性影子候选。

        目前只用于影子观测，不改变 `ReplyPlan`。将来灰度采纳也必须再次通过
        安全/SC/直接问答优先级校验，不能绕过确定性规划器。
        """
        if intent_state is None:
            return None
        self._purge_expired()
        key = (intent_state.stream_session_id, intent_state.version)
        staged = self._next_beat_candidates.pop(key, None)
        if staged is None:
            return None
        self._metrics["next_beat_shadow_observed"] += 1
        return staged.candidate

    def merge_completed_analysis(self, plan: ReplyPlan, analysis) -> ReplyPlan:
        """唯一可当轮使用的候选：不新增等待，且不可越过高优先级互动。"""
        candidate = self._from_analysis(analysis, plan)
        if candidate is None:
            self._metrics["immediate_no_candidate"] += 1
            return plan
        if plan.interaction_mode in {
            InteractionMode.BOUNDARY_SET, InteractionMode.RECEIVE_SC,
            InteractionMode.FOLLOW_UP,
        }:
            self._metrics["immediate_rejected_priority"] += 1
            return plan
        if (
            candidate.interaction_mode == InteractionMode.COMFORT
            and plan.interaction_mode == InteractionMode.ANSWER
        ):
            self._metrics["immediate_applied"] += 1
            return ReplyPlan(
                interaction_mode=candidate.interaction_mode,
                primary_intent=candidate.primary_intent,
                energy_level=plan.energy_level,
                attention_target=plan.attention_target,
                current_beat="hold_emotion",
                allow_light_follow_up=True,
            )
        self._metrics["immediate_no_change"] += 1
        return plan

    async def observe(
        self, *, analysis, plan: ReplyPlan, is_sc: bool,
        event_id: str = "", intent_version: int | None = None,
        stream_session_id: str | None = None,
    ) -> None:
        """仅影子统计；任何等待、失败或晚到都不写入状态。"""
        if not settings.ai.intent_shadow_enabled:
            self._metrics["disabled"] += 1
            return
        if is_sc:
            self._metrics["skipped_sc_priority"] += 1
            return
        if not stream_session_id or settings.ai.intent_shadow_max_per_stream <= 0:
            self._metrics["skipped_no_session_or_quota"] += 1
            return
        if self._stream_attempts[stream_session_id] >= settings.ai.intent_shadow_max_per_stream:
            self._metrics["skipped_stream_quota"] += 1
            return
        self._stream_attempts[stream_session_id] += 1
        if ai_reply_work_gate.snapshot()["waiting"]:
            self._metrics["skipped_main_queue_busy"] += 1
            return
        candidate = self._from_analysis(analysis, plan)
        if candidate:
            self._metrics["reused_analysis"] += 1
            self._metrics["matches_plan" if candidate.interaction_mode == plan.interaction_mode else "differs_plan"] += 1
            return
        if not ai_service.has_role("intent_shadow"):
            self._metrics["insufficient_no_model"] += 1
            return
        lease = await self._gate.acquire(
            limit=settings.ai.intent_shadow_concurrency,
            max_waiters=settings.ai.intent_shadow_max_waiters,
            wait_timeout=0.01,
        )
        if not lease:
            self._metrics["skipped_capacity"] += 1
            return
        try:
            result = await asyncio.wait_for(ai_service.run(
                role="intent_shadow",
                model=settings.ai.intent_shadow_model or settings.ai.default_model,
                model_mode="role_hint",
                timeout=settings.ai.intent_shadow_timeout,
                temperature=0,
                messages=[{"role": "system", "content": "只输出 JSON，勿输出推理或原文。"},
                          {"role": "user", "content": "输出 interaction_mode、primary_intent、energy_level、callback_category、allow_light_follow_up。"}],
            ), timeout=settings.ai.intent_shadow_timeout)
            candidate = self._parse_model_candidate(result.get("reply", ""))
            if candidate is None:
                self._metrics["model_invalid"] += 1
            else:
                self._stage_late_candidate(
                    candidate=candidate,
                    event_id=event_id,
                    stream_session_id=stream_session_id,
                    base_intent_version=intent_version,
                )
        except Exception:
            self._metrics["model_shadow_failed"] += 1
        finally:
            await lease.release()

    def _stage_late_candidate(
        self, *, candidate: IntentCandidate, event_id: str,
        stream_session_id: str | None, base_intent_version: int | None,
    ) -> None:
        """候选和主回复可任意先后完成，最终仍需 CAS 式事件/版本核验。"""
        self._purge_expired()
        if not event_id or not stream_session_id or base_intent_version is None:
            self._metrics["model_valid_late_discarded_invalid_context"] += 1
            return
        key = (stream_session_id, event_id)
        staged = _StagedCandidate(
            candidate=candidate,
            stream_session_id=stream_session_id,
            base_intent_version=base_intent_version,
            expires_at=monotonic() + self._LIFECYCLE_TTL_SECONDS,
        )
        success = self._reply_successes.pop(key, None)
        if success is None:
            self._bounded_set(self._waiting_candidates, key, staged)
            self._metrics["model_valid_waiting_reply_success"] += 1
            return
        self._activate_next_beat_candidate(key, staged, success)

    def _activate_next_beat_candidate(
        self, event_key: tuple[str, str], staged: _StagedCandidate,
        success: _ReplySuccess,
    ) -> None:
        if (
            staged.stream_session_id != success.stream_session_id
            or staged.base_intent_version != success.base_intent_version
        ):
            self._metrics["model_valid_late_discarded_version_mismatch"] += 1
            return
        next_key = (success.stream_session_id, success.committed_intent_version)
        self._bounded_set(self._next_beat_candidates, next_key, staged)
        self._metrics["model_valid_next_beat_staged"] += 1

    def _purge_expired(self) -> None:
        now = monotonic()
        for collection in (
            self._reply_successes, self._waiting_candidates, self._next_beat_candidates,
        ):
            expired = [key for key, value in collection.items() if value.expires_at <= now]
            for key in expired:
                collection.pop(key, None)
                self._metrics["lifecycle_expired"] += 1

    def _bounded_set(self, collection: dict, key, value) -> None:
        if len(collection) >= self._LIFECYCLE_MAX_ITEMS and key not in collection:
            collection.pop(next(iter(collection)))
            self._metrics["lifecycle_evicted_capacity"] += 1
        collection[key] = value

    @staticmethod
    def _from_analysis(analysis, plan: ReplyPlan) -> IntentCandidate | None:
        appraisal = getattr(analysis, "appraisal", None)
        if (
            appraisal is not None
            and appraisal.trigger_class == EventTriggerClass.DISTRESS_SHARE
            and appraisal.confidence >= .35
        ):
            return IntentCandidate(
                InteractionMode.COMFORT, PrimaryIntent.HOLD_EMOTION,
                plan.energy_level, "", True, "event_appraisal",
            )
        tone = getattr(analysis, "emotional_tone", "")
        if tone not in {"positive", "negative", "mixed", "neutral"}:
            return None
        mode = InteractionMode.COMFORT if tone == "negative" else plan.interaction_mode
        intent = PrimaryIntent.HOLD_EMOTION if tone == "negative" else plan.primary_intent
        return IntentCandidate(mode, intent, plan.energy_level, "", False, "impact_analysis")

    @staticmethod
    def _parse_model_candidate(raw: str) -> IntentCandidate | None:
        try:
            data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
            allowed = {
                "interaction_mode", "primary_intent", "energy_level",
                "callback_category", "allow_light_follow_up",
            }
            if not isinstance(data, dict) or set(data) - allowed:
                return None
            mode = InteractionMode(str(data["interaction_mode"]))
            intent = PrimaryIntent(str(data["primary_intent"]))
            energy = float(data["energy_level"])
            callback = str(data.get("callback_category", "")).strip()
            follow_up = data.get("allow_light_follow_up", False)
            if not 0 <= energy <= 1 or len(callback) > 32 or not isinstance(follow_up, bool):
                return None
            return IntentCandidate(mode, intent, energy, callback, follow_up, "shadow_model")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None


intent_candidate_shadow_service = IntentCandidateShadowService()
