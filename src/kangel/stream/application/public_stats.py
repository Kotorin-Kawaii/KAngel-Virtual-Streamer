"""公开直播统计的只读聚合服务。

统计直接从现有业务表读取，使用很短的进程内缓存避免公开页面刷新时把
查询压力带入直播主链。缓存失效或数据库异常都只影响 ``/public/stats``。
"""

from __future__ import annotations

import copy
import threading
import time
from datetime import date, datetime, time as midnight, timedelta, timezone
from typing import Callable, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import settings
from kangel.infrastructure.database import DatabaseManager, db_manager


class PublicStatsService:
    """计算并缓存匿名公开统计。

    ``clock`` 和 ``monotonic_fn`` 作为依赖注入点，方便测试跨时区边界和
    TTL；生产默认使用 UTC 时钟和单调时钟。返回值始终是新副本，调用方
    修改响应对象不会污染缓存。
    """

    MAX_TTL_SECONDS = 60.0

    def __init__(
        self,
        database: DatabaseManager | None = None,
        *,
        ttl_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
        monotonic_fn: Callable[[], float] | None = None,
    ) -> None:
        self.database = database or db_manager
        # 公开读接口的缓存上限固定在一分钟内，避免误配置造成过时统计。
        self.ttl_seconds = min(self.MAX_TTL_SECONDS, max(0.0, float(ttl_seconds)))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_fn = monotonic_fn or time.monotonic
        self._cache: tuple[float, tuple[str, str], dict[str, Any]] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("公开统计 clock 必须返回 datetime")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _configured_zone() -> tuple[ZoneInfo | timezone, str]:
        configured = str(getattr(settings.stream, "timezone", "UTC") or "UTC").strip()
        try:
            return ZoneInfo(configured), configured
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.utc, "UTC"

    @staticmethod
    def _local_boundaries(
        local_date: date, zone: ZoneInfo | timezone
    ) -> tuple[str, str, str, str]:
        local_start = datetime.combine(local_date, midnight.min, tzinfo=zone)
        local_end = datetime.combine(
            local_date + timedelta(days=1), midnight.min, tzinfo=zone
        )
        utc_start = local_start.astimezone(timezone.utc)
        utc_end = local_end.astimezone(timezone.utc)
        # Naive records are historical server-local timestamps; aware records
        # are written in UTC by the account/auth path. Keep both representations
        # so the public range remains half-open for either storage format.
        return (
            local_start.replace(tzinfo=None).isoformat(),
            local_end.replace(tzinfo=None).isoformat(),
            utc_start.isoformat(),
            utc_end.isoformat(),
        )

    def get(self) -> dict[str, Any]:
        now = self._as_utc(self.clock())
        zone, timezone_name = self._configured_zone()
        local_date = now.astimezone(zone).date()
        cache_key = (timezone_name, local_date.isoformat())
        current_mono = float(self.monotonic_fn())

        with self._lock:
            cached = self._cache
            if (
                self.ttl_seconds > 0
                and cached is not None
                and cached[1] == cache_key
                and current_mono - cached[0] < self.ttl_seconds
            ):
                return copy.deepcopy(cached[2])

            local_start, local_end, utc_start, utc_end = self._local_boundaries(
                local_date, zone
            )
            counts = self.database.get_public_stats(
                local_start=local_start,
                local_end=local_end,
                utc_start=utc_start,
                utc_end=utc_end,
            )
            result = {
                "generated_at": now.isoformat(),
                "timezone": timezone_name,
                "today": {
                    "replies": int(counts["today"]["replies"]),
                    "danmaku": int(counts["today"]["danmaku"]),
                    "new_accounts": int(counts["today"]["new_accounts"]),
                },
                "all_time": {
                    "replies": int(counts["all_time"]["replies"]),
                    "danmaku": int(counts["all_time"]["danmaku"]),
                    "registered_accounts": int(
                        counts["all_time"]["registered_accounts"]
                    ),
                },
            }
            if self.ttl_seconds > 0:
                self._cache = (current_mono, cache_key, copy.deepcopy(result))
            else:
                self._cache = None
            return result

    def clear_cache(self) -> None:
        """清空统计缓存；测试或明确的配置边界变化可调用。"""
        with self._lock:
            self._cache = None


public_stats_service = PublicStatsService()


__all__ = ["PublicStatsService", "public_stats_service"]
