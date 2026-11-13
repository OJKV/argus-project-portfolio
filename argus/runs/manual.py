"""MANUAL trade — one deliberate order on the DEMO account, placed by the production path.

CLAUDE.md non-negotiable #1 forbids a manual order from a script, with one exception added on
2026-09-24: owner may ask, in his own words and in that session, for a single manual trade.
This module
is the mechanism for that exception and is built to be no wider than it:

* **Demo only, armed only.** `refusal()` is the first thing the CLI calls, before an adapter
  exists. The live account is refused whatever else is set; `argus.config` already refuses
  `MODE=auto|approve` without `LIVE_TRADING=I_UNDERSTAND`, and this checks the same facts again.
* **The smallest size the instrument allows**, read from the venue's own instrument list (OKX
  `minSz`), never a number in this file. Market entry, isolated margin, 2x.
* **The stop is not written here.** The order is built by `build_entry_order` and sent by
  `place_approved` -> `place_bracket` -> `build_bracket_params`, all unchanged: the stop travels
  with the entry in `attachAlgoOrds`, so the venue refuses an entry it cannot protect (#4), and a
  refusal leaves no position and a REJECTED order row. Nothing here places a stop of its own.
* **Journalled as a manager-placed order is.** A `manual` run row tagged with the venue
  (`runs.config_hash`, F-131/F-149), a plan, a gate row and the order itself with the idempotent
  `clOrdId`. That is what makes the position OURS to the watchdog (`plan_stops_from_journal`,
  `journal_positions`), to reconcile and to the Reviewer, instead of an orphan.

What it deliberately does NOT do is consult the Risk Gate: the size is the instrument's minimum, and
no gate threshold was ever meant to judge a one-lot keep-alive. The gate row says so in its reasons
(`manual`), so the bypass is on the record rather than implied.

Instruments are spelled the way the OKX adapter's market table is keyed (`BTC/USDT:USDT`, ccxt's
unified symbol) - the spelling `tests/exchange/test_okx_demo.py` proves end to end.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import Engine

from argus.config import Settings
from argus.exchange.okx import round_attached_triggers, venue_side
from argus.exchange.reconcile import asset_of
from argus.execution.place import place_approved
from argus.gate.rules import FactorScores, GateReason, GateResult, PlanInput, Side, Target
from argus.gate.sizing import SizingResult
from argus.journal import ledger
from argus.journal.db import new_id

log = structlog.get_logger(__name__)

LEVERAGE = 2
MARGIN_MODE = "isolated"
# A manual trade is a small thing held for a while; the scheduled Reviewer manages it like any other
# position and a time stop is part of every plan it reads.
TIME_STOP_HOURS = 72
# The watchdog throws away a plan stop further than half the liquidation distance (25 % at 2x) and
# falls back to the ATR stop, so a stop beyond that would not be the one that gets repaired.
MAX_STOP_PCT = Decimal("10")
MAX_TARGET_PCT = Decimal("50")
_BASE = re.compile(r"^[A-Z0-9]{2,12}$")

MANUAL_REASON = (
    "manual trade requested by the operator under CLAUDE.md #1 (single demo order): "
    "Risk Gate not consulted, size is the instrument minimum"
)


class ManualTradeRefusedError(RuntimeError):
    """The request is not one this command may act on. The message is for the operator."""


def refusal(settings: Settings) -> str | None:
    """Why this process may not place a manual trade, or None when it may.

    Evaluated before any adapter is built. Demo first: the live account is refused outright, armed
    or not, because a manual order there is not what the 2026-09-24 exception allows.
    """
    if settings.venue != "demo":
        return (
            f"refusing: .env names the {settings.venue} account. manual-trade places on the "
            "DEMO account only (CLAUDE.md #1: a live manual trade is owner's to arm by hand)"
        )
    if settings.mode not in ("approve", "auto"):
        return (
            f"MODE={settings.mode} does not place orders. Set MODE=approve and "
            "LIVE_TRADING=I_UNDERSTAND (the demo account), as for an armed manager run"
        )
    if settings.live_trading != "I_UNDERSTAND":
        return "LIVE_TRADING=I_UNDERSTAND is not set: this run is not armed"
    return None


def base_of(symbol: str) -> str:
    """`btc` -> `BTC`, and nothing but a plain base: an instId or a pair is a different mistake."""
    base = symbol.strip().upper()
    if not _BASE.match(base):
        raise ManualTradeRefusedError(f"--symbol takes a base asset such as BTC, got {symbol!r}")
    return base


def instrument_for(base: str) -> str:
    """The adapter's spelling of the USDT perpetual."""
    return f"{base}/USDT:USDT"


def close_command(base: str) -> str:
    """One instrument. Without --symbol, flatten writes a HALT and closes the whole account."""
    return f"argus flatten --symbol {base}-USDT-SWAP --yes"


