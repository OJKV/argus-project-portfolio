"""Journal versus exchange: the exchange is truth, and every disagreement has a name.

The failure this prevents is documented in the review notes: 246 "ghost positions" — rows the
journal believed were open, which the venue had closed long ago — blocked all trading for months
because nothing ever asked the exchange.
"""

from __future__ import annotations

from decimal import Decimal

from argus.exchange.reconcile import Discrepancy, reconcile


def _journal(**over):
    base = {
        "symbol": "BTC-USDT-SWAP",
        "side": "long",
        "size": Decimal("1.0"),
        "state": "OPEN",
        "plan_id": "p1",
    }
    base.update(over)
    return base


def _venue(**over):
    base = {"symbol": "BTC-USDT-SWAP", "side": "long", "size": Decimal("1.0"), "protected": True}
    base.update(over)
    return base


def test_agreement_produces_nothing_to_do() -> None:
    result = reconcile([_journal()], [_venue()])
    assert result.discrepancies == []
    assert result.clean


def test_a_position_the_venue_does_not_have_is_a_ghost() -> None:
    """The journal thinks we are in a trade that closed. Left alone it blocks the symbol
    forever, because every later run sees an open position that is not there."""
    result = reconcile([_journal()], [])
    assert [d.kind for d in result.discrepancies] == ["ghost"]
    assert result.discrepancies[0].symbol == "BTC-USDT-SWAP"
    assert not result.clean


def test_a_position_we_never_planned_is_an_orphan() -> None:
    """Real money in a trade the journal cannot explain. It is never ignored — it still needs a
    stop, and it still needs a human told."""
    result = reconcile([], [_venue()])
    kinds = [d.kind for d in result.discrepancies]
    assert "orphan" in kinds


def test_an_orphan_without_protection_is_reported_as_both() -> None:
    result = reconcile([], [_venue(protected=False)])
    kinds = {d.kind for d in result.discrepancies}
    assert kinds == {"orphan", "unprotected"}


def test_a_known_position_without_a_stop_is_unprotected() -> None:
    result = reconcile([_journal()], [_venue(protected=False)])
    assert [d.kind for d in result.discrepancies] == ["unprotected"]


def test_a_size_difference_is_drift_not_a_new_position() -> None:
    """A partial fill or a manual trim leaves the journal stale, but it is the same trade."""
    result = reconcile([_journal(size=Decimal("1.0"))], [_venue(size=Decimal("0.6"))])
    assert [d.kind for d in result.discrepancies] == ["size_drift"]
    assert result.discrepancies[0].detail.endswith("0.6")


def test_a_side_flip_is_never_treated_as_drift() -> None:
    """Same symbol, opposite direction, is not the same trade — calling it drift would quietly
    adopt a position nobody planned."""
    result = reconcile([_journal(side="long")], [_venue(side="short")])
    kinds = {d.kind for d in result.discrepancies}
    assert kinds == {"ghost", "orphan"}


def test_closed_journal_rows_are_not_ghosts() -> None:
    assert reconcile([_journal(state="CLOSED")], []).clean


def test_both_symbol_spellings_match_the_same_position() -> None:
    result = reconcile([_journal(symbol="BTC/USDT:USDT")], [_venue(symbol="BTC-USDT-SWAP")])
    assert result.clean


def test_discrepancies_are_ordered_by_severity() -> None:
    """An unprotected position outranks a stale row: one is money at risk, the other is tidying."""
    result = reconcile(
        [_journal(symbol="ETH-USDT-SWAP")],
        [_venue(symbol="BTC-USDT-SWAP", protected=False)],
    )
    assert result.discrepancies[0].kind in ("unprotected", "orphan")
    assert result.discrepancies[-1].kind == "ghost"


def test_severity_is_stated_not_inferred() -> None:
    for kind, expected in (
        ("unprotected", "critical"),
        ("orphan", "critical"),
        ("ghost", "warning"),
        ("size_drift", "warning"),
    ):
        assert Discrepancy(kind=kind, symbol="X", detail="").severity == expected
