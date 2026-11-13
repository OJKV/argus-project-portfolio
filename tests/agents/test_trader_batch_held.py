"""owner 2026-10-28: gate 10 refuses any plan on a symbol the book already holds, and the
Trader kept
spending its picks on BTC while the manual BTC long was open (4 of 5 plans). Held symbols are not
offered to the Trader at all."""

from __future__ import annotations

from argus.llm.agents.trader_batch import drop_held
from argus.llm.golden import CASES


def test_held_symbols_are_not_offered() -> None:
    dossiers = [c.dossier() for c in CASES[:3]]
    held = {dossiers[0].symbol.lower()}
    kept, dropped = drop_held(dossiers, held)
    assert [d.symbol for d in kept] == [d.symbol for d in dossiers[1:]]
    assert dropped == [dossiers[0].symbol]


def test_nothing_held_keeps_everything() -> None:
    dossiers = [c.dossier() for c in CASES[:3]]
    kept, dropped = drop_held(dossiers, set())
    assert kept == dossiers and dropped == []
