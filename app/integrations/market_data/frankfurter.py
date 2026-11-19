"""Exchange rates from Frankfurter (https://frankfurter.dev), free and without a key.

These are daily reference rates (central bank fixings), not live market quotes.
The rate's ``date`` is stored as the market timestamp (00:00 UTC on that date).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timezone

from app.database.types import utc_now
from app.integrations.market_data.base_provider import (
    ExchangeRateProvider,
    ExchangeRateQuote,
    HttpClient,
    ProviderError,
    to_decimal,
)

RATE_URL = "https://api.frankfurter.dev/v2/rate/{base}/{quote}"


class FrankfurterProvider(ExchangeRateProvider):
    name = "frankfurter"

    def __init__(self, http: HttpClient | None = None, now: Callable[[], datetime] = utc_now) -> None:
        self._http = http or HttpClient()
        self._now = now

    def fetch_rate(self, base_currency: str, quote_currency: str) -> ExchangeRateQuote:
        payload = self._http.get_json(RATE_URL.format(base=base_currency, quote=quote_currency))
        if not isinstance(payload, dict):
            raise ProviderError("Unexpected Frankfurter response")
        if payload.get("base") != base_currency or payload.get("quote") != quote_currency:
            raise ProviderError(f"Frankfurter returned {payload.get('base')}/{payload.get('quote')}")
        try:
            rate_date = date.fromisoformat(str(payload.get("date")))
        except ValueError as exc:
            raise ProviderError(f"Invalid rate date: {payload.get('date')!r}") from exc
        return ExchangeRateQuote(
            base_currency=base_currency,
            quote_currency=quote_currency,
            rate=to_decimal(payload.get("rate"), "rate"),
            provider=self.name,
            market_timestamp=datetime.combine(rate_date, time(), timezone.utc),
            retrieved_at=self._now(),
        )
