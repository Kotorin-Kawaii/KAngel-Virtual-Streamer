"""进程内通用事件总线与人格事件公共转发。"""

import asyncio
from functools import wraps
from typing import Any, Callable, Dict, List

from kangel.shared.logging import logger
from kangel.persona import (
    AudienceAtmosphereTickEvent,
    DanmakuReceivedEvent,
    GiftReceivedEvent,
    ModerationActionEvent,
    PersonaEvent,
    PersonaEventReducer,
    PersonaEventType,
    PersonaMutation,
    SemanticImpactAnalyzedEvent,
    SilenceTickEvent,
    StreamLifecycleEvent,
)
from kangel.persona.application.pipeline import PersonaEventPipeline
from kangel.persona.application.runtime import persona_event_pipeline


class EventBus:
    """旧通用事件总线；将在 infrastructure 阶段迁移。"""

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, event_name: str, handler: Callable):
        async with self._lock:
            self._subscribers.setdefault(event_name, []).append(handler)

    async def unsubscribe(self, event_name: str, handler: Callable):
        async with self._lock:
            if event_name in self._subscribers:
                try:
                    self._subscribers[event_name].remove(handler)
                except ValueError:
                    pass

    async def emit(self, event_name: str, *args, **kwargs):
        handlers = self._subscribers.get(event_name, []).copy()
        tasks = []
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    tasks.append(handler(*args, **kwargs))
                else:
                    handler(*args, **kwargs)
            except Exception as exc:
                logger.error("事件处理器执行失败: %s", exc)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def on(self, event_name: str):
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                return await func(*args, **kwargs)

            asyncio.create_task(self.subscribe(event_name, wrapper))
            return wrapper
        return decorator


event_bus = EventBus()
