"""Market data providers, scheduling rules and the collection service (no network access)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import requests
from sqlalchemy import select

from app.database.db import session_scope
from app.database.models import (
    AppSetting,
    CollectionLog,
    DataStatus,
    ExchangeRate,
    MarketPrice,
    RunStatus,
)
from app.database.seed import seed_reference_data
from app.integrations.market_data.base_provider import (
    AssetRef,
    ExchangeRateProvider,
    ExchangeRateQuote,
    HttpClient,
    PriceProvider,
    PriceQuote,
    ProviderError,
)
from app.integrations.market_data.coingecko import CoinGeckoProvider
from app.integrations.market_data.frankfurter import FrankfurterProvider
from app.integrations.market_data.yahoo_finance import YahooFinanceProvider
from app.scheduler.scheduler import ASX, US, JobAlreadyRunning, is_due, single_run_lock
from app.services.market_data_service import MarketDataService, latest_exchange_rate, latest_prices

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)  # Monday 12:00 in Sydney (ASX open), Sunday night in New York


class FakeResponse:
    def __init__(self, status: int, payload: object = None, headers: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, responses: list) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        assert timeout is not None
        self.requests.append((url, params or {}, headers or {}))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def http(responses: list, sleeps: list | None = None) -> tuple[HttpClient, FakeSession]:
    session = FakeSession(responses)
    sleeps = sleeps if sleeps is not None else []
    return HttpClient(session=session, sleep=sleeps.append), session


# --- HTTP helper --------------------------------------------------------------


def test_http_retries_transient_errors_with_backoff():
    sleeps: list[float] = []
    client, _ = http([FakeResponse(503), FakeResponse(429, headers={"Retry-After": "10"}),
                      FakeResponse(200, {"ok": True})], sleeps)
    assert client.get_json("https://example.test/x") == {"ok": True}
    assert sleeps == [2.0, 10.0]


def test_http_does_not_retry_client_errors_and_reports_network_failures():
    client, session = http([FakeResponse(404)])
    with pytest.raises(ProviderError) as error:
        client.get_json("https://example.test/x")
    assert error.value.http_status == 404 and len(session.requests) == 1

    client, _ = http([requests.ConnectionError()] * 3)
    with pytest.raises(ProviderError, match="ConnectionError"):
        client.get_json("https://example.test/x")


# --- providers ------------------------------------------------------------------


def test_coingecko_parses_price_exactly_and_sends_key():
    client, session = http([FakeResponse(200, {"bitcoin": {"aud": 96549.12, "last_updated_at": 1794745263}})])
    quotes = CoinGeckoProvider(client, api_key="demo-key", now=lambda: NOW).fetch_prices(
        [AssetRef("BTC-AUD", "bitcoin", "AUD")]
    )
    assert quotes[0].price == Decimal("96549.12")
    assert quotes[0].market_timestamp == datetime.fromtimestamp(1794745263, UTC)
    assert quotes[0].retrieved_at == NOW
    assert session.requests[0][2]["x-cg-demo-api-key"] == "demo-key"
    assert session.requests[0][1]["vs_currencies"] == "aud"


def yahoo_chart(price: float, currency: str = "AUD", market_time: int = 1794700800) -> dict:
    return {"chart": {"result": [{"meta": {"regularMarketPrice": price, "currency": currency,
                                           "regularMarketTime": market_time}}], "error": None}}


def test_yahoo_skips_failing_symbols_and_checks_currency():
    client, session = http([FakeResponse(200, yahoo_chart(31.42)), FakeResponse(200, yahoo_chart(1.0, "USD"))])
    provider = YahooFinanceProvider(client, now=lambda: NOW, sleep=lambda s: None)
    quotes = provider.fetch_prices([AssetRef("IOZ.AX", "IOZ.AX", "AUD"), AssetRef("NDQ.AX", "NDQ.AX", "AUD")])
    assert [(q.symbol, q.price) for q in quotes] == [("IOZ.AX", Decimal("31.42"))]
    assert session.requests[0][0].endswith("/chart/IOZ.AX")

    client, _ = http([FakeResponse(200, {"chart": {"result": None, "error": {"code": "Not Found"}}})])
    with pytest.raises(ProviderError, match="Not Found"):
        YahooFinanceProvider(client, sleep=lambda s: None).fetch_prices([AssetRef("XXX.AX", "XXX.AX", "AUD")])


def test_frankfurter_rate_and_date():
    client, _ = http([FakeResponse(200, {"date": "2026-11-13", "base": "USD", "quote": "AUD", "rate": 1.4694})])
    quote = FrankfurterProvider(client, now=lambda: NOW).fetch_rate("USD", "AUD")
    assert quote.rate == Decimal("1.4694")
    assert quote.market_timestamp == datetime(2026, 11, 13, tzinfo=UTC)
    client, _ = http([FakeResponse(200, {"date": "2026-11-13", "base": "EUR", "quote": "AUD", "rate": 1.7})])
    with pytest.raises(ProviderError):
        FrankfurterProvider(client).fetch_rate("USD", "AUD")


# --- scheduling -------------------------------------------------------------------


def test_market_hours_follow_each_exchange_and_daylight_saving():
    assert ASX.is_open(datetime(2026, 1, 12, 23, 0, tzinfo=UTC))  # Tue 10:00 AEDT
    assert not ASX.is_open(datetime(2026, 7, 13, 23, 30, tzinfo=UTC))  # Tue 09:30 AEST
    assert US.is_open(datetime(2026, 7, 6, 13, 30, tzinfo=UTC))  # Mon 09:30 EDT
    assert not US.is_open(datetime(2026, 11, 9, 13, 30, tzinfo=UTC))  # Mon 08:30 EST
    assert US.is_open(datetime(2026, 11, 9, 14, 30, tzinfo=UTC))
    assert not ASX.is_open(datetime(2026, 11, 14, 1, 0, tzinfo=UTC))  # Saturday


def test_due_rules():
    interval = timedelta(hours=3)
    # open market: every interval
    assert not is_due(now=NOW, interval=interval, last_success=NOW - timedelta(hours=2), session=ASX)
    assert is_due(now=NOW, interval=interval, last_success=NOW - timedelta(hours=3), session=ASX)
    # after the close: exactly one collection for the closing price
    friday_close = datetime(2026, 11, 13, 5, 0, tzinfo=UTC)  # 16:00 AEDT
    assert not is_due(now=friday_close + timedelta(minutes=10), interval=interval,
                      last_success=friday_close - timedelta(hours=1), session=ASX)
    assert is_due(now=friday_close + timedelta(hours=1), interval=interval,
                  last_success=friday_close - timedelta(hours=1), session=ASX)
    saturday = datetime(2026, 11, 14, 1, 0, tzinfo=UTC)
    assert not is_due(now=saturday, interval=interval,
                      last_success=friday_close + timedelta(hours=1), session=ASX)
    # 24/7 market
    assert is_due(now=saturday, interval=interval, last_success=saturday - interval, session=None)
    assert is_due(now=saturday, interval=interval, last_success=None, session=ASX)


def test_overlapping_runs_are_prevented(tmp_path):
    lock = tmp_path / "collector.lock"
    with single_run_lock(lock):
        with pytest.raises(JobAlreadyRunning):
            with single_run_lock(lock):
                pass
    with single_run_lock(lock):  # released afterwards
        pass


# --- collection service -------------------------------------------------------------


class FakePriceProvider(PriceProvider):
    def __init__(self, name: str, prices: dict[str, str], error: ProviderError | None = None) -> None:
        self.name = name
        self.prices = prices
        self.error = error
        self.calls = 0
        self.now = NOW

    def fetch_prices(self, assets: list[AssetRef]) -> list[PriceQuote]:
        self.calls += 1
        if self.error:
            raise self.error
        return [
            PriceQuote(a.symbol, Decimal(self.prices[a.symbol]), a.currency, self.name,
                       self.now - timedelta(minutes=20), self.now)
            for a in assets if a.symbol in self.prices
        ]


class FakeRateProvider(ExchangeRateProvider):
    name = "fakefx"

    def __init__(self, error: ProviderError | None = None) -> None:
        self.error = error
        self.now = NOW

    def fetch_rate(self, base_currency: str, quote_currency: str) -> ExchangeRateQuote:
        if self.error:
            raise self.error
        return ExchangeRateQuote(base_currency, quote_currency, Decimal("1.4694"), self.name,
                                 datetime(2026, 11, 13, tzinfo=UTC), self.now)


ASX_PRICES = {"IOZ.AX": "31.42", "NDQ.AX": "48.10", "VEQ.AX": "71.05", "SHL.AX": "25.80", "VAP.AX": "88.60"}


@pytest.fixture
def seeded(session_factory):
    with session_scope(session_factory) as session:
        seed_reference_data(session)
    return session_factory


def service(session_factory, yahoo, fx=None, now=NOW):
    coingecko = FakePriceProvider("coingecko", {"BTC-AUD": "96549.12"})
    fx = fx or FakeRateProvider()
    for provider in (yahoo, coingecko, fx):
        provider.now = now
    return MarketDataService(session_factory, {"yahoo": yahoo, "coingecko": coingecko}, fx, now=lambda: now)


def test_prices_are_stored_with_both_timestamps_and_logged(seeded):
    result = service(seeded, FakePriceProvider("yahoo", ASX_PRICES)).collect_prices("prices.asx")
    assert result.status is RunStatus.SUCCESS and result.stored == 5
    with session_scope(seeded) as session:
        price = session.scalars(select(MarketPrice).where(MarketPrice.provider == "yahoo")).first()
        assert price.market_timestamp == NOW - timedelta(minutes=20) and price.retrieved_at == NOW
        assert price.status is DataStatus.VALID
        log = session.scalars(select(CollectionLog)).one()
        assert (log.job, log.provider, log.status) == ("prices.asx", "yahoo", RunStatus.SUCCESS)


def test_failed_collection_keeps_last_price_as_stale_and_backs_off(seeded):
    service(seeded, FakePriceProvider("yahoo", ASX_PRICES)).collect_prices("prices.asx")
    later = NOW + timedelta(hours=3)
    failing = FakePriceProvider("yahoo", {}, ProviderError("HTTP 503", 503))
    result = service(seeded, failing, now=later).collect_prices("prices.asx")
    assert result.status is RunStatus.FAILED and len(result.failed) == 5
    with session_scope(seeded) as session:
        prices = latest_prices(session)
        statuses = {p.status for p in prices.values() if p.provider == "yahoo"}
        assert statuses == {DataStatus.STALE}
        assert len(session.scalars(select(MarketPrice).where(MarketPrice.provider == "yahoo")).all()) == 5
        log = session.scalars(select(CollectionLog).order_by(CollectionLog.id.desc())).first()
        assert log.status is RunStatus.FAILED and log.http_status == 503
    assert not service(seeded, failing, now=later + timedelta(minutes=10)).is_job_due("prices.asx")
    assert service(seeded, failing, now=later + timedelta(minutes=31)).is_job_due("prices.asx")


def test_missing_symbol_is_partial_and_only_that_price_goes_stale(seeded):
    service(seeded, FakePriceProvider("yahoo", ASX_PRICES)).collect_prices("prices.asx")
    partial = {k: v for k, v in ASX_PRICES.items() if k != "SHL.AX"}
    result = service(seeded, FakePriceProvider("yahoo", partial), now=NOW + timedelta(hours=3)).collect_prices("prices.asx")
    assert result.status is RunStatus.PARTIAL and result.failed == ["SHL.AX"]
    with session_scope(seeded) as session:
        stale = [p for p in latest_prices(session).values() if p.status is DataStatus.STALE]
        assert len(stale) == 1 and stale[0].price == Decimal("25.80")


def test_exchange_rates_for_held_currencies(seeded):
    result = service(seeded, FakePriceProvider("yahoo", {})).collect_exchange_rates()
    assert result.status is RunStatus.SUCCESS and result.stored == 1  # USD for AAPL
    service(seeded, FakePriceProvider("yahoo", {}), fx=FakeRateProvider(ProviderError("HTTP 500", 500)),
            now=NOW + timedelta(days=1)).collect_exchange_rates()
    with session_scope(seeded) as session:
        rate = latest_exchange_rate(session, "USD", "AUD")
        assert rate.rate == Decimal("1.4694") and rate.status is DataStatus.STALE
        assert len(session.scalars(select(ExchangeRate)).all()) == 1


def test_run_due_only_calls_providers_when_due(seeded):
    with session_scope(seeded) as session:
        session.get(AppSetting, "schedule.asx_interval_minutes").value = "60"  # editable from the dashboard
    yahoo = FakePriceProvider("yahoo", {**ASX_PRICES, "AAPL": "230.10"})
    first = {r.job for r in service(seeded, yahoo).run_due_jobs()}
    assert first == {"prices.crypto", "prices.asx", "prices.us", "exchange_rates"}
    calls = yahoo.calls
    assert service(seeded, yahoo, now=NOW + timedelta(minutes=30)).run_due_jobs() == []
    assert yahoo.calls == calls
    due = {r.job for r in service(seeded, yahoo, now=NOW + timedelta(minutes=61)).run_due_jobs()}
    assert due == {"prices.asx"}  # US market closed, BTC and FX intervals not reached
