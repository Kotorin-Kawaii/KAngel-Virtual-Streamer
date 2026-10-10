"""Transient audience facts and greeting candidates. Never chat/relationship storage."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import time

from kangel.audience.domain.identity import ViewerIdentity
from .relationship_expression import relationship_expression


def audience_counts(connections):
    accounts = {c.identity.subject_id for c in connections if c.identity.is_authenticated}
    guests = sum(not c.identity.is_authenticated for c in connections)
    return {
        "connection_count": len(connections), "authenticated_viewer_count": len(accounts),
        "guest_connection_count": guests, "online_subject_count": len(accounts) + guests,
        "count_semantics": "connected_accounts_plus_guest_connections_not_verified_humans",
    }


@dataclass(frozen=True)
class GreetingCandidate:
    # Internal only. Do not serialize this object into prompts, observations or logs.
    identity: ViewerIdentity
    visit_id: str
    session_id: str
    joined_at: float
    expires_at: float


class AudienceStagePresence:
    MAX_PENDING = 32  # memory bound, not a speech budget
    REASONS = frozenset({
        "joined", "bootstrap_suppressed", "nickname_changed", "departed", "expired",
        "viewer_cooldown", "queue_full", "shadow_eligible", "low_trust", "invalid_nickname",
        "busy", "stage_cooldown", "active_disabled", "generation_started", "invalid_output",
        "generation_error", "preempted", "stale_facts", "delivery_zero", "delivery_unknown",
        "delivered", "state_commit_failed", "review_error", "reconnect_suppressed",
        "missing_reply", "non_string", "invalid_json", "not_object", "invalid_emotions",
        "invalid_sentences", "invalid_sentence_shape", "invalid_sentence_emotion",
        "invalid_sentence_text", "reply_too_long", "emotion_mismatch",
        "greeting_sentence_count", "greeting_too_long", "activity_fact_conflict",
    })
    INVALID_OUTPUT_REASONS = frozenset({
        "missing_reply", "non_string", "invalid_json", "not_object", "invalid_emotions",
        "invalid_sentences", "invalid_sentence_shape", "invalid_sentence_emotion",
        "invalid_sentence_text", "reply_too_long", "emotion_mismatch",
        "greeting_sentence_count", "greeting_too_long", "activity_fact_conflict",
    })

    def __init__(self):
        self.session_id = None
        self.entries = {}
        self.pending = {}
        self.last_greeted = {}
        self.departed_visits = {}
        self.last_delivery = 0.0
        self.version = 0
        self.counts = audience_counts([])
        self.metrics = Counter()

    def record(self, reason):
        if reason in self.REASONS:
            self.metrics[reason] += 1

    def sync(self, connections, session_id, presence_id, config, now=None, grace_seconds=5):
        now = time.monotonic() if now is None else now
        current = {}
        for connection in connections:
            identity = connection.identity
            key = identity.subject_id
            token = presence_id(key) if identity.is_authenticated else connection.id
            token = token or connection.id
            previous = current.get(key)
            if previous is None or (identity.nickname_version or 0) > (previous[0].nickname_version or 0):
                current[key] = (identity, token)
        # Authenticated identity carries a connection-scoped field. A new tab or
        # reconnect must not invalidate the same account/visit/display identity.
        for key, (identity, token) in list(current.items()):
            previous = self.entries.get(key)
            if previous and previous[1] == token and (
                previous[0].current_nickname == identity.current_nickname
                and previous[0].nickname_version == identity.nickname_version
            ):
                current[key] = previous
        baseline = self.session_id != session_id
        if baseline:
            self.pending.clear()
            self.entries.clear()
            self.departed_visits.clear()
            self.session_id = session_id
        if current != self.entries:
            self.version += 1
        for key in set(self.entries) - set(current):
            self.departed_visits[key] = (self.entries[key][1], now + max(0, grace_seconds))
            if self.pending.pop(key, None):
                self.record("departed")
        for key, (identity, token) in current.items():
            previous = self.entries.get(key)
            if previous == (identity, token):
                continue
            self.pending.pop(key, None)
            if previous and previous[1] == token:
                self.record("nickname_changed")
                continue
            if not identity.is_authenticated or not session_id:
                continue
            if baseline:
                self.record("bootstrap_suppressed")
                continue
            departed = self.departed_visits.get(key)
            if departed and departed[0] == token and now < departed[1]:
                self.record("reconnect_suppressed")
                continue
            self.record("joined")
            if key in self.last_greeted and now - self.last_greeted[key] < config.audience_greeting_viewer_cooldown_seconds:
                self.record("viewer_cooldown")
                continue
            if len(self.pending) >= self.MAX_PENDING:
                self.record("queue_full")
                continue
            self.pending[key] = GreetingCandidate(identity, token, session_id, now,
                now + config.audience_greeting_candidate_ttl_seconds)
        self.entries = current
        self.departed_visits = {k: v for k, v in self.departed_visits.items() if now < v[1]}
        self.counts = audience_counts(connections)
        for key, candidate in list(self.pending.items()):
            if now >= candidate.expires_at:
                del self.pending[key]
                self.record("expired")
        # Forget cooldown identities once no longer needed; no unbounded account history.
        self.last_greeted = {k: v for k, v in self.last_greeted.items()
                             if now - v < config.audience_greeting_viewer_cooldown_seconds}

    def next_candidate(self, config, now=None):
        now = time.monotonic() if now is None else now
        return next((c for c in self.pending.values()
                     if now < c.expires_at and now - c.joined_at >= config.audience_join_stable_seconds), None)

    def valid(self, candidate, now=None):
        now = time.monotonic() if now is None else now
        return bool(self.session_id == candidate.session_id and now < candidate.expires_at
                    and self.pending.get(candidate.identity.subject_id) == candidate
                    and self.entries.get(candidate.identity.subject_id) == (candidate.identity, candidate.visit_id))

    def finish(self, candidate, delivered=False, reason=None, now=None):
        if self.pending.get(candidate.identity.subject_id) == candidate:
            self.pending.pop(candidate.identity.subject_id, None)
        if delivered:
            now = time.monotonic() if now is None else now
            self.last_delivery = now
            self.last_greeted[candidate.identity.subject_id] = now
            self.record("delivered")
        elif reason:
            self.record(reason)

    def snapshot(self):
        return {"counts": dict(self.counts), "presence_version": self.version,
                "pending_candidates": len(self.pending), "metrics": dict(self.metrics),
                "invalid_output_reasons": {
                    reason: int(self.metrics.get(reason, 0))
                    for reason in sorted(self.INVALID_OUTPUT_REASONS)
                },
                "scope": "process_since_start", "greeting_budgets": "none"}


def greeting_target(identity, relationship):
    """Only public display name and coarse expression controls may reach Persona."""
    nickname = identity.current_nickname.strip()
    if not nickname or len(nickname) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in nickname):
        return None, "invalid_nickname"
    if relationship is not None and (relationship.trust < .25 or relationship.boundary_strikes >= 3):
        return None, "low_trust"
    expression = relationship_expression(relationship)
    return {"display_name": nickname,
            "familiarity": "new" if expression["level"] == "new" else "familiar",
            "warmth": expression["warmth"]}, None
