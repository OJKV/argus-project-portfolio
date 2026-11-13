"""OKX adapter over ccxt (ARCHITECTURE.md §8).

Invariants this module enforces, each traceable to a logged failure:
  * net position mode + isolated margin, one position per symbol;
  * protection is attached AT PLACEMENT via `attachAlgoOrds`, never after the fill (F-003);
  * `clOrdId` comes from (run, instrument, side) and the venue is asked for it — open, filled or
    cancelled — before placing (idempotency, review X-8);
  * every state-changing call is refused unless armed for this account (review S-1);
  * every response is validated before arithmetic — zero/absent qty or price is an error (F-030);
  * sizes and prices go through ccxt precision helpers as `Decimal`, never round() (F-027);
  * fee rates are read from the account endpoint, never hardcoded (F-026, F-036).

MODE=demo routes to OKX's demo environment (`x-simulated-trading: 1` via ccxt sandbox mode).
Nothing in this module may be called from a test without `-m demo` (AGENTS.md rule 1).
"""

from __future__ import annotations

import hashlib
import socket
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any

import ccxt
import structlog

from argus.config import Settings, get_settings
from argus.exchange.base import ExchangeAdapter
from argus.exchange.errors import InstrumentNotListedError, TrimTooSmallError, venue_error
from argus.exchange.reconcile import symbol_key

log = structlog.get_logger(__name__)

SETTLEMENT_CCY = "USDT"


def force_ipv4_for_ccxt() -> None:
    """Pin ccxt's requests/urllib3 stack to IPv4 (F-060).

    A dual-stack host may select IPv6 when an API key is restricted to IPv4. Use a predictable
    address family and verify allowlist configuration for the target environment.
    """
    import urllib3.util.connection as urllib3_conn

    urllib3_conn.allowed_gai_family = lambda: socket.AF_INET


class ExchangeResponseError(RuntimeError):
    """The venue answered with something we refuse to do arithmetic on (F-030)."""


class MutationRefusedError(RuntimeError):
    """A state-changing request was refused before it left the process (review S-1)."""


# State-changing names, compared lowercased with underscores removed so that `create_order`,
# `createOrder`, `create_market_order` and `privatePostTradeOrder` all match (review 2026-10-07).
_MUTATING_PREFIXES = (
    "create",
    "edit",
    "cancel",
    "set",
    "close",
    "transfer",
    "withdraw",
    "privatepost",
    "privateput",
    "privatedelete",
)
_REFUSAL = (
    "refused: this adapter is not armed for the account it points at "
    "(OKX_DEMO=true with the sandbox header, or MODE=auto|approve with "
    "LIVE_TRADING=I_UNDERSTAND)"
)


def _mutating_name(name: str) -> bool:
    return name.lower().replace("_", "").startswith(_MUTATING_PREFIXES)


def _private_write(api: Any, method: Any) -> bool:
    """A request to a private endpoint with anything but a read verb."""
    parts = api if isinstance(api, (list, tuple)) else [api]
    private = any("private" in str(part).lower() for part in parts)
    return private and str(method).upper() not in ("GET", "HEAD")


class GuardedClient:
    """The ccxt client, with every state-changing call gated on `allowed()` (review S-1).

    `config.py` refuses MODE=demo with OKX_DEMO=false, but the adapter held no refusal of its
    own: the only sandbox decision was `set_sandbox_mode` at construction, so anything that built
    Settings without validation could point every order, amend, cancel and close at the live
    account. The gate sits on the client rather than on each adapter method so that a method added
    later - or one owned elsewhere, like `amend_stop` - cannot forget it. Reads pass through:
    refusing them would blind the watchdog and `doctor` without making anything safer.

    Two layers (review 2026-10-07: exact names let `create_market_order` and camelCase raw
    endpoints such as `privatePostTradeOrder` through). Names are refused by prefix, which gives
    the clear early error. And every ccxt call — unified or implicit, any spelling — ends in the
    raw client's `fetch2`, which is wrapped to refuse any non-read request to a private endpoint.
    """

    def __init__(self, client: Any, allowed: Callable[[], bool]) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_allowed", allowed)
        fetch2 = getattr(client, "fetch2", None)
        if callable(fetch2):

            def guarded_fetch2(
                path: Any, api: Any = "public", method: Any = "GET", *args: Any, **kwargs: Any
            ) -> Any:
                if _private_write(api, method) and not allowed():
                    raise MutationRefusedError(f"{method} {path} {_REFUSAL}")
                return fetch2(path, api, method, *args, **kwargs)

            client.fetch2 = guarded_fetch2

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if _mutating_name(name) and not self._allowed():
            raise MutationRefusedError(f"{name} {_REFUSAL}")
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._client, name, value)


class StopWideningError(ValueError):
    """An amend that would not move a live stop towards profit. Refused before it is sent."""


def stop_tightens(side: str, new_stop: Decimal, current: Decimal | None) -> bool:
    """True only when `new_stop` moves a known, positive stop towards profit for `side`.

    An unknown or non-positive current level answers False rather than "anything goes": a stop
    whose level cannot be read cannot be shown to be tightened by anything (review R-1, J-N11 —
    an unreadable trigger used to parse as 0.0, which every positive stop "tightened").
    `side` may be either vocabulary; `venue_side` refuses anything that is neither.
    """
    if current is None or current <= 0 or new_stop <= 0:
        return False
    return new_stop > current if venue_side(side) == "buy" else new_stop < current


