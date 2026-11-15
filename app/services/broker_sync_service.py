"""Synchronise Moomoo accounts, positions, balances, fills and cash flows into the database.

Every step runs in its own database transaction and is recorded in ``sync_history``.
Imports are idempotent: fills and cash flows are matched on the broker's unique
identifiers, so re-running a sync never creates duplicates. Existing financial
records are never modified; anomalies (e.g. a fill cancelled after import) are
reported for manual review instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.db import session_scope
from app.database.models import (
    AppSetting,
    Asset,
    BrokerAccount,
    BrokerCashBalance,
    CashMovement,
    Position,
    RecordSource,
    RunStatus,
    SyncHistory,
    SyncType,
    Transaction,
    TransactionSide,
)
from app.database.types import utc_now
from app.integrations.moomoo.accounts import BrokerAccountRecord, parse_accounts
from app.integrations.moomoo.cash_logs import classify_cash_flow, parse_cash_flows
from app.integrations.moomoo.client import Row
from app.integrations.moomoo.positions import parse_cash_balances, parse_positions
from app.integrations.moomoo.transactions import (
    BrokerDealRecord,
    deal_windows,
    parse_deals,
    parse_order_fees,
)

logger = logging.getLogger(__name__)

BROKER = "moomoo"
HISTORY_START_SETTING = "moomoo.history_start"
DEFAULT_HISTORY_START = date(2025, 1, 1)
DEAL_OVERLAP_DAYS = 7
CASH_FLOW_OVERLAP_DAYS = 3
CASH_FLOW_COMMIT_EVERY_DAYS = 30


class BrokerClient(Protocol):
    """The read-only broker queries the sync needs (implemented by ``MoomooClient``)."""

    trading_environment: str

    def list_accounts(self) -> list[Row]: ...
    def get_account_info(self, account_id: int, currency: str = "AUD") -> list[Row]: ...
    def get_positions(self, account_id: int) -> list[Row]: ...
    def get_history_deals(self, account_id: int, start: str, end: str) -> list[Row]: ...
    def get_order_fees(self, account_id: int, order_ids: list[str]) -> list[Row]: ...
    def get_cash_flows(self, account_id: int, clearing_date: str) -> list[Row]: ...


@dataclass
class StepResult:
    """Outcome of one sync step, mirrored in ``sync_history``."""

    sync_type: SyncType
    account: str | None
    status: RunStatus = RunStatus.SUCCESS
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    issues: list[str] = field(default_factory=list)
    error: str | None = None
    history_id: int | None = None


@dataclass
class SyncReport:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return all(step.status is RunStatus.SUCCESS for step in self.steps)


@dataclass(frozen=True)
class HoldingCheck:
    """Broker position quantity vs the quantity implied by imported fills."""

    symbol: str
    broker_quantity: Decimal
    ledger_quantity: Decimal

    @property
    def difference(self) -> Decimal:
        return self.broker_quantity - self.ledger_quantity

    @property
    def matches(self) -> bool:
        return self.difference == 0


@dataclass
class ValidationReport:
    account: str
    checks: list[HoldingCheck] = field(default_factory=list)

    @property
    def matches(self) -> bool:
        return all(check.matches for check in self.checks)


class MoomooSyncService:
    """Pulls broker data through a read-only client and stores it."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        client: BrokerClient,
        *,
        local_timezone: str = "Australia/Melbourne",
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._zone = ZoneInfo(local_timezone)
        self._now = now

    # --- orchestration ------------------------------------------------------

    def sync_all(self) -> SyncReport:
        """Accounts first, then every step for each active account."""
        report = SyncReport()
        accounts_step = self._run_step(SyncType.ACCOUNTS, None, self._sync_accounts)
        report.steps.append(accounts_step)
        if accounts_step.status is RunStatus.FAILED:
            return report

        with session_scope(self._session_factory) as session:
            accounts = session.execute(
                select(BrokerAccount.id, BrokerAccount.external_account_id).where(
                    BrokerAccount.broker == BROKER, BrokerAccount.is_active.is_(True)
                )
            ).all()
        for account_id, external_id in accounts:
            for sync_type, step in (
                (SyncType.POSITIONS, self._sync_positions),
                (SyncType.BALANCES, self._sync_balances),
                (SyncType.TRANSACTIONS, self._sync_transactions),
                (SyncType.CASH_MOVEMENTS, self._sync_cash_movements),
            ):
                report.steps.append(self._run_step(
                    sync_type, account_id,
                    lambda session, result, step=step: step(session, result, account_id, int(external_id)),
                ))
        return report

    def _run_step(
        self,
        sync_type: SyncType,
        account_id: int | None,
        step: Callable[[Session, StepResult], None],
    ) -> StepResult:
        with session_scope(self._session_factory) as session:
            history = SyncHistory(
                broker_account_id=account_id, sync_type=sync_type,
                status=RunStatus.RUNNING, started_at=self._now(),
            )
            session.add(history)
            session.flush()
            history_id = history.id

        result = StepResult(sync_type=sync_type, account=None, history_id=history_id)
        try:
            with session_scope(self._session_factory) as session:
                if account_id is not None:
                    result.account = session.get(BrokerAccount, account_id).name
                step(session, result)
        except Exception as exc:
            logger.exception("Moomoo %s sync failed", sync_type.value)
            result.status = RunStatus.FAILED
            result.error = f"{type(exc).__name__}: {exc}"
        else:
            result.status = RunStatus.PARTIAL if result.issues else RunStatus.SUCCESS

        with session_scope(self._session_factory) as session:
            history = session.get(SyncHistory, history_id)
            history.status = result.status
            history.finished_at = self._now()
            history.records_fetched = result.fetched
            history.records_inserted = result.inserted
            history.records_skipped = result.skipped
            messages = ([result.error] if result.error else []) + result.issues
            history.error_message = "\n".join(messages) or None
        return result

    # --- helpers --------------------------------------------------------------

    def _today(self) -> date:
        return self._now().astimezone(self._zone).date()

    def _history_start(self, session: Session) -> date:
        value = session.scalar(select(AppSetting.value).where(AppSetting.key == HISTORY_START_SETTING))
        return date.fromisoformat(value) if value else DEFAULT_HISTORY_START

    def _last_completed_sync(self, session: Session, sync_type: SyncType, account_id: int) -> date | None:
        started = session.scalar(
            select(func.max(SyncHistory.started_at)).where(
                SyncHistory.sync_type == sync_type,
                SyncHistory.broker_account_id == account_id,
                SyncHistory.status.in_([RunStatus.SUCCESS, RunStatus.PARTIAL]),
            )
        )
        return None if started is None else started.astimezone(self._zone).date()

    @staticmethod
    def _assets_by_broker_symbol(session: Session) -> dict[str, Asset]:
        return {
            asset.broker_symbol: asset
            for asset in session.scalars(select(Asset).where(Asset.broker_symbol.is_not(None)))
        }

    @staticmethod
    def _unknown_symbol(code: str) -> str:
        return f"Unknown broker symbol {code}: set assets.broker_symbol for this asset and re-sync"

    # --- steps ----------------------------------------------------------------

    def _sync_accounts(self, session: Session, result: StepResult) -> None:
        records = parse_accounts(self._client.list_accounts(), self._client.trading_environment)
        result.fetched = len(records)
        existing = {
            account.external_account_id: account
            for account in session.scalars(select(BrokerAccount).where(BrokerAccount.broker == BROKER))
        }
        for record in records:
            account = existing.get(str(record.account_id))
            if account is None:
                session.add(self._new_account(record))
                result.inserted += 1
            else:
                account.account_type = record.account_type
                account.market = ",".join(record.markets) or None
                account.is_active = True
        if not records:
            result.issues.append(
                f"No active {self._client.trading_environment} accounts returned; check "
                "MOOMOO_SECURITY_FIRM and that OpenD is logged in to the right moomoo ID"
            )

    @staticmethod
    def _new_account(record: BrokerAccountRecord) -> BrokerAccount:
        return BrokerAccount(
            broker=BROKER,
            external_account_id=str(record.account_id),
            name=record.display_name,
            account_type=record.account_type,
            market=",".join(record.markets) or None,
        )

    def _sync_positions(self, session: Session, result: StepResult, account_id: int, external_id: int) -> None:
        records = parse_positions(self._client.get_positions(external_id))
        result.fetched = len(records)
        assets = self._assets_by_broker_symbol(session)
        session.execute(delete(Position).where(Position.broker_account_id == account_id))
        synced_at = self._now()
        for record in records:
            asset = assets.get(record.code)
            if asset is None:
                result.issues.append(self._unknown_symbol(record.code))
                result.skipped += 1
                continue
            session.add(Position(
                broker_account_id=account_id, asset_id=asset.id, quantity=record.quantity,
                average_cost=record.average_cost, currency=record.currency or asset.currency,
                synced_at=synced_at, sync_history_id=result.history_id,
            ))
            result.inserted += 1

    def _sync_balances(self, session: Session, result: StepResult, account_id: int, external_id: int) -> None:
        records = parse_cash_balances(self._client.get_account_info(external_id))
        result.fetched = len(records)
        synced_at = self._now()
        for record in records:
            session.add(BrokerCashBalance(
                broker_account_id=account_id, currency=record.currency,
                cash_balance=record.cash_balance, synced_at=synced_at,
                sync_history_id=result.history_id,
            ))
            result.inserted += 1

    def _sync_transactions(self, session: Session, result: StepResult, account_id: int, external_id: int) -> None:
        last_sync = self._last_completed_sync(session, SyncType.TRANSACTIONS, account_id)
        history_start = self._history_start(session)
        start = max(history_start, last_sync - timedelta(days=DEAL_OVERLAP_DAYS)) if last_sync else history_start

        deals: dict[str, BrokerDealRecord] = {}
        for window_start, window_end in deal_windows(start, self._today()):
            for deal in parse_deals(self._client.get_history_deals(external_id, window_start, window_end)):
                deals[deal.deal_id] = deal
        result.fetched = len(deals)

        imported = {
            transaction.broker_transaction_id: transaction
            for transaction in session.scalars(
                select(Transaction).where(Transaction.broker_account_id == account_id)
            )
        }
        orders_with_imported_fills = {t.broker_order_id for t in imported.values() if t.broker_order_id}
        assets = self._assets_by_broker_symbol(session)

        new_deals: list[tuple[BrokerDealRecord, Asset]] = []
        for deal in sorted(deals.values(), key=lambda d: (d.executed_at, d.deal_id)):
            if deal.deal_id in imported:
                if deal.status != "OK":
                    result.issues.append(
                        f"Fill {deal.deal_id} ({deal.code}) was imported but is now {deal.status}; review manually"
                    )
                result.skipped += 1
                continue
            if deal.status == "CANCELLED":
                result.skipped += 1
                continue
            if deal.status != "OK":
                result.issues.append(f"Fill {deal.deal_id} ({deal.code}) has status {deal.status}; not imported")
                result.skipped += 1
                continue
            asset = assets.get(deal.code)
            if asset is None:
                result.issues.append(self._unknown_symbol(deal.code))
                result.skipped += 1
                continue
            new_deals.append((deal, asset))

        fee_orders = sorted({
            deal.order_id for deal, _ in new_deals
            if deal.order_id and deal.order_id not in orders_with_imported_fills
        })
        fees = parse_order_fees(self._client.get_order_fees(external_id, fee_orders)) if fee_orders else {}

        charged_orders: set[str] = set()
        for deal, asset in new_deals:
            fee = Decimal("0")
            # An order's fee is charged once, on its earliest fill.
            if deal.order_id in fees and deal.order_id not in charged_orders:
                fee = fees[deal.order_id]
                charged_orders.add(deal.order_id)
            session.add(Transaction(
                asset_id=asset.id, broker_account_id=account_id,
                broker_transaction_id=deal.deal_id, broker_order_id=deal.order_id,
                side=TransactionSide(deal.side), quantity=deal.quantity, price=deal.price,
                fees=fee, currency=asset.currency, executed_at=deal.executed_at,
                source=RecordSource.BROKER,
            ))
            result.inserted += 1
        missing_fees = sorted(set(fee_orders) - set(fees))
        if missing_fees:
            result.issues.append(f"No fee returned for orders {', '.join(missing_fees)}; fees recorded as 0")

    def _cash_flow_start(self, session: Session, account_id: int) -> date:
        history_start = self._history_start(session)
        candidates = [history_start]
        last_sync = self._last_completed_sync(session, SyncType.CASH_MOVEMENTS, account_id)
        if last_sync:
            candidates.append(last_sync - timedelta(days=CASH_FLOW_OVERLAP_DAYS))
        # Resume after an interrupted backfill: days are processed and committed in order.
        last_movement = session.scalar(
            select(func.max(CashMovement.occurred_at)).where(
                CashMovement.broker_account_id == account_id, CashMovement.source == RecordSource.BROKER
            )
        )
        if last_movement is not None:
            local_day = last_movement.astimezone(self._zone).date()
            candidates.append(local_day - timedelta(days=CASH_FLOW_OVERLAP_DAYS))
        return max(candidates)

    def _sync_cash_movements(self, session: Session, result: StepResult, account_id: int, external_id: int) -> None:
        imported = set(session.scalars(
            select(CashMovement.broker_movement_id).where(CashMovement.broker_account_id == account_id)
        ))
        day = self._cash_flow_start(session, account_id)
        today = self._today()
        days_done = 0
        while day <= today:
            for flow in parse_cash_flows(self._client.get_cash_flows(external_id, f"{day:%Y-%m-%d}")):
                result.fetched += 1
                if flow.cashflow_id in imported:
                    result.skipped += 1
                    continue
                session.add(CashMovement(
                    broker_account_id=account_id, broker_movement_id=flow.cashflow_id,
                    broker_type=flow.broker_type, movement_type=classify_cash_flow(flow.broker_type),
                    amount=flow.amount, currency=flow.currency,
                    occurred_at=datetime.combine(flow.clearing_date, time(), self._zone),
                    source=RecordSource.BROKER, description=flow.remark,
                ))
                imported.add(flow.cashflow_id)
                result.inserted += 1
            day += timedelta(days=1)
            days_done += 1
            if days_done % CASH_FLOW_COMMIT_EVERY_DAYS == 0:
                session.commit()
                logger.info("Moomoo cash flows synchronised up to %s", day - timedelta(days=1))


