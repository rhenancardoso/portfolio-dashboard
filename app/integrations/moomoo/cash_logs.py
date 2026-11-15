"""Account cash flows (``get_acc_cash_flow``), one clearing date per request.

Moomoo's ``cashflow_type`` is free text and not documented, so it is stored raw
on every movement and mapped to a movement type through ``CASHFLOW_TYPE_MAP``.
Unmapped types become ``OTHER`` and can be re-classified later from the raw value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.database.models import CashMovementType
from app.integrations.moomoo.client import Row
from app.integrations.moomoo.parsing import (
    MoomooDataError,
    is_missing,
    require_decimal,
    require_text,
)

# Keys are lower-case Moomoo cashflow_type values. Filled in once real types have
# been seen (run the "probe" command).
CASHFLOW_TYPE_MAP: dict[str, CashMovementType] = {}


@dataclass(frozen=True)
class BrokerCashFlowRecord:
    cashflow_id: str
    clearing_date: date
    currency: str
    amount: Decimal  # signed: inflow positive, outflow negative
    broker_type: str | None
    remark: str | None


def classify_cash_flow(broker_type: str | None) -> CashMovementType:
    if broker_type is None:
        return CashMovementType.OTHER
    return CASHFLOW_TYPE_MAP.get(broker_type.strip().lower(), CashMovementType.OTHER)


def parse_cash_flows(rows: list[Row]) -> list[BrokerCashFlowRecord]:
    flows = []
    for row in rows:
        cashflow_id = require_text(row.get("cashflow_id"), "cashflow_id")
        amount = require_decimal(row.get("cashflow_amount"), "cashflow_amount")
        direction = row.get("cashflow_direction")
        if direction == "OUT":
            amount = -abs(amount)
        elif direction == "IN":
            amount = abs(amount)
        try:
            clearing_date = datetime.strptime(
                require_text(row.get("clearing_date"), "clearing_date"), "%Y-%m-%d"
            ).date()
        except ValueError as exc:
            raise MoomooDataError(f"Cash flow {cashflow_id}: bad clearing_date") from exc
        flows.append(BrokerCashFlowRecord(
            cashflow_id=cashflow_id,
            clearing_date=clearing_date,
            currency=require_text(row.get("currency"), "currency"),
            amount=amount,
            broker_type=None if is_missing(row.get("cashflow_type")) else str(row["cashflow_type"]),
            remark=None if is_missing(row.get("cashflow_remark")) else str(row["cashflow_remark"]),
        ))
    return flows
