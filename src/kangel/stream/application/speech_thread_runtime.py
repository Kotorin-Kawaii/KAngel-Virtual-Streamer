"""One low-priority stage worker. Viewer replies never await stage generation."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from config import settings
from config.emotion_catalog import AVAILABLE_EMOTIONS
from kangel.shared.logging import logger
from kangel.stream.application.speech_thread import SpeechThreadService, select_thread_seed, parse_time
from kangel.stream.domain.live_presence import SpeechThreadStatus
from .stage_speech_journal import public_stage_text


class SpeechThreadRuntime:
    def __init__(self, pusher):
        self.pusher = pusher
        self.service = SpeechThreadService(pusher._ensure_live_presence_service())
        self.wake = asyncio.Event()
        self.task = None
        self.work = None
        self.generation = None
        self.epoch = 0
        self.closed = False
        self.restored_session = None
        self.retry_at = 0.0
        self.active_interruptions = set()
        from .audience_greeting_runtime import AudienceGreetingStage
        self.audience = AudienceGreetingStage(self)

    @staticmethod
    def enabled():
        return settings.stream.live_presence_enabled and (
            settings.stream.live_presence_thread_enabled or settings.stream.audience_presence_enabled)

    async def start(self):
        self.closed = False
        from kangel.audience.application.stage_presence import AudienceStagePresence
        self.audience.tracker = AudienceStagePresence()
        self.audience.refresh()
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        self.closed = True
        self.epoch += 1
        for task in (self.task, self.work):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.task, self.work) if t), return_exceptions=True)
        # generation remains shielded until physical HTTP/fallback work exits.
        # Keeping its task reference prevents a replacement stage request.

    def notify(self):
        self.wake.set()

    def _record_gate(self, operation: str, reason: str) -> None:
        self.service.metrics.record_thread_gate(operation, reason)

    def _record_outcome(self, operation: str, outcome: str) -> None:
        self.service.metrics.record_thread_outcome(operation, outcome)

    def _busy_reason(self) -> str:
        """Break the existing aggregate busy gate into safe diagnostic buckets."""
        try:
            from kangel.infrastructure.bounded_work_gate import ai_reply_work_gate
            work = ai_reply_work_gate.snapshot()
            if work.get("active"):
                return "reply_ai_active"
            if work.get("waiting"):
                return "reply_ai_waiting"
        except Exception:
            return "busy_unclassified"
        try:
            from kangel.integrations.superchat.service import sc_service
            if sc_service.has_active_work():
                return "sc_active"
        except Exception:
            return "busy_unclassified"
        try:
            if self.pusher._has_slow_subscriber():
                return "slow_subscriber"
        except Exception:
            return "busy_unclassified"
        return "busy_unclassified"

    async def safe_facts_with_reason(self):
        if not self.enabled() or self.closed:
            return None, "runtime_disabled"
        if self.active_interruptions:
            return None, "active_interruption"
        from kangel.danmaku.application.pool import danmaku_pool
        if await danmaku_pool.has_pending_reply_work():
            return None, "pending_reply_work"
        facts = self.pusher._build_director_context()
        if not facts:
            return None, "no_live_context"
        if facts.get("semantic_context") is None:
            return None, "semantic_context_missing"
        if facts.get("busy"):
            return None, self._busy_reason()
        if facts.get("viewer_count", 0) <= 0:
            return None, "no_viewers"
        return facts, None

    def preempt(self):
        self.epoch += 1
        self.wake.set()

    async def begin_reply(self, event_id, source="normal"):
        if not self.enabled():
            return None
        self.preempt()
        session = self.pusher._metadata.stream_session_id
        if not session:
            return None
        self.active_interruptions.add(event_id)
        try:
            token = None
            if settings.stream.live_presence_thread_enabled and settings.stream.live_presence_thread_suspend_active_enabled:
                token = await asyncio.to_thread(self.service.suspend, session, event_id, source)
            return (session, event_id, source, token)
        except Exception:
            self.service.metric("errors")
            return (session, event_id, source, None)

    async def finish_reply(self, handle, visible):
        if not handle:
            return
        session, event_id, source, token = handle
        try:
            if (visible and token is None and settings.stream.live_presence_thread_enabled
                and settings.stream.live_presence_thread_suspend_active_enabled):
                # Another reply may have failed and restored ACTIVE while this
                # overlapping SC/boundary reply was still running.
                token = await asyncio.to_thread(self.service.suspend, session, event_id, source)
            await asyncio.to_thread(self.service.finish_interrupt, token, visible)
            if visible and token is None:
                from kangel.stream.domain.live_presence import LiveActType
                state = await asyncio.to_thread(self.service.presence.get, session)
                if state:
                    await asyncio.to_thread(self.service.presence.record_public_speech,
                        stream_session_id=session, action=LiveActType.ANSWER_DANMAKU,
                        reason_code="DANMAKU_AVAILABLE", mainline_beat_version=state.mainline_beat_version,
                        activity_version=state.activity_version)
        except Exception:
            self.service.metric("errors")
        finally:
            self.active_interruptions.discard(event_id)
            self.notify()

    async def run(self):
        while not self.closed:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.service.metric("errors")
                logger.warning("SpeechThread stage tick failed; viewer pipeline unaffected")
            try:
                await asyncio.wait_for(self.wake.wait(), self.audience.wait_seconds())
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def safe_facts(self):
        facts, _ = await self.safe_facts_with_reason()
        return facts

    async def tick(self):
        if not self.enabled() or self.closed:
            return
        if self.work and not self.work.done():
            return
        if self.generation and not self.generation.done():
            return
        self.audience.refresh()
        facts = self.pusher._build_director_context()
        if not facts:
            self._record_gate("open", "no_live_context")
            return
        if facts.get("semantic_context") is None:
            self._record_gate("open", "semantic_context_missing")
            return
        if not settings.stream.live_presence_thread_enabled:
            await self.audience.maybe_schedule(facts, None)
            return
        session = facts["mainline"].stream_session_id
        state = await asyncio.to_thread(self.service.presence.get_or_create, stream_session_id=session)
        if not state or state.status != "active":
            return
        state = await asyncio.to_thread(
            self.service.maintain, state, facts,
            restart=session != self.restored_session and not self.active_interruptions,
        )
        if not state:
            return
        self.restored_session = session
        thread = self.service.current(state)
        if thread and thread.interruption_id not in self.active_interruptions:
            state = await asyncio.to_thread(self.service.recover_interrupt, state)
            thread = self.service.current(state)
        try:
            if await self.audience.maybe_schedule(facts, state):
                return
        except Exception:
            self.audience.tracker.record("generation_error")
        if (thread is None and settings.stream.audience_greeting_active_enabled
            and settings.stream.audience_presence_enabled and self.audience.tracker.pending):
            # Give a stable new arrival its short welcome window before opening a new topic.
            return
        operation = "open" if thread is None else "resume" if thread.status == SpeechThreadStatus.SUSPENDED else "advance"
        if self.active_interruptions:
            self._record_gate(operation, "active_interruption")
            return
        if time.monotonic() < self.retry_at:
            self._record_gate(operation, "retry_backoff")
            return
        safe, safe_reason = await self.safe_facts_with_reason()
        if not safe:
            self._record_gate(operation, safe_reason or "busy_unclassified")
            return
        seed = select_thread_seed(safe["semantic_context"], thread,
            self.service.presence.stage_speech_journal.recent_seed_ids(session))
        if seed is None:
            if thread:
                await asyncio.to_thread(self.service.abandon, state, "unknown_seed")
            else:
                self._record_gate(operation, "no_seed")
            return
        if thread:
            if thread.status == SpeechThreadStatus.SUSPENDED and not thread.interruption_visible:
                self._record_gate(operation, "thread_not_visible")
                return
            last = max(parse_time(thread.last_touched_at), parse_time(state.last_speech_at or thread.last_touched_at))
            if (datetime.now(timezone.utc) - last).total_seconds() < settings.stream.live_presence_thread_interval_seconds:
                self._record_gate(operation, "thread_interval")
                return
            self.service.metric("resume_requested" if operation == "resume" else "advance_requested")
            if not settings.stream.live_presence_thread_resume_active_enabled:
                self.service.metric("shadow_" + operation)
                self._record_gate(operation, "resume_active_disabled")
                return
        else:
            if not settings.stream.live_presence_thread_opener_enabled:
                self._record_gate(operation, "opener_disabled")
                return
            mainline = safe["mainline"]
            if mainline.current_beat_id in {mainline.plan.opening_beat_id, mainline.plan.closing_beat_id}:
                self._record_gate(operation, "opening_or_closing_beat")
                return
            quiet = self.pusher._build_live_presence_context(include_last_speech=True)
            if quiet is None:
                self._record_gate(operation, "quiet_context_unavailable")
                return
            if quiet.sc_pending:
                self._record_gate(operation, "quiet_sc_pending")
                return
            if quiet.ai_waiting:
                self._record_gate(operation, "quiet_ai_busy")
                return
            if quiet.has_selected_danmaku or quiet.danmaku_rate_60s:
                self._record_gate(operation, "quiet_danmaku_active")
                return
            if quiet.silence_seconds < settings.stream.director_quiet_seconds:
                self._record_gate(operation, "quiet_threshold")
                return
            if (quiet.seconds_since_last_speech is not None
                and quiet.seconds_since_last_speech < settings.stream.director_speak_cooldown_seconds):
                self._record_gate(operation, "quiet_speak_cooldown")
                return
        if not settings.stream.live_presence_active_enabled:
            self._record_gate(operation, "live_presence_disabled")
            return
        if not settings.stream.director_enabled:
            self._record_gate(operation, "director_disabled")
            return
        director = self.pusher._director_runtime
        if not director:
            self._record_gate(operation, "director_unavailable")
            return
        block_reason = getattr(director, "thread_performance_block_reason", None)
        if callable(block_reason):
            reason = block_reason(safe, opening=operation == "open")
        elif director.thread_performance_allowed(safe, opening=operation == "open"):
            reason = None
        else:
            reason = "director_performance_disabled"
        if reason:
            self._record_gate(operation, reason)
            return
        self._record_outcome(operation, "attempt_scheduled")
        self.work = asyncio.create_task(self.perform(state, safe, seed, operation, self.epoch))

    @staticmethod
    def consume_task_result(task):
        if not task.cancelled():
            task.exception()

    def _record_semantic_hint_candidate(self, operation, result):
        if not settings.stream.live_presence_thread_semantic_hint_enabled:
            return
        if result.hint_invalid:
            reason = result.hint_invalid_reason or "invalid_shape"
            self.service.metrics.record_semantic_hint(operation, reason)
            if result.hint_action_error:
                self.service.metrics.record_semantic_hint(operation, result.hint_action_error)
        elif result.thread_update is None:
            self.service.metrics.record_semantic_hint(operation, "missing")
        else:
            action = result.thread_update.get("action")
            outcome = {
                "open": "valid_open",
                "advance": "valid_advance",
                "complete": "valid_complete",
            }.get(action)
            if outcome:
                self.service.metrics.record_semantic_hint(operation, outcome)

    def _operation_disabled_reason(self, operation):
        if not self.enabled() or self.closed:
            return "runtime_disabled"
        if not settings.stream.live_presence_active_enabled:
            return "live_presence_disabled"
        if not settings.stream.director_enabled:
            return "director_disabled"
        if not settings.stream.director_performance_enabled:
            return "director_performance_disabled"
        if operation == "open" and not settings.stream.live_presence_thread_opener_enabled:
            return "opener_disabled"
        if operation != "open" and not settings.stream.live_presence_thread_resume_active_enabled:
            return "resume_active_disabled"
        return None

    def operation_enabled(self, operation):
        return bool(self.enabled() and not self.closed
            and settings.stream.live_presence_active_enabled and settings.stream.director_enabled
            and settings.stream.director_performance_enabled
            and (settings.stream.live_presence_thread_opener_enabled if operation == "open"
                 else settings.stream.live_presence_thread_resume_active_enabled))

    async def perform(self, state, facts, seed, operation, epoch):
        reserved = None
        broadcast_started = False
        try:
            thread = self.service.current(state)
            if facts.get("semantic_context") is None:
                self._record_gate(operation, "semantic_context_missing")
                return
            from kangel.persona.application.engine import persona_engine
            from kangel.persona.application.stage_speech import generate_stage_speech, StageSpeechResult
            semantic = settings.stream.live_presence_thread_semantic_hint_enabled
            if operation == "open" and not semantic:
                emotion = "思考" if "思考" in AVAILABLE_EMOTIONS else next(iter(AVAILABLE_EMOTIONS))
                result = StageSpeechResult({"emotions": [emotion], "sentences": [{"emotion": emotion, "text": seed.opener}]})
                self._record_outcome(operation, "template_ready")
            else:
                self._record_outcome(operation, "generation_started")
                if semantic:
                    self.service.metrics.record_semantic_hint(operation, "generation_started")
                async def preflight():
                    current_facts, reason = await self.safe_facts_with_reason()
                    if not self.operation_enabled(operation):
                        reason = self._operation_disabled_reason(operation) or "generation_preempted"
                    elif epoch != self.epoch:
                        reason = "generation_preempted"
                    elif not current_facts:
                        reason = reason or "generation_preempted"
                    elif current_facts["mainline"].stream_session_id != state.stream_session_id:
                        reason = "session_changed"
                    elif thread and datetime.now(timezone.utc) >= parse_time(thread.expires_at):
                        reason = "thread_expired"
                    else:
                        return
                    self._record_gate(operation, reason)
                    if reason == "session_changed":
                        self._record_outcome(operation, "stale_session")
                    from kangel.integrations.ai.service import AIBackgroundBusy
                    raise AIBackgroundBusy("stage_preempted")
                self.generation = asyncio.create_task(generate_stage_speech(
                    persona_engine, facts=facts, thread=thread, seed=seed, operation=operation,
                    preflight=preflight,
                    on_validation_failure=lambda reason: self._record_outcome(
                        operation, "validation_" + reason)))
                self.generation.add_done_callback(self.consume_task_result)
                try:
                    result = await asyncio.shield(self.generation)
                except (asyncio.TimeoutError, TimeoutError):
                    self._record_outcome(operation, "generation_timeout")
                    self.service.metrics.record_error()
                    return
                except Exception as exc:
                    if type(exc).__name__ == "AIBackgroundBusy":
                        self._record_outcome(operation, "preempted")
                        return
                    self._record_outcome(operation, "generation_error")
                    raise
                if not result:
                    self._record_outcome(operation, "generation_invalid_output")
                    return
                self._record_outcome(operation, "generation_valid")
                self._record_semantic_hint_candidate(operation, result)
            if not result:
                self._record_outcome(operation, "generation_invalid_output")
                return
            try:
                public_stage_text(result.public_reply)
            except ValueError:
                self._record_gate(operation, "invalid_public_reply")
                return
            disabled_reason = self._operation_disabled_reason(operation)
            if epoch != self.epoch or disabled_reason:
                self._record_gate(operation, disabled_reason or "generation_preempted")
                self._record_outcome(operation, "preempted")
                return
            latest, latest_reason = await self.safe_facts_with_reason()
            if not latest:
                self._record_gate(operation, latest_reason or "unsafe_context_after_generation")
                self._record_outcome(operation, "preempted")
                return
            if not self.pusher.reply_preserves_activity_fact(result.public_reply):
                self._record_gate(operation, "activity_fact_mismatch")
                self._record_outcome(operation, "activity_fact_rejected")
                return
            if latest["mainline"].stream_session_id != state.stream_session_id:
                self._record_gate(operation, "session_changed")
                self._record_outcome(operation, "stale_session")
                return
            if latest["mainline"].beat_version != facts["mainline"].beat_version:
                self._record_gate(operation, "beat_changed")
                self._record_outcome(operation, "stale_beat")
                return
            if latest["activity"].version != facts["activity"].version:
                self._record_gate(operation, "activity_changed")
                self._record_outcome(operation, "stale_activity")
                return
            current = await asyncio.to_thread(self.service.presence.get, state.stream_session_id)
            if not current or current.version != state.version or current.status != "active":
                self._record_gate(operation, "presence_version_stale")
                self._record_outcome(operation, "stale_presence_version")
                return
            if thread and datetime.now(timezone.utc) >= parse_time(thread.expires_at):
                self._record_gate(operation, "thread_expired")
                return
            if thread and self.service.incompatibility(thread, latest):
                self._record_gate(operation, "thread_incompatible")
                return
            director = self.pusher._director_runtime
            block_reason = getattr(director, "thread_performance_block_reason", None)
            reason = block_reason(latest, opening=operation == "open") if callable(block_reason) else None
            if not callable(block_reason) and not director.thread_performance_allowed(latest, opening=operation == "open"):
                reason = "director_performance_disabled"
            if reason:
                self._record_gate(operation, reason)
                return
            reserved = await asyncio.to_thread(self.service.reserve, current, operation)
            if reserved is None:
                self._record_gate(operation, "attempt_reserve_conflict")
                self._record_outcome(operation, "attempt_reserve_conflict")
                return
            final, final_reason = await self.safe_facts_with_reason()
            final_mismatch = (
                "generation_preempted" if epoch != self.epoch else
                (final_reason or "prebroadcast_preempted") if not final else
                "session_changed" if final["mainline"].stream_session_id != state.stream_session_id else
                "beat_changed" if final["mainline"].beat_version != facts["mainline"].beat_version else
                "activity_changed" if final["activity"].version != facts["activity"].version else None
            )
            if final_mismatch:
                self._record_gate(operation, final_mismatch)
                self._record_outcome(operation, "prebroadcast_preempted")
                await asyncio.to_thread(self.service.finish_speech, reserved, latest, seed, delivered=False)
                self._record_outcome(operation, "known_undelivered")
                return
            delivery_guard_reason = None
            def delivery_guard():
                nonlocal delivery_guard_reason
                if not self.operation_enabled(operation):
                    delivery_guard_reason = self._operation_disabled_reason(operation) or "generation_preempted"
                    return False
                if epoch != self.epoch:
                    delivery_guard_reason = "generation_preempted"
                    return False
                if self.active_interruptions:
                    delivery_guard_reason = "active_interruption"
                    return False
                current_facts = self.pusher._build_director_context()
                if not current_facts:
                    delivery_guard_reason = "no_live_context"
                    return False
                if current_facts.get("semantic_context") is None:
                    delivery_guard_reason = "semantic_context_missing"
                    return False
                if current_facts.get("busy"):
                    delivery_guard_reason = self._busy_reason()
                    return False
                if current_facts["mainline"].stream_session_id != state.stream_session_id:
                    delivery_guard_reason = "session_changed"
                    return False
                if current_facts["mainline"].beat_version != facts["mainline"].beat_version:
                    delivery_guard_reason = "beat_changed"
                    return False
                if current_facts["activity"].version != facts["activity"].version:
                    delivery_guard_reason = "activity_changed"
                    return False
                return True
            broadcast_started = True
            sent = await self.pusher._broadcast_to_all({
                "type": "ai_reply", "data": {"source": "stream_director",
                    "stream_session_id": state.stream_session_id,
                    "beat_version": latest["mainline"].beat_version,
                    "reply": result.public_reply, "timestamp": datetime.now(timezone.utc).isoformat()},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, delivery_guard=delivery_guard)
            if delivery_guard_reason:
                self._record_gate(operation, delivery_guard_reason)
                self._record_outcome(operation, "delivery_guard_blocked")
            if sent:
                director.record_thread_performance(state.stream_session_id)
                try:
                    self.service.presence.stage_speech_journal.record(
                        speech_id=reserved.pending_speech_attempt["attempt_id"],
                        stream_session_id=state.stream_session_id, operation=operation,
                        reply=result.public_reply, seed_id=seed.seed_id,
                        beat_version=latest["mainline"].beat_version,
                        activity_version=latest["activity"].version, delivered_count=sent)
                except Exception:
                    # Review bookkeeping is never another stage failure or retry.
                    self.service.metric("review_errors")
            else:
                if not delivery_guard_reason:
                    self._record_gate(operation, "delivery_zero")
                self._record_outcome(operation, "delivery_zero")
            changed = await asyncio.to_thread(self.service.finish_speech, reserved, latest, seed,
                delivered=bool(sent), hint=result.thread_update, hint_invalid=result.hint_invalid,
                public_reply=result.public_reply)
            if sent:
                try:
                    self.service.presence.stage_speech_journal.mark_commit(
                        reserved.pending_speech_attempt["attempt_id"], changed is not None)
                except Exception:
                    self.service.metric("review_errors")
            if not changed:
                self.service.metric("delivery_commit_failed")
                self._record_outcome(operation, "delivery_commit_conflict")
            elif sent:
                self._record_outcome(operation, "delivery_commit_success")
            else:
                self._record_outcome(operation, "known_undelivered")
        except asyncio.CancelledError:
            # If sending may have happened the persisted attempt remains for
            # recovery; never replay an uncertain stage step.
            self._record_outcome(operation, "preempted")
            raise
        except Exception:
            self.service.metric("errors")
            self._record_outcome(operation, "broadcast_exception" if broadcast_started else "stage_error")
            logger.warning("SpeechThread performance failed; no public fallback")
        finally:
            self.retry_at = time.monotonic() + settings.stream.live_presence_thread_interval_seconds
            self.notify()
