"""Provider-independent market data interfaces and a shared HTTP helper.

Providers return ``PriceQuote`` / ``ExchangeRateQuote`` values; storage, staleness
and scheduling live in the services, so a provider can be replaced without
touching portfolio logic.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 15.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    """A provider request failed or returned unusable data."""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class PriceQuote:
    """A price as published by a provider."""

    symbol: str  # the application's asset symbol, e.g. IOZ.AX
    price: Decimal
    currency: str
    provider: str
    market_timestamp: datetime | None  # when the provider says the price applies (UTC)
    retrieved_at: datetime  # when this application received it (UTC)


@dataclass(frozen=True)
class ExchangeRateQuote:
    """Units of ``quote_currency`` per one ``base_currency``."""

    base_currency: str
    quote_currency: str
    rate: Decimal
    provider: str
    market_timestamp: datetime | None
    retrieved_at: datetime


@dataclass(frozen=True)
class AssetRef:
    """What a price provider needs to know about an asset."""

    symbol: str
    provider_symbol: str
    currency: str


class PriceProvider(ABC):
    """Fetches latest prices for a set of assets."""

    name: str

    @abstractmethod
    def fetch_prices(self, assets: list[AssetRef]) -> list[PriceQuote]:
        """Return quotes for the assets it could price. Missing assets are simply absent."""


class ExchangeRateProvider(ABC):
    name: str

    @abstractmethod
    def fetch_rate(self, base_currency: str, quote_currency: str) -> ExchangeRateQuote:
        ...


def to_decimal(value: Any, field: str) -> Decimal:
    """Exact positive Decimal from a JSON number or string (floats via their shortest repr)."""
    if value is None or isinstance(value, bool):
        raise ProviderError(f"Missing or invalid '{field}': {value!r}")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ProviderError(f"Invalid number in '{field}': {value!r}") from exc
    if not result.is_finite() or result <= 0:
        raise ProviderError(f"Unusable value in '{field}': {value!r}")
    return result


class HttpClient:
    """JSON GET with timeout, bounded retries and exponential backoff.

    Retries network errors and 429/5xx responses; honours ``Retry-After``.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = 3,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    def get_json(self, url: str, *, params: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> Any:
        last_error: ProviderError | None = None
        for attempt in range(1, self._max_attempts + 1):
            delay = self._backoff_seconds * 2 ** (attempt - 1)
            try:
                response = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = ProviderError(f"Request failed: {type(exc).__name__}")
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ProviderError("Response is not valid JSON", 200) from exc
                last_error = ProviderError(f"HTTP {response.status_code}", response.status_code)
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
            # Only the path is logged: query strings may carry an API key.
            logger.warning("GET %s failed (attempt %d/%d): %s",
                           url.split("?")[0], attempt, self._max_attempts, last_error)
            if attempt < self._max_attempts:
                self._sleep(delay)
        assert last_error is not None
        raise last_error
