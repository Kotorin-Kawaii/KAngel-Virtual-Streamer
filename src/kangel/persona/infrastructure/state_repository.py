"""人格状态持久化端口及现有数据库适配器。"""

from typing import Any, Protocol


class PersonaStateRepository(Protocol):
    def get_latest_persona_state(self) -> dict | None: ...
    def get_latest_internal_persona_state(self) -> dict | None: ...
    def get_recent_reply_emotions(self, limit: int) -> list[str]: ...
    def save_persona_state(self, mood: float, stress: float, darkness: float) -> None: ...
    def save_internal_persona_state(self, **values: float) -> None: ...
    def get_or_create_persona_affect_anchor(self, **values: Any) -> dict: ...
    def update_persona_affect_anchor(self, **values: Any) -> dict | None: ...


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

    def get_or_create_persona_affect_anchor(self, **values: Any) -> dict:
        return self._database.get_or_create_persona_affect_anchor(**values)

    def update_persona_affect_anchor(self, **values: Any) -> dict | None:
        return self._database.update_persona_affect_anchor(**values)


class DatabasePersonaEventLog:
    """将脱敏事件回放记录写入现有数据库。"""

    _private_payload_fields = {"nickname", "message", "target", "reason"}

    def __init__(self, database: Any):
        self._database = database

    def claim(self, event: Any) -> bool:
        source_event_id = str(getattr(event, "source_event_id", "") or "")
        event_type = getattr(getattr(event, "event_type", None), "value", "")
        if not source_event_id or not event_type:
            return True
        # 只有新版、带场次作用域的弹幕来源才具备跨重启幂等语义。
        # 旧事件常使用 ``danmaku:<id>``；把它们写入永久 claim 会让测试、
        # 离线回放和历史兼容路径在下一次进程启动后被错误吞掉。
        if source_event_id.startswith("danmaku:") and source_event_id.count(":") < 2:
            return True
        return self._database.claim_persona_source_event(
            source_event_id=source_event_id,
            event_type=str(event_type),
        )

    def append(self, record: dict) -> None:
        safe = dict(record)
        safe["payload"] = {
            key: value for key, value in record.get("payload", {}).items()
            if key not in self._private_payload_fields
        }
        self._database.append_persona_event_log(safe)