def stops_to_amend(
    side: str, new_stop: Decimal, algos: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Every closing-side SL among `algos` (one instrument's), all tightened by `new_stop`.

    A stop already at or beyond `new_stop` is skipped, so a retry after a partial amend moves
    only what is left. Raises StopWideningError, before anything is sent, if `new_stop` would
    widen an unreadable stop or would move none of them.
    The amend used to move the first SL found and return (review X-12); in net mode a position
    built from two entries carries one SL per entry, so the other — possibly the wider — stayed
    live at its old level. All move together or none does. An SL on the opening side would add
    to the position rather than close it, so it is not this position's stop; one whose side is
    not reported counts, as in `is_protected`.
    """
    closing = "sell" if venue_side(side) == "buy" else "buy"
    stops = [
        a
        for a in algos
        if a.get("kind") == "sl" and str(a.get("side") or closing).lower() == closing
    ]
    movers: list[dict[str, Any]] = []
    for algo in stops:
        raw = algo.get("trigger_px")
        current = None if raw in (None, "") else Decimal(str(raw))
        if current is not None and current > 0 and new_stop > 0 and new_stop == current:
            continue  # already there: a retry after a partial amend must not trip on it
        if current is not None and stop_tightens(side, current, new_stop):
            continue  # already tighter than `new_stop`: leave it, it does not widen anything
        if not stop_tightens(side, new_stop, current):
            raise StopWideningError(
                f"{new_stop} does not tighten the live {side} stop {current}"
                f" ({len(stops)} closing-side stop(s) on the position)"
            )
        movers.append(algo)
    if stops and not movers:
        raise StopWideningError(
            f"{new_stop} does not tighten any of the {len(stops)} live {side} stop(s)"
        )
    return movers


def position_row(position: dict[str, Any]) -> dict[str, Any]:
    """One ccxt position mapped to the shape the watchdog and gate expect.

    `margin_ratio` comes from OKX's `mgnRatio` (review X-7). It was never mapped, so the
    watchdog's margin floor read None on every position and the check has never run in
    production. Unknown stays None — "we cannot tell" is not "healthy".
    """
    info = position.get("info") or {}
    ratio = info.get("mgnRatio")
    # ccxt's `timestamp` is OKX's `cTime`. The Reviewer's time stop needs it, and the row never
    # carried it, so `time_stop_breached` could not fire on a real position (review L-3).
    opened = position.get("timestamp")
    return {
        "opened_at": (
            datetime.fromtimestamp(int(opened) / 1000, UTC).isoformat() if opened else None
        ),
        "symbol": position["symbol"],
        "side": position["side"],
        "size": Decimal(str(position.get("contracts") or 0)),
        "entry_price": Decimal(str(position["entryPrice"])),
        "mark_price": Decimal(str(position.get("markPrice") or 0)),
        "leverage": int(float(position.get("leverage") or 1)),
        "liq_price": (
            Decimal(str(position["liquidationPrice"])) if position.get("liquidationPrice") else None
        ),
        "unrealised": Decimal(str(position.get("unrealizedPnl") or 0)),
        # OKX's mgnRatio is a multiple (adjEq / mmr); every floor here is a percent (F-164).
        "margin_ratio": Decimal(str(ratio)) * 100 if ratio not in (None, "") else None,
        # No `raw` (review L-12). It was OKX's whole position object, account and position ids
        # included, and the Reviewer serialises this row into prompts for free LLM providers
        # and Logfire. Every field a consumer reads is mapped above; nothing read `raw`.
    }


def realised_from_history(
    rows: list[dict[str, Any]], inst_id: str, since: datetime
) -> Decimal | None:
    """Sum of `realizedPnl` over `inst_id`'s positions-history rows closed after `since`.

    OKX writes one row per closed position, with `realizedPnl` (price PnL plus fees and funding)
    and `uTime`, the time of its last close. More than one row after `since` is a position closed
    and reopened between two looks: both closes are this account's PnL. No row is None - "no
    record" is not a flat trade - and a figure that is not a finite number is refused.
    """
    floor = since.timestamp() * 1000
    total: Decimal | None = None
    for row in rows:
        if row.get("instId") != inst_id or float(row.get("uTime") or 0) < floor:
            continue
        try:
            pnl = Decimal(str(row.get("realizedPnl")))
        except ArithmeticError as exc:
            raise ExchangeResponseError(f"{inst_id} positions-history: realizedPnl {row}") from exc
        if not pnl.is_finite():
            raise ExchangeResponseError(f"{inst_id} positions-history: realizedPnl {pnl}")
        total = pnl if total is None else total + pnl
    return total


ALGO_ORDER_TYPES = ("conditional", "oco", "trigger", "move_order_stop")
# Every algo ARGUS places is tagged with this prefix, so its venue row can be told from one set
# by hand in the OKX app (review X-12).
ALGO_ID_PREFIX = "argus"
_ALGO_TAGS = {"sl": "sl", "tp": "tp", "trailing": "tr"}


def algo_client_id(kind: str, parent: str | None = None, index: int = 0) -> str:
    """The `algoClOrdId` for an algo ARGUS places — 1..32 letters and digits, as OKX allows.

    Attached to an entry (`parent` is its clOrdId): derived from it and the algo's place in the
    bracket, so a retried bracket carries the same ids and an algo row names the entry it
    protects. Standalone: unique per call — OKX refuses an id already in use (51068), and a
    refused repair stop is a naked position.
    """
    seed = f"{parent}|{kind}|{index}" if parent else uuid.uuid4().hex
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"{ALGO_ID_PREFIX}{_ALGO_TAGS[kind]}{digest}"[:32]


def collect_algo_orders(
    fetch: Callable[[str], list[dict[str, Any]]],
    order_types: Sequence[str] = ALGO_ORDER_TYPES,
) -> list[dict[str, Any]]:
    """Every algo type, or an error — a partial answer is not an answer (review X-1).

    The old loop swallowed a per-type failure and returned the rest. One 429 on the `conditional`
    query made a protected position look unprotected; the watchdog then "repaired" the stop it
    found on the retry by amending it out to 2xATR from entry. A stop trailed into profit came
    back below the entry and Telegram reported success.
    """
    rows: list[dict[str, Any]] = []
    for order_type in order_types:
        try:
            rows.extend(fetch(order_type))
        except Exception as exc:
            raise ExchangeResponseError(
                f"algo-order query failed for ordType={order_type}: {type(exc).__name__}"
            ) from exc
    return rows


class OKXAdapter(ExchangeAdapter):
    name = "okx"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.force_ipv4:
            force_ipv4_for_ccxt()
        key, secret, passphrase = self.settings.okx_credentials
        raw = ccxt.okx(
            {
                "apiKey": key,
                "secret": secret,
                "password": passphrase,
                "enableRateLimit": True,
                "timeout": 20000,
                "hostname": self.settings.okx_host.removeprefix("https://"),
                "options": {"defaultType": "swap"},
            }
        )
        if self.settings.okx_demo:
            raw.set_sandbox_mode(True)
        self.client: Any = GuardedClient(raw, self.mutation_allowed)
        self._markets_loaded = False

    def mutation_allowed(self) -> bool:
        """May this adapter change state on the account it points at? (review S-1)

        Demo: only if the client really carries OKX's `x-simulated-trading: 1` header -
        OKX_DEMO=true is a claim, the header is what the venue sees. Live: only when armed,
        MODE=auto|approve with LIVE_TRADING=I_UNDERSTAND. Evaluated per call, so a settings
        object that skipped validation cannot slip past it.
        """
        if self.settings.okx_demo:
            headers = getattr(self.client, "headers", None) or {}
            return str(headers.get("x-simulated-trading", "")) == "1"
        return self.settings.is_live and self.settings.live_trading == "I_UNDERSTAND"

    # --- internals -------------------------------------------------------
    def _markets(self) -> dict[str, Any]:
        if not self._markets_loaded:
            self.client.load_markets()
            self._markets_loaded = True
        markets: dict[str, Any] = self.client.markets
        return markets

    def _market(self, symbol: str) -> dict[str, Any]:
        market = self._markets().get(symbol)
        if market is None:
            raise InstrumentNotListedError(f"{symbol} is not an OKX market")
        return dict(market)

    def amount_to_precision(self, symbol: str, amount: Decimal) -> Decimal:
        """Per-instrument size rounding via ccxt — never round() (F-027)."""
        self._market(symbol)
        return Decimal(self.client.amount_to_precision(symbol, float(amount)))

    def _tick(self, symbol: str) -> Decimal:
        """The instrument's price tick (OKX `tickSz`; ccxt reports it as precision.price)."""
        raw = (self._market(symbol).get("precision") or {}).get("price")
        return Decimal(str(raw)) if raw not in (None, "") else Decimal("0")

    def price_to_precision(self, symbol: str, price: Decimal) -> Decimal:
        self._market(symbol)
        return Decimal(self.client.price_to_precision(symbol, float(price)))

    @staticmethod
    def _positive(value: Any, field: str, context: str) -> Decimal:
        if value is None:
            raise ExchangeResponseError(f"{context}: {field} missing")
        number = Decimal(str(value))
        if number <= 0:
            raise ExchangeResponseError(f"{context}: {field}={number} is not positive")
        return number

    # --- data ------------------------------------------------------------
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        return list(self.client.fetch_ohlcv(symbol, timeframe, limit=limit))

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        ticker = self.client.fetch_ticker(symbol)
        self._positive(ticker.get("last"), "last", f"ticker {symbol}")
        return dict(ticker)

    def fetch_funding(self, symbol: str) -> dict[str, Any]:
        rate = self.client.fetch_funding_rate(symbol)
        try:
            return {
                "symbol": symbol,
                "funding_rate": Decimal(str(rate.get("fundingRate") or 0)),
                "next_funding_time": rate.get("fundingTimestamp"),
            }
        except (AttributeError, TypeError, ArithmeticError) as exc:
            raise ExchangeResponseError(
                f"funding {symbol}: malformed answer ({type(exc).__name__})"
            ) from exc

    def fetch_open_interest(self, symbol: str) -> dict[str, Any]:
        oi = self.client.fetch_open_interest(symbol)
        try:
            amount = Decimal(str(oi.get("openInterestAmount") or 0))
        except (AttributeError, TypeError, ArithmeticError) as exc:
            raise ExchangeResponseError(
                f"open interest {symbol}: malformed answer ({type(exc).__name__})"
            ) from exc
        return {"symbol": symbol, "open_interest": amount}

    def instrument_spec(self, symbol: str) -> dict[str, Any]:
        """Contract value, minimum size, max leverage and tick — each one present and positive.

        Every field used to fall back to a default (`ctVal or 1`, `lever or 1`, `min or 0`,
        `tick or 0`). On BTC, whose contract is 0.01, a missing `ctVal` sizes 100x (review X-14).
        A missing field is the venue saying something unexpected, and F-030 says refuse. An
        instrument OKX does not list as `live` (suspended, pre-open) is refused the same way.
        """
        market = self._market(symbol)
        info = market.get("info") or {}
        context = f"instrument {symbol}"
        state = info.get("state")
        if state not in (None, "", "live"):
            raise ExchangeResponseError(f"{context}: state is {state}, not live")
        limits = (market.get("limits") or {}).get("amount") or {}
        precision = market.get("precision") or {}
        return {
            "ct_val": self._positive(info.get("ctVal") or None, "ctVal", context),
            "min_size": self._positive(limits.get("min"), "min amount", context),
            "max_leverage": int(self._positive(info.get("lever") or None, "lever", context)),
            "tick": self._positive(precision.get("price"), "tick", context),
            "contract_ccy": info.get("ctValCcy"),
        }

    # --- account ---------------------------------------------------------
    def fetch_balance(self) -> dict[str, Any]:
        """Equity and free margin of the USDT pool — what USDT-margined perps can use (F-047)."""
        try:
            data = self.client.private_get_account_balance()["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ExchangeResponseError(
                f"account balance: malformed answer ({type(exc).__name__})"
            ) from exc
        return {**settlement_balance(data, SETTLEMENT_CCY), "raw": data}

    def fetch_positions(self) -> list[dict[str, Any]]:
        rows = [position_row(p) for p in self.client.fetch_positions()]
        # OKX returns a flat row for every instrument the account has ever touched.
        return [r for r in rows if r["size"] != 0]

    def fetch_open_orders(self) -> list[dict[str, Any]]:
        return [dict(o) for o in self.client.fetch_open_orders()]

    def fetch_algo_orders(self) -> list[dict[str, Any]]:
        """Live conditional/trailing orders — what the watchdog checks a position against."""
        out: list[dict[str, Any]] = []
        for row in collect_algo_orders(
            lambda order_type: list(
                self.client.private_get_trade_orders_algo_pending(
                    {"instType": "SWAP", "ordType": order_type}
                )["data"]
            )
        ):
            out.append(
                {
                    "algo_id": row.get("algoId"),
                    "symbol": row.get("instId"),
                    "kind": _algo_kind(row),
                    "trigger_px": (
                        Decimal(str(row["slTriggerPx"]))
                        if row.get("slTriggerPx")
                        else Decimal(str(row["tpTriggerPx"]))
                        if row.get("tpTriggerPx")
                        else None
                    ),
                    "callback_pct": (
                        Decimal(str(row["callbackRatio"])) if row.get("callbackRatio") else None
                    ),
                    "size": Decimal(str(row.get("sz") or 0)),
                    "state": row.get("state"),
                    # What `is_protected` needs to tell a stop from a look-alike (review X-9).
                    "side": row.get("side") or None,
                    "close_fraction": row.get("closeFraction") or None,
                    "active_px": row.get("activePx") or None,
                    "algo_cl_ord_id": row.get("algoClOrdId") or None,
                    "raw": row,
                }
            )
        return out

    def fetch_realised_pnl(self, symbol: str, since: datetime) -> Decimal | None:
        """The venue's booked PnL for `symbol`'s closes after `since` (positions-history)."""
        inst_id = str(self._market(symbol)["id"])
        response = self.client.private_get_account_positions_history(
            {"instType": "SWAP", "instId": inst_id, "limit": "20"}
        )
        return realised_from_history(list(response["data"]), inst_id, since)

    def fetch_fee_rates(self, symbol: str) -> dict[str, Decimal]:
        """From the account endpoint. OKX reports charges as negative numbers (F-036)."""
        market = self._market(symbol)
        data = self.client.private_get_account_trade_fee(fee_rate_params(market))["data"][0]
        return parse_fee_rates(data, settle=str(market.get("settle") or "USDT"))

    def set_leverage(self, symbol: str, leverage: int) -> None:
        max_lev = self.instrument_spec(symbol)["max_leverage"]
        if not 1 <= leverage <= max_lev:
            raise ValueError(f"leverage {leverage} outside 1..{max_lev} for {symbol}")
        self.client.set_leverage(leverage, symbol, params={"mgnMode": "isolated"})

    # --- execution -------------------------------------------------------
    def place_bracket(self, order: dict[str, Any]) -> dict[str, str]:
        """Entry + attached SL/TP/trailing in one request. Idempotent on clOrdId.

        The venue is asked for the client id before anything is sent (review X-8). Scanning
        `fetch_open_orders()` missed every order that had already filled — a market entry is
        never in the open book — so a retry after a timeout the venue had accepted placed the
        entry twice, with a second set of stops. The lookup covers filled and cancelled orders.
        """
        symbol = order["symbol"]
        cl_ord_id = order["cl_ord_id"]
        if not cl_ord_id or len(cl_ord_id) > 32:
            raise ValueError("clOrdId must be 1..32 alphanumeric characters")
        existing = self.fetch_order_by_client_id(symbol, cl_ord_id)
        if existing is not None:
            return {
                "entry_id": str(existing["id"]),
                "idempotent": "true",
                "status": str(existing.get("status") or ""),
            }
        # Triggers on the instrument's tick, a stop only ever towards the entry (review X-N7).
        order = round_attached_triggers(order, self._tick(symbol))
        params = build_bracket_params(order)
        size = float(self.amount_to_precision(symbol, Decimal(str(order["size"]))))
        price = (
            float(self.price_to_precision(symbol, Decimal(str(order["price"]))))
            if order.get("price") is not None
            else None
        )
        response = self.client.create_order(
            symbol=symbol,
            type=order.get("order_type", "limit"),
            side=venue_side(order["side"]),
            amount=size,
            price=price,
            params=params,
        )
        if not response.get("id"):
            raise ExchangeResponseError(f"create_order returned no id: {response}")
        if response.get("symbol") not in (None, symbol):
            raise ExchangeResponseError(
                f"create_order echoed {response.get('symbol')} for a {symbol} order"
            )
        return {"entry_id": str(response["id"]), "raw": response}

    def amend_stop(self, symbol: str, new_stop: Decimal) -> str:
        """Move the live SL towards profit, or place one where there is none. Never widens.

        Tighten-only here as well as in the Reviewer (review R-1 / X-1): the watchdog calls this
        too, and the venue call is the one place every caller passes through. The side comes from
        the venue's own position, never from the caller. OKX amends the algo order rather than
        replacing it, and an amend it did not confirm with `sCode == "0"` is an error, not a
        moved stop — the response used to be discarded (X-12).

        Every closing-side SL on the position moves, not just the first (`stops_to_amend`).
        Each is attempted even after one is refused — a refusal must not keep the others wide —
        and any refusal is then an error naming the stop that did not move. Returns the amended
        algo ids, comma-separated.
        """
        stop = self.price_to_precision(symbol, new_stop)
        position = self._require_position(symbol)
        inst_id = self._market(symbol)["id"]
        try:
            stops = stops_to_amend(
                str(position["side"]),
                stop,
                [a for a in self.fetch_algo_orders() if a["symbol"] == inst_id],
            )
        except StopWideningError as exc:
            raise StopWideningError(f"{symbol}: {exc}") from None
        if not stops:
            return self.place_stop(symbol, new_stop)
        refused: list[str] = []
        for algo in stops:
            try:
                response = self.client.private_post_trade_amend_algos(
                    {"instId": inst_id, "algoId": algo["algo_id"], "newSlTriggerPx": str(stop)}
                )
            except ccxt.BaseError as exc:
                refused.append(f"{algo['algo_id']}: {venue_error(exc)}")
                continue
            data = (response.get("data") or [{}])[0] or {}
            if str(data.get("sCode")) != "0":
                refused.append(
                    f"{algo['algo_id']}: sCode={data.get('sCode')}, sMsg={data.get('sMsg')}"
                )
        if refused:
            raise ExchangeResponseError(
                f"{symbol}: stop amend not confirmed for {len(refused)} of {len(stops)}"
                f" stop(s) ({'; '.join(refused)})"
            )
        return ",".join(str(a["algo_id"]) for a in stops)

    def place_stop(self, symbol: str, stop: Decimal) -> str:
        """A new market SL that closes the whole position, beside any stop already there."""
        return self._place_algo(
            symbol,
            {
                "slTriggerPx": str(self.price_to_precision(symbol, stop)),
                "slOrdPx": "-1",
                "slTriggerPxType": "mark",
            },
            "conditional",
        )

    def place_trailing(self, symbol: str, callback_pct: Decimal, activation: Decimal | None) -> str:
        params: dict[str, Any] = {"callbackRatio": str(callback_pct / Decimal(100))}
        if activation is not None:
            params["activePx"] = str(self.price_to_precision(symbol, activation))
        return self._place_algo(symbol, params, "move_order_stop")

    def _place_algo(self, symbol: str, extra: dict[str, Any], ord_type: str) -> str:
        position = self._require_position(symbol)
        body = {
            "instId": self._market(symbol)["id"],
            "tdMode": "isolated",
            "side": "sell" if position["side"] == "long" else "buy",
            "ordType": ord_type,
            "reduceOnly": "true",
            "algoClOrdId": algo_client_id("trailing" if ord_type == "move_order_stop" else "sl"),
            **extra,
        }
        if ord_type in ("conditional", "oco"):
            # Close whatever the position is when the stop fires (review X-15). A fixed `sz` was
            # the size at repair time: wrong after a partial close, an orphan after a full one.
            # OKX accepts closeFraction for conditional/oco with a market exit in net mode.
            body["closeFraction"] = "1"
        else:
            body["sz"] = str(position["size"])
        data = self.client.private_post_trade_order_algo(body)["data"][0]
        if data.get("sCode") not in ("0", 0, None):
            raise ExchangeResponseError(f"algo rejected: {data}")
        return str(data["algoId"])

    def partial_close(self, symbol: str, pct: int) -> str:
        if not 0 < pct < 100:
            raise ValueError("partial_close pct must be in (0, 100)")
        position = self._require_position(symbol)
        wanted = position["size"] * Decimal(pct) / Decimal(100)
        try:
            size = self.amount_to_precision(symbol, wanted)
        except ccxt.InvalidOrder as exc:  # ccxt refuses an amount below the lot before we can
            raise TrimTooSmallError(
                f"{pct}% of {position['size']} is below the minimum size"
            ) from exc
        if size <= 0:
            raise TrimTooSmallError(f"{pct}% of {position['size']} is below the minimum size")
        response = self.client.create_order(
            symbol=symbol,
            type="market",
            side="sell" if position["side"] == "long" else "buy",
            amount=float(size),
            params={"tdMode": "isolated", "reduceOnly": True},
        )
        remaining = position["size"] - size
        if remaining > 0:
            self._resize_algos(symbol, remaining)
        return str(response["id"])

    def _resize_algos(self, symbol: str, remaining: Decimal) -> None:
        """Shrink every sized stop/target on the instrument to the position left (review X-N9).

        A trim reduced the position and left each stop at its original `sz` — still counted as
        protection, though whether OKX executes a reduce-only stop larger than the position is
        not a bet to make with the position. Stops with `closeFraction: "1"` already track the
        position, and smaller split targets are left alone. Trailing stops cannot be amended on
        OKX. A failure is logged, not raised: the trim itself went through, and an oversized stop
        still covers what is left.
        """
        inst = self._market(symbol)["id"]
        try:
            algos = self.fetch_algo_orders()
        except Exception as exc:  # noqa: BLE001 - see docstring; the reduce has already gone out
            log.error("okx.resize_after_trim_failed", symbol=symbol, error=venue_error(exc))
            return
        for algo in algos:
            if algo["symbol"] != inst or algo["kind"] not in ("sl", "tp"):
                continue
            if str(algo.get("close_fraction") or "") == "1" or algo["size"] <= remaining:
                continue
            try:
                data = self.client.private_post_trade_amend_algos(
                    {"instId": inst, "algoId": algo["algo_id"], "newSz": str(remaining)}
                )["data"][0]
                if data.get("sCode") not in ("0", 0, None):
                    raise ExchangeResponseError(f"amend refused: sCode {data.get('sCode')}")
            except Exception as exc:  # noqa: BLE001 - see docstring
                log.error(
                    "okx.resize_after_trim_failed",
                    symbol=symbol,
                    algo_id=algo["algo_id"],
                    error=venue_error(exc),
                )

    def close(self, symbol: str) -> str:
        self._require_position(symbol)
        response = self.client.private_post_trade_close_position(
            {"instId": self._market(symbol)["id"], "mgnMode": "isolated", "autoCxl": "true"}
        )
        return str(response.get("data", [{}])[0].get("clOrdId") or "closed")

    def fetch_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        """The venue's own verdict on one order (ccxt unified: `status`, `filled`).

        Raises LookupError when OKX no longer holds it (51603) — the adapter contract, shared with
        the fake. OKX keeps an unfilled cancelled order for only two hours and a filled one far
        longer, so for an order that is also absent from the book this means "cancelled, nothing
        filled". Callers decide what to make of that; the adapter only translates.
        """
        try:
            return dict(self.client.fetch_order(order_id, symbol))
        except ccxt.OrderNotFound as exc:
            raise LookupError(f"venue does not hold order {order_id}: {exc}") from exc

    def fetch_order_by_client_id(self, symbol: str, cl_ord_id: str) -> dict[str, Any] | None:
        """`GET /trade/order?clOrdId=` — open, filled or cancelled. None on 51603 only."""
        try:
            return dict(self.client.fetch_order(None, symbol, {"clOrdId": cl_ord_id}))
        except ccxt.OrderNotFound:
            return None

    def cancel_order(self, symbol: str, order_id: str) -> None:
        """One order, by id. Expiring an entry must never touch its neighbours on the symbol."""
        self.client.cancel_order(order_id, symbol)

    def cancel_all(self, symbol: str) -> None:
        for order in self.fetch_open_orders():
            if order.get("symbol") == symbol:
                self.client.cancel_order(order["id"], symbol)

    def reconcile(self) -> dict[str, Any]:
        """Exchange is truth (F-006). Positions, the unprotected ones, and orphan algos.

        "Protected" is `is_protected` — a stop on the closing side, covering the position,
        or a trailing stop that is live from placement (review X-9). Algos on an instrument with
        no position are reported as `orphan_algos`, never cancelled here: an attached stop can
        exist a moment before its entry's fill is visible, and removing protection on a guess is
        the one mistake this run must not make.
        """
        positions = self.fetch_positions()
        algos = self.fetch_algo_orders()
        by_inst: dict[str, list[dict[str, Any]]] = {}
        for algo in algos:
            by_inst.setdefault(symbol_key(str(algo["symbol"])), []).append(algo)
        held: set[str] = set()
        unprotected: list[str] = []
        for position in positions:
            inst = symbol_key(str(self._market(position["symbol"])["id"]))
            held.add(inst)
            if not is_protected(position, by_inst.get(inst, [])):
                unprotected.append(position["symbol"])
        return {
            "positions": positions,
            "algo_orders": algos,
            "open_orders": self.fetch_open_orders(),
            "unprotected": unprotected,
            "orphan_algos": [a for a in algos if symbol_key(str(a["symbol"])) not in held],
        }

    def _require_position(self, symbol: str) -> dict[str, Any]:
        for position in self.fetch_positions():
            if position["symbol"] == symbol:
                return position
        raise ExchangeResponseError(f"no open position on {symbol}")


def settlement_balance(data: dict[str, Any], ccy: str) -> dict[str, Any]:
    """Equity and free margin in one settlement currency (FAILURE_LOG F-047).

    OKX's `totalEq` values every coin in the account. In Single-currency margin mode only the
    settlement currency backs a USDT-margined perp, so sizing and the drawdown kill-switch must
    read that currency's `eq` (includes unrealised PnL) and `availEq` (excludes margin in use).
    `totalEq` is kept for display only.
    """
    details = data.get("details")
    if not isinstance(details, list):
        raise ExchangeResponseError("balance response has no per-currency details")
    row = next((d for d in details if d.get("ccy") == ccy), None)
    total = data.get("totalEq")
    out: dict[str, Any] = {
        "currency": ccy,
        "account_total_usd": Decimal(str(total)) if total not in (None, "") else None,
    }
    # F-030 and review X-3: zero is an error, never a reading. A payload that omits the
    # settlement currency used to return equity 0, which the drawdown kill-switch read as a 100%
    # loss from the 30-day peak — one glitchy response would flatten the account and write HALT.
    if row is None:
        raise ExchangeResponseError(f"balance response has no {ccy} row")
    if row.get("eq") in (None, ""):
        raise ExchangeResponseError(f"{ccy} balance row has an empty eq")
    if Decimal(str(row["eq"])) <= 0:
        raise ExchangeResponseError(f"{ccy} equity is {row['eq']} — refusing to size or judge on 0")
    free_raw = row.get("availEq") or row.get("availBal") or "0"
    return {**out, "equity": Decimal(str(row["eq"])), "free": Decimal(str(free_raw))}


def fee_rate_params(market: dict[str, Any]) -> dict[str, str]:
    """Params for `GET /account/trade-fee` (FAILURE_LOG F-041).

    Derivatives are keyed by instrument *family* (`BTC-USDT`); sending `instId` with
    `instType=SWAP` returns code 50016 "instId and instType don't match".
    """
    inst_id = str(market["id"])
    kind = str(market.get("type", "swap")).lower()
    if kind == "spot":
        return {"instType": "SPOT", "instId": inst_id}
    family = market.get("info", {}).get("instFamily") or inst_id.rsplit("-", 1)[0]
    return {"instType": kind.upper(), "instFamily": str(family)}


def parse_fee_rates(row: dict[str, Any], settle: str) -> dict[str, Decimal]:
    """Read the rate fields that apply to the contract's settlement currency (F-042).

    OKX returns USDT-margined rates in `makerU`/`takerU`, USDC-margined in `makerUSDC`/
    `takerUSDC`, and coin-margined in `maker`/`taker` — leaving the others empty. An empty
    field is an error, never a zero fee, and a positive taker rate means the sign
    convention changed (F-036).
    """
    suffix = {"USDT": "U", "USDC": "USDC"}.get(settle.upper(), "")
    rates: dict[str, Decimal] = {}
    for side in ("maker", "taker"):
        field = f"{side}{suffix}"
        raw = row.get(field)
        if raw in (None, ""):
            raise ExchangeResponseError(
                f"fee field {field} is empty for {settle}-settled contracts"
            )
        rates[side] = Decimal(str(raw))
    if rates["taker"] > 0:
        raise ExchangeResponseError(
            f"taker fee {rates['taker']} is positive — OKX charges are negative; "
            "refusing to size on it"
        )
    return rates


def is_protected(position: dict[str, Any], algos: list[dict[str, Any]]) -> bool:
    """Does at least one of these algos (all on the position's instrument) protect it?

    Review X-9: any SL or trailing algo on the instrument used to count. Now an algo counts only
    if it would actually close this position on the way down:

    * its side, when reported, is the closing side (sell for a long, buy for a short) — the
      other side would add to the position;
    * together they cover the position: one that closes the whole position
      (`closeFraction: "1"`, or no size reported) is enough, otherwise the reported sizes of the
      qualifying algos are summed. In net mode a second same-side entry adds to the position with
      its own attached SL sized for that entry; two SLs of 5 cover a position of 10 (review
      2026-10-07). One SL for half the position, alone, leaves half naked;
    * a trailing stop counts only without an activation price: one with `activePx` may not have
      activated yet, and before it does it protects nothing. A position whose only cover is
      such a trailing stop is reported unprotected, and the repair adds a plain stop beside it.

    Unreported side or size is not treated as wrong: attached algos may omit them, and calling
    a protected position naked makes the watchdog act on a stop that is fine.
    """
    long = str(position.get("side", "")).lower() in ("long", "buy")
    closing = "sell" if long else "buy"
    size = Decimal(str(position.get("size") or 0))
    covered = Decimal("0")
    found = False
    for algo in algos:
        kind = algo.get("kind")
        if kind not in ("sl", "trailing"):
            continue
        side = str(algo.get("side") or "").lower()
        if side and side != closing:
            continue
        if kind == "trailing" and algo.get("active_px") not in (None, "", 0):
            continue
        algo_size = Decimal(str(algo.get("size") or 0))
        if str(algo.get("close_fraction") or "") == "1" or algo_size <= 0:
            return True
        covered += algo_size
        found = True
    return found and covered >= size


def _algo_kind(row: dict[str, Any]) -> str:
    if row.get("callbackRatio"):
        return "trailing"
    if row.get("slTriggerPx"):
        return "sl"
    if row.get("tpTriggerPx"):
        return "tp"
    return "unknown"


def venue_side(side: str) -> str:
    """`long`/`short` or `buy`/`sell` in, always `buy`/`sell` out.

    This existed as `"buy" if order["side"] == "long" else "sell"`, and `build_entry_order` had
    already converted the plan's `long` into `buy` — so `"buy" == "long"` was False and **every
    long was sent to the venue as a SELL** (F-137). OKX caught it only indirectly, rejecting the
    attached stop with 51049 because a long's stop sits on the wrong side of a short.

    Two vocabularies meeting at one boundary is the hazard; refusing anything that is not clearly
    one of them is the fix. A silent default here is how a long becomes a short.
    """
    normalised = side.strip().lower()
    if normalised in ("long", "buy"):
        return "buy"
    if normalised in ("short", "sell"):
        return "sell"
    raise ValueError(f"unrecognised side {side!r}: expected long/short or buy/sell")


def _on_tick(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return ((price / tick).to_integral_value(rounding=rounding) * tick).quantize(tick)


def round_attached_triggers(order: dict[str, Any], tick: Decimal) -> dict[str, Any]:
    """The order with every attached trigger on the instrument's tick (review X-N7, X-13).

    Triggers went out as `str(trigger_px)` straight from the plan, whose stop is an LLM float
    (`69999.94999999999`). OKX either rejects an off-tick trigger — and the entry with it — or
    rounds it itself, possibly wider than the level the Risk Gate approved. So rounding happens
    here, in a direction chosen for safety:

    * stop-loss: towards the entry (a long's up, a short's down) — tighter, never wider. If that
      lands on or past the entry the order is refused rather than sent with a stop it cannot use;
    * take-profit: towards the entry — it never asks for more than was planned;
    * trailing activation: nearest tick.

    Pure: the order is copied, never mutated, so the journalled request stays what was approved.
    """
    if tick <= 0:
        raise ValueError(f"no usable price tick ({tick}) for {order.get('symbol')}")
    long = venue_side(str(order["side"])) == "buy"
    towards_entry_sl = ROUND_CEILING if long else ROUND_FLOOR
    towards_entry_tp = ROUND_FLOOR if long else ROUND_CEILING
    price = Decimal(str(order["price"])) if order.get("price") is not None else None
    rounded: list[dict[str, Any]] = []
    for algo in order.get("attach_algos", []):
        algo = dict(algo)
        if algo.get("trigger_px") is not None:
            raw = Decimal(str(algo["trigger_px"]))
            kind = algo.get("kind")
            mode = (
                towards_entry_sl
                if kind == "sl"
                else towards_entry_tp
                if kind == "tp"
                else ROUND_HALF_EVEN
            )
            algo["trigger_px"] = _on_tick(raw, tick, mode)
            if kind == "sl" and price is not None:
                stop = algo["trigger_px"]
                if (long and stop >= price) or (not long and stop <= price):
                    raise ValueError(
                        f"stop {raw} rounds to {stop}, on or past the entry {price} at tick {tick}"
                    )
        rounded.append(algo)
    return {**order, "attach_algos": rounded}


def build_bracket_params(order: dict[str, Any]) -> dict[str, Any]:
    """OKX create_order params for an entry with attached protection (DECISION_LOGIC.md §7).

    Pure function so the payload shape is unit-testable without a network call.
    """
    attach: list[dict[str, Any]] = []
    for index, algo in enumerate(order.get("attach_algos", [])):
        kind = algo["kind"]
        if kind == "sl":
            attach.append(
                {
                    "slTriggerPx": str(algo["trigger_px"]),
                    "slOrdPx": "-1",  # market on trigger
                    "slTriggerPxType": "mark",  # mark price, not last (avoids wick hunts)
                }
            )
        elif kind == "tp":
            entry = {"tpTriggerPx": str(algo["trigger_px"]), "tpOrdPx": "-1"}
            if algo.get("size") is not None:
                entry["sz"] = str(algo["size"])
            attach.append(entry)
        elif kind == "trailing":
            entry = {"callbackRatio": str(Decimal(str(algo["callback_pct"])) / Decimal(100))}
            if algo.get("trigger_px") is not None:
                entry["activePx"] = str(algo["trigger_px"])
            attach.append(entry)
        else:
            raise ValueError(f"unknown attached algo kind {kind!r}")
        # OKX re-posts it as the algo's `algoClOrdId` once the entry fills (review X-12).
        attach[-1]["attachAlgoClOrdId"] = algo_client_id(kind, order["cl_ord_id"], index)
    if not any("slTriggerPx" in a for a in attach):
        raise ValueError("every bracket must attach a stop-loss (ARCHITECTURE.md §8)")
    params: dict[str, Any] = {
        "tdMode": "isolated",
        "clOrdId": order["cl_ord_id"],
        "posSide": "net",
    }
    if order.get("post_only"):
        params["ordType"] = "post_only"
    if attach:
        params["attachAlgoOrds"] = attach
    return params
