"""The review run: what happens to a position after it is opened.

Until now nothing did. The manager opens, the watchdog checks a stop still exists, and between
those two a position could run to its stop or its target with no judgement applied — no trim at
the first target, no close on a broken thesis, no stop tightened behind a move. The Reviewer
agent has existed and been measured (18/20) since before execution was wired; `argus review` was
a stub. This is that gap.

Three properties hold whatever the model says:

1. **A stop only ever moves towards profit.** `ReviewDecision` refuses the other direction, and
   `_apply` checks again using only the venue's side and the venue's current stop — refusing
   when either is unknown — and the adapter's `amend_stop` refuses a widening once more. A
   reviewer that can widen a stop is a reviewer that can turn a loss into a bigger one.
2. **No new positions.** The vocabulary is HOLD, TRIM, CLOSE, MOVE_STOP. There is no verb for
   opening, and no size field anywhere.
3. **Silence is not approval.** The Reviewer is `safety` criticality: when it cannot be reached
   the position is left exactly as it is and an alert is raised, rather than the run quietly
   deciding that no news is good news.
4. **HALT means hands off.** With the HALT file present no model is asked — neither the Reviewer
   nor, in `argus review`, the post-mortems — and no order is sent for any position. The one
   venue action the command still takes is the resting-entry sweep, which only cancels entries
   whose life is over: one left to fill would open the very exposure HALT forbids. Whether a
   broken thesis should still be CLOSEd under HALT is the owner's call; today it rides to its
   exchange stop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

import structlog
from sqlalchemy import Engine

from argus.config import get_settings
from argus.exchange.errors import TrimTooSmallError
from argus.exchange.okx import StopWideningError, stop_tightens, venue_side
from argus.exchange.reconcile import asset_of
from argus.features.review_flags import compute_flags
from argus.journal import ledger
from argus.llm.agents.reviewer import review as ask_reviewer
from argus.llm.router import AllModelsFailedError, Router
from argus.observability import alerts, spans
from argus.runs.watchdog import read_halt

log = structlog.get_logger(__name__)


@dataclass
class ReviewOutcome:
    symbol: str
    action: str
    detail: str = ""
    applied: bool = False
    # The decision as acted on: `current_stop` is the venue's level, not the model's guess.
    decision: Any = None
    # applied | dry_run | held | refused | failed | halted | unreviewed | error
    status: str = ""


@dataclass
class ReviewResult:
    run_id: str
    dry_run: bool
    positions: int = 0
    outcomes: list[ReviewOutcome] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    halted: bool = False

    def summary(self) -> str:
        if not self.positions:
            return "flat — nothing to review"
        if self.halted and not any(o.applied for o in self.outcomes):
            return f"HALT — {self.positions} position(s) left exactly as they are"
        acted = sum(1 for o in self.outcomes if o.applied)
        return f"{self.positions} position(s) · {acted} acted on · {len(self.errors)} error(s)"


class PlacedPlan(NamedTuple):
    plan_id: str | None
    plan: dict[str, Any]
    # When its entry order was journalled — sent to the venue moments later.
    placed_at: str | None = None


# An entry the venue refused opened nothing. One cancelled or expired after filling in part
# opened the position as surely as a full fill, and counts as one; one that ended with nothing
# filled is a candidate only when no other entry is.
_REFUSED = frozenset({"REJECTED"})
_ENDED = frozenset({"CANCELLED", "EXPIRED"})
# An entry is journalled moments before it is sent, so it precedes the venue's open time. The
# slack absorbs clock skew between the box and OKX, nothing more: gate 10 refuses a new entry on
# an asset already held, so no other entry for it lands this close after a fill.
_CLOCK_SLACK = timedelta(minutes=5)
# Two 4h bars: one late bar is a slow publish at the boundary, two is a feed that stopped.
_STALE_PRICE = timedelta(hours=8)


def _filled_in_part(engine: Engine, order_id: str) -> bool:
    """Whether an ended entry filled at all. Every path that ends one after a fill — the resting
    sweep, a recorded fill — moves it through PARTIAL first."""
    from sqlalchemy import text

    with engine.connect() as conn:
        hit = conn.execute(
            text(
                "SELECT 1 FROM position_events WHERE machine = 'order'"
                " AND json_extract(detail, '$.order_id') = :o AND to_state = 'PARTIAL' LIMIT 1"
            ),
            {"o": order_id},
        ).first()
    return hit is not None


def _plan_row_for(
    engine: Engine,
    symbol: str,
    side: str | None,
    after: datetime | None = None,
    not_after: datetime | None = None,
    *,
    venue: str,
) -> PlacedPlan:
    """The plan whose entry order was actually placed for this asset and side, newest first.

    Joined through `orders.plan_id` (review J-N10). The newest non-REJECTED *plan* used to win,
    and that was as often a GATED shadow plan, or one whose placement the venue refused, as the
    plan that opened the position — so the Reviewer judged a thesis that never traded. Matched by
    `asset_of`, because plans are keyed `SOL` while OKX positions arrive `SOL/USDT:USDT`.

    Only entries journalled inside (`after`, `not_after`] are candidates, when the caller knows
    the window. Without one an entry placed after the position opened, or one from a trade that
    closed weeks ago, could stand in for the plan that opened it (second review of J-N10).

    The id matters: `postmortems.plan_id` is a foreign key into `plans`, so a synthesised one
    fails the insert rather than silently creating an orphan.

    Only entries `venue`'s own runs placed (F-150): one journal holds both accounts, and a newer
    demo entry on the same asset and side would otherwise stand in for the live trade's plan.
    """

    from sqlalchemy import text

    from argus.execution.lifecycle import order_state

    if side is None:
        return PlacedPlan(None, {})
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT p.plan_id, p.symbol, p.payload, o.order_id, o.created_at AS placed_at"
                    " FROM orders o JOIN plans p ON p.plan_id = o.plan_id"
                    " JOIN runs r ON r.run_id = o.run_id"
                    " WHERE o.kind = 'entry' AND p.side = :side AND r.config_hash = :venue"
                    " ORDER BY o.created_at DESC, o.rowid DESC"
                ),
                {"side": side, "venue": venue},
            )
            .mappings()
            .all()
        )
    asset = asset_of(symbol)
    chosen: Any = None
    fallback: Any = None
    for row in rows:
        if asset_of(row["symbol"]) != asset:
            continue
        placed = _instant(row["placed_at"])
        if (after or not_after) and (
            placed is None
            or (after is not None and placed <= after)
            or (not_after is not None and placed > not_after)
        ):
            continue
        state = order_state(engine, row["order_id"])
        if state in _REFUSED:
            continue
        if state in _ENDED and not _filled_in_part(engine, row["order_id"]):
            fallback = fallback or row
            continue
        chosen = row
        break
    row = chosen or fallback
    if row is None:
        return PlacedPlan(None, {})
    try:
        plan = dict(json.loads(row["payload"]))
    except (TypeError, ValueError):
        plan = {}
    return PlacedPlan(row["plan_id"], plan, row["placed_at"])


# Kinds that protect a position. A trailing stop fires like any other, so it counts — but its
# live trigger moves with price and is not part of the adapter contract (OKX reports the callback,
# not the moving level), so while one rests the binding stop is unknown rather than invisible.
PROTECTIVE_KINDS = ("sl", "trailing")


def _level(value: Any) -> Decimal | None:
    """A positive, finite trigger, or None. Never 0.0 for "unknown" (review J-N11)."""
    try:
        level = Decimal(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return level if level.is_finite() and level > 0 else None


def venue_stops(algos: list[dict[str, Any]]) -> dict[str, list[Decimal | None]]:
    """Every protective order's trigger, per asset; `None` marks one whose level is unknown.

    Keyed by `asset_of` because OKX names an algo by instId (`BTC-USDT-SWAP`) and a position by
    ccxt symbol (`BTC/USDT:USDT`). Keyed by the raw string, no position ever found its stop in
    production and the tighten-only check silently never ran (review R-1). Only the adapter
    contract's keys are read — `symbol`, `kind`, `trigger_px` — never a guessed fallback (J-N11).
    """
    stops: dict[str, list[Decimal | None]] = {}
    for algo in algos:
        kind = algo.get("kind")
        if kind not in PROTECTIVE_KINDS:
            continue
        level = _level(algo.get("trigger_px")) if kind == "sl" else None
        stops.setdefault(asset_of(str(algo.get("symbol") or "")), []).append(level)
    return stops


def binding_stop(levels: list[Decimal | None] | None, side: str | None) -> Decimal | None:
    """The stop that fires first, or None when there is none or any level is unknown."""
    if not levels or side is None or any(level is None for level in levels):
        return None
    known = [level for level in levels if level is not None]
    return max(known) if side == "long" else min(known)


def _instant(stamp: Any) -> datetime | None:
    """An ISO-8601 stamp (the journal's only format) as an aware datetime; None if unknown."""
    if not stamp:
        return None
    try:
        then = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return then if then.tzinfo is not None else then.replace(tzinfo=UTC)


def _price_stale(technical: dict[str, Any], now: datetime) -> bool:
    """No 4h close, or one whose bar closed more than two bars ago (D-N1 / L-3).

    Fresh, the newest closed 4h bar closed under 4 h ago. Only a missing price used to count,
    so a lagging feed or a cached frame — the golden `stale_price_feed` case — never did. A
    price with no `price_at` (a dossier from before it was recorded) is judged by presence.
    """
    if technical.get("price") is None:
        return True
    closed = _instant(technical.get("price_at"))
    return closed is not None and now - closed > _STALE_PRICE


def _hours_since(stamp: Any, now: datetime) -> float | None:
    """Hours from an ISO-8601 UTC stamp to `now`; None if unknown."""
    then = _instant(stamp)
    return None if then is None else round((now - then).total_seconds() / 3600, 1)


def _episode(
    engine: Engine, last_seen: dict[str, Any], *, venue: str
) -> tuple[datetime | None, datetime | None]:
    """The window the entry behind a closed position was journalled in: (after, not_after].

    Not after the position was last seen open: an entry placed later is the next trade's, often
    placed between the stop filling and anyone noticing. And after the previous trade on the
    symbol was last seen open, since that trade's entry, and every earlier one, opened a position
    that has since closed. Not after that trade's CLOSED row: the next entry is routinely placed
    before the watchdog notices the last one closed. A row without a snapshot behind it gets no
    window. Only `venue`'s own snapshots (F-150): a demo trade on the symbol is not the live
    one's predecessor, and taking it as one dropped the live entry out of its own window.
    """
    from sqlalchemy import text

    symbol, seen = last_seen.get("symbol"), last_seen.get("created_at")
    if not symbol or not seen:
        return None, None
    with engine.connect() as conn:
        previous = conn.execute(
            text(
                "SELECT MAX(s.created_at) FROM positions_snapshots s"
                " JOIN runs r ON r.run_id = s.run_id"
                " WHERE s.symbol = :s AND s.state = 'OPEN' AND r.config_hash = :venue"
                " AND s.created_at < ("
                "   SELECT MAX(c.created_at) FROM positions_snapshots c"
                "   JOIN runs rc ON rc.run_id = c.run_id"
                "   WHERE c.symbol = :s AND c.state = 'CLOSED' AND rc.config_hash = :venue"
                "   AND c.created_at < :t)"
            ),
            {"s": symbol, "t": seen, "venue": venue},
        ).scalar()
    return _instant(previous), _instant(seen)


def _normal_side(value: Any) -> str | None:
    """`long`/`short` from either vocabulary; None for anything else, never a guess."""
    try:
        return "long" if venue_side(str(value or "")) == "buy" else "short"
    except ValueError:
        return None


def _side_of(position: dict[str, Any]) -> str | None:
    """The side as the venue reports it."""
    return _normal_side(position.get("side"))


class ReviewIncompleteError(RuntimeError):
    """Raised by the caller inside `start_run` so a review that left work undone is FAILED.

    The run row said OK whatever happened — a CLOSE the venue rejected, a position nobody could
    review — and staleness checks and the dashboard read that row (review J-N3).
    """


def _short(detail: str) -> str:
    """First line, capped: enough to act on from a phone, not a venue body (S-3)."""
    return detail.split("\n", 1)[0][:160]


def _alerts_for(result: ReviewResult, stops_error: str | None) -> list[alerts.Alert | None]:
    """What a human must hear about this review (review J-N3). A failed action or an unreviewed
    position is critical: a decision about live money was taken, or needed, and did not land."""
    found: list[alerts.Alert | None] = []
    if stops_error:
        found.append(
            alerts.Alert(
                "review_stops_unknown",
                "warning",
                f"stop orders could not be read ({_short(stops_error)}); every stop move this"
                " run was refused",
            )
        )
    for outcome in result.outcomes:
        if outcome.status == "failed":
            found.append(
                alerts.Alert(
                    "review_apply_failed",
                    "critical",
                    f"{outcome.symbol}: {outcome.action} did not go through — the position is"
                    f" unchanged ({_short(outcome.detail)})",
                )
            )
        elif outcome.status == "error":
            found.append(
                alerts.Alert(
                    "review_unreviewed",
                    "critical",
                    f"{outcome.symbol} was not reviewed ({_short(outcome.detail)})",
                )
            )
        elif outcome.status == "refused":
            found.append(
                alerts.Alert(
                    "review_refused",
                    "warning",
                    f"{outcome.symbol}: {_short(outcome.detail)}",
                )
            )
    found.extend(
        alerts.Alert("review_journal_failed", "critical", _short(error))
        for error in result.errors
        if "journal write failed" in error
    )
    return found


class RefusedError(ValueError):
    """A decision the run would not send. The safety check working, not the venue failing."""


def _journal_outcome(
    engine: Engine, run_id: str, outcome: ReviewOutcome, plan_id: str | None, dry_run: bool
) -> None:
    """One `position_events` row per outcome, whatever it was (review J-N2).

    CLOSE / TRIM / MOVE_STOP reached the venue and were only logged: no row, no plan link. A
    post-mortem then labelled the system's own close "left the venue between ticks", and a
    refused widening left no trace outside journald. Dry runs and refusals are journalled too —
    what the Reviewer wanted and was not allowed is the most useful row of all.
    """
    decision = outcome.decision
    detail = {
        "status": outcome.status,
        "applied": outcome.applied,
        "dry_run": dry_run,
        "side": getattr(decision, "side", None),
        "current_stop": getattr(decision, "current_stop", None),
        "new_stop": getattr(decision, "new_stop", None),
        "trim_pct": getattr(decision, "trim_pct", None),
        "rationale": (getattr(decision, "rationale", None) or "")[:1000] or None,
    }
    ledger.append(
        engine,
        "position_events",
        {
            "run_id": run_id,
            "symbol": outcome.symbol,
            "plan_id": plan_id,
            "machine": "review",
            "from_state": None,
            "to_state": outcome.action,
            "reason": (outcome.detail or outcome.status or outcome.action)[:500],
            "detail": json.dumps(detail, default=str),
        },
    )


def _apply(
    adapter: Any, decision: Any, symbol: str, side: str | None, venue_stop: Decimal | None
) -> str:
    """Carry out one decision. Returns what was done, or raises."""
    if decision.action == "HOLD":
        return "held"
    if decision.action == "CLOSE":
        adapter.close(symbol)
        return "closed"
    if decision.action == "TRIM":
        adapter.partial_close(symbol, int(decision.trim_pct))
        return f"trimmed {decision.trim_pct}%"
    if decision.action == "MOVE_STOP":
        new = _checked_stop(decision, side, venue_stop)
        adapter.amend_stop(symbol, new)
        return f"stop {venue_stop} -> {new}"
    raise ValueError(f"unknown action {decision.action!r}")


def _checked_stop(decision: Any, side: str | None, venue_stop: Decimal | None) -> Decimal:
    """The MOVE_STOP level, or RefusedError. Touches nothing, so a dry run asks it too.

    The schema refused a widening against the `current_stop` the model wrote back — the model
    checking itself. These checks use only what the venue says: its side, and its stop, which is
    the one that will be replaced. Anything unknown refuses (review R-1).
    """
    new = Decimal(str(decision.new_stop))
    if side is None:
        raise RefusedError("refused: the venue did not say which side this position is")
    if decision.side != side:
        raise RefusedError(
            f"refused: the model judged a {decision.side} but the venue holds a {side}"
        )
    if venue_stop is None:
        raise RefusedError(
            "refused: the venue's current stop is unknown, so no move can be shown to tighten it"
        )
    if not stop_tightens(side, new, venue_stop):
        raise RefusedError(f"refused: {new} does not tighten the venue's stop {venue_stop}")
    return new


def run_review(
    engine: Engine,
    router: Router,
    adapter: Any,
    run_id: str,
    dossier_for: Any,
    dry_run: bool = True,
    portfolio: dict[str, Any] | None = None,
    now: datetime | None = None,
    halt_file: Path | None = None,
    *,
    venue: str,
) -> ReviewResult:
    """Judge every open position once. `dry_run` decides and journals without touching the venue.

    `venue` is the account `adapter` trades ("demo" | "live"); plans are read from its runs only.
    """
    result = ReviewResult(run_id=run_id, dry_run=dry_run)
    # Validate Settings (the MODE interlocks) for every caller, not only the default halt file.
    settings = get_settings()
    halt_path = halt_file or settings.halt_file

    def record(outcome: ReviewOutcome, plan_id: str | None = None) -> None:
        result.outcomes.append(outcome)
        try:
            _journal_outcome(engine, run_id, outcome, plan_id, dry_run)
        except Exception as exc:  # noqa: BLE001 - the action stands; the missing row must not
            result.errors.append(
                f"{outcome.symbol}: journal write failed: {type(exc).__name__}: {exc}"
            )
            log.error("review.journal_failed", symbol=outcome.symbol, error=str(exc))

    try:
        with spans.span("review.positions", run_id=run_id):
            positions = adapter.fetch_positions()
    except Exception as exc:
        alerts.dispatch(
            [
                alerts.Alert(
                    "review_failed",
                    "critical",
                    f"open positions could not be read ({type(exc).__name__}); nothing was"
                    " reviewed",
                )
            ]
        )
        raise
    result.positions = len(positions)
    if not positions:
        return result

    # HALT means hands off (review J-N5). The review used to ignore it: after a kill-switch
    # flatten it still asked the model and could `amend_stop`, which places a NEW algo where none
    # exists. Under HALT nothing is asked and nothing is sent; the positions are only reported.
    # A dry run sends nothing anyway, and is how an operator asks what the review would do before
    # deciding to resume, so HALT binds only an armed review.
    reason = None if dry_run else read_halt(halt_path)
    if reason is not None:
        result.halted = True
        log.warning("review.halted", halt=str(halt_path), reason=reason[:200])
        for venue_row in positions:
            symbol = venue_row.get("symbol") or venue_row.get("instId") or "?"
            record(ReviewOutcome(symbol, "HALTED", f"HALT: {reason[:200]}", status="halted"))
        return result

    stops: dict[str, list[Decimal | None]] = {}
    stops_error: str | None = None
    try:
        stops = venue_stops(adapter.fetch_algo_orders())
    except Exception as exc:  # noqa: BLE001 - a missing stop list is not a reason to skip review
        # Every stop is now unknown, so every MOVE_STOP below refuses; CLOSE and TRIM still act.
        stops_error = f"{type(exc).__name__}: {exc}"
        result.errors.append(f"algo orders: {stops_error}")

    for venue_row in positions:
        symbol = venue_row.get("symbol") or venue_row.get("instId") or "?"
        side = _side_of(venue_row)
        venue_stop = binding_stop(stops.get(asset_of(symbol)), side)
        # The model and the flags are shown the stop the venue holds; nothing else knew it.
        position = {
            **venue_row,
            "current_stop": None if venue_stop is None else float(venue_stop),
        }
        placed = PlacedPlan(None, {})
        try:
            # The asset (`BTC`), never the venue's spelling: the CLI's builder keys the listed
            # universe by asset, and handed `BTC/USDT:USDT` it built a bogus instrument and the
            # position errored out unreviewed (review R-1, runs view).
            dossier = dossier_for(asset_of(symbol).upper())
            opened = _instant(venue_row.get("opened_at"))
            placed = _plan_row_for(
                engine, symbol, side, not_after=opened and opened + _CLOCK_SLACK, venue=venue
            )
            plan = placed.plan
            # `fetch_positions` carries neither the time in trade nor the stop, and nothing set
            # `stale` — so three flags could never fire on a real position (review L-3 / D-N1).
            # The venue's own open time first; the journalled entry when it does not say.
            position["hours_in_trade"] = _hours_since(
                venue_row.get("opened_at") or placed.placed_at, now or datetime.now(UTC)
            )
            technical = dict(getattr(dossier, "technical", {}) or {})
            if _price_stale(technical, now or datetime.now(UTC)):
                technical["stale"] = True
            flags = compute_flags(
                position=position,
                plan=plan,
                derivatives=getattr(dossier, "derivatives", {}) or {},
                technical=technical,
            )
            call = ask_reviewer(router, run_id, position, plan, dossier, flags, portfolio or {})
        except AllModelsFailedError as exc:
            # Safety criticality: leave the position exactly as it is and say so loudly. A
            # position nobody could judge is not a position everyone agreed to hold.
            from argus.llm.router import agent_policy

            alerts.dispatch(
                [
                    alerts.agent_failed(
                        "reviewer", agent_policy("reviewer").criticality, symbol, str(exc)
                    )
                ]
            )
            result.errors.append(f"{symbol}: reviewer unavailable: {exc}")
            record(
                ReviewOutcome(symbol, "UNREVIEWED", str(exc)[:300], status="unreviewed"),
                placed.plan_id,
            )
            continue
        except Exception as exc:  # noqa: BLE001 - one bad position is not a dead run
            result.errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            record(
                ReviewOutcome(
                    symbol, "UNREVIEWED", f"{type(exc).__name__}: {exc}"[:300], status="error"
                ),
                placed.plan_id,
            )
            continue

        # Whatever the model wrote back as the current stop, the venue's is the one on record.
        decision = call.output.model_copy(update={"current_stop": position["current_stop"]})
        outcome = ReviewOutcome(symbol, decision.action, decision.rationale, decision=decision)
        if dry_run and decision.action == "MOVE_STOP":
            # The venue checks a live run makes before sending. Skipped, every move a live run
            # refuses was journalled "dry_run", and a dry run could never show one (R-1).
            try:
                _checked_stop(decision, side, venue_stop)
            except RefusedError as exc:
                outcome.detail = f"MOVE_STOP refused (dry run): {exc}"
                outcome.status = "refused"
                result.errors.append(f"{symbol}: {outcome.detail}")
                log.error("review.refused", symbol=symbol, error=str(exc), dry_run=True)
                record(outcome, placed.plan_id)
                continue
        if dry_run or decision.action == "HOLD":
            outcome.detail = f"{decision.action}: {decision.rationale}"
            outcome.status = "held" if decision.action == "HOLD" else "dry_run"
            record(outcome, placed.plan_id)
            continue

        # Checked again at the last moment: a model call can take minutes, and an operator's
        # `touch HALT` in that window must still stop the order.
        reason = None if dry_run else read_halt(halt_path)
        if reason is not None:
            result.halted = True
            outcome.detail = f"{decision.action} not sent: HALT written during the review"
            outcome.status = "halted"
            record(outcome, placed.plan_id)
            continue

        try:
            outcome.detail = _apply(adapter, decision, symbol, side, venue_stop)
            outcome.applied = True
            outcome.status = "applied"
            log.info("review.applied", symbol=symbol, action=decision.action, detail=outcome.detail)
        except TrimTooSmallError as exc:  # not an error: the position just stays as it is
            outcome.detail = f"TRIM skipped: {exc}"
            outcome.status = "refused"
            log.info("review.trim_too_small", symbol=symbol, detail=str(exc))
        except (RefusedError, StopWideningError) as exc:
            outcome.detail = f"{decision.action} refused: {exc}"
            outcome.status = "refused"
            result.errors.append(f"{symbol}: {outcome.detail}")
            log.error("review.refused", symbol=symbol, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - a failed action must not stop the others
            outcome.detail = f"{decision.action} failed: {type(exc).__name__}: {exc}"
            outcome.status = "failed"
            result.errors.append(f"{symbol}: {outcome.detail}")
            log.error("review.apply_failed", symbol=symbol, error=str(exc))
        record(outcome, placed.plan_id)

    found = _alerts_for(result, stops_error)
    if found:
        alerts.dispatch(found)
    return result


def _outcome_for(closed: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """What the trade did, from the last snapshot and the plan that opened it.

    Deliberately thin. `outcome_r` is computed from the entry and stop the plan declared, because
    that is the risk the trade was sized against — inferring R from the venue's PnL would use a
    denominator nobody decided.
    """
    entry = closed.entry_price
    stop = plan.get("stop")
    exit_price = _f(closed.last_seen.get("mark_price"))
    outcome: dict[str, Any] = {
        "exit_reason": "left the venue between ticks (stop, target or manual)",
        "exit_price": exit_price,
        "hours_held": None,
        "thesis_at_exit": "unknown — the position was gone when we next looked",
    }
    if entry and stop and exit_price:
        risk = abs(entry - float(stop))
        if risk > 0:
            move = exit_price - entry if closed.side == "long" else entry - exit_price
            outcome["outcome_r"] = round(move / risk, 2)
    return outcome


def _f(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


# How far back a review looks for closed trades still owed a post-mortem. Long enough to outlast a
# HALT or a run of failed model calls; bounded so a close nobody can judge is not re-read for ever.
POSTMORTEM_LOOKBACK = timedelta(days=14)


def plan_behind(engine: Engine, position: Any, *, venue: str) -> PlacedPlan:
    """The placed plan that opened a closed position, looked for only inside its own episode
    and only among `venue`'s runs (F-150)."""
    after, not_after = _episode(engine, position.last_seen, venue=venue)
    return _plan_row_for(
        engine,
        position.symbol,
        _normal_side(position.side),
        after=after,
        not_after=not_after,
        venue=venue,
    )


def postmortems_due(engine: Engine, now: datetime, *, venue: str) -> list[Any]:
    """Every trade closed inside the lookback whose plan has no post-mortem yet, newest first.

    Read from the journal, not from `newly_closed`: the watchdog ticks every 20 minutes and the
    review four times a day, so the watchdog marks nearly every close before a review looks, and
    a review that judged only the closes it noticed itself judged almost none (F-151). Keyed on
    the plan, so a trade is judged once however many reviews pass, and a close with no plan
    behind it — a position this system did not open — is never due. A close two runs marked at
    once (the review's pass runs outside the run lock) is still one trade.
    """
    from sqlalchemy import text

    from argus.journal.positions import closed_since

    due: list[Any] = []
    seen: set[str] = set()
    for position in reversed(closed_since(engine, now - POSTMORTEM_LOOKBACK, venue=venue)):
        plan_id = plan_behind(engine, position, venue=venue).plan_id
        if plan_id is None or plan_id in seen:
            continue
        seen.add(plan_id)
        with engine.connect() as conn:
            judged = conn.execute(
                text("SELECT 1 FROM postmortems WHERE plan_id = :p"), {"p": plan_id}
            ).first()
        if judged is None:
            due.append(position)
    return due


def write_postmortems(
    engine: Engine,
    router: Router,
    run_id: str,
    closed: list[Any],
    dry_run: bool = False,
    *,
    venue: str,
) -> list[str]:
    """One post-mortem per closed trade handed in — `postmortems_due`, in production.

    `store_postmortem` had no production caller (F-132), so closed trades never reached the
    Reflector with a verdict attached — and a Reflector reading outcomes with no judgement behind
    them is left inferring process quality from PnL, which is the exact bias the Post-mortem
    prompt spends most of its length preventing.
    """
    from argus.features.trade_flags import compute_trade_flags
    from argus.journal.trades import store_postmortem
    from argus.llm.agents import postmortem as agent

    written: list[str] = []
    for position in closed:
        symbol = position.symbol
        plan_id, plan, _ = plan_behind(engine, position, venue=venue)
        if plan_id is None:
            # A position with no plan behind it was not opened by this system. Judging the
            # process that produced it would be judging a process we never ran.
            log.warning("review.postmortem_skipped", symbol=symbol, reason="no plan on record")
            continue
        outcome = _outcome_for(position, plan)
        try:
            call = agent.analyse(router, run_id, plan, outcome)
        except AllModelsFailedError as exc:
            # Optional criticality: a missed post-mortem costs a lesson, not a trade, and it stays
            # due. The rest wait too: each would spend its own full router chain on the same
            # outage, and a backlog of them can outrun the review unit's time limit.
            log.warning("review.postmortem_unavailable", symbol=symbol, error=str(exc))
            break
        if dry_run:
            written.append(f"{symbol}: {call.output.process_verdict} (dry run — not stored)")
            continue
        store_postmortem(
            engine,
            run_id=run_id,
            plan_id=plan_id,
            symbol=symbol,
            report=call.output,
            outcome=outcome,
            flags=compute_trade_flags(plan, outcome),
        )
        written.append(f"{symbol}: {call.output.process_verdict}")
    return written
