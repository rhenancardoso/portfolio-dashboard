"""Positions (``position_list_query``) and cash balances (``accinfo_query``)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.integrations.moomoo.client import Row
from app.integrations.moomoo.parsing import (
    MoomooDataError,
    code_market,
    is_missing,
    require_decimal,
    require_text,
    to_decimal,
)

# accinfo_query cash fields per currency.
CASH_FIELDS = {
    "AUD": "au_cash",
    "USD": "us_cash",
    "HKD": "hk_cash",
    "CNH": "cn_cash",
    "JPY": "jp_cash",
    "SGD": "sg_cash",
    "CAD": "ca_cash",
    "MYR": "my_cash",
}


@dataclass(frozen=True)
class BrokerPositionRecord:
    code: str
    market: str
    name: str | None
    quantity: Decimal
    average_cost: Decimal | None
    currency: str | None
    market_value: Decimal | None


@dataclass(frozen=True)
class BrokerCashBalanceRecord:
    currency: str
    cash_balance: Decimal


def _average_cost(row: Row) -> Decimal | None:
    average = to_decimal(row.get("average_cost"))
    if average:
        return average
    if row.get("cost_price_valid") is True:
        return to_decimal(row.get("cost_price"))
    return None


def parse_positions(rows: list[Row]) -> list[BrokerPositionRecord]:
    """Open long positions. Short positions are not supported and raise."""
    positions = []
    for row in rows:
        code = require_text(row.get("code"), "code")
        quantity = require_decimal(row.get("qty"), "qty")
        if quantity == 0:
            continue
        if row.get("position_side") == "SHORT" or quantity < 0:
            raise MoomooDataError(f"Short position in {code} is not supported")
        market = row.get("position_market")
        positions.append(BrokerPositionRecord(
            code=code,
            market=code_market(code) if is_missing(market) else str(market),
            name=None if is_missing(row.get("stock_name")) else str(row["stock_name"]),
            quantity=quantity,
            average_cost=_average_cost(row),
            currency=None if is_missing(row.get("currency")) else str(row["currency"]),
            market_value=to_decimal(row.get("market_val")),
        ))
    return positions


def parse_cash_balances(rows: list[Row]) -> list[BrokerCashBalanceRecord]:
    """Cash per currency for every currency the broker reports a number for."""
    if not rows:
        return []
    row = rows[0]
    balances = []
    for currency, field in CASH_FIELDS.items():
        amount = to_decimal(row.get(field))
        if amount is not None:
            balances.append(BrokerCashBalanceRecord(currency=currency, cash_balance=amount))
    return balances
