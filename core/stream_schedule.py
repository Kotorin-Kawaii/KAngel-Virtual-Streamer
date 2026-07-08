"""基于 IANA 时区和每周时段计算直播状态。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from utils.logger import logger


WEEKDAYS = (
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)


@dataclass(frozen=True)
class ScheduleSnapshot:
    is_live: bool
    stream_status: str
    schedule_timezone: str
    schedule_config_valid: bool
    schedule_errors: list[str]
    current_stream_start_time: Optional[str]
    current_stream_end_time: Optional[str]
    next_stream_start_time: Optional[str]
    next_stream_end_time: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


class StreamScheduleService:
    """容错解析排期；无效项被跳过，无效时区则安全保持下播。"""

    def __init__(self, timezone_name: str, weekly_schedule: Any):
        self.configured_timezone = (timezone_name or "").strip()
        self.errors: list[str] = []
        try:
            self.zone = ZoneInfo(self.configured_timezone)
            self.timezone_name = self.configured_timezone
        except (ZoneInfoNotFoundError, ValueError):
            self.zone = timezone.utc
            self.timezone_name = "UTC"
            self.errors.append(
                f"无效 IANA 时区: {self.configured_timezone or '<empty>'}"
            )
        self.windows = self._parse_schedule(weekly_schedule)
        if self.errors:
            logger.warning("直播排期配置存在问题，将安全降级: %s", "; ".join(self.errors))

    def evaluate(self, now: Optional[datetime] = None) -> ScheduleSnapshot:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        local_now = reference.astimezone(self.zone)

        # 时区无效时不尝试使用可能被误解读的排期。
        if self.configured_timezone != self.timezone_name:
            return self._snapshot(False, None, None)

        intervals = self._candidate_intervals(local_now.date())
        active = [item for item in intervals if item[0] <= local_now < item[1]]
        current = None
        if active:
            current = (min(item[0] for item in active), max(item[1] for item in active))
        upcoming = next((item for item in intervals if item[0] > local_now), None)
        return self._snapshot(bool(current), current, upcoming)

    def seconds_until_change(self, now: Optional[datetime] = None) -> Optional[float]:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        snapshot = self.evaluate(reference)
        target_text = (
            snapshot.current_stream_end_time if snapshot.is_live
            else snapshot.next_stream_start_time
        )
        if not target_text:
            return None
        return max(0.0, (datetime.fromisoformat(target_text) - reference.astimezone(self.zone)).total_seconds())

    def _snapshot(self, is_live, current, upcoming) -> ScheduleSnapshot:
        return ScheduleSnapshot(
            is_live=is_live,
            stream_status="streaming" if is_live else "offline",
            schedule_timezone=self.timezone_name,
            schedule_config_valid=not self.errors,
            schedule_errors=list(self.errors),
            current_stream_start_time=current[0].isoformat() if current else None,
            current_stream_end_time=current[1].isoformat() if current else None,
            next_stream_start_time=upcoming[0].isoformat() if upcoming else None,
            next_stream_end_time=upcoming[1].isoformat() if upcoming else None,
        )

    def _parse_schedule(self, raw: Any) -> dict[int, list[tuple[time, time]]]:
        parsed = {index: [] for index in range(7)}
        if raw in (None, {}):
            return parsed
        if not isinstance(raw, dict):
            self.errors.append("weekly_schedule 必须是对象")
            return parsed
        for key, entries in raw.items():
            weekday = self._weekday_index(key)
            if weekday is None:
                self.errors.append(f"未知星期键: {key}")
                continue
            if not isinstance(entries, list):
                self.errors.append(f"{key} 的时段必须是数组")
                continue
            for index, entry in enumerate(entries):
                try:
                    if not isinstance(entry, dict):
                        raise ValueError("时段必须是对象")
                    start = time.fromisoformat(str(entry["start"]))
                    end = time.fromisoformat(str(entry["end"]))
                    if start == end:
                        raise ValueError("开始与结束时间不能相同")
                    parsed[weekday].append((start, end))
                except (KeyError, TypeError, ValueError) as exc:
                    self.errors.append(f"{key}[{index}] 无效: {exc}")
        return parsed

    def _weekday_index(self, key: Any) -> Optional[int]:
        normalized = str(key).strip().casefold()
        aliases = {name: index for index, name in enumerate(WEEKDAYS)}
        aliases.update({name[:3]: index for index, name in enumerate(WEEKDAYS)})
        return aliases.get(normalized)

    def _candidate_intervals(self, local_date: date) -> list[tuple[datetime, datetime]]:
        intervals = []
        # 前一天用于识别跨零点，后八天用于找到下一场。
        for offset in range(-1, 9):
            day = local_date + timedelta(days=offset)
            for start_time, end_time in self.windows[day.weekday()]:
                start = datetime.combine(day, start_time, self.zone)
                end = datetime.combine(day, end_time, self.zone)
                if end <= start:
                    end += timedelta(days=1)
                intervals.append((start, end))
        merged: list[tuple[datetime, datetime]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged
