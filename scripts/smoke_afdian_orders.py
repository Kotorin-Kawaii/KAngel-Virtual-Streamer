#!/usr/bin/env python3
"""Run a bounded, privacy-safe smoke check against Afdian ``query-order``.

This is intentionally opt-in.  It performs one page request by default and
prints only field presence/counts; it never prints credentials, order IDs,
user fields, amounts, timestamps, or the response body.  Run it from the
server checkout after the production environment has been loaded::

    uv run python scripts/smoke_afdian_orders.py --allow-live

The command does not write SQLite or alter any sponsor state.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings
from kangel.integrations.sponsor.client import AfdianClient, AfdianError


SAFE_FIELDS = ("out_trade_no", "total_amount", "status", "create_time")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-live",
        action="store_true",
        help="确认允许发起一次真实 query-order 请求",
    )
    parser.add_argument("--page", type=int, default=1, choices=range(1, 201))
    args = parser.parse_args()
    if not args.allow_live:
        parser.error("需要显式传入 --allow-live；默认不会访问外部 API")

    if not settings.sponsor.afdian_user_id.strip() or not settings.sponsor.afdian_token.strip():
        print(json.dumps({"ok": False, "code": "missing_credentials"}, ensure_ascii=False))
        return 2

    try:
        data = AfdianClient().query_order_page(args.page)
    except AfdianError as exc:
        print(json.dumps({"ok": False, "code": exc.code}, ensure_ascii=False))
        return 1

    items = data.get("list")
    if not isinstance(items, list):
        print(json.dumps({"ok": False, "code": "invalid_list"}, ensure_ascii=False))
        return 1

    field_presence = {
        field: sum(isinstance(item, dict) and item.get(field) not in (None, "") for item in items)
        for field in SAFE_FIELDS
    }
    paid_count = sum(
        isinstance(item, dict) and str(item.get("status", "")).strip() == "2"
        for item in items
    )
    print(json.dumps({
        "ok": True,
        "page": args.page,
        "item_count": len(items),
        "total_page_present": data.get("total_page") is not None,
        "field_presence": field_presence,
        "status_2_count": paid_count,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
