"""Bitcoin (and other crypto) prices from the CoinGecko API.

Uses the free Demo plan. The key is read from ``COINGECKO_API_KEY`` and sent as a
header; without it the request is attempted anonymously, which CoinGecko may reject.
The asset's ``provider_symbol`` is the CoinGecko coin id (e.g. ``bitcoin``).
"""

from __future__ import annotations

import os
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

BASE_URL = "https://api.coingecko.com/api/v3"


class CoinGeckoProvider(PriceProvider):
    name = "coingecko"

    def __init__(
        self,
        http: HttpClient | None = None,
        api_key: str | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._http = http or HttpClient()
        self._api_key = api_key if api_key is not None else os.environ.get("COINGECKO_API_KEY")
        self._now = now

    def fetch_prices(self, assets: list[AssetRef]) -> list[PriceQuote]:
        if not assets:
            return []
        currencies = sorted({asset.currency.lower() for asset in assets})
        headers = {"accept": "application/json"}
        if self._api_key:
            headers["x-cg-demo-api-key"] = self._api_key
        payload = self._http.get_json(
            f"{BASE_URL}/simple/price",
            params={
                "ids": ",".join(sorted({asset.provider_symbol for asset in assets})),
                "vs_currencies": ",".join(currencies),
                "include_last_updated_at": "true",
            },
            headers=headers,
        )
        if not isinstance(payload, dict):
            raise ProviderError("Unexpected CoinGecko response")
        retrieved_at = self._now()
        quotes = []
        for asset in assets:
            coin = payload.get(asset.provider_symbol)
            if not isinstance(coin, dict) or asset.currency.lower() not in coin:
                continue
            updated = coin.get("last_updated_at")
            quotes.append(PriceQuote(
                symbol=asset.symbol,
                price=to_decimal(coin[asset.currency.lower()], f"{asset.provider_symbol}.{asset.currency}"),
                currency=asset.currency,
                provider=self.name,
                market_timestamp=(
                    datetime.fromtimestamp(int(updated), timezone.utc) if isinstance(updated, int) else None
                ),
                retrieved_at=retrieved_at,
            ))
        return quotes
