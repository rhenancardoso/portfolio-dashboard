"""Read-only client for the Moomoo trade API (through OpenD).

Only query calls are reachable: the SDK context is wrapped so that order
placement, modification, cancellation and trade unlocking cannot be called.
Calls are rate limited per the documented limits and retried on failure.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from app.integrations.moomoo.authentication import OpenDSettings, configure_sdk

logger = logging.getLogger(__name__)

RET_OK = 0

# The only SDK methods the application may use.
READ_ONLY_METHODS = frozenset({
    "get_acc_list",
    "accinfo_query",
    "position_list_query",
    "history_deal_list_query",
    "order_fee_query",
    "get_acc_cash_flow",
    "set_sync_query_connect_timeout",
    "close",
})

# (max calls, period in seconds) per account, from the Moomoo API documentation.
# get_acc_list has no documented limit; a conservative one is used.
RATE_LIMITS: dict[str, tuple[int, float]] = {
    "get_acc_list": (10, 30.0),
    "accinfo_query": (10, 30.0),
    "position_list_query": (10, 30.0),
    "history_deal_list_query": (10, 30.0),
    "order_fee_query": (10, 30.0),
    "get_acc_cash_flow": (20, 30.0),
}

MAX_ORDERS_PER_FEE_QUERY = 400

Row = dict[str, Any]


class MoomooApiError(RuntimeError):
    """A Moomoo API call failed after all retries."""


class ReadOnlyContext:
    """Proxy exposing only read-only SDK methods."""

    def __init__(self, context: Any) -> None:
        self._context = context

    def __getattr__(self, name: str) -> Any:
        if name not in READ_ONLY_METHODS:
            raise PermissionError(f"'{name}' is not a permitted read-only Moomoo call")
        return getattr(self._context, name)


class RateLimiter:
    """Sliding-window limiter: at most ``max_calls`` per ``period`` seconds."""

    def __init__(
        self,
        max_calls: int,
        period: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_calls = max_calls
        self._period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self._period:
                self._calls.popleft()
            if len(self._calls) < self._max_calls:
                self._calls.append(now)
                return
            self._sleep(self._period - (now - self._calls[0]))


def _create_sdk_context(settings: OpenDSettings) -> Any:
    """Connect to OpenD, failing within the connect timeout.

    The SDK constructor retries the connection forever, so OpenD is checked first
    and construction runs in a daemon thread with a deadline as a backstop.
    """
    try:
        socket.create_connection(
            (settings.host, settings.port), timeout=settings.connect_timeout_seconds
        ).close()
    except OSError as exc:
        raise MoomooApiError(
            f"OpenD is not reachable at {settings.host}:{settings.port} ({exc}); "
            "start OpenD and log in to your moomoo ID"
        ) from exc

    configure_sdk(settings)
    from moomoo import OpenSecTradeContext, TrdMarket

    created: list[Any] = []
    errors: list[BaseException] = []

    def construct() -> None:
        try:
            created.append(OpenSecTradeContext(
                filter_trdmarket=TrdMarket.NONE,
                host=settings.host,
                port=settings.port,
                security_firm=settings.security_firm,
            ))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=construct, name="moomoo-connect", daemon=True)
    worker.start()
    worker.join(settings.connect_timeout_seconds)
    if errors:
        raise MoomooApiError(f"Could not connect to OpenD: {errors[0]}") from errors[0]
    if not created:
        raise MoomooApiError(
            f"OpenD at {settings.host}:{settings.port} did not complete the connection "
            f"within {settings.connect_timeout_seconds}s"
        )
    return created[0]


class MoomooClient:
    """Read-only Moomoo queries returning plain rows (dicts).

    ``context_factory`` builds the SDK trade context; tests pass a fake one.
    """

    def __init__(
        self,
        settings: OpenDSettings,
        *,
        context_factory: Callable[[OpenDSettings], Any] = _create_sdk_context,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 3,
        retry_delay_seconds: float = 2.0,
    ) -> None:
        self.settings = settings
        self._context_factory = context_factory
        self._clock = clock
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._retry_delay_seconds = retry_delay_seconds
        self._context: ReadOnlyContext | None = None
        self._limiters: dict[tuple[str, int], RateLimiter] = {}

    def __enter__(self) -> MoomooClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None

    @property
    def trading_environment(self) -> str:
        return self.settings.trading_environment

    def _get_context(self) -> ReadOnlyContext:
        if self._context is None:
            context = ReadOnlyContext(self._context_factory(self.settings))
            # Without this the SDK waits forever when OpenD is unreachable.
            context.set_sync_query_connect_timeout(self.settings.connect_timeout_seconds)
            self._context = context
        return self._context

    def _limiter(self, method: str, account_id: int) -> RateLimiter:
        key = (method, account_id)
        if key not in self._limiters:
            max_calls, period = RATE_LIMITS[method]
            self._limiters[key] = RateLimiter(max_calls, period, self._clock, self._sleep)
        return self._limiters[key]

    def _call(self, method: str, account_id: int = 0, **kwargs: Any) -> list[Row]:
        last_error = ""
        for attempt in range(1, self._max_attempts + 1):
            self._limiter(method, account_id).acquire()
            try:
                ret, data = getattr(self._get_context(), method)(**kwargs)
            except PermissionError:
                raise
            except Exception as exc:  # SDK raises on socket and protocol errors
                ret, data = -1, f"{type(exc).__name__}: {exc}"
            if ret == RET_OK:
                return data.to_dict("records") if hasattr(data, "to_dict") else list(data)
            last_error = str(data)
            logger.warning("Moomoo %s failed (attempt %d/%d): %s",
                           method, attempt, self._max_attempts, last_error)
            if attempt < self._max_attempts:
                self._sleep(self._retry_delay_seconds * 2 ** (attempt - 1))
        raise MoomooApiError(f"{method} failed: {last_error}")

    def list_accounts(self) -> list[Row]:
        return self._call("get_acc_list")

    def get_account_info(self, account_id: int, currency: str = "AUD") -> list[Row]:
        return self._call(
            "accinfo_query", account_id,
            trd_env=self.trading_environment, acc_id=account_id,
            refresh_cache=True, currency=currency,
        )

    def get_positions(self, account_id: int) -> list[Row]:
        return self._call(
            "position_list_query", account_id,
            trd_env=self.trading_environment, acc_id=account_id, refresh_cache=True,
        )

    def get_history_deals(self, account_id: int, start: str, end: str) -> list[Row]:
        """Executed fills between ``start`` and ``end`` ("YYYY-MM-DD HH:MM:SS")."""
        return self._call(
            "history_deal_list_query", account_id,
            start=start, end=end, trd_env=self.trading_environment, acc_id=account_id,
        )

    def get_order_fees(self, account_id: int, order_ids: list[str]) -> list[Row]:
        rows: list[Row] = []
        for offset in range(0, len(order_ids), MAX_ORDERS_PER_FEE_QUERY):
            rows += self._call(
                "order_fee_query", account_id,
                order_id_list=order_ids[offset:offset + MAX_ORDERS_PER_FEE_QUERY],
                trd_env=self.trading_environment, acc_id=account_id,
            )
        return rows

    def get_cash_flows(self, account_id: int, clearing_date: str) -> list[Row]:
        """Cash flows for one clearing date ("YYYY-MM-DD"); the API serves one day per call."""
        return self._call(
            "get_acc_cash_flow", account_id,
            clearing_date=clearing_date, trd_env=self.trading_environment, acc_id=account_id,
        )
