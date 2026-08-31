"""赞助者感谢墙的持久化与脱敏读取。

设计约束（P25）：
  - 赞助不授予任何功能权益，这里没有权限判定，只有展示。
  - 公开读取只暴露昵称；金额只在内部统计，从不出现在任何响应里。
  - 名单只增不减：同步失败或某次未返回某人，都不会把已有名单清空。
  - 顺序与金额无关：按 platform_user_id 的哈希稳定打散，不做任何排行。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Optional

from config import settings
from kangel.infrastructure.database import DatabaseManager, db_manager

from .client import AfdianClient, AfdianError, afdian_client

PLATFORM_AFDIAN = "afdian"

# 感谢墙单次读取的硬上限，避免名单意外膨胀时把整张表读进内存。
_MAX_SCAN_ROWS = 5000

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SponsorService:
    def __init__(
        self,
        database: DatabaseManager | None = None,
        client: AfdianClient | None = None,
        clock=None,
    ):
        self.database = database or db_manager
        self.client = client or afdian_client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: Optional[tuple[float, dict[str, Any]]] = None

    # ------------------------------------------------------------------
    #  归一化工具
    # ------------------------------------------------------------------

    def normalize_display_name(self, raw: Any) -> str:
        """清洗外部昵称：去控制字符、压缩空白、截断长度。"""
        text = raw if isinstance(raw, str) else ""
        text = _CONTROL_CHARS.sub("", text)
        text = " ".join(text.split())
        limit = settings.sponsor.max_display_name_chars
        if len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text

    @staticmethod
    def is_anonymous_name(name: str) -> bool:
        """昵称命中匿名关键词则不上真名。"""
        if not name:
            return True
        lowered = name.lower()
        return any(
            keyword.strip().lower() in lowered
            for keyword in settings.sponsor.anonymous_keywords
            if keyword.strip()
        )

    @staticmethod
    def parse_amount_cents(value: Any) -> int:
        """爱发电金额是字符串（如 "5.00"）；解析失败按 0 处理。"""
        try:
            return max(0, int(round(float(value) * 100)))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def parse_timestamp(value: Any) -> Optional[str]:
        """unix 秒 -> ISO 字符串；非法值返回 None。"""
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            return None
        if seconds <= 0:
            return None
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()

    def build_record(self, item: Any) -> Optional[dict[str, Any]]:
        """把 query-sponsor 的一条记录转成待入库的最小字段集。"""
        if not isinstance(item, dict):
            return None
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        platform_user_id = str(user.get("user_id") or "").strip()
        if not platform_user_id:
            return None
        raw_name = self.normalize_display_name(user.get("name"))
        anonymous = self.is_anonymous_name(raw_name)
        config = settings.sponsor
        return {
            "platform_user_id": platform_user_id,
            "display_name": config.anonymous_display_name if anonymous else raw_name,
            "anonymous": 1 if anonymous else 0,
            "hidden": 1 if platform_user_id in set(config.hidden_platform_user_ids) else 0,
            "sum_amount_cents": self.parse_amount_cents(item.get("all_sum_amount")),
            "first_sponsored_at": self.parse_timestamp(
                item.get("first_pay_time") or item.get("create_time")
            ),
            "last_sponsored_at": self.parse_timestamp(item.get("last_pay_time")),
        }

    # ------------------------------------------------------------------
    #  同步
    # ------------------------------------------------------------------

    def fetch_all(self) -> list[dict[str, Any]]:
        """翻页拉取全部赞助者；页数受 sync_max_pages 硬约束。"""
        records: dict[str, dict[str, Any]] = {}
        page = 1
        total_page = 1
        while page <= min(total_page, settings.sponsor.sync_max_pages):
            data = self.client.query_sponsor_page(page)
            try:
                total_page = max(1, int(data.get("total_page") or 1))
            except (TypeError, ValueError):
                total_page = page
            items = data.get("list")
            if not isinstance(items, list):
                raise AfdianError("invalid_response", "爱发电返回 list 字段异常")
            for item in items:
                record = self.build_record(item)
                if record:
                    # 同一人多页重复时保留后出现的一条即可（字段等价）。
                    records[record["platform_user_id"]] = record
            if not items:
                break
            page += 1
        return list(records.values())

    def upsert_records(self, records: list[dict[str, Any]]) -> int:
        """在单个事务里 upsert；不删除库中已有但本次未返回的记录。"""
        now_text = self.clock().isoformat()
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in records:
                conn.execute(
                    """
                    INSERT INTO sponsor_records (
                        platform, platform_user_id, display_name, anonymous, hidden,
                        sum_amount_cents, first_sponsored_at, last_sponsored_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, platform_user_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        anonymous = excluded.anonymous,
                        hidden = excluded.hidden,
                        sum_amount_cents = excluded.sum_amount_cents,
                        first_sponsored_at = COALESCE(
                            sponsor_records.first_sponsored_at, excluded.first_sponsored_at
                        ),
                        last_sponsored_at = COALESCE(
                            excluded.last_sponsored_at, sponsor_records.last_sponsored_at
                        ),
                        updated_at = excluded.updated_at
                    """,
                    (
                        PLATFORM_AFDIAN,
                        record["platform_user_id"],
                        record["display_name"],
                        record["anonymous"],
                        record["hidden"],
                        record["sum_amount_cents"],
                        record["first_sponsored_at"],
                        record["last_sponsored_at"],
                        now_text,
                        now_text,
                    ),
                )
            # 配置里新增的屏蔽项要立刻生效，即使这些人本次没被返回。
            hidden_ids = [str(item).strip() for item in settings.sponsor.hidden_platform_user_ids if str(item).strip()]
            if hidden_ids:
                placeholders = ",".join("?" for _ in hidden_ids)
                conn.execute(
                    f"UPDATE sponsor_records SET hidden = 1, updated_at = ? "
                    f"WHERE platform = ? AND platform_user_id IN ({placeholders})",
                    (now_text, PLATFORM_AFDIAN, *hidden_ids),
                )
            conn.execute(
                """
                INSERT INTO sponsor_sync_state (
                    platform, last_success_at, last_attempt_at, last_error_code,
                    consecutive_failures, synced_count, updated_at
                ) VALUES (?, ?, ?, NULL, 0, ?, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    last_success_at = excluded.last_success_at,
                    last_attempt_at = excluded.last_attempt_at,
                    last_error_code = NULL,
                    consecutive_failures = 0,
                    synced_count = excluded.synced_count,
                    updated_at = excluded.updated_at
                """,
                (PLATFORM_AFDIAN, now_text, now_text, len(records), now_text),
            )
        self.invalidate_cache()
        return len(records)

    def sync_once(self) -> int:
        """同步一次并返回入库条数；失败时抛出，由调用方记录并退避。"""
        records = self.fetch_all()
        return self.upsert_records(records)

    def record_failure(self, error_code: str) -> int:
        """记录一次失败并返回连续失败次数；已有名单保持可读。"""
        now_text = self.clock().isoformat()
        code = (error_code or "unknown")[:64]
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO sponsor_sync_state (
                    platform, last_success_at, last_attempt_at, last_error_code,
                    consecutive_failures, synced_count, updated_at
                ) VALUES (?, NULL, ?, ?, 1, 0, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    last_attempt_at = excluded.last_attempt_at,
                    last_error_code = excluded.last_error_code,
                    consecutive_failures = sponsor_sync_state.consecutive_failures + 1,
                    updated_at = excluded.updated_at
                """,
                (PLATFORM_AFDIAN, now_text, code, now_text),
            )
            row = conn.execute(
                "SELECT consecutive_failures FROM sponsor_sync_state WHERE platform = ?",
                (PLATFORM_AFDIAN,),
            ).fetchone()
        return int(row["consecutive_failures"]) if row else 1

    # ------------------------------------------------------------------
    #  公开读取
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        self._cache = None

    @staticmethod
    def _shuffle_key(platform_user_id: str) -> str:
        """与金额、入库时间都无关的稳定顺序键。"""
        return hashlib.md5(platform_user_id.encode("utf-8")).hexdigest()

    def _load_public(self) -> dict[str, Any]:
        config = settings.sponsor
        with self.database._get_connection() as conn:
            count_row = conn.execute(
                "SELECT COUNT(*) AS total FROM sponsor_records "
                "WHERE platform = ? AND hidden = 0",
                (PLATFORM_AFDIAN,),
            ).fetchone()
            rows = conn.execute(
                "SELECT platform_user_id, display_name FROM sponsor_records "
                "WHERE platform = ? AND hidden = 0 "
                "ORDER BY platform_user_id LIMIT ?",
                (PLATFORM_AFDIAN, _MAX_SCAN_ROWS),
            ).fetchall()
            state_row = conn.execute(
                "SELECT last_success_at FROM sponsor_sync_state WHERE platform = ?",
                (PLATFORM_AFDIAN,),
            ).fetchone()
        entries = sorted(rows, key=lambda row: self._shuffle_key(row["platform_user_id"]))
        return {
            "enabled": True,
            "total_count": int(count_row["total"]) if count_row else 0,
            "updated_at": state_row["last_success_at"] if state_row else None,
            "sponsors": [
                {"display_name": row["display_name"]}
                for row in entries[: config.list_limit]
            ],
        }

    def list_public(self) -> dict[str, Any]:
        """感谢墙数据：仅昵称，无排序，无金额。带进程内 TTL 缓存。"""
        config = settings.sponsor
        if not config.list_enabled:
            return {
                "enabled": False, "total_count": 0,
                "updated_at": None, "sponsors": [],
            }
        ttl = config.list_cache_seconds
        if ttl and self._cache and self._cache[0] > monotonic():
            return self._cache[1]
        payload = self._load_public()
        if ttl:
            self._cache = (monotonic() + ttl, payload)
        return payload

    def get_stats(self) -> dict[str, Any]:
        """管理端同步健康度；不返回凭据，也不返回单人金额。"""
        config = settings.sponsor
        with self.database._get_connection() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN hidden = 1 THEN 1 ELSE 0 END) AS hidden_count, "
                "SUM(CASE WHEN anonymous = 1 THEN 1 ELSE 0 END) AS anonymous_count "
                "FROM sponsor_records WHERE platform = ?",
                (PLATFORM_AFDIAN,),
            ).fetchone()
            state = conn.execute(
                "SELECT last_success_at, last_attempt_at, last_error_code, "
                "consecutive_failures, synced_count "
                "FROM sponsor_sync_state WHERE platform = ?",
                (PLATFORM_AFDIAN,),
            ).fetchone()
        return {
            "enabled": config.enabled,
            "sync_enabled": config.sync_enabled,
            "credentials_configured": bool(
                config.afdian_user_id.strip() and config.afdian_token.strip()
            ),
            "sponsor_count": int(totals["total"] or 0) if totals else 0,
            "hidden_count": int(totals["hidden_count"] or 0) if totals else 0,
            "anonymous_count": int(totals["anonymous_count"] or 0) if totals else 0,
            "synced_count": int(state["synced_count"] or 0) if state else 0,
            "consecutive_failures": int(state["consecutive_failures"] or 0) if state else 0,
            "last_success_at": state["last_success_at"] if state else None,
            "last_attempt_at": state["last_attempt_at"] if state else None,
            "last_error_code": state["last_error_code"] if state else None,
        }

    def public_config(self) -> dict[str, Any]:
        """前端展示元数据；绝不包含 afdian_user_id / afdian_token。"""
        config = settings.sponsor
        return {
            "enabled": config.enabled,
            "list_enabled": config.list_enabled,
            "platform_name": config.platform_name,
            "platform_url": config.platform_url,
            "notice_text": config.notice_text,
        }


sponsor_service = SponsorService()
