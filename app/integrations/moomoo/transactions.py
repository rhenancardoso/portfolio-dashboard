"""Executed fills (``history_deal_list_query``) and order fees (``order_fee_query``).

Fills are used, never submitted orders. A fill has no fee; fees are reported per
order and fetched separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.integrations.moomoo.client import Row
from app.integrations.moomoo.parsing import (
    MoomooDataError,
    code_market,
    is_missing,
    market_time_to_utc,
    require_decimal,
    require_text,
)

# The API defaults to 90-day windows when one bound is missing; requests use the same size.
DEAL_WINDOW_DAYS = 90

TRADE_SIDES = {"BUY": "buy", "SELL": "sell"}


@dataclass(frozen=True)
class BrokerDealRecord:
    deal_id: str
    order_id: str | None
    code: str
    market: str
    side: str  # "buy" or "sell"
    quantity: Decimal
    price: Decimal
    executed_at: datetime  # UTC
    status: str  # OK, CANCELLED or CHANGED


def parse_deals(rows: list[Row]) -> list[BrokerDealRecord]:
    deals = []
    for row in rows:
        deal_id = require_text(row.get("deal_id"), "deal_id")
        code = require_text(row.get("code"), "code")
        side = TRADE_SIDES.get(str(row.get("trd_side")))
        if side is None:
            raise MoomooDataError(f"Deal {deal_id}: unsupported trade side {row.get('trd_side')!r}")
        market = row.get("deal_market")
        market = code_market(code) if is_missing(market) else str(market)
        deals.append(BrokerDealRecord(
            deal_id=deal_id,
            order_id=None if is_missing(row.get("order_id")) else str(row["order_id"]),
            code=code,
            market=market,
            side=side,
            quantity=require_decimal(row.get("qty"), "qty"),
            price=require_decimal(row.get("price"), "price"),
            executed_at=market_time_to_utc(require_text(row.get("create_time"), "create_time"), market),
            status="OK" if is_missing(row.get("status")) else str(row["status"]),
        ))
    return deals


def parse_order_fees(rows: list[Row]) -> dict[str, Decimal]:
    """Total fee per order id."""
    return {
        require_text(row.get("order_id"), "order_id"): require_decimal(row.get("fee_amount"), "fee_amount")
        for row in rows
    }


def deal_windows(start: date, end: date) -> list[tuple[str, str]]:
    """Consecutive inclusive date windows covering ``start``..``end``, as API time strings."""
    windows = []
    window_start = start
    while window_start <= end:
        window_end = min(window_start + timedelta(days=DEAL_WINDOW_DAYS - 1), end)
        windows.append((f"{window_start:%Y-%m-%d} 00:00:00", f"{window_end:%Y-%m-%d} 23:59:59"))
        window_start = window_end + timedelta(days=1)
    return windows
