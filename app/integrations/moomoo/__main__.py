"""Moomoo command line.

Usage (from the project root, with OpenD running and logged in):
    python -m app.integrations.moomoo probe      # read-only check, writes nothing
    python -m app.integrations.moomoo sync       # import into the database, then validate
    python -m app.integrations.moomoo validate   # compare synced positions with imported fills
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta

from app.config import load_settings
from app.database.db import create_db_engine, create_session_factory
from app.integrations.moomoo.accounts import parse_accounts
from app.integrations.moomoo.authentication import load_opend_settings
from app.integrations.moomoo.client import MoomooApiError, MoomooClient
from app.integrations.moomoo.positions import parse_cash_balances, parse_positions
from app.integrations.moomoo.transactions import parse_order_fees
from app.services.broker_sync_service import (
    MoomooSyncService,
    SyncReport,
    format_report,
    validate_broker_holdings,
)

PROBE_CASH_FLOW_DAYS = 14


def _mask(value: object) -> str:
    text = str(value)
    return "****" + text[-4:]


def probe(client: MoomooClient) -> None:
    """Print what the API returns for this account, to confirm market support and data shapes."""
    raw_accounts = client.list_accounts()
    print(f"Accounts returned: {len(raw_accounts)}")
    for row in raw_accounts:
        print(f"  {_mask(row.get('acc_id'))} env={row.get('trd_env')} type={row.get('acc_type')} "
              f"firm={row.get('security_firm')} markets={row.get('trdmarket_auth')} "
              f"status={row.get('acc_status')}")

    for account in parse_accounts(raw_accounts, client.trading_environment):
        account_id = account.account_id
        print(f"\n== Account {_mask(account_id)} ({', '.join(account.markets)}) ==")
        _probe_step("Cash", lambda: [
            f"{b.currency}: {b.cash_balance}" for b in parse_cash_balances(client.get_account_info(account_id))
        ])
        _probe_step("Positions", lambda: [
            f"{p.code} market={p.market} qty={p.quantity} avg_cost={p.average_cost} {p.currency}"
            for p in parse_positions(client.get_positions(account_id))
        ])

        def deals() -> list[str]:
            end = date.today()
            rows = client.get_history_deals(
                account_id, f"{end - timedelta(days=89):%Y-%m-%d} 00:00:00", f"{end:%Y-%m-%d} 23:59:59"
            )
            lines = [f"{len(rows)} fills in the last 90 days, by market: "
                     f"{dict(Counter(str(r.get('deal_market')) for r in rows))}"]
            lines += [f"{r.get('code')} {r.get('trd_side')} qty={r.get('qty')} price={r.get('price')} "
                      f"time={r.get('create_time')} status={r.get('status')}" for r in rows[:5]]
            order_ids = [str(r["order_id"]) for r in rows[:1]]
            if order_ids:
                fees = parse_order_fees(client.get_order_fees(account_id, order_ids))
                lines.append(f"fee for order of first fill: {fees}")
            return lines

        _probe_step("Fills", deals)

        def cash_flows() -> list[str]:
            types: Counter[tuple[str, str, str]] = Counter()
            for offset in range(PROBE_CASH_FLOW_DAYS):
                day = date.today() - timedelta(days=offset)
                for row in client.get_cash_flows(account_id, f"{day:%Y-%m-%d}"):
                    types[(str(row.get("cashflow_type")), str(row.get("cashflow_direction")),
                           str(row.get("currency")))] += 1
            return [f"last {PROBE_CASH_FLOW_DAYS} days, distinct types (type, direction, currency):"] + [
                f"{key} x{count}" for key, count in types.most_common()
            ]

        _probe_step("Cash flows", cash_flows)


def _probe_step(title: str, step: Callable[[], list[str]]) -> None:
    print(f"-- {title}")
    try:
        for line in step():
            print(f"   {line}")
    except (MoomooApiError, ValueError) as exc:
        print(f"   UNAVAILABLE: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Moomoo read-only synchronisation")
    parser.add_argument("command", choices=["probe", "sync", "validate"])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = load_settings()
    if args.command == "validate":
        engine = create_db_engine(settings)
        print(format_report(SyncReport(), validate_broker_holdings(create_session_factory(engine))))
        engine.dispose()
        return 0

    with MoomooClient(load_opend_settings()) as client:
        if args.command == "probe":
            try:
                probe(client)
            except MoomooApiError as exc:
                print(f"Moomoo probe failed: {exc}", file=sys.stderr)
                return 2
            return 0
        engine = create_db_engine(settings)
        session_factory = create_session_factory(engine)
        service = MoomooSyncService(session_factory, client, local_timezone=settings.display_timezone)
        report = service.sync_all()
        print(format_report(report, validate_broker_holdings(session_factory)))
        engine.dispose()
        return 0 if report.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
