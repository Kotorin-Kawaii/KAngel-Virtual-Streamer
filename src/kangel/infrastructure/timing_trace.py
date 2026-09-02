"""单条弹幕的端到端时序追踪（延迟优化 v1 §2）。

为什么要在 `reply_timing_metrics` 之外再来一层：分位数只能告诉你「哪个阶段慢」，
不能告诉你「这一条为什么慢」。同一条弹幕上的一串检查点才能把 6 秒的注意力闸门
拆成「模型 4s + 本地锁等待 2s」，也才能把「一次逻辑调用」与「三次 API attempt」
分开——带回退的调用在分位数里会被平均掉。

三条硬约束（来自需求 §2）：
  1. **不记录任何正文**：只存检查点名与 `perf_counter()` 时刻，弹幕原文、昵称、
     账号一律不进这里；对外快照用自增序号，原始 danmaku_id 只在 DEBUG 日志出现。
  2. **绝不阻塞、绝不抛**：所有公开方法都吞异常，失败只丢这一条追踪。
  3. **有界**：未完成与已完成的追踪都用上限容器，长直播不会涨内存。
"""

from __future__ import annotations

from collections import OrderedDict, deque
from contextvars import ContextVar
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .reply_timing import reply_timing_metrics

logger = logging.getLogger(__name__)

# 需求 §2 点名的检查点；未列出的名字会被丢弃，避免拼错后静默产生垃圾序列。
_CHECKPOINTS = frozenset({
    "received_at", "pool_ready_at",
    "attention_started_at", "attention_finished_at",
    "context_started_at", "context_finished_at",
    "moderation_started_at", "moderation_finished_at",
    "qa_started_at", "qa_finished_at",
    "impact_started_at", "impact_finished_at",
    "parallel_started_at", "parallel_finished_at",
    "prompt_started_at", "prompt_finished_at",
    "reply_llm_started_at", "reply_llm_finished_at",
    "validation_finished_at",
    "persona_commit_finished_at", "memory_commit_finished_at",
    "reply_record_finished_at", "broadcast_at",
})

# 派生指标 → (起点检查点, 终点检查点)；缺任一端就不产出该项（不猜数字）。
_DERIVED: Tuple[Tuple[str, str, str], ...] = (
    ("attention_ms", "attention_started_at", "attention_finished_at"),
    ("context_ms", "context_started_at", "context_finished_at"),
    ("moderation_ms", "moderation_started_at", "moderation_finished_at"),
    ("qa_ms", "qa_started_at", "qa_finished_at"),
    ("impact_ms", "impact_started_at", "impact_finished_at"),
    ("parallel_context_critical_path_ms", "parallel_started_at", "parallel_finished_at"),
    ("prompt_build_ms", "prompt_started_at", "prompt_finished_at"),
    ("reply_llm_ms", "reply_llm_started_at", "reply_llm_finished_at"),
    ("validation_ms", "reply_llm_finished_at", "validation_finished_at"),
    ("persona_commit_ms", "validation_finished_at", "persona_commit_finished_at"),
    ("memory_commit_ms", "persona_commit_finished_at", "memory_commit_finished_at"),
    ("reply_record_ms", "memory_commit_finished_at", "reply_record_finished_at"),
    ("broadcast_ms", "reply_record_finished_at", "broadcast_at"),
    ("read_to_reply_ms", "attention_finished_at", "broadcast_at"),
    ("arrival_to_reply_ms", "received_at", "broadcast_at"),
)

# 派生指标 → 喂给 reply_timing_metrics 的 (stage, model_role)。
# 只喂 engine/routes 尚未直接记录的序列：context / qa_selection / impact_analysis /
# prompt_build / reply_model / output_validation / state_commit / broadcast 已经在
# 业务代码里 record 过，这里再喂一遍会让同一次调用在分位数里出现两次。
_SERIES: Dict[str, Tuple[str, str]] = {
    "attention_ms": ("attention", "attention"),
    "parallel_context_critical_path_ms": ("parallel_context_critical_path", "none"),
    "memory_commit_ms": ("memory_commit", "none"),
    "reply_record_ms": ("reply_record", "none"),
    "read_to_reply_ms": ("read_to_reply", "none"),
    "arrival_to_reply_ms": ("arrival_to_reply", "none"),
}

# AIService 的角色名 → ``reply_timing._MODEL_ROLES`` 里的业务标签。
# 必须在这里归一，否则 attempt 序列会撞上 reply_timing 的白名单：
# danmaku_selector / qa_selector / impact_analysis 都不在名单里，会被统一归成
# ``other``，三个角色的 attempt 分位数糊成一条，「按 role 给 P50/P95」就没了。
# 归一到既有词表而不是把三个新名字加进白名单，是为了同一件事只有一个名字
# （engine.py 记阶段时用的就是 qa / impact / reply / attention）。
_METRICS_ROLE: Dict[str, str] = {
    "danmaku_selector": "attention",
    "qa_selector": "qa",
    "impact_analysis": "impact",
    "default": "reply",
}


