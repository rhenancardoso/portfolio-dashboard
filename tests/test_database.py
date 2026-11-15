"""Integration tests for the SQLite database layer."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from app.database.db import backup_database, restore_database, session_scope
from app.database.models import (
    Asset,
    AssetClass,
    Base,
    BrokerAccount,
    CashMovement,
    CashMovementType,
    Dividend,
    DividendStatus,
    ForecastPlan,
    MonthlyDividend,
    MonthlyPerformance,
    PortfolioForecast,
    PortfolioSnapshot,
    RecordSource,
    Region,
    RetirementForecast,
    SavingsEntry,
    SnapshotCorrection,
    SnapshotType,
    Transaction,
    TransactionSide,
    YearlyPerformance,
)
from app.database.seed import INITIAL_ASSETS, seed_reference_data
from app.services.forecast_service import ensure_baseline_retirement_plan

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_asset(symbol: str = "AAPL") -> Asset:
    return Asset(symbol=symbol, name=symbol, asset_class=AssetClass.SHARE, market="US", currency="USD")


def make_transaction(asset: Asset, account: BrokerAccount | None, broker_id: str | None, **kw):
    values = dict(
        asset=asset,
        broker_account_id=account.id if account else None,
        broker_transaction_id=broker_id,
        side=TransactionSide.BUY,
        quantity=Decimal("1.5"),
        price=Decimal("123.456789"),
        currency="USD",
        executed_at=NOW,
        source=RecordSource.BROKER if broker_id else RecordSource.MANUAL,
    )
    values.update(kw)
    return Transaction(**values)


def make_snapshot(**kw) -> PortfolioSnapshot:
    values = dict(
        snapshot_type=SnapshotType.MONTHLY,
        snapshot_date=date(2026, 1, 31),
        total_value=Decimal("1000.10"),
        shares_value=Decimal("600"),
        bitcoin_value=Decimal("200"),
        cash_value=Decimal("200.10"),
        investment_cost=Decimal("900"),
        unrealised_profit_loss=Decimal("100"),
        realised_profit_loss=Decimal("0"),
        dividend_income=Decimal("0"),
    )
    values.update(kw)
    return PortfolioSnapshot(**values)


def test_migrations_match_models(engine):
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)


def test_pragmas_applied(engine):
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"


def test_decimals_round_trip_exactly(session_factory):
    with session_scope(session_factory) as session:
        asset = make_asset()
        session.add(make_transaction(asset, None, None, quantity=Decimal("0.1"), price=Decimal("0.2")))
    with session_scope(session_factory) as session:
        transaction = session.scalars(select(Transaction)).one()
        assert transaction.quantity + transaction.price == Decimal("0.3")
        assert transaction.executed_at == NOW


def test_float_amounts_rejected(session_factory):
    with pytest.raises(StatementError, match="not float"):
        with session_scope(session_factory) as session:
            session.add(make_transaction(make_asset(), None, None, price=1.1))


def test_naive_timestamps_rejected(session_factory):
    with pytest.raises(StatementError, match="timezone-aware"):
        with session_scope(session_factory) as session:
            session.add(make_transaction(make_asset(), None, None, executed_at=datetime(2026, 1, 1)))


def test_duplicate_broker_transaction_rejected(session_factory):
    with session_scope(session_factory) as session:
        account = BrokerAccount(broker="moomoo", external_account_id="123")
        asset = make_asset()
        session.add_all([account, asset])
        session.flush()
        session.add(make_transaction(asset, account, "fill-1"))

    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            account = session.scalars(select(BrokerAccount)).one()
            asset = session.scalars(select(Asset)).one()
            session.add(make_transaction(asset, account, "fill-1"))


def test_broker_transaction_requires_identifier(session_factory):
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            asset = make_asset()
            session.add(make_transaction(asset, None, None, source=RecordSource.BROKER))


def test_failed_transaction_rolls_back(session_factory):
    with pytest.raises(RuntimeError):
        with session_scope(session_factory) as session:
            session.add(make_asset("MSFT"))
            session.flush()
            raise RuntimeError("boom")
    with session_scope(session_factory) as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == 0


def test_asset_in_use_cannot_be_deleted(session_factory):
    with session_scope(session_factory) as session:
        session.add(make_transaction(make_asset(), None, None))
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.delete(session.scalars(select(Asset)).one())


def test_confirmed_dividend_requires_cash_movement(session_factory):
    with session_scope(session_factory) as session:
        asset = make_asset()
        session.add(asset)
        session.flush()
        movement = CashMovement(
            movement_type=CashMovementType.DIVIDEND,
            amount=Decimal("10"),
            currency="USD",
            occurred_at=NOW,
            source=RecordSource.MANUAL,
        )
        session.add(movement)
        session.flush()
        session.add(Dividend(
            asset_id=asset.id, status=DividendStatus.CONFIRMED, currency="USD",
            source=RecordSource.MANUAL, cash_movement_id=movement.id,
        ))
        session.add(Dividend(
            asset_id=asset.id, status=DividendStatus.EXPECTED, currency="USD",
            source=RecordSource.MANUAL,
        ))

    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            asset = session.scalars(select(Asset)).one()
            session.add(Dividend(
                asset_id=asset.id, status=DividendStatus.CONFIRMED, currency="USD",
                source=RecordSource.MANUAL,
            ))


def test_snapshot_is_immutable(session_factory):
    with session_scope(session_factory) as session:
        session.add(make_snapshot())

    with pytest.raises(Exception, match="immutable"):
        with session_scope(session_factory) as session:
            session.scalars(select(PortfolioSnapshot)).one().total_value = Decimal("1")
    with pytest.raises(Exception, match="cannot be deleted"):
        with session_scope(session_factory) as session:
            session.delete(session.scalars(select(PortfolioSnapshot)).one())


def test_snapshot_correction_is_audited_and_single_use(session_factory):
    with session_scope(session_factory) as session:
        snapshot = make_snapshot()
        session.add(snapshot)
        session.flush()
        session.add(SnapshotCorrection(snapshot_id=snapshot.id, reason="Wrong price", corrected_by="admin"))

    with session_scope(session_factory) as session:
        session.scalars(select(PortfolioSnapshot)).one().total_value = Decimal("999.99")

    with session_scope(session_factory) as session:
        correction = session.scalars(select(SnapshotCorrection)).one()
        assert correction.applied_at is not None
        assert '"total_value":"1000.10"' in correction.old_values
        assert session.scalars(select(PortfolioSnapshot)).one().total_value == Decimal("999.99")

    with pytest.raises(Exception, match="immutable"):
        with session_scope(session_factory) as session:
            session.scalars(select(PortfolioSnapshot)).one().total_value = Decimal("1")
    with pytest.raises(Exception, match="audit trail"):
        with session_scope(session_factory) as session:
            session.delete(session.scalars(select(SnapshotCorrection)).one())


def test_seed_is_idempotent_and_preserves_edits(session_factory):
    with session_scope(session_factory) as session:
        seed_reference_data(session)
    with session_scope(session_factory) as session:
        session.scalars(select(Asset).where(Asset.symbol == "AAPL")).one().is_active = False
        seed_reference_data(session)
    with session_scope(session_factory) as session:
        assert session.scalar(select(func.count()).select_from(Asset)) == len(INITIAL_ASSETS)
        assert session.scalars(select(Asset).where(Asset.symbol == "AAPL")).one().is_active is False


def make_plan(**kw) -> ForecastPlan:
    values = dict(
        year=2026,
        start_date=date(2026, 1, 1),
        initial_value=Decimal("143828"),
        monthly_investment_target=Decimal("1500"),
        annual_growth_rate=Decimal("0.07"),
        annual_dividend_yield=Decimal("0.03"),
    )
    values.update(kw)
    return ForecastPlan(**values)


def test_yearly_plan_targets_and_monthly_tracking(session_factory):
    with session_scope(session_factory) as session:
        plan = make_plan()
        snapshot = make_snapshot(snapshot_date=date(2026, 1, 31), total_value=Decimal("144598"))
        session.add_all([plan, snapshot])
        session.flush()
        session.add(PortfolioForecast(
            plan_id=plan.id, month_end=date(2026, 1, 31), target_value=Decimal("146835")
        ))
        session.add(MonthlyPerformance(
            plan_id=plan.id, snapshot_id=snapshot.id, month_end=date(2026, 1, 31),
            current_portfolio_value=Decimal("144598"), target_portfolio_value=Decimal("146835"),
            difference_amount=Decimal("-2237"), difference_pct=Decimal("-0.0152"),
            cash_deposit=Decimal("1500"), investment_deposit=Decimal("2800"),
            market_growth_amount=Decimal("-3530"), market_growth_pct=Decimal("-0.0245"),
            dividend_amount=Decimal("254"), dividend_yield=Decimal("0.0018"),
        ))

    with session_scope(session_factory) as session:
        row = session.scalars(select(MonthlyPerformance)).one()
        assert row.market_growth_amount == Decimal("-3530")
        assert row.difference_pct == Decimal("-0.0152")


def test_plan_is_unique_per_year_and_scenario(session_factory):
    with session_scope(session_factory) as session:
        session.add(make_plan())
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.add(make_plan())
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.add(make_plan(year=2027))  # start_date outside the plan year


def test_month_end_dates_enforced(session_factory):
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.add(make_snapshot(snapshot_date=date(2026, 1, 30)))
    with session_scope(session_factory) as session:
        session.add(make_snapshot(snapshot_type=SnapshotType.DAILY, snapshot_date=date(2026, 1, 30)))
        plan = make_plan()
        session.add(plan)
        session.flush()
        session.add(PortfolioForecast(
            plan_id=plan.id, month_end=date(2026, 2, 28), target_value=Decimal("1")
        ))
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            plan = session.scalars(select(ForecastPlan)).one()
            session.add(PortfolioForecast(
                plan_id=plan.id, month_end=date(2026, 3, 30), target_value=Decimal("1")
            ))


def test_savings_entry_is_monthly_and_validated(session_factory):
    with session_scope(session_factory) as session:
        session.add(SavingsEntry(
            month_end=date(2026, 1, 31), deposit=Decimal("1500"),
            interest=Decimal("12.34"), balance=Decimal("21512.34"),
        ))
    with session_scope(session_factory) as session:
        entry = session.scalars(select(SavingsEntry)).one()
        assert entry.interest == Decimal("12.34")
    for bad in (
        dict(month_end=date(2026, 1, 31), balance=Decimal("1")),  # duplicate month
        dict(month_end=date(2026, 2, 27), balance=Decimal("1")),  # not month end
        dict(month_end=date(2026, 2, 28), balance=Decimal("-1")),
        dict(month_end=date(2026, 2, 28), interest=Decimal("-1"), balance=Decimal("1")),
    ):
        with pytest.raises(IntegrityError):
            with session_scope(session_factory) as session:
                session.add(SavingsEntry(**bad))


def test_retirement_overview_tracking(session_factory):
    with session_scope(session_factory) as session:
        plan = ensure_baseline_retirement_plan(session)
        # A past year: only the high-level figures, the detail is left empty.
        session.add(YearlyPerformance(
            plan_id=plan.id, year=2023, as_of_date=date(2023, 12, 31),
            current_portfolio_value=Decimal("84645"), predicted_portfolio_value=Decimal("73980"),
            difference_pct=Decimal("0.144"), market_growth_pct=Decimal("0.231"),
        ))
        session.add(MonthlyDividend(month_end=date(2023, 1, 31), amount=Decimal("28.29")))

    with session_scope(session_factory) as session:
        row = session.scalars(select(YearlyPerformance)).one()
        assert row.snapshot_id is None and row.opening_value is None
        assert row.cash_deposit is None and row.dividend_amount is None
        assert row.difference_pct == Decimal("0.144")

    for bad in (
        YearlyPerformance(  # as_of_date outside the year
            plan_id=1, year=2024, as_of_date=date(2023, 12, 31),
            current_portfolio_value=Decimal("1"), predicted_portfolio_value=Decimal("1"),
            difference_pct=Decimal("0"), market_growth_pct=Decimal("0"),
        ),
        RetirementForecast(plan_id=1, year=2023, predicted_value=Decimal("1"),
                           forecast_dividend=Decimal("0")),  # duplicate year
        MonthlyDividend(month_end=date(2023, 1, 31), amount=Decimal("1")),  # duplicate month
        MonthlyDividend(month_end=date(2023, 2, 27), amount=Decimal("1")),  # not month end
    ):
        with pytest.raises(IntegrityError):
            with session_scope(session_factory) as session:
                session.add(bad)


def test_seed_assigns_exposure_regions(session_factory):
    with session_scope(session_factory) as session:
        seed_reference_data(session)
    with session_scope(session_factory) as session:
        regions = {a.symbol: a.region.name if a.region else None for a in session.scalars(select(Asset))}
    assert regions == {
        "BTC-AUD": None,
        "AAPL": "USA",
        "NDQ.AX": "USA",
        "VEQ.AX": "Europe",
        "IOZ.AX": "Australia",
        "SHL.AX": "Australia",
        "VAP.AX": "Australia",
    }


def test_property_asset_class_and_region_constraints(session_factory):
    with session_scope(session_factory) as session:
        region = Region(code="AU", name="Australia")
        session.add(Asset(
            symbol="VAP.AX", name="Property", asset_class=AssetClass.PROPERTY,
            market="ASX", currency="AUD", region=region,
        ))
    with session_scope(session_factory) as session:
        assert session.scalars(select(Asset)).one().asset_class is AssetClass.PROPERTY

    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.add(Region(code="AU", name="Other"))
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.delete(session.scalars(select(Region)).one())


def test_backup_and_restore(settings, engine, session_factory):
    with session_scope(session_factory) as session:
        session.add(make_asset("AAPL"))
    backup = backup_database(settings)

    with session_scope(session_factory) as session:
        session.add(make_asset("MSFT"))
    engine.dispose()

    safety_copy = restore_database(backup, settings)
    assert safety_copy.exists()
    with session_scope(session_factory) as session:
        assert [a.symbol for a in session.scalars(select(Asset))] == ["AAPL"]


def test_restore_rejects_corrupt_backup(settings, engine, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(Exception):
        restore_database(bad, settings)
