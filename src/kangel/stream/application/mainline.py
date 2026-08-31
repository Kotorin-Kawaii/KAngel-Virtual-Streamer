"""每日直播计划快照与主线节拍事实服务。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from kangel.infrastructure.database import DatabaseManager
from kangel.stream.domain.mainline import (
    DailyStreamPlanBeat,
    DailyStreamPlanSnapshot,
    StreamMainlineState,
)


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PLAN_RUNTIME_KEYS = frozenset({
    "current_progress", "current_beat", "visited_beats", "detour_count",
    "current_target", "recent_decisions", "history_reasons", "activity",
    "cooldown", "persona_state", "room_state", "ai_output",
})
_BEAT_KINDS = frozenset({"opening", "mainline", "detour", "transition", "wrap_up"})
_PLAN_KEYS = frozenset({
    "schema_version", "profile_id", "direction", "opening_beat_id",
    "closing_beat_id", "beats",
})
_BEAT_KEYS = frozenset({
    "beat_id", "kind", "label", "objective", "compatible_activity_ids",
    "return_to", "return_policy",
})


class DailyStreamPlanService:
    """校验配置或生成确定性回退；输出永远不含运行状态。"""

    def __init__(self, activity_candidates: Iterable[dict[str, Any]]):
        self.activity_candidates = [dict(item) for item in activity_candidates]

    def build(
        self,
        *,
        theme_id: str,
        theme_name: str,
        raw_plan: Any,
        initial_activity_id: str,
    ) -> tuple[DailyStreamPlanSnapshot, list[str]]:
        errors: list[str] = []
        try:
            return self._parse(raw_plan), errors
        except ValueError as exc:
            if raw_plan not in (None, {}):
                errors.append(str(exc))
        return self._fallback(theme_id, theme_name, initial_activity_id), errors

    def from_dict(self, value: Any) -> DailyStreamPlanSnapshot:
        """恢复已持久化快照：仍校验结构，但不再比对当前 Activity 目录。

        快照在创建时已针对当时的目录校验过，此后它就是本场的冻结事实。
        若恢复时重新比对活目录，从配置里删掉一个 activity 再重启就会让
        进行中的场次永久读不出来（get() 抛 ValueError），而调用方位于
        回复热路径上——这正是"配置热更新不重新校验本场 Plan"要避免的。
        """
        return self._parse(value, validate_activities=False)

    def _parse(
        self, raw: Any, *, validate_activities: bool = True
    ) -> DailyStreamPlanSnapshot:
        if not isinstance(raw, dict):
            raise ValueError("stream_plan 必须是对象")
        forbidden = sorted(set(raw) & _PLAN_RUNTIME_KEYS)
        if forbidden:
            raise ValueError(f"stream_plan 包含运行时字段: {', '.join(forbidden)}")
        unknown_plan_keys = sorted(set(raw) - _PLAN_KEYS)
        if unknown_plan_keys:
            raise ValueError(f"stream_plan 包含未知字段: {', '.join(unknown_plan_keys)}")
        schema_version = raw.get("schema_version", 1)
        if schema_version != 1:
            raise ValueError("stream_plan.schema_version 目前只能为 1")
        profile_id = self._id(raw.get("profile_id"), "profile_id")
        direction = self._text(raw.get("direction"), "direction", 1, 240)
        opening = self._id(raw.get("opening_beat_id"), "opening_beat_id")
        closing = self._id(raw.get("closing_beat_id"), "closing_beat_id")
        raw_beats = raw.get("beats")
        if not isinstance(raw_beats, list) or not 2 <= len(raw_beats) <= 24:
            raise ValueError("stream_plan.beats 必须包含 2-24 个节拍")
        known_activities = (
            {str(item.get("id")) for item in self.activity_candidates}
            if validate_activities else None
        )
        beats: list[DailyStreamPlanBeat] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_beats):
            if not isinstance(item, dict):
                raise ValueError(f"stream_plan.beats[{index}] 必须是对象")
            unknown_beat_keys = sorted(set(item) - _BEAT_KEYS)
            if unknown_beat_keys:
                raise ValueError(
                    f"stream_plan.beats[{index}] 包含未知字段: "
                    f"{', '.join(unknown_beat_keys)}"
                )
            beat_id = self._id(item.get("beat_id"), f"beats[{index}].beat_id")
            if beat_id in seen:
                raise ValueError(f"stream_plan beat_id 重复: {beat_id}")
            kind = str(item.get("kind", "")).strip()
            if kind not in _BEAT_KINDS:
                raise ValueError(f"beats[{index}].kind 无效")
            label = self._text(item.get("label"), f"beats[{index}].label", 1, 80)
            objective = self._text(
                item.get("objective"), f"beats[{index}].objective", 1, 240
            )
            activity_ids = item.get("compatible_activity_ids", [])
            if not isinstance(activity_ids, list) or not activity_ids:
                raise ValueError(f"beats[{index}].compatible_activity_ids 必须是非空数组")
            normalized_activities = tuple(
                self._id(value, f"beats[{index}].compatible_activity_ids")
                for value in activity_ids
            )
            if known_activities is not None:
                unknown = sorted(set(normalized_activities) - known_activities)
                if unknown:
                    raise ValueError(
                        f"beats[{index}] 引用了未知 Activity: {', '.join(unknown)}"
                    )
            return_to = item.get("return_to")
            return_policy = item.get("return_policy")
            if kind == "detour":
                return_to = self._id(return_to, f"beats[{index}].return_to")
                if return_policy != "natural":
                    raise ValueError(f"beats[{index}].return_policy 目前只能为 natural")
            elif return_to is not None or return_policy is not None:
                raise ValueError(f"只有 detour beat 可以声明 return_to/return_policy")
            beats.append(DailyStreamPlanBeat(
                beat_id=beat_id,
                kind=kind,
                label=label,
                objective=objective,
                compatible_activity_ids=normalized_activities,
                return_to=return_to,
                return_policy=return_policy,
            ))
            seen.add(beat_id)
        if opening not in seen or closing not in seen:
            raise ValueError("opening_beat_id/closing_beat_id 必须引用已声明 beat")
        by_id = {item.beat_id: item for item in beats}
        if by_id[opening].kind != "opening" or by_id[closing].kind != "wrap_up":
            raise ValueError("opening/closing beat 的 kind 必须分别为 opening/wrap_up")
        if not any(item.kind == "mainline" for item in beats):
            raise ValueError("stream_plan 至少需要一个 mainline beat")
        for beat in beats:
            if beat.kind == "detour":
                target = by_id.get(str(beat.return_to))
                if target is None or target.kind == "detour":
                    raise ValueError(f"detour {beat.beat_id} 必须返回非 detour beat")
        return DailyStreamPlanSnapshot(
            schema_version=1,
            profile_id=profile_id,
            direction=direction,
            opening_beat_id=opening,
            closing_beat_id=closing,
            beats=tuple(beats),
        )

    def _fallback(
        self, theme_id: str, theme_name: str, initial_activity_id: str
    ) -> DailyStreamPlanSnapshot:
        known = {str(item.get("id")): item for item in self.activity_candidates}
        if initial_activity_id not in known:
            initial_activity_id = next(iter(known), "free-chat")
        chat_id = "free-chat" if "free-chat" in known else initial_activity_id
        main_id = "main_activity"
        beats = (
            DailyStreamPlanBeat(
                "opening", "opening", "进入直播状态",
                f"自然开始今天的{theme_name}", (initial_activity_id,),
            ),
            DailyStreamPlanBeat(
                main_id, "mainline", f"进行{theme_name}",
                "保持今天直播的主要方向", (initial_activity_id,),
            ),
            DailyStreamPlanBeat(
                "chat_detour", "detour", "和观众聊一会儿",
                "根据现场互动短暂偏离主线", (chat_id,),
                return_to=main_id, return_policy="natural",
            ),
            DailyStreamPlanBeat(
                "wrap_up", "wrap_up", "自然收尾",
                "回顾并结束本场直播",
                tuple(dict.fromkeys((initial_activity_id, chat_id))),
            ),
        )
        return DailyStreamPlanSnapshot(
            schema_version=1,
            profile_id=f"{theme_id[:52]}-fallback-v1",
            direction=f"以{theme_name}为本场主线，允许自然聊天、短暂偏航并回归。",
            opening_beat_id="opening",
            closing_beat_id="wrap_up",
            beats=beats,
        )

    @staticmethod
    def _id(value: Any, field: str) -> str:
        normalized = str(value or "").strip()
        if not _ID.fullmatch(normalized):
            raise ValueError(f"{field} 必须为合法 ID")
        return normalized

    @staticmethod
    def _text(value: Any, field: str, minimum: int, maximum: int) -> str:
        normalized = " ".join(str(value or "").split())
        if not minimum <= len(normalized) <= maximum:
            raise ValueError(f"{field} 长度必须为 {minimum}-{maximum}")
        return normalized


class StreamMainlineService:
    """Plan 写入一次；当前 Beat 通过独立乐观版本推进。"""

    def __init__(self, database: DatabaseManager, plan_service: DailyStreamPlanService):
        self.database = database
        self.plan_service = plan_service

    def get_or_create(
        self,
        *,
        stream_session_id: str,
        theme_id: str,
        theme_date: str,
        special_theme_id: str | None,
        theme_snapshot: dict[str, Any] | None = None,
        plan: DailyStreamPlanSnapshot,
        started_at: str,
        trigger_source: str = "stream_initialization",
    ) -> StreamMainlineState:
        payload = json.dumps(
            plan.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        frozen_theme = json.dumps(
            theme_snapshot or {"id": theme_id, "date": theme_date},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        now = datetime.now(timezone.utc).isoformat()
        opening = plan.beat(plan.opening_beat_id)
        if opening is None:
            raise ValueError("Plan opening beat 不存在")
        with self.database._get_connection() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO stream_mainline_sessions (
                    stream_session_id, theme_id, theme_date, special_theme_id,
                    theme_snapshot_json,
                    plan_profile_id, plan_snapshot_json, plan_version,
                    current_beat_id, current_beat_kind, current_beat_label,
                    beat_started_at, beat_version, trigger_source, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 1, ?, 'active', ?, ?)
            """, (
                stream_session_id, theme_id, theme_date, special_theme_id,
                frozen_theme, plan.profile_id, payload,
                opening.beat_id, opening.kind, opening.label,
                started_at, trigger_source, now, now,
            ))
            row = conn.execute(
                "SELECT * FROM stream_mainline_sessions WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
            conn.execute("""
                INSERT OR IGNORE INTO stream_mainline_beat_transitions (
                    stream_session_id, beat_version, beat_id, beat_kind, beat_label,
                    trigger_source, reason_code, changed_at
                ) VALUES (?, 1, ?, ?, ?, ?, 'STREAM_START', ?)
            """, (
                stream_session_id, row["current_beat_id"], row["current_beat_kind"],
                row["current_beat_label"], row["trigger_source"], row["beat_started_at"],
            ))
        return self._from_row(row)

    def get(self, stream_session_id: str) -> StreamMainlineState | None:
        with self.database._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM stream_mainline_sessions WHERE stream_session_id = ?",
                (stream_session_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def set_beat(
        self,
        *,
        current: StreamMainlineState,
        target_beat_id: str,
        changed_at: str,
        trigger_source: str,
        reason_code: str,
        activity_version: int | None = None,
    ) -> StreamMainlineState | None:
        target = current.plan.beat(target_beat_id)
        if target is None or target.beat_id == current.current_beat_id:
            return None
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("""
                UPDATE stream_mainline_sessions
                SET current_beat_id = ?, current_beat_kind = ?, current_beat_label = ?,
                    beat_started_at = ?, beat_version = beat_version + 1,
                    trigger_source = ?, updated_at = ?
                WHERE stream_session_id = ? AND plan_version = ? AND beat_version = ?
                    AND status = 'active'
            """, (
                target.beat_id, target.kind, target.label, changed_at,
                trigger_source, changed_at, current.stream_session_id,
                current.plan_version, current.beat_version,
            ))
            if cursor.rowcount != 1:
                return None
            conn.execute("""
                INSERT INTO stream_mainline_beat_transitions (
                    stream_session_id, beat_version, previous_beat_id,
                    beat_id, beat_kind, beat_label, activity_version,
                    trigger_source, reason_code, changed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                current.stream_session_id, current.beat_version + 1,
                current.current_beat_id, target.beat_id, target.kind, target.label,
                activity_version, trigger_source, reason_code, changed_at,
            ))
            row = conn.execute(
                "SELECT * FROM stream_mainline_sessions WHERE stream_session_id = ?",
                (current.stream_session_id,),
            ).fetchone()
        return self._from_row(row)

    def end_other_sessions(self, current_session_id: str | None, ended_at: str) -> int:
        with self.database._get_connection() as conn:
            if current_session_id:
                cursor = conn.execute("""
                    UPDATE stream_mainline_sessions SET status = 'ended', ended_at = ?, updated_at = ?
                    WHERE status = 'active' AND stream_session_id != ?
                """, (ended_at, ended_at, current_session_id))
            else:
                cursor = conn.execute("""
                    UPDATE stream_mainline_sessions SET status = 'ended', ended_at = ?, updated_at = ?
                    WHERE status = 'active'
                """, (ended_at, ended_at))
            return cursor.rowcount

    def list_transitions(self, stream_session_id: str, limit: int = 20) -> list[dict]:
        with self.database._get_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM stream_mainline_beat_transitions
                WHERE stream_session_id = ? ORDER BY beat_version DESC LIMIT ?
            """, (stream_session_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def prompt_context(self, state: StreamMainlineState) -> dict[str, Any]:
        beat = state.current_beat
        return {
            "plan": {
                "profile_id": state.plan.profile_id,
                "direction": state.plan.direction[:240],
                "version": state.plan_version,
            },
            "current_mainline_beat": {
                "id": state.current_beat_id,
                "kind": state.current_beat_kind,
                "label": state.current_beat_label,
                "return_to": beat.return_to if beat else None,
                "version": state.beat_version,
            },
        }

    def _from_row(self, row) -> StreamMainlineState:
        plan = self.plan_service.from_dict(json.loads(row["plan_snapshot_json"]))
        return StreamMainlineState(
            stream_session_id=row["stream_session_id"], theme_id=row["theme_id"],
            theme_date=row["theme_date"], special_theme_id=row["special_theme_id"],
            theme_snapshot=json.loads(row["theme_snapshot_json"] or "{}"),
            plan=plan, plan_version=row["plan_version"],
            current_beat_id=row["current_beat_id"],
            current_beat_kind=row["current_beat_kind"],
            current_beat_label=row["current_beat_label"],
            beat_started_at=row["beat_started_at"], beat_version=row["beat_version"],
            trigger_source=row["trigger_source"], status=row["status"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            ended_at=row["ended_at"],
        )