def check_levels(stop_pct: Decimal, target_pct: Decimal) -> None:
    """The percentages are positive and inside what the watchdog will honour. No adapter needed."""
    if not Decimal(0) < stop_pct <= MAX_STOP_PCT:
        raise ManualTradeRefusedError(f"--stop-pct must be above 0 and at most {MAX_STOP_PCT}")
    if not Decimal(0) < target_pct <= MAX_TARGET_PCT:
        raise ManualTradeRefusedError(f"--target-pct must be above 0 and at most {MAX_TARGET_PCT}")


@dataclass(frozen=True)
class Ticket:
    """Exactly what will be sent, derived from the venue's own numbers."""

    base: str
    instrument: str
    side: Side
    contracts: Decimal
    ct_val: Decimal
    mark: Decimal
    stop: Decimal
    target: Decimal
    stop_pct: Decimal
    target_pct: Decimal
    leverage: int
    notional_usd: Decimal
    margin_usd: Decimal
    risk_usd: Decimal
    entry_kind: str = "market"
    margin_mode: str = MARGIN_MODE

    def lines(self, venue: str) -> list[str]:
        return [
            f"account      {venue.upper()}",
            f"instrument   {self.instrument}",
            f"side         {self.side.upper()} ({venue_side(self.side)})",
            f"size         {self.contracts} contracts  (the venue's minimum)",
            f"notional     ~${self.notional_usd:,.2f} at mark {self.mark}",
            f"entry        {self.entry_kind}",
            f"stop         {self.stop}  ({self.stop_pct}% from mark, attached to the entry)",
            f"target       {self.target}  ({self.target_pct}% from mark, attached to the entry)",
            f"leverage     {self.leverage}x",
            f"margin       {self.margin_mode}, ~${self.margin_usd:,.2f}",
            f"risk at stop ~${self.risk_usd:,.2f}",
        ]


def build_ticket(
    adapter: Any, base: str, side: Side, stop_pct: Decimal, target_pct: Decimal
) -> Ticket:
    """The order, from the venue's instrument list and a fresh mark. Places nothing."""
    check_levels(stop_pct, target_pct)
    instrument = instrument_for(base)
    spec = adapter.instrument_spec(instrument)  # raises if OKX does not list it live
    contracts = Decimal(str(spec["min_size"]))
    ct_val = Decimal(str(spec["ct_val"]))
    leverage = min(LEVERAGE, int(spec["max_leverage"]))
    mark = Decimal(str(adapter.fetch_ticker(instrument)["last"]))
    if contracts <= 0 or ct_val <= 0 or mark <= 0:
        raise ManualTradeRefusedError(
            f"{instrument}: unusable venue numbers (min {contracts}, ctVal {ct_val}, mark {mark})"
        )

    long = side == "long"
    sign = Decimal(1) if long else Decimal(-1)
    # Rounded onto the tick by the adapter's own function, in the direction it rounds for safety, so
    # the ticket shows - and the journal records - the triggers OKX will actually be sent.
    rounded = round_attached_triggers(
        {
            "side": venue_side(side),
            "price": mark,
            "attach_algos": [
                {"kind": "sl", "trigger_px": mark * (1 - sign * stop_pct / 100)},
                {"kind": "tp", "trigger_px": mark * (1 + sign * target_pct / 100)},
            ],
        },
        Decimal(str(spec["tick"])),
    )["attach_algos"]
    stop, target = (Decimal(str(a["trigger_px"])) for a in rounded)
    if (long and not stop < mark < target) or (not long and not target < mark < stop):
        raise ManualTradeRefusedError(
            f"stop {stop} / target {target} do not bracket the mark {mark} for a {side}"
        )
    notional = contracts * ct_val * mark
    return Ticket(
        base=base,
        instrument=instrument,
        side=side,
        contracts=contracts,
        ct_val=ct_val,
        mark=mark,
        stop=stop,
        target=target,
        stop_pct=stop_pct,
        target_pct=target_pct,
        leverage=leverage,
        notional_usd=notional,
        margin_usd=notional / leverage,
        risk_usd=notional * abs(mark - stop) / mark,
    )


def blocker(adapter: Any, settings: Settings, ticket: Ticket) -> str | None:
    """Why a placement must not happen right now, or None.

    Read inside the run lock. A HALT stops it as it stops the manager. Anything already held or
    resting on the asset stops it too: OKX nets one position per instrument, so a buy against a
    short reduces or flips it, and the stop attached to this entry would no longer describe it.
    """
    if settings.halt_file.exists():
        return f"HALT file present ({settings.halt_file}): nothing is placed under a HALT"
    asset = asset_of(ticket.instrument)
    for row in adapter.fetch_positions():
        if asset_of(str(row.get("symbol"))) == asset:
            return (
                f"the venue already holds {row.get('symbol')} ({row.get('side')} "
                f"{row.get('size')}); close it first: {close_command(ticket.base)}"
            )
    for row in adapter.fetch_open_orders():
        if asset_of(str(row.get("symbol"))) == asset:
            return f"an order is already resting on {row.get('symbol')}; cancel it first"
    return None


