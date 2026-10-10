"""共享的时间解析辅助函数。"""

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_timestamp_as_utc(
    value: Any,
    *,
    naive_timezone: str = "UTC",
) -> datetime:
    """将 ISO 时间戳统一解析为带时区的 UTC 时间。

    旧数据中的普通弹幕时间戳可能是服务器本地时间（无 offset），而
    SC 时间戳通常带有 UTC offset。统一到 UTC 后再比较，避免字符串排序
    把两种表示当成同一时区。
    """
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)

    if parsed.tzinfo is None:
        try:
            local_zone = ZoneInfo(naive_timezone)
        except (TypeError, ZoneInfoNotFoundError, ValueError):
            local_zone = timezone.utc
        parsed = parsed.replace(tzinfo=local_zone)

    return parsed.astimezone(timezone.utc)
