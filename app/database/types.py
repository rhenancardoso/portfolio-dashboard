"""Custom column types enforcing exact decimals and UTC timestamps."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_display_timezone(value: datetime, tz_name: str = "Australia/Melbourne") -> datetime:
    """Convert an aware datetime to the presentation timezone."""
    return value.astimezone(ZoneInfo(tz_name))


class Money(TypeDecorator[Decimal]):
    """Exact decimal stored as TEXT.

    SQLite has no decimal type; NUMERIC columns silently become binary floats.
    Floats are rejected on write. Do not SUM/ORDER on these columns in SQL;
    aggregate with Decimal in Python.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("Monetary values must be Decimal, int or str - not float")
        decimal_value = value if isinstance(value, Decimal) else Decimal(value)
        if not decimal_value.is_finite():
            raise ValueError("Monetary values must be finite")
        return format(decimal_value, "f")

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime stored as naive UTC.

    Naive datetimes are rejected on write so local times cannot leak in.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timestamps must be timezone-aware")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=timezone.utc)