def _metrics_role(role: str) -> str:
    return _METRICS_ROLE.get(str(role), str(role))



class TimingTrace:
    """一条弹幕的检查点集合；只存时刻与角色计数，不存任何正文。"""

    __slots__ = ("seq", "path", "started", "checkpoints", "attempts", "notes")

    def __init__(self, seq: int, path: str, started: float):
        self.seq = seq
        self.path = path
        self.started = started
        self.checkpoints: Dict[str, float] = {}
        # 每个元素是一次真实 API attempt：(role, status, latency_ms)。
        self.attempts: List[Tuple[str, str, int]] = []
        self.notes: Dict[str, Any] = {}


class TimingTraceRecorder:
    """有界的追踪表；对外只暴露自增序号，原始弹幕标识不出现在快照里。"""

    def __init__(self, *, max_open: int = 64, max_completed: int = 64):
        self.max_open = max(1, int(max_open))
        self.max_completed = max(1, int(max_completed))
        self._open: "OrderedDict[str, TimingTrace]" = OrderedDict()
        self._completed: deque = deque(maxlen=self.max_completed)
        self._lock = threading.Lock()
        self._seq = 0
        self._stats = {"started": 0, "completed": 0, "abandoned": 0, "dropped": 0}

    # ---- 采集 ---------------------------------------------------------------

    def start(self, trace_id: str, *, path: str = "normal") -> None:
        if not trace_id:
            return
        try:
            now = time.perf_counter()
            with self._lock:
                self._seq += 1
                trace = TimingTrace(self._seq, path, now)
                trace.checkpoints["received_at"] = now
                self._open[trace_id] = trace
                self._open.move_to_end(trace_id)
                self._stats["started"] += 1
                while len(self._open) > self.max_open:
                    self._open.popitem(last=False)
                    self._stats["abandoned"] += 1
        except Exception:  # pragma: no cover - 观测不允许影响业务
            logger.debug("时序追踪 start 失败，已忽略")

    def mark(self, trace_id: Optional[str], checkpoint: str) -> None:
        """打一个检查点；同名重复只保留第一次，避免循环里越写越晚。"""
        if not trace_id or checkpoint not in _CHECKPOINTS:
            return
        try:
            now = time.perf_counter()
            with self._lock:
                trace = self._open.get(trace_id)
                if trace is None:
                    self._stats["dropped"] += 1
                    return
                trace.checkpoints.setdefault(checkpoint, now)
        except Exception:  # pragma: no cover
            logger.debug("时序追踪 mark 失败，已忽略")

    def mark_at(self, trace_id: Optional[str], checkpoint: str, moment: float) -> None:
        """补记一个已经发生的时刻。

        注意力闸门开始时还不知道会选中哪一条，所以调用方先用 `perf_counter()`
        记住起点，选出结果后再补到那一条的追踪上。
        """
        if not trace_id or checkpoint not in _CHECKPOINTS:
            return
        try:
            with self._lock:
                trace = self._open.get(trace_id)
                if trace is None:
                    self._stats["dropped"] += 1
                    return
                trace.checkpoints.setdefault(checkpoint, float(moment))
        except Exception:  # pragma: no cover
            logger.debug("时序追踪 mark_at 失败，已忽略")

    def record_attempt(
        self, trace_id: Optional[str], *, role: str, status: str, latency_ms: int
    ) -> None:
        """记一次 API attempt；一次逻辑调用回退三家就会有三条。"""
        if not trace_id:
            return
        try:
            with self._lock:
                trace = self._open.get(trace_id)
                if trace is None:
                    return
                if len(trace.attempts) < 32:
                    trace.attempts.append(
                        (_metrics_role(role), str(status), max(0, int(latency_ms)))
                    )
        except Exception:  # pragma: no cover
            logger.debug("时序追踪 record_attempt 失败，已忽略")

    def note(self, trace_id: Optional[str], key: str, value: Any) -> None:
        """挂一个非敏感标注（如 candidate_count、是否走了本地兜底）。"""
        if not trace_id or not isinstance(value, (int, float, bool, str)):
            return
        try:
            with self._lock:
                trace = self._open.get(trace_id)
                if trace is not None and len(trace.notes) < 16:
                    trace.notes[str(key)[:32]] = value if not isinstance(value, str) else value[:32]
        except Exception:  # pragma: no cover
            logger.debug("时序追踪 note 失败，已忽略")

    def finish(self, trace_id: Optional[str], *, outcome: str = "success") -> Optional[Dict[str, Any]]:
        """结算一条追踪：算派生指标、喂分位数、写 DEBUG 日志。"""
        if not trace_id:
            return None
        try:
            with self._lock:
                trace = self._open.pop(trace_id, None)
            if trace is None:
                return None
            summary = self._summarize(trace, outcome)
            for name, (stage, model_role) in _SERIES.items():
                value = summary["derived"].get(name)
                if value is not None:
                    reply_timing_metrics.record(
                        stage, value, path=trace.path,
                        outcome=outcome, model_role=model_role,
                    )
            for role, status, latency in trace.attempts:
                reply_timing_metrics.record(
                    "api_attempt", latency, path=trace.path,
                    outcome="success" if status == "success" else "error",
                    model_role=role,
                )
            with self._lock:
                self._completed.append(summary)
                self._stats["completed"] += 1
            # 原始弹幕标识只出现在 DEBUG 日志，便于现场对齐；快照里一律只有 seq。
            logger.debug(
                "时序追踪 #%d danmaku=%s outcome=%s %s",
                trace.seq, trace_id, outcome,
                " ".join(f"{k}={v}" for k, v in summary["derived"].items()),
            )
            return summary
        except Exception:  # pragma: no cover
            logger.debug("时序追踪 finish 失败，已忽略")
            return None

    # ---- 结算与读取 ---------------------------------------------------------

    @staticmethod
    def _summarize(trace: TimingTrace, outcome: str) -> Dict[str, Any]:
        points = trace.checkpoints
        derived: Dict[str, int] = {}
        for name, start_key, end_key in _DERIVED:
            start, end = points.get(start_key), points.get(end_key)
            if start is None or end is None or end < start:
                continue
            derived[name] = int((end - start) * 1000)

        by_role: Dict[str, Dict[str, int]] = {}
        for role, status, latency in trace.attempts:
            bucket = by_role.setdefault(role, {"attempts": 0, "failed": 0, "attempt_ms": 0})
            bucket["attempts"] += 1
            bucket["attempt_ms"] += latency
            if status != "success":
                bucket["failed"] += 1
        # 逻辑耗时 vs attempt 总耗时：两者差得多，说明时间花在回退或本地等待上。
        # 键是 ``_METRICS_ROLE`` 归一之后的业务标签（attempt 在 record_attempt
        # 里就归一了），和 engine.py 记阶段用的词表是同一套。
        logical = {
            "attention": derived.get("attention_ms"),
            "qa": derived.get("qa_ms"),
            "impact": derived.get("impact_ms"),
            "reply": derived.get("reply_llm_ms"),
        }
        for role, bucket in by_role.items():
            logical_ms = logical.get(role)
            if logical_ms is not None:
                bucket["logical_ms"] = logical_ms
                bucket["overhead_ms"] = max(0, logical_ms - bucket["attempt_ms"])

        return {
            "seq": trace.seq,
            "path": trace.path,
            "outcome": outcome,
            "checkpoints": sorted(points),
            "derived": derived,
            "attempts_by_role": by_role,
            "attempt_total": len(trace.attempts),
            "notes": dict(trace.notes),
        }

    def snapshot(self, *, limit: int = 20) -> Dict[str, Any]:
        with self._lock:
            recent = list(self._completed)[-max(1, int(limit)):]
            stats = dict(self._stats)
            open_count = len(self._open)
        return {
            "recent": list(reversed(recent)),
            "open_traces": open_count,
            "stats": stats,
            "capacity": {"max_open": self.max_open, "max_completed": self.max_completed},
            "sample_policy": {
                "identifier": "自增 seq；原始 danmaku_id 只写 DEBUG 日志",
                "excludes": ["message", "account_id", "nickname", "ip", "prompt"],
            },
        }

    def clear(self) -> None:
        with self._lock:
            self._open.clear()
            self._completed.clear()
            for key in self._stats:
                self._stats[key] = 0


timing_trace_recorder = TimingTraceRecorder(max_open=128, max_completed=64)

# 当前正在处理的弹幕追踪 ID。用 ContextVar 而不是把 id 串进十几个函数签名：
# asyncio.gather / create_task 会自动复制上下文，所以 AIService 里的每次 attempt
# 都能找回自己属于哪一条弹幕，业务签名一个都不用改。
current_trace_id: ContextVar[Optional[str]] = ContextVar("kangel_timing_trace_id", default=None)


def mark_current(checkpoint: str) -> None:
    timing_trace_recorder.mark(current_trace_id.get(), checkpoint)


def note_current(key: str, value: Any) -> None:
    timing_trace_recorder.note(current_trace_id.get(), key, value)


def record_attempt_current(*, role: str, status: str, latency_ms: int) -> None:
    timing_trace_recorder.record_attempt(
        current_trace_id.get(), role=role, status=status, latency_ms=latency_ms
    )


__all__ = [
    "TimingTrace", "TimingTraceRecorder", "timing_trace_recorder",
    "current_trace_id", "mark_current", "note_current", "record_attempt_current",
]


