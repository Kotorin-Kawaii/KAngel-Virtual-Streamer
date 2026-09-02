"""注意力闸门的判定计数（延迟优化 v1 §A）。

为什么要单独一个计数器：`reply_timing_metrics` 记的是**耗时**，回答不了
「这一轮为什么没有回复」。而这个问题必须能被回答，否则三种完全不同的情况会
被混成一句「没选中」：

1. `ignored_by_attention` —— 主播**主动**决定这一轮谁都不读。这是有语义价值的
   自主决策，是产品行为。
2. `deferred_due_to_capacity` —— AI 并发闸门满，这一轮**根本没问模型**。
3. `deferred_due_to_model_failure` / `deferred_due_to_invalid_output` /
   `deferred_due_to_local_error` —— 系统故障或模型输出不可解析。

把 2、3 记成 1 就等于把系统故障伪装成主播的性格，这是明确禁止的：
延迟审计里「Attention 仍能 ignore」这条回归会因此变得无法验证 ——
忽略率上升到底是模型变木了还是供应商在报 401，读计数才分得清。

只记低基数标量：没有弹幕正文、昵称、账号、IP，也没有供应商与模型 ID。
"""

from __future__ import annotations

from enum import Enum
import threading
from typing import Any, Dict, Optional


class AttentionOutcome(str, Enum):
    """一次注意力闸门判定的结果。

    只有 ``SELECTED`` 与 ``IGNORED`` 是主播的自主决策；其余四项都是
    「这一轮先不处理」（DEFER），候选**不被 claim、不被标记已回复、不被删除**，
    后续 tick 仍可重新参与判定。
    """

    SELECTED = "selected"
    IGNORED = "ignored_by_attention"
    DEFERRED_CAPACITY = "deferred_due_to_capacity"
    DEFERRED_MODEL_FAILURE = "deferred_due_to_model_failure"
    DEFERRED_INVALID_OUTPUT = "deferred_due_to_invalid_output"
    DEFERRED_LOCAL_ERROR = "deferred_due_to_local_error"

    @property
    def is_deferral(self) -> bool:
        return self not in (AttentionOutcome.SELECTED, AttentionOutcome.IGNORED)

    @property
    def is_autonomous(self) -> bool:
        """是不是主播自己做的决定（而不是系统状况导致的让行）。"""
        return not self.is_deferral


class AttentionGateMetrics:
    """固定键集合的计数器；键集合固定，所以天然有界。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {item.value: 0 for item in AttentionOutcome}
        self._candidates_seen = 0
        self._last_outcome: Optional[str] = None

    def record(self, outcome: AttentionOutcome, *, candidate_count: int = 0) -> None:
        try:
            with self._lock:
                self._counts[outcome.value] = self._counts.get(outcome.value, 0) + 1
                self._candidates_seen += max(0, int(candidate_count))
                self._last_outcome = outcome.value
        except Exception:  # pragma: no cover - 观测绝不影响业务
            pass

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counts = dict(self._counts)
            candidates_seen = self._candidates_seen
            last_outcome = self._last_outcome
        decided = counts[AttentionOutcome.SELECTED.value] + counts[AttentionOutcome.IGNORED.value]
        deferred = sum(
            counts[item.value] for item in AttentionOutcome if item.is_deferral
        )
        return {
            "counts": counts,
            # 分母刻意用「主播真的做了决定」的次数，而不是全部判定次数：
            # 把 DEFER 算进分母会让忽略率随供应商故障而漂移，那个数就不可读了。
            "autonomous_decisions": decided,
            "deferrals": deferred,
            "ignore_rate": round(counts[AttentionOutcome.IGNORED.value] / decided, 4)
            if decided else None,
            "candidates_seen": candidates_seen,
            "last_outcome": last_outcome,
            "policy": {
                "on_saturation": "defer",
                "on_model_failure": "defer",
                "on_invalid_output": "defer",
                "note": "DEFER 不 claim、不标记已回复、不删除候选，也不计入 ignore_rate",
            },
        }

    def clear(self) -> None:
        with self._lock:
            for key in self._counts:
                self._counts[key] = 0
            self._candidates_seen = 0
            self._last_outcome = None


attention_gate_metrics = AttentionGateMetrics()

__all__ = ["AttentionOutcome", "AttentionGateMetrics", "attention_gate_metrics"]
