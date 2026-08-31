"""人格事件串行队列与生命周期。"""

import asyncio
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
import inspect
from itertools import count
from typing import Callable, Optional, Protocol

from kangel.shared.logging import logger

from ..domain.dynamics import clamp
from ..domain.events import (
    DanmakuReceivedEvent,
    GiftReceivedEvent,
    ModerationActionEvent,
    AudienceAtmosphereTickEvent,
    PersonaEvent,
    PersonaEventType,
    SemanticImpactAnalyzedEvent,
    SilenceTickEvent,
    StreamLifecycleEvent,
)
from ..domain.mutations import PersonaMutation
from ..domain.reducer import PersonaEventReducer
from ..domain.state import InternalPersonaState, PersonaState


StateProvider = Callable[[], tuple[PersonaState, InternalPersonaState]]
MutationHandler = Callable[[PersonaMutation], object]


class PersonaEventLog(Protocol):
    def claim(self, event: PersonaEvent) -> bool: ...
    def append(self, record: dict) -> None: ...


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # 兼容旧调用者传入的本地 naive 时间；新事件一律生成 aware UTC。
        return value.astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


class PersonaEventPipeline:
    """运行期通过有界优先队列单消费者串行更新人格状态。"""

    pipeline_version = "persona-events-v2"

    def __init__(
        self,
        reducer: Optional[PersonaEventReducer] = None,
        tick_seconds: float = 30.0,
        queue_capacity: int = 256,
        event_log: PersonaEventLog | None = None,
    ):
        self.reducer = reducer or PersonaEventReducer()
        self.tick_seconds = tick_seconds
        self.queue_capacity = max(1, queue_capacity)
        self.event_log = event_log
        self._state_provider: Optional[StateProvider] = None
        self._mutation_handler: Optional[MutationHandler] = None
        self._direct_lock = asyncio.Lock()
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(self.queue_capacity)
        self._sequence = count()
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._tick_task: Optional[asyncio.Task] = None
        self._last_activity_at = datetime.now(timezone.utc)
        self._current_danmaku_rate = 0
        self._recent_sentiments: deque[float] = deque(maxlen=30)
        self._history: deque[dict] = deque(maxlen=100)
        self._processed_order: deque[str] = deque(maxlen=2048)
        self._processed_ids: set[str] = set()
        self._observers: list[Callable[[PersonaEvent, dict], object]] = []

    @property
    def current_danmaku_rate(self) -> int:
        return self._current_danmaku_rate

    @property
    def audience_sentiment(self) -> float:
        if not self._recent_sentiments:
            return 0.0
        return sum(self._recent_sentiments) / len(self._recent_sentiments)

    @property
    def audience_sentiment_sample_count(self) -> int:
        """房间氛围聚合样本数；供长时锚点拒绝单条弹幕写入。"""
        return len(self._recent_sentiments)

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def bind(self, state_provider: StateProvider, mutation_handler: MutationHandler) -> None:
        self._state_provider = state_provider
        self._mutation_handler = mutation_handler

    def add_observer(self, observer: Callable[[PersonaEvent, dict], object]) -> None:
        """注册只读观察者；观察失败不得影响人格状态提交。"""
        if observer not in self._observers:
            self._observers.append(observer)

    def remove_observer(self, observer: Callable[[PersonaEvent, dict], object]) -> None:
        try:
            self._observers.remove(observer)
        except ValueError:
            pass

    async def publish(
        self,
        event: PersonaEvent,
        *,
        state: Optional[PersonaState] = None,
        internal_state: Optional[InternalPersonaState] = None,
        mutation_handler: Optional[MutationHandler] = None,
    ) -> PersonaMutation:
        if self._running:
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            item = (
                self._priority(event),
                next(self._sequence),
                event,
                state,
                internal_state,
                mutation_handler,
                future,
            )
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull as exc:
                raise RuntimeError("人格事件队列已满") from exc
            return await future
        async with self._direct_lock:
            return await self._process(event, state, internal_state, mutation_handler)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_activity_at = datetime.now(timezone.utc)
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._tick_task = asyncio.create_task(self._silence_loop())
        logger.info("人格事件流水线已启动，静默检查间隔: %.0f秒", self.tick_seconds)

    async def stop(self) -> None:
        if not self._running:
            return
        # 先停生产者，再让单消费者排空已接收事件。旧实现同时 cancel 两者，
        # 会在 tick 已入队但 future 尚未完成时丢掉人格 mutation。
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        await self._queue.join()
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._tick_task = None
        self._worker_task = None
        while not self._queue.empty():
            item = self._queue.get_nowait()
            future = item[-1]
            if not future.done():
                future.set_exception(asyncio.CancelledError())
            self._queue.task_done()
        logger.info("人格事件流水线已停止")

    async def _worker_loop(self) -> None:
        while self._running:
            item = await self._queue.get()
            _, _, event, state, internal_state, handler, future = item
            try:
                mutation = await self._process(event, state, internal_state, handler)
                if not future.done():
                    future.set_result(mutation)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _process(self, event, state, internal_state, mutation_handler):
        if event.event_id in self._processed_ids:
            return PersonaMutation(reason="重复事件已忽略")
        if isinstance(event, SemanticImpactAnalyzedEvent) and self.event_log:
            claim = getattr(self.event_log, "claim", None)
            if callable(claim) and not claim(event):
                self._remember_event(event.event_id)
                return PersonaMutation(reason="重复来源语义影响已忽略")
        if isinstance(event, DanmakuReceivedEvent):
            self._last_activity_at = _as_utc(event.occurred_at)
            self._current_danmaku_rate = max(0, event.danmaku_rate)
            self._recent_sentiments.append(clamp(event.sentiment, -1.0, 1.0))
        elif isinstance(event, (GiftReceivedEvent, ModerationActionEvent)):
            self._last_activity_at = _as_utc(event.occurred_at)
        elif isinstance(event, StreamLifecycleEvent) and event.phase.casefold() in {"started", "start"}:
            self._last_activity_at = _as_utc(event.occurred_at)

        if state is None or internal_state is None:
            if not self._state_provider:
                raise RuntimeError("人格事件流水线尚未绑定状态提供器")
            state, internal_state = self._state_provider()
        before = {"persona": state.model_dump(), "internal": internal_state.model_dump()}
        mutation = self.reducer.reduce(event, state, internal_state)
        handler = mutation_handler or self._mutation_handler
        if mutation.has_changes and handler:
            result = handler(mutation)
            if inspect.isawaitable(result):
                await result
        after_state, after_internal = (
            self._state_provider() if self._state_provider else (state, internal_state)
        )
        self._remember_event(event.event_id)
        snapshot = self._snapshot(event, mutation, before, {
            "persona": after_state.model_dump(),
            "internal": after_internal.model_dump(),
        })
        self._history.append(snapshot)
        if self.event_log:
            try:
                self.event_log.append(snapshot)
            except Exception as exc:
                # 回放日志不得在状态已提交后诱发业务重试或重复 mutation。
                logger.error("人格事件回放日志写入失败: %s", exc)
        for observer in tuple(self._observers):
            try:
                result = observer(event, snapshot)
                if inspect.isawaitable(result):
                    asyncio.create_task(result)
            except Exception as exc:
                logger.debug("人格事件只读观察者失败: %s", exc)
        return mutation

    def _remember_event(self, event_id: str) -> None:
        if len(self._processed_order) == self._processed_order.maxlen:
            self._processed_ids.discard(self._processed_order[0])
        self._processed_order.append(event_id)
        self._processed_ids.add(event_id)

    async def _silence_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.tick_seconds)
                seconds = max(
                    0.0,
                    (datetime.now(timezone.utc) - _as_utc(self._last_activity_at)).total_seconds(),
                )
                await self.publish(AudienceAtmosphereTickEvent(
                    danmaku_rate=self._current_danmaku_rate,
                    audience_sentiment=self.audience_sentiment,
                    source="timer",
                ))
                await self.publish(SilenceTickEvent(seconds_since_activity=seconds, source="timer"))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("人格静默事件处理失败: %s", exc)

    def get_debug_info(self) -> dict:
        return {
            "running": self._running,
            "tick_seconds": self.tick_seconds,
            "queue_capacity": self.queue_capacity,
            "queue_size": self.queue_size,
            "current_danmaku_rate": self._current_danmaku_rate,
            "last_activity_at": self._last_activity_at.isoformat(),
            "processed_events": len(self._history),
            "recent_events": list(self._history)[-10:],
            "pipeline_version": self.pipeline_version,
            "reserved_event_types": [
                PersonaEventType.GIFT_RECEIVED.value,
                PersonaEventType.MODERATION_ACTION.value,
            ],
        }

    def _priority(self, event: PersonaEvent) -> int:
        if isinstance(event, SemanticImpactAnalyzedEvent):
            return 0
        if isinstance(event, StreamLifecycleEvent):
            return 1
        if isinstance(event, SilenceTickEvent):
            return 9
        return 5

    def _snapshot(self, event, mutation, before, after) -> dict:
        event_data = asdict(event)
        event_data["event_type"] = event.event_type.value
        event_data["occurred_at"] = event.occurred_at.isoformat()
        if isinstance(event, SemanticImpactAnalyzedEvent):
            event_data["raw_delta"] = event.raw_delta.model_dump()
            event_data["internal_delta"] = event.internal_delta.model_dump()
            event_data["dynamics_context"] = asdict(event.dynamics_context)
        return {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "occurred_at": event.occurred_at.isoformat(),
            "source": event.source,
            "payload": event_data,
            "mutation": {
                "emotion_delta": mutation.emotion_delta.model_dump(),
                "internal_delta": mutation.internal_delta.model_dump(),
                "reason": mutation.reason,
                "uses_dynamics": mutation.dynamics_context is not None,
            },
            "state_before": before,
            "state_after": after,
            "pipeline_version": self.pipeline_version,
        }
