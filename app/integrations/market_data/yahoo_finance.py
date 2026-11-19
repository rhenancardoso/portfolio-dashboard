"""Share prices from Yahoo Finance's chart endpoint (ASX ``IOZ.AX``, US ``AAPL``).

Yahoo has no official public API: this endpoint is unofficial, unsupported and may
change or be rate limited without notice. It sits behind ``PriceProvider`` so it can
be replaced by changing ``assets.price_provider``. Prices are typically delayed;
``regularMarketTime`` is stored as the market timestamp.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from app.database.types import utc_now
from app.integrations.market_data.base_provider import (
    AssetRef,
    HttpClient,
    PriceProvider,
    PriceQuote,
    ProviderError,
    to_decimal,
)

logger = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal portfolio dashboard)", "Accept": "application/json"}
PAUSE_BETWEEN_REQUESTS_SECONDS = 1.0


class YahooFinanceProvider(PriceProvider):
    name = "yahoo"

    def __init__(
        self,
        http: HttpClient | None = None,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or HttpClient()
        self._now = now
        self._sleep = sleep

    def fetch_prices(self, assets: list[AssetRef]) -> list[PriceQuote]:
        """One request per symbol; a failing symbol is logged and left out of the result.

        Raises only when every symbol fails, so a provider outage is reported as such.
        """
        quotes = []
        last_error: ProviderError | None = None
        for index, asset in enumerate(assets):
            if index:
                self._sleep(PAUSE_BETWEEN_REQUESTS_SECONDS)
            try:
                quotes.append(self._fetch(asset))
            except ProviderError as exc:
                logger.warning("Yahoo price for %s failed: %s", asset.symbol, exc)
                last_error = exc
        if last_error is not None and not quotes:
            raise last_error
        return quotes

    def _fetch(self, asset: AssetRef) -> PriceQuote:
        payload = self._http.get_json(
            CHART_URL.format(symbol=asset.provider_symbol),
            params={"range": "1d", "interval": "1d"},
            headers=HEADERS,
        )
        try:
            meta = payload["chart"]["result"][0]["meta"]
        except (KeyError, IndexError, TypeError) as exc:
            error = (payload or {}).get("chart", {}).get("error") if isinstance(payload, dict) else None
            raise ProviderError(f"No quote for {asset.provider_symbol}: {error or 'unexpected response'}") from exc
        currency = meta.get("currency")
        if currency != asset.currency:
            raise ProviderError(
                f"{asset.provider_symbol} is quoted in {currency!r}, expected {asset.currency}"
            )
        market_time = meta.get("regularMarketTime")
        return PriceQuote(
            symbol=asset.symbol,
            price=to_decimal(meta.get("regularMarketPrice"), "regularMarketPrice"),
            currency=asset.currency,
            provider=self.name,
            market_timestamp=(
                datetime.fromtimestamp(int(market_time), timezone.utc)
                if isinstance(market_time, (int, float)) else None
            ),
            retrieved_at=self._now(),
        )
