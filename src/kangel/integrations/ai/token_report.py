"""Token 审计的读取侧：把每日聚合行组装成后台需要的视图并折算花费。

花费在这里按当前价目表现算，不读库里的历史金额——改价后曲线自动跟着变。
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import settings
from kangel.infrastructure.database import db_manager

from . import pricing


def _zone():
    try:
        return ZoneInfo(settings.stream.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def today_local() -> str:
    return datetime.now(timezone.utc).astimezone(_zone()).strftime("%Y-%m-%d")


def day_range(days: int) -> tuple[str, str]:
    """含今天在内的最近 days 个自然日。"""
    end = datetime.now(timezone.utc).astimezone(_zone()).date()
    start = end - timedelta(days=max(1, days) - 1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _blank_totals() -> Dict[str, Any]:
    return {
        "calls": 0, "failed_calls": 0, "usage_missing_calls": 0,
        "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
        "reasoning_tokens": 0, "reasoning_missing_calls": 0,
        "total_tokens": 0, "latency_ms_sum": 0,
    }


def _accumulate(target: Dict[str, Any], row: Dict[str, Any]) -> None:
    for key in _blank_totals():
        target[key] += int(row.get(key) or 0)


def _with_cost(row: Dict[str, Any], model: str) -> Dict[str, Any]:
    """给一行附加金额；未配价时 cost_amount 为 None 并计入未计价 token。"""
    cost = pricing.estimate_cost(
        input_tokens=row.get("input_tokens") or 0,
        output_tokens=row.get("output_tokens") or 0,
        cached_input_tokens=row.get("cached_input_tokens") or 0,
        price=pricing.resolve_price(model),
    )
    return {
        **row,
        "cost_amount": None if cost["amount"] is None else round(cost["amount"], 4),
        "priced": cost["priced"],
    }


def daily_report(days: int = 14, database=None) -> Dict[str, Any]:
    """每天一行的总量与折算花费；缺数据的日子补零，方便前端直接画图。"""
    database = database or db_manager
    start, end = day_range(days)
    breakdown = database.get_ai_token_daily_breakdown(start, end)
    rows = database.get_ai_token_daily_totals(start, end)

    indexed = {row["day"]: row for row in rows}
    days_out: List[Dict[str, Any]] = []
    cursor = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    while cursor <= last:
        key = cursor.strftime("%Y-%m-%d")
        base = {**_blank_totals(), **{
            field: int(indexed.get(key, {}).get(field) or 0)
            for field in _blank_totals()
        }}
        days_out.append({"day": key, **base})
        cursor += timedelta(days=1)

    # 按 (day, model) 折算，再回填到对应日期。
    cost_by_day: Dict[str, Dict[str, Any]] = {
        item["day"]: {"amount": 0.0, "unpriced_tokens": 0, "priced": True}
        for item in days_out
    }
    for row in database.get_ai_token_model_days(start, end):
        bucket = cost_by_day.setdefault(
            row["day"], {"amount": 0.0, "unpriced_tokens": 0, "priced": True}
        )
        cost = pricing.estimate_cost(
            input_tokens=row.get("input_tokens") or 0,
            output_tokens=row.get("output_tokens") or 0,
            cached_input_tokens=row.get("cached_input_tokens") or 0,
            price=pricing.resolve_price(str(row.get("model") or "")),
        )
        if cost["amount"] is None:
            bucket["priced"] = False
            bucket["unpriced_tokens"] += int(row.get("total_tokens") or 0)
        else:
            bucket["amount"] += cost["amount"]

    for item in days_out:
        bucket = cost_by_day.get(item["day"], {})
        item["cost_amount"] = round(bucket.get("amount", 0.0), 4)
        item["unpriced_tokens"] = bucket.get("unpriced_tokens", 0)
        item["fully_priced"] = bool(bucket.get("priced", True))

    totals = _blank_totals()
    for item in days_out:
        _accumulate(totals, item)
    total_cost = round(sum(item["cost_amount"] for item in days_out), 4)
    unpriced = sum(item["unpriced_tokens"] for item in days_out)

    return {
        "start_day": start,
        "end_day": end,
        "timezone": settings.stream.timezone,
        "currency": pricing.price_currency(),
        "pricing_configured": bool(settings.ai.pricing),
        "days": days_out,
        "totals": {
            **totals,
            "cost_amount": total_cost,
            "unpriced_tokens": unpriced,
            "distinct_models": len({row["model"] for row in breakdown}),
        },
    }


def breakdown_report(
    start_day: Optional[str] = None, end_day: Optional[str] = None,
    days: int = 14, database=None,
) -> Dict[str, Any]:
    """同一区间一次返回 role / provider / model 三种分组，减少后台请求数。"""
    database = database or db_manager
    if not start_day or not end_day:
        start_day, end_day = day_range(days)
    rows = database.get_ai_token_daily_breakdown(start_day, end_day)

    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {
        "by_role": {}, "by_provider": {}, "by_model": {},
    }
    for row in rows:
        for view, key in (
            ("by_role", row["role"]),
            ("by_provider", row["provider"]),
            ("by_model", row["model"]),
        ):
            bucket = grouped[view].setdefault(
                key, {"key": key, **_blank_totals(), "cost_amount": 0.0,
                      "unpriced_tokens": 0, "fully_priced": True}
            )
            _accumulate(bucket, row)
            cost = pricing.estimate_cost(
                input_tokens=row.get("input_tokens") or 0,
                output_tokens=row.get("output_tokens") or 0,
                cached_input_tokens=row.get("cached_input_tokens") or 0,
                price=pricing.resolve_price(str(row.get("model") or "")),
            )
            if cost["amount"] is None:
                bucket["fully_priced"] = False
                bucket["unpriced_tokens"] += int(row.get("total_tokens") or 0)
            else:
                bucket["cost_amount"] += cost["amount"]

    def finish(view: str) -> List[Dict[str, Any]]:
        items = []
        for item in grouped[view].values():
            item["cost_amount"] = round(item["cost_amount"], 4)
            item["avg_latency_ms"] = (
                round(item["latency_ms_sum"] / item["calls"]) if item["calls"] else 0
            )
            items.append(item)
        return sorted(items, key=lambda x: (-x["total_tokens"], x["key"]))

    return {
        "start_day": start_day,
        "end_day": end_day,
        "currency": pricing.price_currency(),
        "by_role": finish("by_role"),
        "by_provider": finish("by_provider"),
        "by_model": finish("by_model"),
    }


def records_report(
    *, day: Optional[str] = None, role: Optional[str] = None,
    status: Optional[str] = None, limit: int = 100, offset: int = 0,
    database=None,
) -> Dict[str, Any]:
    database = database or db_manager
    payload = database.list_ai_token_usage_records(
        day=day, role=role, status=status, limit=limit, offset=offset
    )
    records = [
        _with_cost(dict(row), str(row.get("model") or ""))
        for row in payload["records"]
    ]
    return {
        **payload,
        "records": records,
        "currency": pricing.price_currency(),
        "detail_enabled": settings.token_audit.detail_enabled,
        "detail_retention_days": settings.token_audit.detail_retention_days,
    }


def audit_stats(database=None) -> Dict[str, Any]:
    """记账器健康度 + 价目覆盖：列出有用量却没配价的模型。"""
    database = database or db_manager
    from .token_audit import token_audit_recorder

    start, end = day_range(30)
    rows = database.get_ai_token_daily_breakdown(start, end)
    unpriced = sorted({
        row["model"] for row in rows
        if pricing.resolve_price(str(row.get("model") or "")) is None
    })
    return {
        "recorder": token_audit_recorder.get_stats(),
        "storage": database.get_ai_token_usage_summary(),
        "pricing": {
            "configured_models": [item.model for item in settings.ai.pricing],
            "currency": pricing.price_currency(),
            "models_without_price": unpriced,
            "lookback_days": 30,
        },
        "timezone": settings.stream.timezone,
        "today": today_local(),
    }


__all__ = [
    "daily_report", "breakdown_report", "records_report", "audit_stats",
    "day_range", "today_local",
]
