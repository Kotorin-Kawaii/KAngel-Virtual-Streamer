"""AI token 花费折算：读取时按价目表计算，不落库。

刻意不存金额：改价目表后历史数据自动按新价重算，也避免把过时单价固化进数据库。
未配价的模型不做兜底假设，由调用方显示「未配价」并单独汇总未计价 token。
"""

from typing import Any, Dict, Optional

from config import settings
from config.settings import AIModelPrice

WILDCARD = "*"


def resolve_price(model: str) -> Optional[AIModelPrice]:
    """精确匹配（大小写不敏感）优先，其次 "*" 兜底；都没有则返回 None。"""
    if not model:
        return None
    target = model.strip().casefold()
    fallback: Optional[AIModelPrice] = None
    for item in settings.ai.pricing:
        name = item.model.strip()
        if name == WILDCARD:
            fallback = item
            continue
        if name.casefold() == target:
            return item
    return fallback


def estimate_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    price: Optional[AIModelPrice],
) -> Dict[str, Any]:
    """按 100 万 token 单价折算；未配价时 amount 为 None 而不是 0。"""
    if price is None:
        return {"amount": None, "currency": None, "priced": False}
    cached = max(0, min(int(cached_input_tokens or 0), max(0, int(input_tokens or 0))))
    uncached = max(0, int(input_tokens or 0)) - cached
    cached_rate = (
        price.input_per_1m if price.cached_input_per_1m is None
        else price.cached_input_per_1m
    )
    amount = (
        uncached * price.input_per_1m
        + cached * cached_rate
        + max(0, int(output_tokens or 0)) * price.output_per_1m
    ) / 1_000_000
    return {
        "amount": round(amount, 6),
        "currency": price.currency,
        "priced": True,
    }


def price_currency() -> Optional[str]:
    """价目表币种；配置校验已保证同一份配置只用一种币种。"""
    for item in settings.ai.pricing:
        return item.currency
    return None


def cost_for_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """给聚合行附加花费字段；调用方负责求和与舍入。"""
    price = resolve_price(str(row.get("model") or ""))
    return estimate_cost(
        input_tokens=row.get("input_tokens") or 0,
        output_tokens=row.get("output_tokens") or 0,
        cached_input_tokens=row.get("cached_input_tokens") or 0,
        price=price,
    )


__all__ = ["resolve_price", "estimate_cost", "price_currency", "cost_for_row", "WILDCARD"]
