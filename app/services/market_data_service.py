"""Collect market prices and exchange rates, record every attempt, and decide what is due.

Providers are chosen per asset (``assets.price_provider``), so replacing a provider is
a data change. When a fetch fails, the asset's last valid price is kept and flagged
``stale``; a later successful fetch adds a new ``valid`` row.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.db import session_scope
from app.database.models import (
    AppSetting,
    Asset,
    CollectionLog,
    CollectionType,
    DataStatus,
    ExchangeRate,
    MarketPrice,
    RunStatus,
)
from app.database.types import utc_now
from app.integrations.market_data.base_provider import (
    AssetRef,
    ExchangeRateProvider,
    PriceProvider,
    PriceQuote,
    ProviderError,
)
from app.scheduler.scheduler import ASX, US, MarketSession, is_due

logger = logging.getLogger(__name__)

RETRY_AFTER_FAILURE = timedelta(minutes=30)
EXCHANGE_RATES_JOB = "exchange_rates"
EXCHANGE_RATES_INTERVAL_SETTING = "schedule.exchange_rates_interval_minutes"
DEFAULT_INTERVAL_MINUTES = 180


@dataclass(frozen=True)
class PriceJob:
    """A scheduled price collection for all assets of one market."""

    name: str
    interval_setting: str
    market: str  # assets.market value
    session: MarketSession | None


PRICE_JOBS = {
    job.name: job
    for job in (
        PriceJob("prices.crypto", "schedule.bitcoin_interval_minutes", "Cryptocurrency", None),
        PriceJob("prices.asx", "schedule.asx_interval_minutes", "ASX", ASX),
        PriceJob("prices.us", "schedule.us_interval_minutes", "US", US),
    )
}
ALL_JOBS = (*PRICE_JOBS, EXCHANGE_RATES_JOB)


@dataclass
class JobResult:
    job: str
    status: RunStatus = RunStatus.SUCCESS
    stored: int = 0
    failed: list[str] = field(default_factory=list)


class MarketDataService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        price_providers: dict[str, PriceProvider],
        exchange_rate_provider: ExchangeRateProvider,
        *,
        reporting_currency: str = "AUD",
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._price_providers = price_providers
        self._fx_provider = exchange_rate_provider
        self._reporting_currency = reporting_currency
        self._now = now

    # --- scheduling -----------------------------------------------------------

    def run_due_jobs(self) -> list[JobResult]:
        return [self.run_job(job) for job in ALL_JOBS if self.is_job_due(job)]

    def run_job(self, job: str) -> JobResult:
        if job == EXCHANGE_RATES_JOB:
            return self.collect_exchange_rates()
        if job in PRICE_JOBS:
            return self.collect_prices(job)
        raise ValueError(f"Unknown job {job!r}; expected one of {ALL_JOBS}")

    def is_job_due(self, job: str) -> bool:
        now = self._now()
        setting = EXCHANGE_RATES_INTERVAL_SETTING if job == EXCHANGE_RATES_JOB else PRICE_JOBS[job].interval_setting
        session_hours = None if job == EXCHANGE_RATES_JOB else PRICE_JOBS[job].session
        with session_scope(self._session_factory) as session:
            interval = timedelta(minutes=self._interval_minutes(session, setting))
            last_success = session.scalar(
                select(func.max(CollectionLog.started_at)).where(
                    CollectionLog.job == job,
                    CollectionLog.status.in_([RunStatus.SUCCESS, RunStatus.PARTIAL]),
                )
            )
            last_attempt = session.execute(
                select(CollectionLog.started_at, CollectionLog.status)
                .where(CollectionLog.job == job)
                .order_by(CollectionLog.started_at.desc())
                .limit(1)
            ).first()
        if last_attempt and last_attempt.status is RunStatus.FAILED:
            if now - last_attempt.started_at < min(interval, RETRY_AFTER_FAILURE):
                return False
        return is_due(now=now, interval=interval, last_success=last_success, session=session_hours)

    @staticmethod
    def _interval_minutes(session: Session, key: str) -> int:
        value = session.scalar(select(AppSetting.value).where(AppSetting.key == key))
        try:
            minutes = int(value) if value is not None else DEFAULT_INTERVAL_MINUTES
        except ValueError:
            logger.warning("Invalid interval %r for %s; using %d minutes", value, key, DEFAULT_INTERVAL_MINUTES)
            return DEFAULT_INTERVAL_MINUTES
        return max(minutes, 1)

    # --- prices ---------------------------------------------------------------

    def collect_prices(self, job_name: str) -> JobResult:
        job = PRICE_JOBS[job_name]
        result = JobResult(job=job_name)
        with session_scope(self._session_factory) as session:
            assets = session.scalars(
                select(Asset).where(
                    Asset.market == job.market,
                    Asset.is_active.is_(True),
                    Asset.price_provider.is_not(None),
                    Asset.provider_symbol.is_not(None),
                )
            ).all()
            by_provider: dict[str, list[Asset]] = {}
            for asset in assets:
                by_provider.setdefault(asset.price_provider, []).append(asset)
            for provider_name, provider_assets in by_provider.items():
                self._collect_from_provider(session, job_name, provider_name, provider_assets, result)
        result.status = _overall_status(result)
        return result

    def _collect_from_provider(
        self, session: Session, job: str, provider_name: str, assets: list[Asset], result: JobResult
    ) -> None:
        log = CollectionLog(
            job=job, provider=provider_name, data_type=CollectionType.PRICE,
            asset_id=assets[0].id if len(assets) == 1 else None,
            status=RunStatus.RUNNING, started_at=self._now(),
        )
        session.add(log)
        provider = self._price_providers.get(provider_name)
        quotes: list[PriceQuote] = []
        try:
            if provider is None:
                raise ProviderError(f"No provider configured named {provider_name!r}")
            quotes = provider.fetch_prices([
                AssetRef(symbol=a.symbol, provider_symbol=a.provider_symbol, currency=a.currency)
                for a in assets
            ])
        except ProviderError as exc:
            log.http_status = exc.http_status
            log.error_message = str(exc)

        by_symbol = {asset.symbol: asset for asset in assets}
        for quote in quotes:
            asset = by_symbol.get(quote.symbol)
            if asset is None:
                continue
            session.add(MarketPrice(
                asset_id=asset.id, price=quote.price, currency=quote.currency,
                provider=quote.provider, market_timestamp=quote.market_timestamp,
                retrieved_at=quote.retrieved_at, status=DataStatus.VALID,
            ))
            result.stored += 1

        priced = {quote.symbol for quote in quotes}
        missing = [asset for asset in assets if asset.symbol not in priced]
        for asset in missing:
            self._mark_latest_price_stale(session, asset.id)
            result.failed.append(asset.symbol)
        if missing and quotes:
            log.error_message = f"No price for {', '.join(a.symbol for a in missing)}"
        log.status = RunStatus.FAILED if not quotes else (RunStatus.PARTIAL if missing else RunStatus.SUCCESS)
        log.finished_at = self._now()

    @staticmethod
    def _mark_latest_price_stale(session: Session, asset_id: int) -> None:
        latest = session.scalars(
            select(MarketPrice).where(MarketPrice.asset_id == asset_id)
            .order_by(MarketPrice.retrieved_at.desc()).limit(1)
        ).first()
        if latest is not None and latest.status is DataStatus.VALID:
            latest.status = DataStatus.STALE

    # --- exchange rates ---------------------------------------------------------

    def collect_exchange_rates(self) -> JobResult:
        """Fetch a rate to the reporting currency for every other currency held."""
        result = JobResult(job=EXCHANGE_RATES_JOB)
        with session_scope(self._session_factory) as session:
            currencies = sorted(set(session.scalars(
                select(Asset.currency).where(Asset.is_active.is_(True))
            )) - {self._reporting_currency})
            for currency in currencies:
                log = CollectionLog(
                    job=EXCHANGE_RATES_JOB, provider=self._fx_provider.name,
                    data_type=CollectionType.EXCHANGE_RATE, status=RunStatus.RUNNING,
                    started_at=self._now(),
                )
                session.add(log)
                pair = f"{currency}/{self._reporting_currency}"
                try:
                    quote = self._fx_provider.fetch_rate(currency, self._reporting_currency)
                except ProviderError as exc:
                    log.status, log.http_status, log.error_message = RunStatus.FAILED, exc.http_status, f"{pair}: {exc}"
                    self._mark_latest_rate_stale(session, currency, self._reporting_currency)
                    result.failed.append(pair)
                else:
                    session.add(ExchangeRate(
                        base_currency=quote.base_currency, quote_currency=quote.quote_currency,
                        rate=quote.rate, provider=quote.provider, market_timestamp=quote.market_timestamp,
                        retrieved_at=quote.retrieved_at, status=DataStatus.VALID,
                    ))
                    log.status = RunStatus.SUCCESS
                    result.stored += 1
                log.finished_at = self._now()
        result.status = _overall_status(result)
        return result

    @staticmethod
    def _mark_latest_rate_stale(session: Session, base: str, quote: str) -> None:
        latest = session.scalars(
            select(ExchangeRate).where(ExchangeRate.base_currency == base, ExchangeRate.quote_currency == quote)
            .order_by(ExchangeRate.retrieved_at.desc()).limit(1)
        ).first()
        if latest is not None and latest.status is DataStatus.VALID:
            latest.status = DataStatus.STALE


def _overall_status(result: JobResult) -> RunStatus:
    if not result.failed:
        return RunStatus.SUCCESS
    return RunStatus.PARTIAL if result.stored else RunStatus.FAILED


def latest_prices(session: Session) -> dict[int, MarketPrice]:
    """Most recently retrieved price per asset (check ``status`` for staleness)."""
    newest = (
        select(MarketPrice.asset_id, func.max(MarketPrice.retrieved_at).label("retrieved_at"))
        .group_by(MarketPrice.asset_id).subquery()
    )
    rows = session.scalars(
        select(MarketPrice).join(
            newest,
            (MarketPrice.asset_id == newest.c.asset_id) & (MarketPrice.retrieved_at == newest.c.retrieved_at),
        )
    )
    return {price.asset_id: price for price in rows}


def latest_exchange_rate(session: Session, base: str, quote: str) -> ExchangeRate | None:
    """Most recently retrieved rate for a pair (check ``status`` for staleness)."""
    return session.scalars(
        select(ExchangeRate).where(ExchangeRate.base_currency == base, ExchangeRate.quote_currency == quote)
        .order_by(ExchangeRate.retrieved_at.desc()).limit(1)
    ).first()
