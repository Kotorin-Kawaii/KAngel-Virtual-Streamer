"""Bounded aggregate stage diagnostics. Never accepts evidence or letter text."""

from contextvars import ContextVar
from threading import Lock


current_impression_stage: ContextVar[str | None] = ContextVar("impression_stage", default=None)
_STAGES = frozenset({"archaeology", "merge", "synthesis", "writer", "critic", "repair"})


class ImpressionStageMetrics:
    def __init__(self):
        self._lock = Lock()
        self._attempts = {}
        self._validations = {}

    def attempt(self, *, role, provider, model, status, latency_ms, usage=None):
        stage = current_impression_stage.get()
        if stage not in _STAGES:
            return
        key = (stage, str(role), str(provider), str(model))
        with self._lock:
            if key not in self._attempts and len(self._attempts) >= 128:
                return
            row = self._attempts.setdefault(key, {
                "stage": stage, "role": role, "provider": provider, "model": model,
                "calls": 0, "successes": 0, "latency_ms": 0, "usage_reported": 0,
                "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            })
            row["calls"] += 1
            row["successes"] += int(status == "success")
            row["latency_ms"] += max(0, int(latency_ms))
            row["usage_reported"] += int(bool(usage))
            for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                row[field] += max(0, int((usage or {}).get(field) or 0))

    def validation(self, stage, outcome):
        if stage not in _STAGES or outcome not in {"accepted", "rejected", "cache_hit"}:
            return
        with self._lock:
            row = self._validations.setdefault(stage, {"accepted": 0, "rejected": 0, "cache_hit": 0})
            row[outcome] += 1

    def snapshot(self):
        from kangel.integrations.ai.pricing import cost_for_row
        with self._lock:
            rows = [dict(row) for row in self._attempts.values()]
            validations = {stage: dict(row) for stage, row in self._validations.items()}
        for row in rows:
            count = row["calls"]
            row["success_rate"] = row["successes"] / count
            row["average_latency_ms"] = row["latency_ms"] / count
            for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                row["average_" + field] = row[field] / row["usage_reported"] if row["usage_reported"] else None
            cost = cost_for_row(row)
            # Missing usage/price is unknown, never a fabricated zero cost.
            row["estimated_cost"] = cost if row["usage_reported"] else None
            amount = cost.get("amount")
            row["average_known_cost"] = amount / row["usage_reported"] if amount is not None and row["usage_reported"] else None
        return {"scope": "current_process", "attempts": rows, "validations": validations}


impression_stage_metrics = ImpressionStageMetrics()
