"""按配置时区自然日稳定轮换直播主题。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from utils.logger import logger


DEFAULT_THEME = {
    "id": "just-chatting",
    "name": "轻松杂谈",
    "prompt_hint": "今天以轻松杂谈为点缀。",
}
_THEME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class DailyThemeSnapshot:
    daily_theme_id: str
    daily_theme_name: str
    daily_theme_date: str
    theme_config_valid: bool
    theme_errors: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


class DailyThemeService:
    """容错解析主题，并以稳定打乱后的循环顺序每日轮换。"""

    def __init__(self, timezone_value: str | ZoneInfo, raw_themes: Any):
        self.zone = (
            timezone_value if isinstance(timezone_value, ZoneInfo)
            else ZoneInfo(str(timezone_value))
        )
        self.errors: list[str] = []
        self.themes = self._parse_themes(raw_themes)
        if not self.themes:
            self.errors.append("没有可用主题，已使用默认主题")
            self.themes = [DEFAULT_THEME.copy()]
        self.themes = sorted(
            self.themes,
            key=lambda item: hashlib.sha256(
                f"kangel-theme-order:{item['id']}".encode("utf-8")
            ).digest(),
        )
        self._offset = int.from_bytes(
            hashlib.sha256(
                "|".join(item["id"] for item in self.themes).encode("utf-8")
            ).digest()[:4],
            "big",
        ) % len(self.themes)
        if self.errors:
            logger.warning("每日主题配置存在问题，将安全降级: %s", "; ".join(self.errors))

    def evaluate(self, now: Optional[datetime] = None) -> DailyThemeSnapshot:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        local_date = reference.astimezone(self.zone).date()
        theme = self._theme_for_date(local_date)
        return DailyThemeSnapshot(
            daily_theme_id=theme["id"],
            daily_theme_name=theme["name"],
            daily_theme_date=local_date.isoformat(),
            theme_config_valid=not self.errors,
            theme_errors=list(self.errors),
        )

    def prompt_context(self, now: Optional[datetime] = None) -> dict:
        snapshot = self.evaluate(now)
        theme = next(
            item for item in self.themes if item["id"] == snapshot.daily_theme_id
        )
        return {
            "id": snapshot.daily_theme_id,
            "name": snapshot.daily_theme_name,
            "date": snapshot.daily_theme_date,
            "prompt_hint": theme.get("prompt_hint", ""),
        }

    def seconds_until_change(self, now: Optional[datetime] = None) -> float:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        local_now = reference.astimezone(self.zone)
        next_midnight = datetime.combine(
            local_now.date() + timedelta(days=1), time.min, self.zone
        )
        return max(0.0, (next_midnight - local_now).total_seconds())

    def _theme_for_date(self, local_date: date) -> dict:
        index = (local_date.toordinal() + self._offset) % len(self.themes)
        return self.themes[index]

    def _parse_themes(self, raw: Any) -> list[dict]:
        if not isinstance(raw, list):
            self.errors.append("daily_themes 必须是数组")
            return []
        parsed, seen = [], set()
        for index, item in enumerate(raw):
            try:
                if not isinstance(item, dict):
                    raise ValueError("主题必须是对象")
                theme_id = str(item.get("id", "")).strip()
                name = " ".join(str(item.get("name", "")).strip().split())
                hint = " ".join(str(item.get("prompt_hint", "")).strip().split())
                if not _THEME_ID.fullmatch(theme_id):
                    raise ValueError("id 必须为 1-64 位字母、数字、下划线或连字符")
                if theme_id in seen:
                    raise ValueError("id 重复")
                if not 1 <= len(name) <= 80:
                    raise ValueError("name 必须为 1-80 位")
                if len(hint) > 200:
                    raise ValueError("prompt_hint 不能超过 200 位")
                parsed.append({"id": theme_id, "name": name, "prompt_hint": hint})
                seen.add(theme_id)
            except ValueError as exc:
                self.errors.append(f"daily_themes[{index}] 无效: {exc}")
        return parsed
