"""Sponsor Fund Transparency 的收入同步、支出登记与聚合读取。

这条链路与 ``SponsorService``（感谢墙）完全旁路：收入账本不保存赞助者身份，
支出只能新增/作废，公开读取只返回聚合金额和公开用途。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from time import monotonic
from typing import Any, Optional
from zoneinfo import ZoneInfo

from config import settings
from kangel.infrastructure.database import DatabaseManager, db_manager

from .client import AfdianClient, AfdianError, afdian_client

PLATFORM_AFDIAN = "afdian"
EXPENSE_CATEGORIES = frozenset({
    "ai_api", "server", "network", "domain", "cdn", "software", "hardware", "other",
})
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MAX_ORDER_PAGES = 200


class SponsorFinanceError(Exception):
    """可安全展示给管理端的财务错误；不携带订单原文或凭据。"""

    expected_business_error = True

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(message or code)


class SponsorFinanceService:
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
    #  订单解析（只保留财务字段）
    # ------------------------------------------------------------------

    @staticmethod
    def parse_amount_cents(value: Any) -> int:
        """使用 Decimal 精确转换元为分；不接受负数、NaN 或超过两位小数。"""
        if isinstance(value, bool) or value is None:
            raise SponsorFinanceError("invalid_amount", "订单金额无效")
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise SponsorFinanceError("invalid_amount", "订单金额无效") from None
        if not amount.is_finite() or amount <= 0:
            raise SponsorFinanceError("invalid_amount", "订单金额无效")
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cents = amount * 100
        if cents != cents.to_integral_value():
            raise SponsorFinanceError("invalid_amount", "订单金额无效")
        return int(cents)

    @staticmethod
    def parse_paid_at(value: Any) -> Optional[datetime]:
        """解析订单提供的付款/创建时间，绝不使用数据库插入时间代替。"""
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = float(value)
            if seconds > 100_000_000_000:
                seconds /= 1000
            if seconds <= 0:
                return None
            try:
                return datetime.fromtimestamp(seconds, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        text = str(value).strip()
        try:
            numeric = float(text)
        except ValueError:
            numeric = None
        if numeric is not None:
            return SponsorFinanceService.parse_paid_at(numeric)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    @staticmethod
    def _is_paid(item: dict[str, Any]) -> bool:
        value = item.get("status", item.get("order_status"))
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().casefold()
        return normalized in {"2", "paid", "success", "succeeded", "completed", "已支付", "已完成"}

    @staticmethod
    def _order_identifier(item: dict[str, Any]) -> str:
        for key in ("out_trade_no", "order_id", "order_no", "custom_order_id"):
            value = item.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _accounting_month(self, paid_at: datetime) -> str:
        try:
            tz = ZoneInfo(settings.stream.timezone)
        except Exception:
            tz = timezone.utc
        return paid_at.astimezone(tz).strftime("%Y-%m")

    def build_order(self, item: Any) -> Optional[dict[str, Any]]:
        """把 query-order 订单映射成无身份的财务行；非成功订单返回 None。"""
        if not isinstance(item, dict) or not self._is_paid(item):
            return None
        identifier = self._order_identifier(item)
        if not identifier:
            return None
        amount = next((item.get(key) for key in ("total_amount", "amount", "paid_amount") if item.get(key) is not None), None)
        amount_cents = self.parse_amount_cents(amount)
        paid_value = next(
            (item.get(key) for key in ("pay_time", "payment_time", "paid_at", "create_time", "created_at") if item.get(key) not in (None, "")),
            None,
        )
        paid_at = self.parse_paid_at(paid_value)
        if paid_at is None:
            raise SponsorFinanceError("missing_payment_time", "订单缺少可靠付款时间")
        # 只保存哈希键；原始订单标识永不进入数据库或公开响应。
        order_key = hashlib.sha256(f"{PLATFORM_AFDIAN}:{identifier}".encode("utf-8")).hexdigest()
        status = str(item.get("status", item.get("order_status", "paid"))).strip()[:32] or "paid"
        paid_text = paid_at.isoformat()
        return {
            "order_key": order_key,
            "platform": PLATFORM_AFDIAN,
            "amount_cents": amount_cents,
            "paid_at": paid_text,
            "accounting_month": self._accounting_month(paid_at),
            "order_status": status,
        }

    # ------------------------------------------------------------------
    #  同步
    # ------------------------------------------------------------------

    def fetch_all(self) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        page = 1
        total_page = 1
        max_pages = min(_MAX_ORDER_PAGES, settings.sponsor.finance_sync_max_pages)
        while page <= min(total_page, max_pages):
            data = self.client.query_order_page(page)
            try:
                total_page = max(1, int(data.get("total_page") or 1))
            except (TypeError, ValueError):
                total_page = page
            items = data.get("list")
            if not isinstance(items, list):
                raise AfdianError("invalid_response", "爱发电订单 list 字段异常")
            for item in items:
                try:
                    order = self.build_order(item)
                except SponsorFinanceError:
                    # 单条异常订单不能伪造月份；其余合法订单仍可同步。
                    continue
                if order:
                    records[order["order_key"]] = order
            if not items:
                break
            page += 1
        return list(records.values())

    def sync_once(self) -> int:
        orders = self.fetch_all()
        now_text = self.clock().isoformat()
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for order in orders:
                conn.execute(
                    """
                    INSERT INTO sponsor_orders (
                        order_key, platform, amount_cents, paid_at, accounting_month,
                        order_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(order_key) DO UPDATE SET
                        platform = excluded.platform,
                        amount_cents = excluded.amount_cents,
                        paid_at = excluded.paid_at,
                        accounting_month = excluded.accounting_month,
                        order_status = excluded.order_status,
                        updated_at = excluded.updated_at
                    """,
                    (
                        order["order_key"], order["platform"], order["amount_cents"],
                        order["paid_at"], order["accounting_month"], order["order_status"],
                        now_text, now_text,
                    ),
                )
            conn.execute(
                """
                INSERT INTO sponsor_finance_sync_state (
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
                (PLATFORM_AFDIAN, now_text, now_text, len(orders), now_text),
            )
        self.invalidate_cache()
        return len(orders)

    def record_failure(self, error_code: str) -> int:
        now_text = self.clock().isoformat()
        code = (error_code or "unknown")[:64]
        with self.database._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO sponsor_finance_sync_state (
                    platform, last_success_at, last_attempt_at, last_error_code,
                    consecutive_failures, synced_count, updated_at
                ) VALUES (?, NULL, ?, ?, 1, 0, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    last_attempt_at = excluded.last_attempt_at,
                    last_error_code = excluded.last_error_code,
                    consecutive_failures = sponsor_finance_sync_state.consecutive_failures + 1,
                    updated_at = excluded.updated_at
                """,
                (PLATFORM_AFDIAN, now_text, code, now_text),
            )
            row = conn.execute(
                "SELECT consecutive_failures FROM sponsor_finance_sync_state WHERE platform = ?",
                (PLATFORM_AFDIAN,),
            ).fetchone()
        return int(row["consecutive_failures"]) if row else 1

    def get_sync_stats(self) -> dict[str, Any]:
        with self.database._get_connection() as conn:
            row = conn.execute(
                "SELECT last_success_at, last_attempt_at, last_error_code, consecutive_failures, synced_count "
                "FROM sponsor_finance_sync_state WHERE platform = ?", (PLATFORM_AFDIAN,)
            ).fetchone()
        return {
            "enabled": settings.sponsor.finance_sync_enabled,
            "transparency_enabled": settings.sponsor.transparency_enabled,
            "credentials_configured": bool(settings.sponsor.afdian_user_id.strip() and settings.sponsor.afdian_token.strip()),
            "synced_count": int(row["synced_count"] or 0) if row else 0,
            "consecutive_failures": int(row["consecutive_failures"] or 0) if row else 0,
            "last_success_at": row["last_success_at"] if row else None,
            "last_attempt_at": row["last_attempt_at"] if row else None,
            "last_error_code": row["last_error_code"] if row else None,
        }

    # ------------------------------------------------------------------
    #  支出登记
    # ------------------------------------------------------------------

    @staticmethod
    def validate_expense(month: str, amount_cents: int, category: str, title: str, public_note: Optional[str] = None) -> dict[str, Any]:
        month = str(month or "").strip()
        if not MONTH_RE.fullmatch(month):
            raise SponsorFinanceError("invalid_month", "月份必须是 YYYY-MM")
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
            raise SponsorFinanceError("invalid_amount", "支出金额必须是正整数分")
        category = str(category or "").strip()
        if category not in EXPENSE_CATEGORIES:
            raise SponsorFinanceError("invalid_category", "支出类别无效")
        title = " ".join(str(title or "").split()).strip()
        if not title or len(title) > 120:
            raise SponsorFinanceError("invalid_title", "支出标题不能为空且不得超过 120 字")
        note = None if public_note is None else str(public_note).strip()
        if note and len(note) > 500:
            raise SponsorFinanceError("invalid_note", "公开备注不得超过 500 字")
        return {"month": month, "amount_cents": amount_cents, "category": category, "title": title, "public_note": note or None}

    def list_expenses(self, *, include_void: bool = True) -> list[dict[str, Any]]:
        where = "" if include_void else "WHERE status = 'active'"
        with self.database._get_connection() as conn:
            rows = conn.execute(
                f"SELECT entry_id, month, amount_cents, category, title, public_note, status, created_at, updated_at "
                f"FROM sponsor_fund_entries {where} ORDER BY month DESC, updated_at DESC, entry_id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_expense(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = self.validate_expense(**payload)
        now_text = self.clock().isoformat()
        entry = {"entry_id": uuid.uuid4().hex, **values, "status": "active", "created_at": now_text, "updated_at": now_text}
        with self.database._get_connection() as conn:
            conn.execute(
                "INSERT INTO sponsor_fund_entries (entry_id, month, amount_cents, category, title, public_note, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(entry[key] for key in ("entry_id", "month", "amount_cents", "category", "title", "public_note", "status", "created_at", "updated_at")),
            )
        self.invalidate_cache()
        return entry

    def update_expense(self, entry_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        values = self.validate_expense(**payload)
        now_text = self.clock().isoformat()
        with self.database._get_connection() as conn:
            row = conn.execute("SELECT * FROM sponsor_fund_entries WHERE entry_id = ?", (entry_id,)).fetchone()
            if row is None:
                raise SponsorFinanceError("not_found", "支出记录不存在")
            if row["status"] == "void":
                raise SponsorFinanceError("void_entry", "已作废记录不能编辑")
            conn.execute(
                "UPDATE sponsor_fund_entries SET month=?, amount_cents=?, category=?, title=?, public_note=?, updated_at=? WHERE entry_id=?",
                (values["month"], values["amount_cents"], values["category"], values["title"], values["public_note"], now_text, entry_id),
            )
            updated = conn.execute("SELECT * FROM sponsor_fund_entries WHERE entry_id = ?", (entry_id,)).fetchone()
        self.invalidate_cache()
        return dict(updated)

    def void_expense(self, entry_id: str) -> dict[str, Any]:
        now_text = self.clock().isoformat()
        with self.database._get_connection() as conn:
            row = conn.execute("SELECT * FROM sponsor_fund_entries WHERE entry_id = ?", (entry_id,)).fetchone()
            if row is None:
                raise SponsorFinanceError("not_found", "支出记录不存在")
            if row["status"] == "void":
                return dict(row)
            conn.execute("UPDATE sponsor_fund_entries SET status='void', updated_at=? WHERE entry_id=?", (now_text, entry_id))
            updated = conn.execute("SELECT * FROM sponsor_fund_entries WHERE entry_id = ?", (entry_id,)).fetchone()
        self.invalidate_cache()
        return dict(updated)

    # ------------------------------------------------------------------
    #  公开聚合
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        self._cache = None

    def _load_public(self) -> dict[str, Any]:
        if not settings.sponsor.transparency_enabled:
            return self._disabled_payload()
        with self.database._get_connection() as conn:
            totals = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM sponsor_orders WHERE platform=? AND order_status IN ('2','paid','success','succeeded','completed','已支付','已完成')",
                (PLATFORM_AFDIAN,),
            ).fetchone()
            income_rows = conn.execute(
                "SELECT accounting_month AS month, COALESCE(SUM(amount_cents), 0) AS amount FROM sponsor_orders "
                "WHERE platform=? AND order_status IN ('2','paid','success','succeeded','completed','已支付','已完成') GROUP BY accounting_month",
                (PLATFORM_AFDIAN,),
            ).fetchall()
            expense_rows = conn.execute(
                "SELECT entry_id, month, amount_cents, category, title, public_note, updated_at FROM sponsor_fund_entries WHERE status='active' ORDER BY month ASC, updated_at ASC, entry_id ASC"
            ).fetchall()
            expense_total = conn.execute(
                "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM sponsor_fund_entries WHERE status='active'"
            ).fetchone()
            supporter_row = conn.execute("SELECT COUNT(*) AS total FROM sponsor_records WHERE platform=?", (PLATFORM_AFDIAN,)).fetchone()
            sync_row = conn.execute("SELECT last_success_at FROM sponsor_finance_sync_state WHERE platform=?", (PLATFORM_AFDIAN,)).fetchone()
        received = int(totals["total"] or 0)
        spent = int(expense_total["total"] or 0)
        income = {str(row["month"]): int(row["amount"] or 0) for row in income_rows if MONTH_RE.fullmatch(str(row["month"]))}
        expenses_by_month: dict[str, list[dict[str, Any]]] = {}
        expense_by_month: dict[str, int] = {}
        for row in expense_rows:
            month = str(row["month"])
            expenses_by_month.setdefault(month, []).append({
                "category": row["category"], "title": row["title"], "amount_cents": int(row["amount_cents"]), "note": row["public_note"]
            })
            expense_by_month[month] = expense_by_month.get(month, 0) + int(row["amount_cents"])
        months: list[dict[str, Any]] = []
        balance = 0
        for month in sorted(set(income) | set(expense_by_month)):
            received_month = income.get(month, 0)
            spent_month = expense_by_month.get(month, 0)
            closing = balance + received_month - spent_month
            months.append({
                "month": month, "opening_balance_cents": balance,
                "received_cents": received_month, "spent_cents": spent_month,
                "closing_balance_cents": closing, "expenses": expenses_by_month.get(month, []),
            })
            balance = closing
        updated_candidates = [sync_row["last_success_at"]] if sync_row and sync_row["last_success_at"] else []
        updated_candidates.extend(str(row["updated_at"]) for row in expense_rows if row["updated_at"])
        return {
            "enabled": True,
            "currency": "CNY",
            "received_total_cents": received,
            "spent_total_cents": spent,
            "remaining_cents": received - spent,
            "supporter_count": int(supporter_row["total"] or 0) if supporter_row else 0,
            "updated_at": max(updated_candidates) if updated_candidates else None,
            "months": list(reversed(months)),
        }

    @staticmethod
    def _disabled_payload() -> dict[str, Any]:
        return {
            "enabled": False, "currency": "CNY", "received_total_cents": 0,
            "spent_total_cents": 0, "remaining_cents": 0, "supporter_count": 0,
            "updated_at": None, "months": [],
        }

    def public_transparency(self) -> dict[str, Any]:
        if not settings.sponsor.transparency_enabled:
            return self._disabled_payload()
        ttl = settings.sponsor.transparency_cache_seconds
        if ttl and self._cache and self._cache[0] > monotonic():
            return self._cache[1]
        payload = self._load_public()
        if ttl:
            self._cache = (monotonic() + ttl, payload)
        return payload


sponsor_finance_service = SponsorFinanceService()
