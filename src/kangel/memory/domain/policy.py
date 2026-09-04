"""账号级人物记忆的最小存储、脱敏与保留策略。"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

class AccountMemoryPolicy:
    def __init__(self, max_text_length: int = 500, retention_days: int = 180):
        self.max_text_length = max(1, max_text_length)
        self.retention_days = max(1, retention_days)

    _do_not_store = re.compile(
        r"(?:不要|别|请勿)(?:记住|记录|保存)|不要写进记忆|off[ -]?the[ -]?record",
        re.IGNORECASE,
    )
    _redactions = (
        # Start only at a local-part boundary. Without this guard a long word
        # with no '@' retries the greedy prefix at every character (quadratic),
        # which is costly for v2's retained long summaries even off the event loop.
        (re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "[已隐藏邮箱]"),
        (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[已隐藏手机号]"),
        (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[已隐藏证件号]"),
        (re.compile(
            r"(?i)(password|passwd|api[_ -]?key|access[_ -]?token|secret)\s*[:=]\s*\S+"
        ), "[已隐藏凭据]"),
    )

    def prepare_text(self, text: str) -> Optional[str]:
        """返回可持久化文本；明确要求不记忆时返回 None。"""
        normalized = " ".join((text or "").strip().split())
        if not normalized or self._do_not_store.search(normalized):
            return None
        for pattern, replacement in self._redactions:
            normalized = pattern.sub(replacement, normalized)
        return normalized[:self.max_text_length]

    def retention_cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=self.retention_days)

    def is_expired(self, timestamp: str) -> bool:
        if not timestamp:
            return False
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed < self.retention_cutoff()
        except ValueError:
            return True
