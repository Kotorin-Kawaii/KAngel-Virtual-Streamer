import asyncio
from typing import Callable, Dict, List, Any
from functools import wraps
from utils.logger import logger


class EventBus:
    """事件总线，支持异步事件处理"""
    
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = asyncio.Lock()
    
    async def subscribe(self, event_name: str, handler: Callable):
        """订阅事件"""
        async with self._lock:
            if event_name not in self._subscribers:
                self._subscribers[event_name] = []
            self._subscribers[event_name].append(handler)
        logger.debug(f"订阅事件: {event_name}")
    
    async def unsubscribe(self, event_name: str, handler: Callable):
        """取消订阅事件"""
        async with self._lock:
            if event_name in self._subscribers:
                try:
                    self._subscribers[event_name].remove(handler)
                    logger.debug(f"取消订阅事件: {event_name}")
                except ValueError:
                    pass
    
    async def emit(self, event_name: str, *args, **kwargs):
        """触发事件"""
        if event_name not in self._subscribers:
            return
        
        handlers = self._subscribers[event_name].copy()
        logger.debug(f"触发事件: {event_name}, 订阅者数量: {len(handlers)}")
        
        tasks = []
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    task = handler(*args, **kwargs)
                    tasks.append(task)
                else:
                    handler(*args, **kwargs)
            except Exception as e:
                logger.error(f"事件处理器执行失败: {e}")
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    def on(self, event_name: str):
        """装饰器：订阅事件"""
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                return await func(*args, **kwargs)
            
            asyncio.create_task(self.subscribe(event_name, wrapper))
            return wrapper
        return decorator


event_bus = EventBus()


# ==================== 人格事件流水线 ====================

import inspect
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import uuid4

from config import settings
from core.persona_dynamics import DynamicsContext
from models.persona import EmotionDelta, InternalPersonaState, InternalStateDelta, PersonaState


class PersonaEventType(str, Enum):
    DANMAKU_RECEIVED = "danmaku_received"
    SEMANTIC_IMPACT_ANALYZED = "semantic_impact_analyzed"
    GIFT_RECEIVED = "gift_received"
    MODERATION_ACTION = "moderation_action"
    SILENCE_TICK = "silence_tick"
    STREAM_LIFECYCLE = "stream_lifecycle"


@dataclass(frozen=True, kw_only=True)
class PersonaEvent:
    event_id: str = field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = field(default_factory=datetime.now)
    source: str = "backend"


@dataclass(frozen=True, kw_only=True)
class DanmakuReceivedEvent(PersonaEvent):
    nickname: str
    message: str
    sentiment: float = 0.0
    topics: tuple[str, ...] = ()
    danmaku_rate: int = 0
    event_type: PersonaEventType = field(default=PersonaEventType.DANMAKU_RECEIVED, init=False)


@dataclass(frozen=True, kw_only=True)
class SemanticImpactAnalyzedEvent(PersonaEvent):
    danmaku_id: str
    raw_delta: EmotionDelta
    internal_delta: InternalStateDelta
    dynamics_context: DynamicsContext
    event_type: PersonaEventType = field(default=PersonaEventType.SEMANTIC_IMPACT_ANALYZED, init=False)


@dataclass(frozen=True, kw_only=True)
class GiftReceivedEvent(PersonaEvent):
    nickname: str
    gift_name: str
    value: float = 0.0
    message: str = ""
    event_type: PersonaEventType = field(default=PersonaEventType.GIFT_RECEIVED, init=False)


@dataclass(frozen=True, kw_only=True)
class ModerationActionEvent(PersonaEvent):
    action: str
    target: str = ""
    reason: str = ""
    severity: float = 0.5
    event_type: PersonaEventType = field(default=PersonaEventType.MODERATION_ACTION, init=False)


@dataclass(frozen=True, kw_only=True)
class SilenceTickEvent(PersonaEvent):
    seconds_since_activity: float
    event_type: PersonaEventType = field(default=PersonaEventType.SILENCE_TICK, init=False)


@dataclass(frozen=True, kw_only=True)
class StreamLifecycleEvent(PersonaEvent):
    phase: str
    event_type: PersonaEventType = field(default=PersonaEventType.STREAM_LIFECYCLE, init=False)


@dataclass
class PersonaMutation:
    emotion_delta: EmotionDelta = field(
        default_factory=lambda: EmotionDelta(mood=0.0, stress=0.0, darkness=0.0)
    )
    internal_delta: InternalStateDelta = field(default_factory=InternalStateDelta)
    dynamics_context: Optional[DynamicsContext] = None
    reason: str = ""

    @property
    def has_changes(self) -> bool:
        values = [*self.emotion_delta.model_dump().values(), *self.internal_delta.model_dump().values()]
        return any(abs(float(value)) > 1e-9 for value in values)


