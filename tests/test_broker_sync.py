"""Moomoo synchronisation against a fake broker: idempotency, fees, failures and validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.database.db import session_scope
from app.database.models import (
    AppSetting,
    BrokerAccount,
    BrokerCashBalance,
    CashMovement,
    CashMovementType,
    Position,
    RunStatus,
    SyncHistory,
    SyncType,
    Transaction,
)
from app.database.seed import seed_reference_data
from app.integrations.moomoo.client import MoomooApiError
from app.services.broker_sync_service import MoomooSyncService, validate_broker_holdings

NOW = datetime(2026, 11, 15, 10, 0, tzinfo=timezone.utc)  # 21:00 in Melbourne


class FakeBroker:
    trading_environment = "REAL"

    def __init__(self) -> None:
        self.accounts = [{
            "acc_id": 281756000001234, "trd_env": "REAL", "acc_type": "MARGIN",
            "card_num": "1001000012345678", "security_firm": "FUTUAU",
            "trdmarket_auth": ["AU", "US"], "acc_status": "ACTIVE",
        }]
        self.positions = [
            {"code": "AU.IOZ", "position_market": "AU", "qty": 150.0, "average_cost": 31.3, "currency": "AUD"},
            {"code": "US.AAPL", "position_market": "US", "qty": 2.0, "average_cost": 180.5, "currency": "USD"},
        ]
        self.deals = [
            {"deal_id": "D1", "order_id": "O1", "code": "AU.IOZ", "deal_market": "AU", "trd_side": "BUY",
             "qty": 100.0, "price": 31.25, "create_time": "2026-11-03 10:15:00", "status": "OK"},
            {"deal_id": "D2", "order_id": "O1", "code": "AU.IOZ", "deal_market": "AU", "trd_side": "BUY",
             "qty": 50.0, "price": 31.4, "create_time": "2026-11-03 10:16:00", "status": "OK"},
            {"deal_id": "D3", "order_id": "O2", "code": "US.AAPL", "deal_market": "US", "trd_side": "BUY",
             "qty": 2.0, "price": 180.5, "create_time": "2026-11-05 09:31:00", "status": "OK"},
        ]
        self.fees = {"O1": 6.0, "O2": 1.99}
        self.cash_flows = {
            "2026-11-02": [{"cashflow_id": 101, "clearing_date": "2026-11-02", "currency": "AUD",
                            "cashflow_type": "Deposit", "cashflow_direction": "IN",
                            "cashflow_amount": 5000.0, "cashflow_remark": "Bank transfer"}],
        }
        self.failing: set[str] = set()
        self.deal_requests: list[tuple[str, str]] = []
        self.cash_flow_days: list[str] = []

    def _check(self, name: str) -> None:
        if name in self.failing:
            raise MoomooApiError(f"{name} failed: simulated outage")

    def list_accounts(self):
        self._check("accounts")
        return self.accounts

    def get_account_info(self, account_id, currency="AUD"):
        self._check("balances")
        return [{"au_cash": 512.34, "us_cash": 10.0}]

    def get_positions(self, account_id):
        self._check("positions")
        return self.positions

    def get_history_deals(self, account_id, start, end):
        self._check("deals")
        self.deal_requests.append((start, end))
        return self.deals

    def get_order_fees(self, account_id, order_ids):
        return [{"order_id": order_id, "fee_amount": self.fees[order_id]} for order_id in order_ids]

    def get_cash_flows(self, account_id, clearing_date):
        self._check("cash_flows")
        self.cash_flow_days.append(clearing_date)
        return self.cash_flows.get(clearing_date, [])


@pytest.fixture
def seeded(session_factory):
    with session_scope(session_factory) as session:
        seed_reference_data(session)
        session.get(AppSetting, "moomoo.history_start").value = "2026-11-01"
    return session_factory


def run_sync(session_factory, broker: FakeBroker, now: datetime = NOW):
    return MoomooSyncService(session_factory, broker, now=lambda: now).sync_all()


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_full_sync_imports_everything_once(seeded):
    broker = FakeBroker()
    report = run_sync(seeded, broker)
    assert report.succeeded, [(s.sync_type, s.error, s.issues) for s in report.steps]

    with session_scope(seeded) as session:
        account = session.scalars(select(BrokerAccount)).one()
        assert account.name == "Moomoo MARGIN ****5678" and account.market == "AU,US"
        fills = {t.broker_transaction_id: t for t in session.scalars(select(Transaction))}
        assert fills["D1"].fees == Decimal("6.0") and fills["D2"].fees == Decimal("0")  # once per order
        assert fills["D3"].fees == Decimal("1.99") and fills["D3"].currency == "USD"
        assert fills["D1"].executed_at == datetime(2026, 11, 2, 23, 15, tzinfo=timezone.utc)
        deposit = session.scalars(select(CashMovement)).one()
        assert deposit.amount == Decimal("5000.0") and deposit.broker_type == "Deposit"
        assert deposit.movement_type is CashMovementType.OTHER  # until the type map is confirmed
        assert count(session, Position) == 2
        assert {b.currency: b.cash_balance for b in session.scalars(select(BrokerCashBalance))} == {
            "AUD": Decimal("512.34"), "USD": Decimal("10.0")
        }
    assert broker.deal_requests == [("2026-11-01 00:00:00", "2026-11-15 23:59:59")]
    assert broker.cash_flow_days[0] == "2026-11-01" and broker.cash_flow_days[-1] == "2026-11-15"

    second = run_sync(seeded, broker, NOW + timedelta(hours=1))
    assert second.succeeded
    with session_scope(seeded) as session:
        assert count(session, Transaction) == 3
        assert count(session, CashMovement) == 1
        assert count(session, Position) == 2  # replaced, not appended
    transactions_step = next(s for s in second.steps if s.sync_type is SyncType.TRANSACTIONS)
    assert transactions_step.inserted == 0 and transactions_step.skipped == 3


def test_unknown_symbols_are_reported_not_guessed(seeded):
    broker = FakeBroker()
    broker.positions.append({"code": "AU.VAS", "position_market": "AU", "qty": 10.0, "currency": "AUD"})
    broker.deals.append({"deal_id": "D4", "order_id": "O3", "code": "AU.VAS", "deal_market": "AU",
                         "trd_side": "BUY", "qty": 10.0, "price": 100.0,
                         "create_time": "2026-11-06 11:00:00", "status": "OK"})
    report = run_sync(seeded, broker)
    statuses = {s.sync_type: s for s in report.steps}
    assert statuses[SyncType.POSITIONS].status is RunStatus.PARTIAL
    assert statuses[SyncType.TRANSACTIONS].status is RunStatus.PARTIAL
    assert "AU.VAS" in statuses[SyncType.TRANSACTIONS].issues[0]
    with session_scope(seeded) as session:
        assert count(session, Transaction) == 3


def test_fill_cancelled_after_import_is_flagged_not_changed(seeded):
    broker = FakeBroker()
    run_sync(seeded, broker)
    broker.deals[0] = {**broker.deals[0], "status": "CANCELLED"}
    report = run_sync(seeded, broker, NOW + timedelta(hours=1))
    step = next(s for s in report.steps if s.sync_type is SyncType.TRANSACTIONS)
    assert step.status is RunStatus.PARTIAL and "D1" in step.issues[0]
    with session_scope(seeded) as session:
        assert count(session, Transaction) == 3


def test_api_failure_rolls_back_the_step_and_is_recorded(seeded):
    broker = FakeBroker()
    broker.failing = {"deals"}
    report = run_sync(seeded, broker)
    statuses = {s.sync_type: s.status for s in report.steps}
    assert statuses[SyncType.TRANSACTIONS] is RunStatus.FAILED
    assert statuses[SyncType.CASH_MOVEMENTS] is RunStatus.SUCCESS  # other steps still run
    with session_scope(seeded) as session:
        assert count(session, Transaction) == 0
        history = session.scalars(
            select(SyncHistory).where(SyncHistory.sync_type == SyncType.TRANSACTIONS)
        ).one()
        assert history.status is RunStatus.FAILED and "simulated outage" in history.error_message
        assert history.finished_at is not None


def test_accounts_failure_stops_the_sync(seeded):
    broker = FakeBroker()
    broker.failing = {"accounts"}
    report = run_sync(seeded, broker)
    assert [s.sync_type for s in report.steps] == [SyncType.ACCOUNTS]
    assert report.steps[0].status is RunStatus.FAILED


def test_incremental_sync_overlaps_the_previous_run(seeded):
    broker = FakeBroker()
    run_sync(seeded, broker)
    broker.deal_requests.clear()
    broker.cash_flow_days.clear()
    later = datetime(2026, 12, 20, 10, 0, tzinfo=timezone.utc)
    MoomooSyncService(seeded, broker, now=lambda: later).sync_all()
    assert broker.deal_requests == [("2026-11-08 00:00:00", "2026-12-20 23:59:59")]  # last run - 7 days
    assert broker.cash_flow_days[0] == "2026-11-12"  # last run - 3 days


def test_validation_compares_positions_with_fills(seeded):
    broker = FakeBroker()
    broker.positions[0]["qty"] = 200.0  # e.g. shares bought before the history start
    run_sync(seeded, broker)
    report = validate_broker_holdings(seeded)[0]
    checks = {c.symbol: c for c in report.checks}
    assert checks["AAPL"].matches
    assert checks["IOZ.AX"].difference == Decimal("50.0")
    assert not report.matches
