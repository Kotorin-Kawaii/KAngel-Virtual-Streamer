"""Immutable v2 snapshot projection. No AI calls or memory writes."""

from __future__ import annotations

import hashlib
import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Callable


SCHEMA_VERSION = "viewer_impression_deep_reflection_v2"
EVIDENCE_CATEGORIES = (
    "conversation_fragments", "topic_memories", "episodic_memories", "nickname_history",
)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def interaction_periods(fragments: list[dict[str, Any]], *, gap_days: int = 7) -> list[dict[str, Any]]:
    """Observed periods, not a claim about activity absent from retained data."""
    periods: list[dict[str, Any]] = []
    last = None
    for item in sorted(fragments, key=lambda row: parse_time(row["created_at"])):
        instant = parse_time(item["created_at"])
        if last is None or instant - last > timedelta(days=gap_days):
            periods.append({"start": item["created_at"], "end": item["created_at"],
                            "interaction_count": 0, "evidence_ids": []})
        period = periods[-1]
        period["end"] = item["created_at"]
        period["interaction_count"] += 1
        period["evidence_ids"].append(item["id"])
        last = instant
    return periods


def build_evidence_snapshot(pool: dict[str, Any], *, cutoff_at: str,
                            stable_persona: str,
                            sanitize: Callable[[Any, int], str]) -> dict[str, Any]:
    """Allowlist every outward field even if the repository adds more later.

    `sanitize` must apply the existing memory privacy policy plus credential,
    network, payment and moderation redactions. Caller freezes the result once.
    Nicknames are only obtained from retained nickname rows, never conversations.
    Optional resolved_reference and room joins are deliberately not exposed:
    the existing free-form payload has no account-safe reference contract.
    """
    previous = pool.get("previous_cutoff_at")
    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evidence_cutoff_at": cutoff_at,
        "previous_cutoff_at": previous,
        "stable_persona": stable_persona,
        "persona_version": hashlib.sha256(stable_persona.encode("utf-8")).hexdigest(),
    }
    relationship = pool.get("relationship") or {}
    snapshot["relationship"] = {
        "id": "relationship:timeline",
        **{key: relationship[key] for key in (
            "first_seen_at", "last_seen_at", "interaction_count", "reply_count"
        ) if relationship.get(key) is not None},
        "recent_topics": [sanitize(topic, 300) for topic in relationship.get("recent_topics", [])[:50]],
    }
    fragments = []
    for row in pool.get("conversation_fragments", []):
        fragments.append({
            "id": f"fragment:{row['id']}",
            "viewer_message": sanitize(row.get("viewer_message"), 10000),
            "streamer_reply": sanitize(row.get("streamer_reply"), 10000),
            "topic_label": sanitize(row.get("topic_label"), 300),
            "transition": sanitize(row.get("transition"), 80),
            "sentiment": float(row.get("sentiment") or 0),
            "importance": float(row.get("importance") or 0),
            "created_at": row["created_at"],
            "session_scope_id": str(row["session_scope_id"]),
            "archived": bool(row.get("archived")),
        })
    snapshot["conversation_fragments"] = fragments
    snapshot["topic_memories"] = [{
        "id": f"topic:{row['id']}", "topic": sanitize(row.get("topic_label"), 300),
        "summary": sanitize(row.get("summary"), 10000),
        "source_count": int(row.get("source_count") or 0),
        "importance": float(row.get("importance") or 0),
        "first_seen_at": row["first_seen_at"], "last_seen_at": row["last_seen_at"],
    } for row in pool.get("topic_memories", [])]
    snapshot["episodic_memories"] = [{
        "id": f"episodic:{row['memory_id']}",
        **{key: sanitize(row.get(key), 10000) for key in (
            "event_type", "topic", "summary", "why_notable", "emotional_mark", "follow_up_hint"
        )},
        "salience": float(row.get("salience") or 0), "occurred_at": row["occurred_at"],
    } for row in pool.get("episodic_memories", [])]
    snapshot["nickname_history"] = [{
        "id": f"nickname:{row['version']}", "nickname": sanitize(row.get("nickname"), 200),
        "started_at": row["started_at"], "ended_at": row.get("ended_at"),
        "is_current": bool(row.get("is_current")),
    } for row in pool.get("nickname_history", [])]
    snapshot["interaction_periods"] = interaction_periods(fragments)
    snapshot["periods_scope"] = "retained_selected_fragments_only; gaps do not prove absence"
    historical, delta = [], []
    for category, date_field in zip(EVIDENCE_CATEGORIES, (
        "created_at", "last_seen_at", "occurred_at", "started_at"
    )):
        for row in snapshot[category]:
            is_new = previous is None or parse_time(row[date_field]) > parse_time(previous)
            (delta if is_new else historical).append(row["id"])
    snapshot["historical_evidence_ids"] = historical
    snapshot["recent_delta_evidence_ids"] = delta
    return snapshot


def evidence_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = [row for category in EVIDENCE_CATEGORIES for row in snapshot.get(category, [])]
    relationship = snapshot.get("relationship") or {}
    if relationship.get("id"):
        entries.append(relationship)
    index = {row["id"]: row for row in entries}
    if len(index) != len(entries):
        raise ValueError("duplicate_evidence_id")
    return index


def representative_excerpts(snapshot: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    """Only backend-owned text, never quotes supplied by a model."""
    index = evidence_index(snapshot)
    if any(ref not in index for ref in ids):
        raise ValueError("unknown_evidence_id")
    return [copy.deepcopy(index[ref]) for ref in dict.fromkeys(ids)]