class PersonaEventReducer:
    """模型解释语义，归约器以确定性规则计算最终状态变化。"""

    def reduce(self, event: PersonaEvent, state: PersonaState, internal_state: InternalPersonaState) -> PersonaMutation:
        if isinstance(event, SemanticImpactAnalyzedEvent):
            return PersonaMutation(
                emotion_delta=event.raw_delta,
                internal_delta=event.internal_delta,
                dynamics_context=event.dynamics_context,
                reason="模型完成语义理解，后端提交确定性动力学变化",
            )
        if isinstance(event, DanmakuReceivedEvent):
            return self._reduce_danmaku(event)
        if isinstance(event, GiftReceivedEvent):
            return self._reduce_gift(event)
        if isinstance(event, ModerationActionEvent):
            return self._reduce_moderation(event)
        if isinstance(event, SilenceTickEvent):
            return self._reduce_silence(event, state, internal_state)
        if isinstance(event, StreamLifecycleEvent):
            return self._reduce_lifecycle(event)
        return PersonaMutation(reason="未知事件，不改变状态")

    def _reduce_danmaku(self, event: DanmakuReceivedEvent) -> PersonaMutation:
        sentiment = self._clamp(event.sentiment, -1.0, 1.0)
        negative, positive = max(0.0, -sentiment), max(0.0, sentiment)
        load = min(max(event.danmaku_rate, 0) / 30.0, 1.0)
        return PersonaMutation(
            emotion_delta=EmotionDelta(
                mood=sentiment * 0.0035,
                stress=load * 0.0035 + negative * 0.002,
                darkness=negative * 0.0025,
            ),
            internal_delta=InternalStateDelta(
                arousal=load * 0.005 + abs(sentiment) * 0.0015,
                fatigue=load * 0.0025,
                attachment=positive * 0.0015 - negative * 0.001,
                confidence=sentiment * 0.001,
            ),
            reason="未选中弹幕对直播间整体气氛产生轻微影响",
        )

    def _reduce_gift(self, event: GiftReceivedEvent) -> PersonaMutation:
        # 预留规则：未来礼物模块只需发布此事件。
        strength = min(max(event.value, 0.0) / 1000.0, 1.0)
        return PersonaMutation(
            emotion_delta=EmotionDelta(
                mood=0.008 + strength * 0.025,
                stress=-0.004 + strength * 0.006,
                darkness=-0.003,
            ),
            internal_delta=InternalStateDelta(
                arousal=0.012 + strength * 0.025,
                fatigue=0.002,
                attachment=0.006 + strength * 0.012,
                confidence=0.008 + strength * 0.015,
            ),
            reason="礼物事件预留归约规则",
        )

    def _reduce_moderation(self, event: ModerationActionEvent) -> PersonaMutation:
        # 预留规则：severity 由未来房管模块归一化到 0-1。
        severity = self._clamp(event.severity, 0.0, 1.0)
        return PersonaMutation(
            emotion_delta=EmotionDelta(
                mood=-0.004 * severity,
                stress=0.012 * severity,
                darkness=0.006 * severity,
            ),
            internal_delta=InternalStateDelta(
                arousal=0.010 * severity,
                fatigue=0.004 * severity,
                attachment=-0.003 * severity,
                confidence=-0.004 * severity,
            ),
            reason="房管事件预留归约规则",
        )

    def _reduce_silence(
        self,
        event: SilenceTickEvent,
        state: PersonaState,
        internal_state: InternalPersonaState,
    ) -> PersonaMutation:
        if event.seconds_since_activity < 30:
            return PersonaMutation(reason="静默时间不足，不更新状态")
        factor = min(max(event.seconds_since_activity / 30.0, 1.0), 4.0)
        mood_delta = (settings.persona.initial_mood - state.mood) * 0.006 * factor
        stress_delta = (settings.persona.initial_stress - state.stress) * 0.009 * factor
        darkness_delta = (settings.persona.initial_darkness - state.darkness) * 0.005 * factor
        if event.seconds_since_activity >= 120:
            mood_delta -= 0.0015
            stress_delta += 0.001
        return PersonaMutation(
            emotion_delta=EmotionDelta(
                mood=self._clamp(mood_delta, -0.02, 0.02),
                stress=self._clamp(stress_delta, -0.025, 0.025),
                darkness=self._clamp(darkness_delta, -0.015, 0.015),
            ),
            internal_delta=InternalStateDelta(
                arousal=self._toward(internal_state.arousal, 0.35, 0.012 * factor),
                fatigue=self._toward(internal_state.fatigue, 0.2, 0.010 * factor),
                attachment=-0.0005 if event.seconds_since_activity >= 120 else 0.0,
                confidence=self._toward(internal_state.confidence, 0.65, 0.003 * factor),
            ),
            reason="直播间静默时执行自然恢复与轻微冷场反应",
        )

    def _reduce_lifecycle(self, event: StreamLifecycleEvent) -> PersonaMutation:
        phase = event.phase.casefold()
        if phase in {"started", "start", "opening"}:
            return PersonaMutation(
                emotion_delta=EmotionDelta(mood=0.006, stress=0.004, darkness=0.0),
                internal_delta=InternalStateDelta(arousal=0.025, confidence=0.006),
                reason="直播开始",
            )
        if phase in {"ended", "end", "stopped"}:
            return PersonaMutation(
                emotion_delta=EmotionDelta(mood=0.0, stress=-0.008, darkness=0.0),
                internal_delta=InternalStateDelta(arousal=-0.018, fatigue=0.008),
                reason="直播结束",
            )
        return PersonaMutation(reason=f"未配置状态变化的直播阶段: {event.phase}")

    def _toward(self, current: float, target: float, rate: float) -> float:
        return self._clamp((target - current) * rate, -0.05, 0.05)

    def _clamp(self, value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))


