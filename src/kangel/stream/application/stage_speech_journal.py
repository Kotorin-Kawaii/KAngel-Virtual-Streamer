"""Bounded Admin review of actually delivered autonomous speech, not chat memory."""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock

from config.emotion_catalog import AVAILABLE_EMOTIONS


def public_stage_text(reply: dict) -> str:
    """Project only the validated public sentences; reject unbounded/raw data."""
    if not isinstance(reply, dict):
        raise ValueError("invalid public stage reply")
    sentences = reply.get("sentences")
    if not isinstance(sentences, list) or not 1 <= len(sentences) <= 2:
        raise ValueError("invalid public stage sentences")
    parts = []
    for item in sentences:
        if not isinstance(item, dict) or item.get("emotion") not in AVAILABLE_EMOTIONS:
            raise ValueError("invalid public stage emotion")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or any(ord(c) < 32 for c in text):
            raise ValueError("invalid public stage text")
        parts.append(text.strip())
    text = "".join(parts)
    if len(text) > 160:
        raise ValueError("public stage text too long")
    return text


class StageSpeechJournal:
    """Last 32 public receipts per process. No persistence, logging or model input."""

    MAX_RECORDS = 32

    def __init__(self):
        self._records = deque(maxlen=self.MAX_RECORDS)
        self._lock = Lock()

    def record(self, *, speech_id, stream_session_id, operation, reply,
               beat_version, activity_version, seed_id=None, delivered_count=0):
        if delivered_count <= 0:
            return
        if operation not in {"open", "advance", "resume", "director_speak", "audience_greeting"}:
            raise ValueError("invalid stage operation")
        text = public_stage_text(reply)
        row = {
            "speech_id": str(speech_id)[:64], "stream_session_id": str(stream_session_id)[:160],
            "operation": operation, "text": text, "occurred_at": datetime.now(timezone.utc).isoformat(),
            "beat_version": beat_version, "activity_version": activity_version,
            "seed_id": str(seed_id)[:64] if seed_id else None,
            "state_committed": None,
        }
        with self._lock:
            if any(r["speech_id"] == row["speech_id"] for r in self._records):
                return
            self._records.append(row)

    def mark_commit(self, speech_id, committed):
        with self._lock:
            for row in self._records:
                if row["speech_id"] == speech_id:
                    row["state_committed"] = bool(committed)
                    return

    def recent_seed_ids(self, session_id):
        with self._lock:
            return tuple(r["seed_id"] for r in self._records
                         if r["stream_session_id"] == session_id and r["operation"] == "open")

    def snapshot(self):
        with self._lock:
            return {"scope": "process_since_start", "max_records": self.MAX_RECORDS,
                    "restart_clears_records": True, "records": deepcopy(list(self._records))}
