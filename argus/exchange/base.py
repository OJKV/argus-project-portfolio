"""ExchangeAdapter interface (ARCHITECTURE.md §8). OKX first; Bybit KZ data now, execution later."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any


class ExchangeAdapter(ABC):
    name: str

    # --- data ---
    @abstractmethod
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]: ...
    @abstractmethod
    def fetch_ticker(self, symbol: str) -> dict[str, Any]: ...
    @abstractmethod
    def fetch_funding(self, symbol: str) -> dict[str, Any]: ...
    @abstractmethod
    def fetch_open_interest(self, symbol: str) -> dict[str, Any]: ...
    @abstractmethod
    def instrument_spec(self, symbol: str) -> dict[str, Any]: ...

    # --- account & execution (execution adapters only) ---
    @abstractmethod
    def fetch_balance(self) -> dict[str, Any]: ...
    @abstractmethod
    def fetch_positions(self) -> list[dict[str, Any]]: ...
    @abstractmethod
    def fetch_open_orders(self) -> list[dict[str, Any]]: ...
    @abstractmethod
    def fetch_algo_orders(self) -> list[dict[str, Any]]: ...

    def fetch_realised_pnl(self, symbol: str, since: datetime) -> Decimal | None:
        """Realised PnL the venue booked for closes of `symbol` after `since`, or None.

        A close raises no event (F-132): the position just stops appearing, up to a watchdog
        tick after the last snapshot. Its worth is what the venue booked, not the last unrealised
        we saw. An adapter that cannot say returns None, and the journal's reader falls back to
        the loss at the plan stop - never to the stale unrealised.
        """
        return None

    @abstractmethod
    def fetch_fee_rates(self, symbol: str) -> dict[str, Decimal]: ...
    @abstractmethod
    def set_leverage(self, symbol: str, leverage: int) -> None: ...
    @abstractmethod
    def place_bracket(self, order: dict[str, Any]) -> dict[str, str]: ...
    @abstractmethod
    def amend_stop(self, symbol: str, new_stop: Decimal) -> str: ...
    @abstractmethod
    def place_stop(self, symbol: str, stop: Decimal) -> str:
        """Add a stop that closes the whole position; never touch the stops already there.

        The watchdog's repair (review 2026-10-07): amending "the" SL moves the first one found,
        which in net mode can be another entry's stop — and can widen it.
        """

    @abstractmethod
    def place_trailing(
        self, symbol: str, callback_pct: Decimal, activation: Decimal | None
    ) -> str: ...
    @abstractmethod
    def partial_close(self, symbol: str, pct: int) -> str: ...
    @abstractmethod
    def close(self, symbol: str) -> str: ...
    @abstractmethod
    def cancel_all(self, symbol: str) -> None: ...

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str) -> None: ...

    @abstractmethod
    def fetch_order(self, symbol: str, order_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def fetch_order_by_client_id(self, symbol: str, cl_ord_id: str) -> dict[str, Any] | None:
        """The venue's copy of the order we sent with this client id, or None if it holds none.

        Covers filled and cancelled orders, not just the open book (review X-8, X-N5): it is
        how a retry or a timed-out placement learns whether the venue already has the order.
        Raises on any other failure — "could not ask" is not "not there".
        """

    @abstractmethod
    def reconcile(self) -> dict[str, Any]: ...
