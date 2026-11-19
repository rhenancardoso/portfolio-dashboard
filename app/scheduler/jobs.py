"""Market data jobs, started by a systemd timer.

Usage (from the project root):
    python -m app.scheduler.jobs run-due          # run whichever jobs are due (used by the timer)
    python -m app.scheduler.jobs collect JOB      # run one job now: prices.crypto, prices.asx,
                                                  # prices.us or exchange_rates
    python -m app.scheduler.jobs status           # latest price and rate per asset/pair
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import select

from app.config import Settings, load_settings
from app.database.db import create_db_engine, create_session_factory, session_scope
from app.database.models import Asset
from app.database.types import to_display_timezone
from app.integrations.market_data.coingecko import CoinGeckoProvider
from app.integrations.market_data.frankfurter import FrankfurterProvider
from app.integrations.market_data.yahoo_finance import YahooFinanceProvider
from app.scheduler.scheduler import JobAlreadyRunning, single_run_lock
from app.services.market_data_service import (
    ALL_JOBS,
    MarketDataService,
    latest_exchange_rate,
    latest_prices,
)

logger = logging.getLogger(__name__)


def build_service(settings: Settings) -> MarketDataService:
    engine = create_db_engine(settings)
    providers = [CoinGeckoProvider(), YahooFinanceProvider()]
    return MarketDataService(
        create_session_factory(engine),
        {provider.name: provider for provider in providers},
        FrankfurterProvider(),
    )


def print_status(settings: Settings) -> None:
    engine = create_db_engine(settings)
    with session_scope(create_session_factory(engine)) as session:
        prices = latest_prices(session)
        for asset in session.scalars(select(Asset).order_by(Asset.symbol)):
            price = prices.get(asset.id)
            if price is None:
                print(f"{asset.symbol:<10} no price")
                continue
            market_time = (
                to_display_timezone(price.market_timestamp, settings.display_timezone).strftime("%Y-%m-%d %H:%M")
                if price.market_timestamp else "unknown"
            )
            retrieved = to_display_timezone(price.retrieved_at, settings.display_timezone).strftime("%Y-%m-%d %H:%M")
            print(f"{asset.symbol:<10} {price.price:>12} {price.currency} {price.status.value:<7} "
                  f"market {market_time}  retrieved {retrieved}  ({price.provider})")
        rate = latest_exchange_rate(session, "USD", "AUD")
        if rate is not None:
            print(f"USD/AUD    {rate.rate:>12} {rate.status.value:<7} "
                  f"rate date {rate.market_timestamp:%Y-%m-%d}  ({rate.provider})")
    engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Market data collection")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run-due", help="run every job that is due")
    collect = commands.add_parser("collect", help="run one job now")
    collect.add_argument("job", choices=ALL_JOBS)
    commands.add_parser("status", help="show latest prices and rates")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = load_settings()
    if args.command == "status":
        print_status(settings)
        return 0

    try:
        with single_run_lock(settings.database_path.parent / "collector.lock"):
            service = build_service(settings)
            results = service.run_due_jobs() if args.command == "run-due" else [service.run_job(args.job)]
    except JobAlreadyRunning as exc:
        logger.info("Skipped: %s", exc)
        return 0
    for result in results:
        failed = f" failed: {', '.join(result.failed)}" if result.failed else ""
        logger.info("%s: %s, stored %d%s", result.job, result.status.value, result.stored, failed)
    if not results:
        logger.info("No jobs due")
    return 0 if all(not r.failed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