StateProvider = Callable[[], tuple[PersonaState, InternalPersonaState]]
MutationHandler = Callable[[PersonaMutation], object]


class PersonaEventPipeline:
    """串行处理人格事件，并为未来事件来源保留统一入口。"""

    def __init__(self, reducer: Optional[PersonaEventReducer] = None, tick_seconds: float = 30.0):
        self.reducer = reducer or PersonaEventReducer()
        self.tick_seconds = tick_seconds
        self._state_provider: Optional[StateProvider] = None
        self._mutation_handler: Optional[MutationHandler] = None
        self._lock = asyncio.Lock()
        self._running = False
        self._tick_task: Optional[asyncio.Task] = None
        self._last_activity_at = datetime.now()
        self._current_danmaku_rate = 0
        self._recent_sentiments: deque[float] = deque(maxlen=30)
        self._history: deque[dict] = deque(maxlen=100)

    @property
    def current_danmaku_rate(self) -> int:
        return self._current_danmaku_rate

    @property
    def audience_sentiment(self) -> float:
        if not self._recent_sentiments:
            return 0.0
        return sum(self._recent_sentiments) / len(self._recent_sentiments)

    def bind(self, state_provider: StateProvider, mutation_handler: MutationHandler) -> None:
        self._state_provider = state_provider
        self._mutation_handler = mutation_handler

    async def publish(
        self,
        event: PersonaEvent,
        *,
        state: Optional[PersonaState] = None,
        internal_state: Optional[InternalPersonaState] = None,
        mutation_handler: Optional[MutationHandler] = None,
    ) -> PersonaMutation:
        async with self._lock:
            if isinstance(event, DanmakuReceivedEvent):
                self._last_activity_at = event.occurred_at
                self._current_danmaku_rate = max(0, event.danmaku_rate)
                self._recent_sentiments.append(self.reducer._clamp(event.sentiment, -1.0, 1.0))
            elif isinstance(event, (GiftReceivedEvent, ModerationActionEvent)):
                self._last_activity_at = event.occurred_at
            elif isinstance(event, StreamLifecycleEvent) and event.phase.casefold() in {"started", "start"}:
                self._last_activity_at = event.occurred_at

            if state is None or internal_state is None:
                if not self._state_provider:
                    raise RuntimeError("人格事件流水线尚未绑定状态提供器")
                state, internal_state = self._state_provider()
            mutation = self.reducer.reduce(event, state, internal_state)
            handler = mutation_handler or self._mutation_handler
            if mutation.has_changes and handler:
                result = handler(mutation)
                if inspect.isawaitable(result):
                    await result
            self._history.append(self._snapshot(event, mutation))
            return mutation

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_activity_at = datetime.now()
        self._tick_task = asyncio.create_task(self._silence_loop())
        logger.info(f"人格事件流水线已启动，静默检查间隔: {self.tick_seconds:.0f}秒")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        self._tick_task = None
        logger.info("人格事件流水线已停止")

    async def _silence_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.tick_seconds)
                seconds = max(0.0, (datetime.now() - self._last_activity_at).total_seconds())
                await self.publish(SilenceTickEvent(seconds_since_activity=seconds, source="timer"))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"人格静默事件处理失败: {exc}")

    def get_debug_info(self) -> dict:
        return {
            "running": self._running,
            "tick_seconds": self.tick_seconds,
            "current_danmaku_rate": self._current_danmaku_rate,
            "last_activity_at": self._last_activity_at.isoformat(),
            "processed_events": len(self._history),
            "recent_events": list(self._history)[-10:],
            "reserved_event_types": [
                PersonaEventType.GIFT_RECEIVED.value,
                PersonaEventType.MODERATION_ACTION.value,
            ],
        }

    def _snapshot(self, event: PersonaEvent, mutation: PersonaMutation) -> dict:
        event_data = asdict(event)
        event_data["event_type"] = event.event_type.value
        event_data["occurred_at"] = event.occurred_at.isoformat()
        if isinstance(event, SemanticImpactAnalyzedEvent):
            event_data["raw_delta"] = event.raw_delta.model_dump()
            event_data["internal_delta"] = event.internal_delta.model_dump()
            event_data["dynamics_context"] = asdict(event.dynamics_context)
        return {
            "event": event_data,
            "mutation": {
                "emotion_delta": mutation.emotion_delta.model_dump(),
                "internal_delta": mutation.internal_delta.model_dump(),
                "reason": mutation.reason,
                "uses_dynamics": mutation.dynamics_context is not None,
            },
        }


persona_event_pipeline = PersonaEventPipeline()
