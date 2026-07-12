"""插件可访问的最小、只读能力集合。"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping

from kangel.shared.logging import logger


async def _discard_event(_name: str, _payload: Mapping[str, Any]) -> None:
    return None


def _empty_snapshot() -> Mapping[str, Any]:
    return MappingProxyType({})


@dataclass(frozen=True)
class PluginContext:
    publish_event: Callable[[str, Mapping[str, Any]], Awaitable[None]] = _discard_event
    persona_snapshot: Callable[[], Mapping[str, Any]] = _empty_snapshot
    stream_snapshot: Callable[[], Mapping[str, Any]] = _empty_snapshot
    _logger: Any = field(default=logger, repr=False)

    def log(self, level: str, message: str, *args: Any) -> None:
        method = getattr(self._logger, level, self._logger.info)
        method(message, *args)
