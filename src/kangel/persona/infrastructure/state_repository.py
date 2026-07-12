"""人格状态持久化端口及现有数据库适配器。"""

from typing import Any, Protocol


class PersonaStateRepository(Protocol):
    def get_latest_persona_state(self) -> dict | None: ...
    def get_latest_internal_persona_state(self) -> dict | None: ...
    def get_recent_reply_emotions(self, limit: int) -> list[str]: ...
    def save_persona_state(self, mood: float, stress: float, darkness: float) -> None: ...
    def save_internal_persona_state(self, **values: float) -> None: ...


class DatabasePersonaStateRepository:
    """将旧 DatabaseManager 限制在人格所需的最小端口内。"""

    def __init__(self, database: Any):
        self._database = database

    def get_latest_persona_state(self) -> dict | None:
        return self._database.get_latest_persona_state()

    def get_latest_internal_persona_state(self) -> dict | None:
        return self._database.get_latest_internal_persona_state()

    def get_recent_reply_emotions(self, limit: int) -> list[str]:
        return self._database.get_recent_reply_emotions(limit=limit)

    def save_persona_state(self, mood: float, stress: float, darkness: float) -> None:
        self._database.save_persona_state(mood, stress, darkness)

    def save_internal_persona_state(self, **values: float) -> None:
        self._database.save_internal_persona_state(**values)


class DatabasePersonaEventLog:
    """将脱敏事件回放记录写入现有数据库。"""

    _private_payload_fields = {"nickname", "message", "target", "reason"}

    def __init__(self, database: Any):
        self._database = database

    def append(self, record: dict) -> None:
        safe = dict(record)
        safe["payload"] = {
            key: value for key, value in record.get("payload", {}).items()
            if key not in self._private_payload_fields
        }
        self._database.append_persona_event_log(safe)
