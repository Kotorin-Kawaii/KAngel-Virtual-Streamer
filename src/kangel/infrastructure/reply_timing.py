"""有界的回复链阶段耗时观测；不记录弹幕或观众身份。"""

from __future__ import annotations

from collections import defaultdict, deque
from math import ceil
import threading


_STAGES = {
    "context", "qa_selection", "impact_analysis", "prompt_build",
    "reply_model", "output_validation", "state_commit", "broadcast", "total",
    # 延迟优化 v1 §2：补齐注意力闸门、审核、并行关键路径、提交细分与到达锚点。
    "attention", "moderation", "parallel_context_critical_path",
    "memory_commit", "reply_record", "read_to_reply", "arrival_to_reply",
    # 单次 API attempt 的耗时，与上面的「逻辑阶段」分开统计：
    # 带回退的一次逻辑调用会产生多次 attempt，两者混在一起会低估回退成本。
    "api_attempt",
}
_PATHS = {"normal", "sc"}
_OUTCOMES = {"success", "error", "skipped", "degraded", "retry", "fallback"}
_MODEL_ROLES = {
    "none", "qa", "impact", "reply", "attention", "moderation",
    "intent_shadow", "session_memory", "stream_director", "default",
}


class ReplyTimingMetrics:
    """进程内滑动样本，提供阶段 P50/P95/P99，避免无限内存增长。"""

    def __init__(self, max_samples_per_series: int = 512):
        self.max_samples_per_series = max(1, int(max_samples_per_series))
        self._samples: dict[tuple[str, str, str, str], deque[float]] = defaultdict(
            lambda: deque(maxlen=self.max_samples_per_series)
        )
        self._lock = threading.Lock()

    def record(
        self,
        stage: str,
        duration_ms: float,
        *,
        path: str,
        outcome: str = "success",
        model_role: str = "none",
    ) -> None:
        key = (
            stage if stage in _STAGES else "other",
            path if path in _PATHS else "other",
            outcome if outcome in _OUTCOMES else "other",
            model_role if model_role in _MODEL_ROLES else "other",
        )
        with self._lock:
            self._samples[key].append(max(0.0, float(duration_ms)))

    def snapshot(self) -> dict:
        with self._lock:
            rows = {
                self._series_key(key): self._percentiles(list(samples))
                for key, samples in sorted(self._samples.items())
                if samples
            }
        return {
            "series": rows,
            "sample_policy": {
                "max_samples_per_series": self.max_samples_per_series,
                "labels": ["stage", "path", "outcome", "model_role"],
                "excludes": ["message", "account_id", "nickname", "ip", "model_id"],
            },
        }

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()

    @staticmethod
    def _series_key(key: tuple[str, str, str, str]) -> str:
        stage, path, outcome, model_role = key
        return f"{stage}:{path}:{outcome}:{model_role}"

    @staticmethod
    def _percentiles(samples: list[float]) -> dict:
        ordered = sorted(samples)

        def percentile(ratio: float) -> float:
            index = max(0, ceil(len(ordered) * ratio) - 1)
            return round(ordered[index], 3)

        return {
            "count": len(ordered),
            "p50_ms": percentile(0.50),
            "p95_ms": percentile(0.95),
            "p99_ms": percentile(0.99),
            "max_ms": round(ordered[-1], 3),
        }


reply_timing_metrics = ReplyTimingMetrics()
