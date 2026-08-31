"""Prompt 分层预算的低基数聚合观测，不保存提示词或观众内容。"""

from collections import Counter, defaultdict
from threading import Lock


class PromptBudgetMetrics:
    def __init__(self):
        self._chars = defaultdict(int)
        self._samples = Counter()
        self._truncated = Counter()
        self._lock = Lock()

    def record(self, layers: list[tuple[str, str, int]]) -> None:
        with self._lock:
            for name, content, limit in layers:
                self._chars[name] += len(content)
                self._samples[name] += 1
                if len(content) >= limit:
                    self._truncated[name] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {name: {
                "samples": self._samples[name],
                "average_chars": round(self._chars[name] / self._samples[name], 1),
                "truncated": self._truncated[name],
            } for name in sorted(self._samples)}


prompt_budget_metrics = PromptBudgetMetrics()
