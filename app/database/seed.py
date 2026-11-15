"""Idempotent seed of reference data: regions, the initial asset list, default settings
and the baseline retirement plan.

Only configuration is seeded. Quantities, purchase prices and cash balances are
never hardcoded; import them into the database separately. Existing rows are
never modified, so edits made through the dashboard survive re-seeding.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import AppSetting, Asset, AssetClass, Region
from app.services.forecast_service import ensure_baseline_retirement_plan

INITIAL_REGIONS: dict[str, str] = {
    "AU": "Australia",
    "US": "USA",
    "EU": "Europe",
}

# "region" is the geographic exposure of the asset, not its listing market.
# "broker_symbol" is the Moomoo security code used to match synced positions and fills.
INITIAL_ASSETS: list[dict[str, object]] = [
    {"symbol": "BTC-AUD", "name": "Bitcoin", "asset_class": AssetClass.CRYPTO,
     "market": "Cryptocurrency", "currency": "AUD", "region": None, "broker_symbol": None},
    {"symbol": "AAPL", "name": "Apple", "asset_class": AssetClass.SHARE,
     "market": "US", "currency": "USD", "region": "US", "broker_symbol": "US.AAPL"},
    {"symbol": "IOZ.AX", "name": "iShares Core S&P/ASX 200 ETF", "asset_class": AssetClass.SHARE,
     "market": "ASX", "currency": "AUD", "region": "AU", "broker_symbol": "AU.IOZ"},
    {"symbol": "NDQ.AX", "name": "BetaShares Nasdaq 100 ETF", "asset_class": AssetClass.SHARE,
     "market": "ASX", "currency": "AUD", "region": "US", "broker_symbol": "AU.NDQ"},
    {"symbol": "VEQ.AX", "name": "Vanguard FTSE Europe ETF", "asset_class": AssetClass.SHARE,
     "market": "ASX", "currency": "AUD", "region": "EU", "broker_symbol": "AU.VEQ"},
    {"symbol": "SHL.AX", "name": "Sonic Healthcare", "asset_class": AssetClass.SHARE,
     "market": "ASX", "currency": "AUD", "region": "AU", "broker_symbol": "AU.SHL"},
    {"symbol": "VAP.AX", "name": "Vanguard Australian Property ETF", "asset_class": AssetClass.PROPERTY,
     "market": "ASX", "currency": "AUD", "region": "AU", "broker_symbol": "AU.VAP"},
]

# Recommended update frequencies (minutes), configurable from the dashboard.
DEFAULT_APP_SETTINGS: dict[str, tuple[str, str]] = {
    "schedule.bitcoin_interval_minutes": ("180", "Bitcoin price collection interval"),
    "schedule.asx_interval_minutes": ("180", "ASX price collection interval (market hours)"),
    "schedule.us_interval_minutes": ("180", "US price collection interval (market hours)"),
    "schedule.exchange_rates_interval_minutes": ("1440", "Exchange rate collection interval"),
    "schedule.moomoo_transactions_interval_minutes": ("180", "Moomoo transaction sync interval"),
    "schedule.moomoo_positions_interval_minutes": ("180", "Moomoo position sync interval"),
    "schedule.dividends_interval_minutes": ("1440", "Dividend information collection interval"),
    "display.timezone": ("Australia/Melbourne", "Presentation timezone"),
    "reporting.currency": ("AUD", "Reporting currency"),
    "moomoo.history_start": ("2025-01-01", "Earliest date to import Moomoo fills and cash flows from"),
}


def seed_reference_data(session: Session) -> None:
    """Insert missing regions, assets and settings. The caller commits (see ``session_scope``)."""
    regions = {region.code: region for region in session.scalars(select(Region))}
    for code, name in INITIAL_REGIONS.items():
        if code not in regions:
            regions[code] = Region(code=code, name=name)
            session.add(regions[code])

    existing_symbols = set(session.scalars(select(Asset.symbol)))
    for asset in INITIAL_ASSETS:
        if asset["symbol"] not in existing_symbols:
            values = {key: value for key, value in asset.items() if key != "region"}
            region_code = asset["region"]
            session.add(Asset(**values, region=regions[region_code] if region_code else None))

    existing_keys = set(session.scalars(select(AppSetting.key)))
    for key, (value, description) in DEFAULT_APP_SETTINGS.items():
        if key not in existing_keys:
            session.add(AppSetting(key=key, value=value, description=description))

    ensure_baseline_retirement_plan(session)
