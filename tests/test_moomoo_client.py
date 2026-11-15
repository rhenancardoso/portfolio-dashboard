"""Moomoo client safety, rate limiting, retries and response parsing (no OpenD needed)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import socket

import pandas as pd
import pytest

from app.database.models import CashMovementType
from app.integrations.moomoo import cash_logs
from app.integrations.moomoo.authentication import OpenDSettings
from app.integrations.moomoo.cash_logs import classify_cash_flow, parse_cash_flows
from app.integrations.moomoo.client import (
    MoomooApiError,
    MoomooClient,
    RateLimiter,
    _create_sdk_context,
)
from app.integrations.moomoo.parsing import MoomooDataError
from app.integrations.moomoo.positions import parse_cash_balances, parse_positions
from app.integrations.moomoo.transactions import deal_windows, parse_deals


class FakeContext:
    """Stands in for the SDK's OpenSecTradeContext."""

    def __init__(self, responses: list[tuple[int, object]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.connect_timeout: int | None = None

    def set_sync_query_connect_timeout(self, seconds: int) -> None:
        self.connect_timeout = seconds

    def get_acc_list(self, **kwargs):
        self.calls.append(("get_acc_list", kwargs))
        return self.responses.pop(0)

    def order_fee_query(self, **kwargs):
        self.calls.append(("order_fee_query", kwargs))
        return 0, pd.DataFrame([{"order_id": order_id, "fee_amount": 1.0} for order_id in kwargs["order_id_list"]])

    def place_order(self, **kwargs):  # must never be reachable
        raise AssertionError("place_order called")

    def close(self) -> None:
        pass


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(context: FakeContext, clock: FakeClock | None = None) -> MoomooClient:
    clock = clock or FakeClock()
    return MoomooClient(
        OpenDSettings(connect_timeout_seconds=7),
        context_factory=lambda settings: context, clock=clock, sleep=clock.sleep,
    )


def test_only_read_only_calls_are_reachable():
    context = FakeContext([(0, pd.DataFrame([{"acc_id": 1}]))])
    client = make_client(context)
    assert client.list_accounts() == [{"acc_id": 1}]
    assert context.connect_timeout == 7
    proxy = client._get_context()
    for forbidden in ("place_order", "modify_order", "cancel_all_order", "unlock_trade"):
        with pytest.raises(PermissionError):
            getattr(proxy, forbidden)


def test_failed_calls_are_retried_with_backoff_then_raise():
    clock = FakeClock()
    context = FakeContext([(-1, "busy"), (0, pd.DataFrame([{"acc_id": 1}]))])
    assert make_client(context, clock).list_accounts() == [{"acc_id": 1}]
    assert clock.sleeps == [2.0]

    clock = FakeClock()
    context = FakeContext([(-1, "OpenD not logged in")] * 3)
    with pytest.raises(MoomooApiError, match="not logged in"):
        make_client(context, clock).list_accounts()
    assert clock.sleeps == [2.0, 4.0]


def test_rate_limiter_waits_for_the_window():
    clock = FakeClock()
    limiter = RateLimiter(2, 30.0, clock, clock.sleep)
    limiter.acquire()
    clock.now = 10.0
    limiter.acquire()
    limiter.acquire()  # third call must wait until the first leaves the window
    assert clock.sleeps == [20.0]


def test_order_fee_queries_are_batched():
    context = FakeContext([])
    rows = make_client(context).get_order_fees(1, [str(i) for i in range(401)])
    assert len(rows) == 401
    assert [len(kwargs["order_id_list"]) for _, kwargs in context.calls] == [400, 1]


def test_deals_are_parsed_exactly_in_utc():
    deals = parse_deals([
        {"deal_id": "D1", "order_id": "O1", "code": "AU.IOZ", "deal_market": "AU", "trd_side": "BUY",
         "qty": 100.0, "price": 31.255, "create_time": "2026-11-03 10:15:00", "status": "OK"},
        {"deal_id": "D2", "order_id": "O2", "code": "US.AAPL", "deal_market": "N/A", "trd_side": "SELL",
         "qty": 2.0, "price": 180.1, "create_time": "2026-07-01 09:31:00.500", "status": "OK"},
    ])
    assert deals[0].price == Decimal("31.255")
    assert deals[0].executed_at == datetime(2026, 11, 2, 23, 15, tzinfo=timezone.utc)  # AEDT
    assert deals[1].market == "US"  # from the code when the market is missing
    assert deals[1].executed_at == datetime(2026, 7, 1, 13, 31, 0, 500000, tzinfo=timezone.utc)  # EDT
    with pytest.raises(MoomooDataError, match="SELL_SHORT"):
        parse_deals([{**{"deal_id": "D3", "code": "US.AAPL", "qty": 1, "price": 1,
                         "create_time": "2026-01-01 10:00:00"}, "trd_side": "SELL_SHORT"}])


def test_positions_and_balances_parsing():
    positions = parse_positions([
        {"code": "AU.IOZ", "position_market": "AU", "qty": 150.0, "average_cost": 0.0,
         "cost_price": 31.3, "cost_price_valid": True, "currency": "AUD", "market_val": 4800.0},
        {"code": "US.AAPL", "position_market": "US", "qty": 0.0},
    ])
    assert len(positions) == 1 and positions[0].average_cost == Decimal("31.3")
    with pytest.raises(MoomooDataError):
        parse_positions([{"code": "US.AAPL", "qty": 1.0, "position_side": "SHORT"}])
    balances = parse_cash_balances([{"au_cash": 120.55, "us_cash": "N/A", "hk_cash": float("nan")}])
    assert [(b.currency, b.cash_balance) for b in balances] == [("AUD", Decimal("120.55"))]


def test_cash_flow_sign_follows_direction_and_unknown_types_are_other(monkeypatch):
    flows = parse_cash_flows([
        {"cashflow_id": 7, "clearing_date": "2026-11-05", "currency": "AUD", "cashflow_type": "Trade",
         "cashflow_direction": "OUT", "cashflow_amount": 4687.5, "cashflow_remark": "N/A"},
    ])
    assert flows[0].amount == Decimal("-4687.5") and flows[0].remark is None
    assert classify_cash_flow("Something new") is CashMovementType.OTHER
    monkeypatch.setitem(cash_logs.CASHFLOW_TYPE_MAP, "deposit", CashMovementType.DEPOSIT)
    assert classify_cash_flow(" Deposit ") is CashMovementType.DEPOSIT


def test_deal_windows_cover_the_range_without_gaps():
    windows = deal_windows(date(2025, 1, 1), date(2025, 6, 30))
    assert windows[0] == ("2025-01-01 00:00:00", "2025-03-31 23:59:59")
    assert windows[-1][1] == "2025-06-30 23:59:59"
    assert len(windows) == 3


def test_unreachable_opend_fails_fast_instead_of_hanging():
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        free_port = probe_socket.getsockname()[1]
    with pytest.raises(MoomooApiError, match="not reachable"):
        _create_sdk_context(OpenDSettings(port=free_port, connect_timeout_seconds=2))
