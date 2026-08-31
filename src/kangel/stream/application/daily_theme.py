"""按配置时区自然日稳定轮换直播主题。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from kangel.shared.logging import logger


DEFAULT_THEME = {
    "id": "just-chatting",
    "name": "轻松杂谈",
    "prompt_hint": "今天以轻松杂谈为点缀。",
}
_THEME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MONTH_DAY = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_BIAS_KEYS = frozenset({"mood", "stress", "darkness"})


@dataclass(frozen=True)
class DailyThemeSnapshot:
    daily_theme_id: str
    daily_theme_name: str
    daily_theme_date: str
    theme_config_valid: bool
    theme_errors: list[str]
    special_date_theme: Optional[dict] = None
    activity_theme_id: Optional[str] = None
    special_idle_state_hint: Optional[str] = None
    special_mood_bias: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class DailyThemeService:
    """容错解析主题，并以稳定打乱后的循环顺序每日轮换。"""

    def __init__(
        self,
        timezone_value: str | ZoneInfo,
        raw_themes: Any,
        raw_special_date_themes: Any = None,
    ):
        self.zone = (
            timezone_value if isinstance(timezone_value, ZoneInfo)
            else ZoneInfo(str(timezone_value))
        )
        self.errors: list[str] = []
        self.themes = self._parse_themes(raw_themes)
        self.special_date_themes = self._parse_special_date_themes(
            [] if raw_special_date_themes is None else raw_special_date_themes
        )
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
        special = self._special_theme_for_date(local_date)
        return DailyThemeSnapshot(
            daily_theme_id=theme["id"],
            daily_theme_name=theme["name"],
            daily_theme_date=local_date.isoformat(),
            theme_config_valid=not self.errors,
            theme_errors=list(self.errors),
            special_date_theme=self._public_special_theme(special, local_date),
            activity_theme_id=(
                special.get("activity_theme_id") if special else None
            ),
            special_idle_state_hint=(
                special.get("idle_state_hint") if special else None
            ),
            special_mood_bias=(dict(special.get("mood_bias", {})) if special else {}),
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
            "special_date_theme": self._prompt_special_theme(
                self._special_theme_for_date(
                    datetime.fromisoformat(snapshot.daily_theme_date).date()
                )
            ),
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

    def _special_theme_for_date(self, local_date: date) -> Optional[dict]:
        candidates = [
            item for item in self.special_date_themes
            if item["date"] == local_date.strftime("%m-%d")
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: (-item["priority"], item["id"]))

    @staticmethod
    def _public_special_theme(item: Optional[dict], local_date: date) -> Optional[dict]:
        if not item:
            return None
        return {
            "id": item["id"],
            "name": item["name"],
            "title": item["title"],
            "frontend_theme": item.get("frontend_theme") or None,
            "date": local_date.isoformat(),
        }

    @staticmethod
    def _prompt_special_theme(item: Optional[dict]) -> Optional[dict]:
        if not item:
            return None
        return {
            "id": item["id"],
            "name": item["name"],
            "title": item["title"],
            "prompt_hint": item.get("prompt_hint", ""),
            "mood_bias": dict(item.get("mood_bias", {})),
            "idle_state_hint": item.get("idle_state_hint"),
        }

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
                stream_plan = item.get("stream_plan")
                if stream_plan is not None and not isinstance(stream_plan, dict):
                    raise ValueError("stream_plan 必须是对象")
                # 这里只保留配置；Plan 的交叉引用和 immutable schema 由
                # DailyStreamPlanService 校验，避免 DailyTheme 演化成第二套计划系统。
                parsed.append({
                    "id": theme_id,
                    "name": name,
                    "prompt_hint": hint,
                    "stream_plan": stream_plan,
                })
                seen.add(theme_id)
            except ValueError as exc:
                self.errors.append(f"daily_themes[{index}] 无效: {exc}")
        return parsed

    def get_stream_plan_config(self, theme_id: str) -> Optional[dict]:
        """返回主题声明的 Plan 配置副本；运行时状态永远不写回此对象。"""
        item = next((theme for theme in self.themes if theme["id"] == theme_id), None)
        plan = item.get("stream_plan") if item else None
        if not isinstance(plan, dict):
            return None
        # JSON round-trip 提供足够的深拷贝，同时约束配置必须可序列化。
        import json
        try:
            return json.loads(json.dumps(plan, ensure_ascii=False))
        except (TypeError, ValueError):
            return None

    def _parse_special_date_themes(self, raw: Any) -> list[dict]:
        if not isinstance(raw, list):
            self.errors.append("special_date_themes 必须是数组")
            return []
        parsed, seen = [], set()
        for index, item in enumerate(raw):
            try:
                if not isinstance(item, dict):
                    raise ValueError("主题必须是对象")
                item_id = str(item.get("id", "")).strip()
                date_value = str(item.get("date", "")).strip()
                name = " ".join(str(item.get("name", "")).strip().split())
                title = " ".join(str(item.get("title", "")).strip().split())
                hint = " ".join(str(item.get("prompt_hint", "")).strip().split())
                if not _THEME_ID.fullmatch(item_id):
                    raise ValueError("id 必须为 1-64 位字母、数字、下划线或连字符")
                if item_id in seen:
                    raise ValueError("id 重复")
                if not self._valid_month_day(date_value):
                    raise ValueError("date 必须是有效 MM-DD（02-29 允许）")
                if not 1 <= len(name) <= 80:
                    raise ValueError("name 必须为 1-80 位")
                if not 1 <= len(title) <= 120:
                    raise ValueError("title 必须为 1-120 位")
                if len(hint) > 200:
                    raise ValueError("prompt_hint 不能超过 200 位")
                priority = item.get("priority", 0)
                if isinstance(priority, bool) or not isinstance(priority, int):
                    raise ValueError("priority 必须是整数")
                if not -10000 <= priority <= 10000:
                    raise ValueError("priority 必须在 -10000 到 10000")
                bias = self._parse_mood_bias(item.get("mood_bias", {}))
                frontend_theme = self._optional_id(item.get("frontend_theme"), "frontend_theme")
                activity_theme_id = self._optional_id(
                    item.get("activity_theme_id"), "activity_theme_id"
                )
                idle_state_hint = self._optional_id(
                    item.get("idle_state_hint"), "idle_state_hint"
                )
                parsed.append({
                    "id": item_id, "date": date_value, "name": name, "title": title,
                    "priority": priority, "prompt_hint": hint, "mood_bias": bias,
                    "frontend_theme": frontend_theme,
                    "activity_theme_id": activity_theme_id,
                    "idle_state_hint": idle_state_hint,
                })
                seen.add(item_id)
            except ValueError as exc:
                self.errors.append(f"special_date_themes[{index}] 无效: {exc}")
        return parsed

    @staticmethod
    def _valid_month_day(value: str) -> bool:
        if not _MONTH_DAY.fullmatch(value):
            return False
        month, day = (int(part) for part in value.split("-"))
        max_days = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
        return day <= max_days[month - 1]

    @staticmethod
    def _optional_id(value: Any, field: str) -> Optional[str]:
        if value is None or str(value).strip() == "":
            return None
        normalized = str(value).strip()
        if not _THEME_ID.fullmatch(normalized):
            raise ValueError(f"{field} 必须为 1-64 位字母、数字、下划线或连字符")
        return normalized

    @staticmethod
    def _parse_mood_bias(value: Any) -> dict[str, float]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("mood_bias 必须是对象")
        unknown = set(value) - _BIAS_KEYS
        if unknown:
            raise ValueError("mood_bias 包含未知字段")
        parsed = {}
        for key, amount in value.items():
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                raise ValueError(f"mood_bias.{key} 必须是数字")
            amount = float(amount)
            if not -0.2 <= amount <= 0.2:
                raise ValueError(f"mood_bias.{key} 必须在 -0.2 到 0.2")
            parsed[key] = amount
        return parsed
