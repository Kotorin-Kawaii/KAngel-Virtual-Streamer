"""Read-only projections of committed session facts; no runtime decisions."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


def unified_stream_semantics_enabled(stream_config) -> bool:
    return bool(stream_config.live_presence_enabled and stream_config.live_presence_active_enabled)


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _copy(value):
    if isinstance(value, Mapping):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_copy(item) for item in value]
    return value


@dataclass(frozen=True)
class StreamSemanticContext:
    """One coherent, immutable Theme/Plan/Beat/Activity read."""

    stream_session_id: str
    theme: Mapping[str, Any]
    plan: Mapping[str, Any]
    beat: Mapping[str, Any]
    activity: Mapping[str, Any]

    def __post_init__(self):
        for name in ("theme", "plan", "beat", "activity"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    @classmethod
    def from_committed(cls, mainline, activity) -> StreamSemanticContext | None:
        if not mainline or not activity or mainline.status != "active":
            return None
        if activity.stream_session_id != mainline.stream_session_id or not mainline.current_beat:
            return None
        theme = dict(mainline.theme_snapshot)
        theme.update(id=mainline.theme_id, date=mainline.theme_date)
        theme.setdefault("name", mainline.theme_id)
        return cls(
            stream_session_id=mainline.stream_session_id,
            theme=theme,
            plan=mainline.public_plan(),
            beat={**mainline.public_beat(), "objective": mainline.current_beat.objective},
            activity={
                "activity_id": activity.activity_id, "category": activity.category,
                "display_name": activity.display_name, "object_name": activity.object_name,
                "started_at": activity.started_at, "version": activity.version,
            },
        )

    def theme_prompt_context(self) -> dict:
        return _copy(self.theme)

    def activity_prompt_context(self) -> dict:
        return _copy(self.activity)

    def mainline_prompt_context(self) -> dict:
        # Preserve the ordinary reply's legacy context shape and prompt budget.
        plan = _copy(self.plan)
        plan["direction"] = plan["direction"][:240]
        return {
            "plan": plan,
            "current_mainline_beat": {
                key: _copy(self.beat[key])
                for key in ("id", "kind", "label", "return_to", "version")
            },
        }

    def stage_prompt_context(self) -> dict:
        return {
            "theme": _copy(self.theme), "plan": _copy(self.plan),
            "beat": _copy(self.beat), "activity": _copy(self.activity),
        }