def validate_broker_holdings(session_factory: sessionmaker[Session]) -> list[ValidationReport]:
    """Compare the latest synced broker positions with the quantities implied by imported fills.

    Differences are expected for holdings bought before the sync history start
    until those opening positions are recorded.
    """
    with session_scope(session_factory) as session:
        accounts = session.scalars(
            select(BrokerAccount).where(BrokerAccount.broker == BROKER, BrokerAccount.is_active.is_(True))
        ).all()
        return [_validate_account(session, account) for account in accounts]


def _validate_account(session: Session, account: BrokerAccount) -> ValidationReport:
    broker = {
        position.asset_id: position.quantity
        for position in session.scalars(select(Position).where(Position.broker_account_id == account.id))
    }
    ledger: dict[int, Decimal] = {}
    for transaction in session.scalars(select(Transaction).where(Transaction.broker_account_id == account.id)):
        signed = transaction.quantity if transaction.side is TransactionSide.BUY else -transaction.quantity
        ledger[transaction.asset_id] = ledger.get(transaction.asset_id, Decimal("0")) + signed
    symbols = dict(session.execute(select(Asset.id, Asset.symbol)).all())
    report = ValidationReport(account=account.name or account.external_account_id)
    for asset_id in sorted(set(broker) | set(ledger), key=lambda i: symbols[i]):
        report.checks.append(HoldingCheck(
            symbol=symbols[asset_id],
            broker_quantity=broker.get(asset_id, Decimal("0")),
            ledger_quantity=ledger.get(asset_id, Decimal("0")),
        ))
    return report


def format_report(report: SyncReport, validations: Iterable[ValidationReport]) -> str:
    """Human-readable summary for the command line."""
    lines = []
    for step in report.steps:
        where = f" [{step.account}]" if step.account else ""
        lines.append(
            f"{step.sync_type.value:<15}{where} {step.status.value:<8} "
            f"fetched={step.fetched} inserted={step.inserted} skipped={step.skipped}"
        )
        if step.error:
            lines.append(f"    error: {step.error}")
        lines.extend(f"    - {issue}" for issue in step.issues)
    for validation in validations:
        lines.append(f"\nValidation {validation.account}: {'OK' if validation.matches else 'DIFFERENCES'}")
        for check in validation.checks:
            flag = "ok" if check.matches else f"diff {check.difference}"
            lines.append(
                f"  {check.symbol:<10} broker={check.broker_quantity} fills={check.ledger_quantity} {flag}"
            )
    return "\n".join(lines)
