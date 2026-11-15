"""SQLAlchemy models for the portfolio database.

Conventions:
- Monetary amounts and quantities are ``Decimal`` (stored as exact TEXT).
- Timestamps are timezone-aware UTC; convert to Australia/Melbourne for display.
- Reporting values (snapshots, forecasts) are in AUD. Transactions keep their
  original currency.
- The schema is created and changed only through Alembic migrations.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.database.types import Money, UTCDateTime, utc_now

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with named constraints and exact decimal/UTC types."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {Decimal: Money, datetime: UTCDateTime}


# --- Enumerations -----------------------------------------------------------


class AssetClass(str, enum.Enum):
    SHARE = "share"
    PROPERTY = "property"
    CRYPTO = "crypto"
    CASH = "cash"
    OTHER = "other"


class TransactionSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class RecordSource(str, enum.Enum):
    BROKER = "broker"
    PROVIDER = "provider"
    MANUAL = "manual"


class DataStatus(str, enum.Enum):
    VALID = "valid"
    STALE = "stale"
    INVALID = "invalid"


class CashMovementType(str, enum.Enum):
    DEPOSIT = "deposit"  # into a broker (Moomoo) account
    WITHDRAWAL = "withdrawal"
    DIVIDEND = "dividend"
    WITHHOLDING_TAX = "withholding_tax"
    FEE = "fee"
    INTEREST = "interest"
    OTHER = "other"


class DividendStatus(str, enum.Enum):
    ANNOUNCED = "announced"
    EXPECTED = "expected"
    CONFIRMED = "confirmed"


class SnapshotType(str, enum.Enum):
    DAILY = "daily"
    MONTHLY = "monthly"


class ForecastScenario(str, enum.Enum):
    CONSERVATIVE = "conservative"
    EXPECTED = "expected"
    OPTIMISTIC = "optimistic"


class SyncType(str, enum.Enum):
    ACCOUNTS = "accounts"
    POSITIONS = "positions"
    TRANSACTIONS = "transactions"
    CASH_MOVEMENTS = "cash_movements"
    BALANCES = "balances"
    DIVIDENDS = "dividends"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class CollectionType(str, enum.Enum):
    PRICE = "price"
    EXCHANGE_RATE = "exchange_rate"
    DIVIDEND = "dividend"


def _enum(enum_class: type[enum.Enum]) -> Enum:
    """Portable string enum with a CHECK constraint (no native enum type in SQLite)."""
    return Enum(
        enum_class,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda members: [member.value for member in members],
        name=enum_class.__name__.lower(),
    )


# --- Reference data ---------------------------------------------------------


class Region(Base):
    """A geographic region an asset gives exposure to (drives geographic allocation).

    This is the exposure of the investment, not the listing market: NDQ.AX trades
    on the ASX but is exposed to the USA.
    """

    __tablename__ = "regions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)


class Asset(Base):
    """An asset definition. New assets are added as rows, not code."""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    asset_class: Mapped[AssetClass] = mapped_column(_enum(AssetClass))
    market: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3))
    region_id: Mapped[int | None] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"), index=True
    )
    provider_symbol: Mapped[str | None] = mapped_column(String(64))
    # Broker security code (Moomoo "MARKET.CODE", e.g. US.AAPL) used to match synced data.
    broker_symbol: Mapped[str | None] = mapped_column(String(32), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    # Manually managed assets are held outside the broker and never overwritten by syncs.
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)

    region: Mapped[Region | None] = relationship()


class BrokerAccount(Base):
    """A brokerage account. Credentials are never stored here."""

    __tablename__ = "broker_accounts"
    __table_args__ = (UniqueConstraint("broker", "external_account_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(32))
    external_account_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(128))
    account_type: Mapped[str | None] = mapped_column(String(32))
    market: Mapped[str | None] = mapped_column(String(32))
    base_currency: Mapped[str | None] = mapped_column(String(3))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


# --- Transactional data -----------------------------------------------------


class Transaction(Base):
    """An executed fill or manual trade, in its original currency."""

    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("broker_account_id", "broker_transaction_id"),
        CheckConstraint("CAST(quantity AS REAL) > 0", name="quantity_positive"),
        CheckConstraint("CAST(price AS REAL) >= 0", name="price_non_negative"),
        CheckConstraint("CAST(fees AS REAL) >= 0", name="fees_non_negative"),
        CheckConstraint(
            "source != 'broker' OR "
            "(broker_account_id IS NOT NULL AND broker_transaction_id IS NOT NULL)",
            name="broker_source_has_identifier",
        ),
        Index("ix_transactions_asset_id_executed_at", "asset_id", "executed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    broker_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="RESTRICT")
    )
    broker_transaction_id: Mapped[str | None] = mapped_column(String(64))
    broker_order_id: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[TransactionSide] = mapped_column(_enum(TransactionSide))
    quantity: Mapped[Decimal]
    price: Mapped[Decimal]
    fees: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3))
    fx_rate_to_aud: Mapped[Decimal | None]
    executed_at: Mapped[datetime]
    source: Mapped[RecordSource] = mapped_column(_enum(RecordSource))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)

    asset: Mapped[Asset] = relationship()


class Position(Base):
    """Latest synchronised broker holding (replaced on each sync)."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("broker_account_id", "asset_id"),
        CheckConstraint("CAST(quantity AS REAL) >= 0", name="quantity_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_account_id: Mapped[int] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="CASCADE")
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    quantity: Mapped[Decimal]
    average_cost: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(String(3))
    synced_at: Mapped[datetime]
    sync_history_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_history.id", ondelete="SET NULL")
    )