def _plan(ticket: Ticket) -> PlanInput:
    mark = float(ticket.mark)
    return PlanInput(
        symbol=ticket.base,
        side=ticket.side,
        entry_kind="market",
        entry_low=mark,
        entry_high=mark,
        stop=float(ticket.stop),
        targets=[Target(price=float(ticket.target), close_pct=100)],
        time_stop_hours=TIME_STOP_HOURS,
        scores=FactorScores(fundamental=0, onchain=0, macro=0, catalyst=0, technical=0),
        thesis=(
            f"Manual {ticket.side} {ticket.base} placed by the operator: stop {ticket.stop_pct}%, "
            f"target {ticket.target_pct}%, minimum size. Not a model-proposed trade."
        ),
    )


def _gate(ticket: Ticket) -> GateResult:
    sized = SizingResult(
        ok=True,
        reason="",
        step=0,
        risk_usd=float(ticket.risk_usd),
        stop_dist_pct=float(ticket.stop_pct),
        notional_usd=float(ticket.notional_usd),
        leverage=ticket.leverage,
        margin_usd=float(ticket.margin_usd),
        contracts=ticket.contracts,
        liq_dist_pct=float(Decimal(100) / ticket.leverage),
    )
    return GateResult(
        status="APPROVED",
        sized=sized,
        reasons=[GateReason(gate=0, name="manual", verdict="PASS", detail=MANUAL_REASON)],
    )


def _record_decision(
    engine: Engine, run_id: str, settings: Settings, plan: PlanInput, gate: GateResult, t: Ticket
) -> str:
    """The same two ledger rows `manager._record_decision` writes, minus the dossier and market.

    The payload carries every key the Reviewer, post-mortem, dashboard and book queries read
    (`stop`, `entry`, `targets`, `time_stop_hours`, `scores`, `thesis`, `market`), and the gate row
    carries `sized` with a real risk and notional, because `manager._resting` raises on an entry
    order whose plan has none.
    """
    plan_id = new_id()
    ledger.append(
        engine,
        "plans",
        {
            "plan_id": plan_id,
            "run_id": run_id,
            "mode": settings.mode,
            "symbol": plan.symbol,
            "side": plan.side,
            "state": "GATED",
            "step": 0,
            "payload": json.dumps(
                {
                    "entry": [plan.entry_low, plan.entry_high],
                    "entry_kind": plan.entry_kind,
                    "stop": plan.stop,
                    "targets": [[x.price, x.close_pct] for x in plan.targets],
                    "time_stop_hours": plan.time_stop_hours,
                    "scores": vars(plan.scores),
                    "invalidation_rules": [],
                    "thesis": plan.thesis,
                    "trader_family": "",
                    "dossier_id": None,
                    "market": {
                        "mark": float(t.mark),
                        "beta_btc": None,
                        "min_contracts": str(t.contracts),
                        "max_leverage": t.leverage,
                    },
                    "proposer": "manual",
                    "manual": True,
                },
                default=str,
            ),
        },
    )
    ledger.append(
        engine,
        "gate_results",
        {
            "plan_id": plan_id,
            "run_id": run_id,
            "verdict": gate.status,
            "reasons": json.dumps(gate.as_dict(), default=str),
        },
    )
    return plan_id


def place(
    engine: Engine, adapter: Any, settings: Settings, run_id: str, ticket: Ticket
) -> tuple[str, str]:
    """Journal the decision and place the order through the production path.

    Returns (plan_id, order_id). `place_approved` raises `PlacementError` when the venue refuses; by
    then the order row exists and is REJECTED (or left NEW when the outcome is unknown, which the
    watchdog's orphan check settles), exactly as for a manager-placed order.
    """
    plan, gate = _plan(ticket), _gate(ticket)
    plan_id = _record_decision(engine, run_id, settings, plan, gate, ticket)
    order_id = place_approved(engine, adapter, run_id, plan, gate, plan_id, ticket.instrument)
    if order_id is None:  # unreachable: the gate above is APPROVED with a size
        raise ManualTradeRefusedError("nothing was placed")
    log.info("manual.placed", instrument=ticket.instrument, side=ticket.side, run_id=run_id)
    return plan_id, order_id


def protection_after(adapter: Any, ticket: Ticket) -> str:
    """What the venue says about the stop just after placement: protected, unprotected or pending.

    A market order fills at once but its attached stop goes live a moment later, so 'unprotected'
    here is a prompt to look, not proof; the watchdog repairs a genuinely naked position on its
    next tick either way.
    """
    state = adapter.reconcile()
    asset = asset_of(ticket.instrument)
    held = [p for p in state["positions"] if asset_of(str(p["symbol"])) == asset]
    if not held:
        return "pending"
    naked = {asset_of(str(s)) for s in state.get("unprotected", [])}
    return "unprotected" if asset in naked else "protected"
