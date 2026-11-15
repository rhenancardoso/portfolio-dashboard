"""Conversion helpers for Moomoo SDK rows."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

MISSING_VALUES = {"", "N/A", None}

# Moomoo reports securities trade times in the exchange's local time.
MARKET_TIMEZONES = {
    "AU": "Australia/Sydney",
    "US": "America/New_York",
    "HK": "Asia/Hong_Kong",
    "HKCC": "Asia/Shanghai",
    "CN": "Asia/Shanghai",
    "SG": "Asia/Singapore",
    "JP": "Asia/Tokyo",
    "MY": "Asia/Kuala_Lumpur",
    "CA": "America/Toronto",
}


class MoomooDataError(ValueError):
    """A Moomoo response row is missing data or has an unexpected value."""


def is_missing(value: Any) -> bool:
    return value in MISSING_VALUES or (isinstance(value, float) and math.isnan(value))


def to_decimal(value: Any) -> Decimal | None:
    """Exact Decimal from an SDK number (floats go through their shortest repr)."""
    if is_missing(value):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise MoomooDataError(f"Not a number: {value!r}") from exc
    if not result.is_finite():
        raise MoomooDataError(f"Not a finite number: {value!r}")
    return result


def require_decimal(value: Any, field: str) -> Decimal:
    result = to_decimal(value)
    if result is None:
        raise MoomooDataError(f"Missing required field '{field}'")
    return result


def require_text(value: Any, field: str) -> str:
    if is_missing(value):
        raise MoomooDataError(f"Missing required field '{field}'")
    return str(value)


def code_market(code: str) -> str:
    """Market prefix of a Moomoo security code ("US.AAPL" -> "US")."""
    market, separator, _ = code.partition(".")
    if not separator:
        raise MoomooDataError(f"Unexpected security code format: {code!r}")
    return market


def market_time_to_utc(text: str, market: str) -> datetime:
    """Parse "YYYY-MM-DD HH:MM:SS[.fff]" in the market's local time to UTC."""
    zone_name = MARKET_TIMEZONES.get(market)
    if zone_name is None:
        raise MoomooDataError(f"No timezone known for market {market!r}")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            local = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return local.replace(tzinfo=ZoneInfo(zone_name)).astimezone(timezone.utc)
    raise MoomooDataError(f"Unexpected time format: {text!r}")