class BrokerCashBalance(Base):
    """Cash held in a broker account in one currency, as reported by the broker.

    Append-only: every balance sync adds rows, so the history is preserved.
    """

    __tablename__ = "broker_cash_balances"
    __table_args__ = (
        UniqueConstraint("broker_account_id", "currency", "synced_at"),
        Index("ix_broker_cash_balances_broker_account_id_synced_at", "broker_account_id", "synced_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_account_id: Mapped[int] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3))
    cash_balance: Mapped[Decimal]
    synced_at: Mapped[datetime]
    sync_history_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_history.id", ondelete="SET NULL")
    )


class MarketPrice(Base):
    """A retrieved price. Market time and retrieval time are kept separately."""

    __tablename__ = "market_prices"
    __table_args__ = (
        UniqueConstraint("asset_id", "provider", "retrieved_at"),
        CheckConstraint("CAST(price AS REAL) >= 0", name="price_non_negative"),
        Index("ix_market_prices_asset_id_retrieved_at", "asset_id", "retrieved_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    price: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3))
    provider: Mapped[str] = mapped_column(String(32))
    market_timestamp: Mapped[datetime | None]
    retrieved_at: Mapped[datetime]
    status: Mapped[DataStatus] = mapped_column(_enum(DataStatus), default=DataStatus.VALID)


class ExchangeRate(Base):
    """Units of ``quote_currency`` per one unit of ``base_currency`` (USD/AUD: base USD)."""

    __tablename__ = "exchange_rates"
    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency", "provider", "retrieved_at"),
        CheckConstraint("CAST(rate AS REAL) > 0", name="rate_positive"),
        Index(
            "ix_exchange_rates_base_currency_quote_currency_retrieved_at",
            "base_currency",
            "quote_currency",
            "retrieved_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3))
    quote_currency: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal]
    provider: Mapped[str] = mapped_column(String(32))
    market_timestamp: Mapped[datetime | None]
    retrieved_at: Mapped[datetime]
    status: Mapped[DataStatus] = mapped_column(_enum(DataStatus), default=DataStatus.VALID)


class CashMovement(Base):
    """Signed cash movement (credits positive, debits negative)."""

    __tablename__ = "cash_movements"
    __table_args__ = (
        UniqueConstraint("broker_account_id", "broker_movement_id"),
        CheckConstraint(
            "source != 'broker' OR "
            "(broker_account_id IS NOT NULL AND broker_movement_id IS NOT NULL)",
            name="broker_source_has_identifier",
        ),
        Index("ix_cash_movements_occurred_at", "occurred_at"),
        Index("ix_cash_movements_movement_type_occurred_at", "movement_type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="RESTRICT")
    )
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    broker_movement_id: Mapped[str | None] = mapped_column(String(64))
    # Raw broker cash flow type, kept so movements can be re-classified later.
    broker_type: Mapped[str | None] = mapped_column(String(64))
    movement_type: Mapped[CashMovementType] = mapped_column(_enum(CashMovementType))
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(3))
    fx_rate_to_aud: Mapped[Decimal | None]
    occurred_at: Mapped[datetime]
    source: Mapped[RecordSource] = mapped_column(_enum(RecordSource))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class Dividend(Base):
    """Announced, expected or confirmed dividend.

    Only ``confirmed`` rows, which must link to the cash credit, count as income.
    """

    __tablename__ = "dividends"
    __table_args__ = (
        CheckConstraint(
            "status != 'confirmed' OR cash_movement_id IS NOT NULL",
            name="confirmed_requires_cash_movement",
        ),
        UniqueConstraint("cash_movement_id"),
        Index("ix_dividends_asset_id_pay_date", "asset_id", "pay_date"),
        Index("ix_dividends_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"))
    status: Mapped[DividendStatus] = mapped_column(_enum(DividendStatus))
    ex_date: Mapped[date | None] = mapped_column(Date)
    pay_date: Mapped[date | None] = mapped_column(Date)
    amount_per_share: Mapped[Decimal | None]
    quantity: Mapped[Decimal | None]
    gross_amount: Mapped[Decimal | None]
    withholding_tax: Mapped[Decimal | None]
    net_amount: Mapped[Decimal | None]
    currency: Mapped[str] = mapped_column(String(3))
    cash_movement_id: Mapped[int | None] = mapped_column(
        ForeignKey("cash_movements.id", ondelete="RESTRICT")
    )
    needs_reconciliation: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    source: Mapped[RecordSource] = mapped_column(_enum(RecordSource))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


# --- Snapshots and forecasting ---------------------------------------------


class PortfolioSnapshot(Base):
    """Historical valuation in AUD. Immutable (enforced by database triggers).

    ``snapshot_date`` is the Australia/Melbourne calendar date. Changes are only
    possible through a ``SnapshotCorrection`` audit record.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_type", "snapshot_date"),
        CheckConstraint(
            "snapshot_type != 'monthly' OR strftime('%d', snapshot_date, '+1 day') = '01'",
            name="monthly_is_month_end",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_type: Mapped[SnapshotType] = mapped_column(_enum(SnapshotType))
    snapshot_date: Mapped[date] = mapped_column(Date)
    total_value: Mapped[Decimal]
    shares_value: Mapped[Decimal]
    bitcoin_value: Mapped[Decimal]
    cash_value: Mapped[Decimal]
    investment_cost: Mapped[Decimal]
    unrealised_profit_loss: Mapped[Decimal]
    realised_profit_loss: Mapped[Decimal]
    dividend_income: Mapped[Decimal]
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class SnapshotCorrection(Base):
    """Audit record authorising and documenting one correction of a snapshot.

    Insert a row (``applied_at`` NULL) to permit a single UPDATE of the snapshot.
    Database triggers then record ``old_values`` and ``applied_at``. Rows cannot
    be deleted.
    """

    __tablename__ = "snapshot_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="RESTRICT"), index=True
    )
    reason: Mapped[str] = mapped_column(Text)
    corrected_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    applied_at: Mapped[datetime | None]
    old_values: Mapped[str | None] = mapped_column(Text)


class ForecastPlan(Base):
    """Yearly investment plan, fixed at the start of the year.

    Mirrors the spreadsheet's "investment calc rules". Rates are fractions
    (0.07 = 7%). Monthly targets are generated into ``PortfolioForecast``:

        monthly_growth = (1 + annual_growth_rate + annual_dividend_yield) ** (1/12) - 1
        target[m] = target[m-1] * (1 + monthly_growth + annual_dividend_yield / 12)
                    + monthly_investment_target

    starting from ``initial_value`` on ``start_date``. Monthly growth is derived,
    so it is not stored.
    """

    __tablename__ = "forecast_plans"
    __table_args__ = (
        UniqueConstraint("year", "scenario"),
        CheckConstraint(
            "CAST(strftime('%Y', start_date) AS INTEGER) = year", name="start_date_in_year"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    scenario: Mapped[ForecastScenario] = mapped_column(
        _enum(ForecastScenario), default=ForecastScenario.EXPECTED
    )
    start_date: Mapped[date] = mapped_column(Date)
    initial_value: Mapped[Decimal]
    monthly_investment_target: Mapped[Decimal]
    annual_growth_rate: Mapped[Decimal]
    annual_dividend_yield: Mapped[Decimal]
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class PortfolioForecast(Base):
    """Target portfolio value at the end of one month of a plan."""

    __tablename__ = "portfolio_forecasts"
    __table_args__ = (
        UniqueConstraint("plan_id", "month_end"),
        CheckConstraint(
            "strftime('%d', month_end, '+1 day') = '01'", name="month_end_is_last_day"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("forecast_plans.id", ondelete="RESTRICT"))
    month_end: Mapped[date] = mapped_column(Date)
    target_value: Mapped[Decimal]
    generated_at: Mapped[datetime] = mapped_column(default=utc_now)


class SavingsEntry(Base):
    """Manually entered month-end record of the savings account.

    ``balance`` is the account balance at month end and feeds the monthly
    snapshot's ``cash_value``; ``deposit`` feeds ``MonthlyPerformance.cash_deposit``;
    ``interest`` is the interest paid that month. The balance is entered, not
    derived, so withdrawals and fees need no separate handling.
    """

    __tablename__ = "savings_entries"
    __table_args__ = (
        CheckConstraint(
            "strftime('%d', month_end, '+1 day') = '01'", name="month_end_is_last_day"
        ),
        CheckConstraint("CAST(deposit AS REAL) >= 0", name="deposit_not_negative"),
        CheckConstraint("CAST(interest AS REAL) >= 0", name="interest_not_negative"),
        CheckConstraint("CAST(balance AS REAL) >= 0", name="balance_not_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    month_end: Mapped[date] = mapped_column(Date, unique=True)
    deposit: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    interest: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    balance: Mapped[Decimal]
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class MonthlyPerformance(Base):
    """Month-end result of a plan: actual vs target, as in the spreadsheet.

    Closed once per month from the monthly snapshot and the month's deposits.
    All amounts are AUD; ratios are fractions.

    ``cash_deposit``: money added to the savings account (``SavingsEntry.deposit``).
    ``investment_deposit``: money deposited to the Moomoo account
    (``DEPOSIT`` movements) plus money spent buying Bitcoin.
    ``market_growth_amount`` = change in value - cash deposit - investment deposit.
    """

    __tablename__ = "monthly_performance"
    __table_args__ = (
        UniqueConstraint("plan_id", "month_end"),
        UniqueConstraint("snapshot_id"),
        CheckConstraint(
            "strftime('%d', month_end, '+1 day') = '01'", name="month_end_is_last_day"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("forecast_plans.id", ondelete="RESTRICT"))
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="RESTRICT")
    )
    month_end: Mapped[date] = mapped_column(Date)
    current_portfolio_value: Mapped[Decimal]
    target_portfolio_value: Mapped[Decimal]
    difference_amount: Mapped[Decimal]
    difference_pct: Mapped[Decimal]
    cash_deposit: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    investment_deposit: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    market_growth_amount: Mapped[Decimal]
    market_growth_pct: Mapped[Decimal]
    dividend_amount: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    dividend_yield: Mapped[Decimal]
    created_at: Mapped[datetime] = mapped_column(default=utc_now)


class MonthlyDividend(Base):
    """Dividend income received in one month (the retirement overview's month x year grid).

    Kept separately from ``MonthlyPerformance`` so years before the app existed can
    be backfilled by hand (``source`` = manual).
    """

    __tablename__ = "monthly_dividends"
    __table_args__ = (
        CheckConstraint(
            "strftime('%d', month_end, '+1 day') = '01'", name="month_end_is_last_day"
        ),
        CheckConstraint("CAST(amount AS REAL) >= 0", name="amount_not_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    month_end: Mapped[date] = mapped_column(Date, unique=True)
    amount: Mapped[Decimal]
    source: Mapped[RecordSource] = mapped_column(_enum(RecordSource), default=RecordSource.MANUAL)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class RetirementPlan(Base):
    """Long-range projection (the "Retirement Overview"), one value per year.

    The assumptions are kept here and the projected values are stored per year in
    ``RetirementForecast``. Exactly one plan is the *baseline*: the fixed "truth"
    every year is measured against. Triggers (see the migration) stop the baseline
    and its forecasts from being changed or deleted. Other plans are what-if
    scenarios and can be freely created and removed.

    ``opening_value`` is the portfolio value at the start of ``start_year``. Rates
    are fractions (0.09 = 9%). ``annual_return_rate`` is the total yearly return
    *including* dividends; ``annual_dividend_yield`` is the part paid out as
    dividends. Compounding is monthly at ``(1 + annual_return_rate) ** (1/12) - 1``,
    with the monthly investment added at the end of each month:

        value[m] = value[m-1] * (1 + monthly_rate) + monthly_investment
        forecast_dividend[y] = value at 31 Dec of y * annual_dividend_yield
    """

    __tablename__ = "retirement_plans"
    __table_args__ = (
        CheckConstraint("end_year >= start_year", name="end_year_after_start"),
        CheckConstraint(
            "CAST(annual_dividend_yield AS REAL) <= CAST(annual_return_rate AS REAL)",
            name="yield_within_return",
        ),
        Index(
            "uq_retirement_plans_single_baseline", "is_baseline", unique=True,
            sqlite_where=text("is_baseline = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    start_year: Mapped[int] = mapped_column(Integer)
    end_year: Mapped[int] = mapped_column(Integer)
    opening_value: Mapped[Decimal]
    monthly_investment: Mapped[Decimal]
    annual_return_rate: Mapped[Decimal]
    annual_dividend_yield: Mapped[Decimal]
    created_at: Mapped[datetime] = mapped_column(default=utc_now)

    forecasts: Mapped[list[RetirementForecast]] = relationship(
        back_populates="plan", order_by="RetirementForecast.year",
        cascade="all, delete-orphan",
    )


class RetirementForecast(Base):
    """Projected portfolio value and dividend at 31 Dec of one year of a retirement plan."""

    __tablename__ = "retirement_forecasts"
    __table_args__ = (UniqueConstraint("plan_id", "year"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("retirement_plans.id", ondelete="RESTRICT")
    )
    year: Mapped[int] = mapped_column(Integer)
    predicted_value: Mapped[Decimal]
    forecast_dividend: Mapped[Decimal]
    generated_at: Mapped[datetime] = mapped_column(default=utc_now)

    plan: Mapped[RetirementPlan] = relationship(back_populates="forecasts")


class YearlyPerformance(Base):
    """Year-end result against the retirement plan; the yearly counterpart of ``MonthlyPerformance``.

    ``as_of_date`` is 31 Dec for a finished year, or the latest date for the current
    year. Only the overview's high-level figures are required: current and predicted
    value, difference % and market growth %. Past years are entered by hand with the
    detail left empty (NULL), i.e. snapshot, opening value, difference amount,
    deposits, market growth amount and dividend total. For years tracked by the app
    these are filled in. Market growth is the year-end value minus the opening
    value and all deposits; the percentage is that amount divided by
    ``opening_value``.
    """

    __tablename__ = "yearly_performance"
    __table_args__ = (
        UniqueConstraint("plan_id", "year"),
        CheckConstraint("CAST(strftime('%Y', as_of_date) AS INTEGER) = year", name="as_of_in_year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("retirement_plans.id", ondelete="RESTRICT")
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolio_snapshots.id", ondelete="RESTRICT")
    )
    year: Mapped[int] = mapped_column(Integer)
    as_of_date: Mapped[date] = mapped_column(Date)
    opening_value: Mapped[Decimal | None]
    current_portfolio_value: Mapped[Decimal]
    predicted_portfolio_value: Mapped[Decimal]
    difference_amount: Mapped[Decimal | None]
    difference_pct: Mapped[Decimal]
    cash_deposit: Mapped[Decimal | None]
    investment_deposit: Mapped[Decimal | None]
    market_growth_amount: Mapped[Decimal | None]
    market_growth_pct: Mapped[Decimal]
    dividend_amount: Mapped[Decimal | None]
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


# --- Operations -------------------------------------------------------------


class AppSetting(Base):
    """Key/value application configuration editable from the dashboard."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)


class SyncHistory(Base):
    """One broker synchronisation run."""

    __tablename__ = "sync_history"
    __table_args__ = (Index("ix_sync_history_sync_type_started_at", "sync_type", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    broker_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("broker_accounts.id", ondelete="SET NULL")
    )
    sync_type: Mapped[SyncType] = mapped_column(_enum(SyncType))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus))
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    records_fetched: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    records_inserted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    records_skipped: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text)


class CollectionLog(Base):
    """One market data collection attempt."""

    __tablename__ = "collection_logs"
    __table_args__ = (Index("ix_collection_logs_provider_started_at", "provider", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    data_type: Mapped[CollectionType] = mapped_column(_enum(CollectionType))
    asset_id: Mapped[int | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus))
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
