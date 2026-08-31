"""直播主线的不可变计划与版本化节拍领域模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class DailyStreamPlanBeat:
    beat_id: str
    kind: str
    label: str
    objective: str
    compatible_activity_ids: tuple[str, ...]
    return_to: str | None = None
    return_policy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["compatible_activity_ids"] = list(self.compatible_activity_ids)
        return data


@dataclass(frozen=True)
class DailyStreamPlanSnapshot:
    """场次创建后不可变的配置快照；不承载任何运行时进度。"""

    schema_version: int
    profile_id: str
    direction: str
    opening_beat_id: str
    closing_beat_id: str
    beats: tuple[DailyStreamPlanBeat, ...]

    def beat(self, beat_id: str) -> DailyStreamPlanBeat | None:
        return next((item for item in self.beats if item.beat_id == beat_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "direction": self.direction,
            "opening_beat_id": self.opening_beat_id,
            "closing_beat_id": self.closing_beat_id,
            "beats": [item.to_dict() for item in self.beats],
        }


@dataclass(frozen=True)
class StreamMainlineState:
    stream_session_id: str
    theme_id: str
    theme_date: str
    special_theme_id: str | None
    theme_snapshot: dict[str, Any]
    plan: DailyStreamPlanSnapshot
    plan_version: int
    current_beat_id: str
    current_beat_kind: str
    current_beat_label: str
    beat_started_at: str
    beat_version: int
    trigger_source: str
    status: str
    created_at: str
    updated_at: str
    ended_at: str | None = None

    @property
    def current_beat(self) -> DailyStreamPlanBeat | None:
        return self.plan.beat(self.current_beat_id)

    def public_plan(self) -> dict[str, Any]:
        return {
            "profile_id": self.plan.profile_id,
            "version": self.plan_version,
            "direction": self.plan.direction,
        }

    def public_beat(self) -> dict[str, Any]:
        beat = self.current_beat
        return {
            "id": self.current_beat_id,
            "kind": self.current_beat_kind,
            "label": self.current_beat_label,
            "return_to": beat.return_to if beat else None,
            "version": self.beat_version,
            "started_at": self.beat_started_at,
        }
