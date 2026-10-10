"""Audience stage branch sharing the existing worker and physical generation slot."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import time
from uuid import uuid4

from config import settings
from kangel.audience.application.stage_presence import AudienceStagePresence, greeting_target
from kangel.shared.logging import logger
from .stage_speech_journal import public_stage_text


class AudienceGreetingStage:
    def __init__(self, owner):
        self.owner = owner
        self.tracker = AudienceStagePresence()

    def refresh(self, facts=None):
        if not settings.stream.audience_presence_enabled:
            return
        from kangel.transport.websocket.connection_manager import connection_manager
        from kangel.audience.application.presence_service import viewer_presence_coordinator
        session = (facts["mainline"].stream_session_id if facts
                   else self.owner.pusher._metadata.stream_session_id)
        self.tracker.sync(tuple(connection_manager.active_connections.values()), session,
                          viewer_presence_coordinator.presence_id, settings.stream,
                          grace_seconds=settings.rate_limit.ws_presence_grace_seconds)

    def active(self):
        return bool(self.owner.enabled() and not self.owner.closed
                    and settings.stream.audience_presence_enabled
                    and settings.stream.audience_greeting_active_enabled
                    and settings.stream.live_presence_active_enabled
                    and settings.stream.director_enabled
                    and settings.stream.director_performance_enabled)

    def wait_seconds(self):
        """Wake once when a new join becomes stable, not periodic model polling."""
        now = time.monotonic()
        waits = [c.joined_at + settings.stream.audience_join_stable_seconds - now
                 for c in self.tracker.pending.values()]
        return min([15.0] + [max(.05, w) for w in waits if w > 0])

    async def target(self, candidate):
        from kangel.audience.application.relationship_service import audience_relationship_manager
        relation = await asyncio.to_thread(audience_relationship_manager.peek,
            candidate.identity.current_nickname, identity=candidate.identity)
        return greeting_target(candidate.identity, relation)

    async def maybe_schedule(self, facts, state):
        if not settings.stream.audience_presence_enabled:
            return False
        candidate = self.tracker.next_candidate(settings.stream)
        if candidate is None:
            return False
        target, reason = await self.target(candidate)
        if not self.tracker.valid(candidate):
            return False
        if reason:
            self.tracker.finish(candidate, reason=reason)
            return False
        if not settings.stream.audience_greeting_active_enabled:
            self.tracker.finish(candidate, reason="shadow_eligible")
            return False
        if not self.active():
            self.tracker.record("active_disabled")
            return False
        safe = await self.owner.safe_facts()
        if not safe:
            self.tracker.record("busy")
            return False
        # Ordinary replies participate in the stage interval too, not just Director speech.
        if state is None:
            state = await asyncio.to_thread(self.owner.service.presence.get, candidate.session_id)
        if state and state.last_speech_at:
            from .speech_thread import parse_time
            if (datetime.now(timezone.utc) - parse_time(state.last_speech_at)).total_seconds() < min(
                settings.stream.audience_greeting_global_cooldown_seconds,
                settings.stream.live_presence_thread_interval_seconds,
            ):
                self.tracker.record("stage_cooldown")
                return False
        director = self.owner.pusher._director_runtime
        if director is None:
            return False
        reason = director.greeting_performance_block_reason(safe)
        if reason or (self.tracker.last_delivery and time.monotonic() - self.tracker.last_delivery
                      < settings.stream.audience_greeting_global_cooldown_seconds):
            self.tracker.record(reason or "stage_cooldown")
            return False
        # Do not insert a greeting inside an unfinished delivery/interruption.
        thread = self.owner.service.current(state) if state else None
        if state and state.pending_speech_attempt:
            return False
        if thread:
            from .speech_thread import parse_time
            if thread.interruption_id and not thread.interruption_visible:
                return False
            if (datetime.now(timezone.utc) - parse_time(thread.last_touched_at)).total_seconds() < settings.stream.live_presence_thread_interval_seconds:
                return False
        self.owner.work = asyncio.create_task(self.perform(candidate, target, safe, self.owner.epoch))
        return True

    def facts_match(self, candidate, facts, baseline, epoch):
        if not self.active() or self.owner.epoch != epoch or self.owner.active_interruptions:
            return False
        self.refresh(facts)
        return bool(self.tracker.valid(candidate) and facts and not facts.get("busy")
                    and facts.get("viewer_count", 0) > 0
                    and facts.get("semantic_context") is not None
                    and facts["mainline"].stream_session_id == candidate.session_id
                    and facts["mainline"].beat_version == baseline["mainline"].beat_version
                    and facts["activity"].version == baseline["activity"].version)

    async def perform(self, candidate, target, facts, epoch):
        owner = self.owner
        sent = False
        broadcast_started = False
        try:
            from kangel.persona.application.engine import persona_engine
            from kangel.persona.application.audience_greeting import generate_audience_greeting
            async def preflight():
                latest = await owner.safe_facts()
                if not self.facts_match(candidate, latest, facts, epoch):
                    from kangel.integrations.ai.service import AIBackgroundBusy
                    raise AIBackgroundBusy("greeting_preempted")
            await preflight()
            self.tracker.record("generation_started")
            def record_validation_failure(reason):
                self.tracker.record("invalid_output")
                self.tracker.record(reason)
            owner.generation = asyncio.create_task(generate_audience_greeting(
                persona_engine, facts=facts, target=target, preflight=preflight,
                on_validation_failure=record_validation_failure))
            owner.generation.add_done_callback(owner.consume_task_result)
            reply = await asyncio.shield(owner.generation)
            if not reply:
                return
            public_stage_text(reply)
            latest = await owner.safe_facts()
            if not self.facts_match(candidate, latest, facts, epoch):
                self.tracker.record("preempted")
                return
            # Memory opt-out/expiry/relationship changes cannot leave a stale familiar greeting.
            fresh_target, reason = await self.target(candidate)
            if reason or fresh_target != target:
                self.tracker.record(reason or "stale_facts")
                return
            if not owner.pusher.reply_preserves_activity_fact(reply):
                self.tracker.record("invalid_output")
                self.tracker.record("activity_fact_conflict")
                return
            director = owner.pusher._director_runtime
            if director.greeting_performance_block_reason(latest):
                self.tracker.record("stage_cooldown")
                return
            def guard():
                current = owner.pusher._build_director_context()
                return self.facts_match(candidate, current, facts, epoch)
            broadcast_started = True
            speech_id = str(uuid4())
            delivered = await owner.pusher._broadcast_to_all({
                "type": "ai_reply", "data": {"source": "stream_director",
                    "stream_session_id": candidate.session_id, "beat_version": latest["mainline"].beat_version,
                    "reply": reply, "timestamp": datetime.now(timezone.utc).isoformat()},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, delivery_guard=guard)
            if not delivered:
                self.tracker.record("delivery_zero")
                return
            sent = True
            self.tracker.finish(candidate, delivered=True)
            director.record_greeting_performance()
            try:
                owner.service.presence.stage_speech_journal.record(speech_id=speech_id,
                    stream_session_id=candidate.session_id, operation="audience_greeting", reply=reply,
                    beat_version=latest["mainline"].beat_version, activity_version=latest["activity"].version,
                    delivered_count=delivered)
            except Exception:
                self.tracker.record("review_error")
            def record_stage_time():
                presence = owner.service.presence
                from kangel.stream.domain.live_presence import LiveActType
                presence.get_or_create(stream_session_id=candidate.session_id)
                return presence.record_public_speech(stream_session_id=candidate.session_id,
                    action=LiveActType.SELF_TALK, reason_code=None,
                    mainline_beat_version=latest["mainline"].beat_version,
                    activity_version=latest["activity"].version)
            try:
                if await asyncio.to_thread(record_stage_time) is None:
                    self.tracker.record("state_commit_failed")
            except Exception:
                self.tracker.record("state_commit_failed")
        except asyncio.CancelledError:
            self.tracker.record("delivery_unknown" if broadcast_started else "preempted")
            raise
        except Exception as exc:
            self.tracker.record("delivery_unknown" if broadcast_started else
                                "preempted" if type(exc).__name__ == "AIBackgroundBusy" else "generation_error")
            logger.warning("Audience greeting suppressed; viewer pipeline unaffected")
        finally:
            # Never replay a failed/uncertain greeting; a future visit can create a new candidate.
            if not sent:
                if broadcast_started:
                    # Unknown delivery must not cause a burst of replacement greetings.
                    self.tracker.last_delivery = time.monotonic()
                self.tracker.finish(candidate)
            owner.notify()
