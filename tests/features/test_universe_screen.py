"""Universe option D: core + whitelist (expiring) + ≤3 satellites from the screen."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from argus.features import universe as uni

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
OLD = int((NOW - timedelta(days=400)).timestamp() * 1000)
NEW = int((NOW - timedelta(days=30)).timestamp() * 1000)


def _inst(sym: str, category: str = "1", lever: str = "50", listed: int = OLD) -> dict[str, Any]:
    return {
        "instId": f"{sym}-USDT-SWAP",
        "instCategory": category,
        "lever": lever,
        "listTime": str(listed),
        "state": "live",
    }


def _tick(
    sym: str,
    turnover_m: float = 100,
    last: float = 10.0,
    open24h: float = 10.0,
    spread_bp: float = 1.0,
) -> dict[str, Any]:
    ask = last * (1 + spread_bp / 10_000)
    return {
        "instId": f"{sym}-USDT-SWAP",
        "last": str(last),
        "open24h": str(open24h),
        "volCcy24h": str(turnover_m * 1e6 / last),
        "bidPx": str(last),
        "askPx": str(ask),
    }


def _screen(
    instruments: list[dict[str, Any]], tickers: list[dict[str, Any]], **kw: Any
) -> list[str]:
    rows = uni.screen(instruments, tickers, now=NOW, **kw)
    return [r.symbol for r in rows]


def test_the_screen_keeps_liquid_seasoned_crypto_perps() -> None:
    assert _screen([_inst("PEPE")], [_tick("PEPE")]) == ["PEPE"]


@pytest.mark.parametrize(
    ("inst", "tick", "why"),
    [
        (_inst("AAPL", category="3"), _tick("AAPL"), "tokenized equity"),
        (_inst("XAU", category="4"), _tick("XAU"), "commodity"),
        (_inst("NEWT", listed=NEW), _tick("NEWT"), "listed < 90 days"),
        (_inst("LOWLEV", lever="5"), _tick("LOWLEV"), "max leverage < 10"),
        (_inst("THIN"), _tick("THIN", turnover_m=10), "turnover < $30M"),
        (_inst("WIDE"), _tick("WIDE", spread_bp=10), "spread > 0.06%"),
        (_inst("USDC"), _tick("USDC"), "stablecoin"),
        (_inst("WBTC"), _tick("WBTC"), "wrapped"),
        (_inst("BTC3L"), _tick("BTC3L"), "leveraged token"),
    ],
)
def test_the_screen_rejects(inst: dict[str, Any], tick: dict[str, Any], why: str) -> None:
    assert _screen([inst], [tick]) == [], why


def test_equity_perps_are_rejected_even_if_the_denylist_misses_them() -> None:
    # 177 equity perps exist; the old 15-name denylist would have let ~170 through.
    assert _screen([_inst("ANTHROPIC", category="3")], [_tick("ANTHROPIC")]) == []


def test_blocklisted_excluded_and_already_listed_symbols_are_skipped() -> None:
    insts = [_inst(s) for s in ("PEPE", "SCAM", "HELD", "BTC")]
    ticks = [_tick(s) for s in ("PEPE", "SCAM", "HELD", "BTC")]
    got = _screen(insts, ticks, blocklist={"SCAM"}, exclude={"HELD"}, already_listed={"BTC"})
    assert got == ["PEPE"]


def test_whitelist_entries_expire_after_the_window() -> None:
    entries = [
        {
            "symbol": "FIL",
            "okx": "FIL-USDT-SWAP",
            "coingecko": "filecoin",
            "renewed": date(2026, 9, 17),
        },
        {"symbol": "OLD", "okx": "OLD-USDT-SWAP", "coingecko": "old", "renewed": date(2026, 8, 1)},
    ]
    active, expired = uni.active_whitelist(entries, today=date(2026, 9, 20), expiry_days=30)
    assert [a.symbol for a in active] == ["FIL"]
    assert active[0].expires == date(2026, 10, 17)
    assert expired == ["OLD"]


def test_attention_ranks_movers_and_rewards_a_near_catalyst() -> None:
    rows = [
        uni.ScreenRow("CALM", "CALM-USDT-SWAP", 100e6, ret_24h=0.5),
        uni.ScreenRow("MOVER", "MOVER-USDT-SWAP", 100e6, ret_24h=12.0),
        uni.ScreenRow("EVENT", "EVENT-USDT-SWAP", 100e6, ret_24h=0.6),
    ]
    ranked = uni.rank_by_attention(rows, catalyst_symbols={"EVENT"})
    # z(|ret_24h|): MOVER +1.41, EVENT and CALM ≈ −0.7; the +2 bonus lifts EVENT to +1.30.
    assert [r.symbol for r in ranked] == ["MOVER", "EVENT", "CALM"]
    event = next(r for r in ranked if r.symbol == "EVENT")
    assert event.components["catalyst"] == 2.0


def test_missing_attention_components_count_as_zero_not_as_errors() -> None:
    rows = [
        uni.ScreenRow("A", "A-USDT-SWAP", 1e8, ret_24h=5.0, ret_7d=None, rel_volume=None),
        uni.ScreenRow("B", "B-USDT-SWAP", 1e8, ret_24h=1.0, ret_7d=20.0, rel_volume=3.0),
    ]
    ranked = uni.rank_by_attention(rows)
    assert {r.symbol for r in ranked} == {"A", "B"}
    assert all(r.attention is not None for r in ranked)


def test_selection_caps_satellites_and_the_dossier_budget() -> None:
    core = [uni.Asset(s, f"{s}-USDT-SWAP", "x", "core") for s in ("BTC", "ETH")]
    whitelist = [uni.Asset("FIL", "FIL-USDT-SWAP", "filecoin", "whitelist")]
    ranked = [
        uni.ScreenRow(s, f"{s}-USDT-SWAP", 1e8, attention=10 - i)
        for i, s in enumerate(["P1", "P2", "P3", "P4"])
    ]
    chosen = uni.select(core, whitelist, ranked, max_satellites=3, max_dossiers=5)
    assert [a.symbol for a in chosen] == ["BTC", "ETH", "FIL", "P1", "P2"]  # budget of 5 wins
    assert [a.source for a in chosen][-2:] == ["satellite", "satellite"]


def test_core_is_never_cut_by_the_budget() -> None:
    core = [uni.Asset(s, f"{s}-USDT-SWAP", "x", "core") for s in ("A", "B", "C")]
    chosen = uni.select(core, [], [], max_satellites=3, max_dossiers=2)
    assert [a.symbol for a in chosen] == ["A", "B", "C"]


def test_enrichment_fills_the_remaining_attention_components() -> None:
    import numpy as np
    import pandas as pd

    close = pd.Series(100.0 * np.cumprod(np.full(30, 1.01)))
    daily = pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": [1.0] * 29 + [3.0]}
    )
    row = uni.ScreenRow("PEPE", "PEPE-USDT-SWAP", 1e8, ret_24h=2.0)
    uni.enrich(row, daily=daily, funding=0.0004)
    assert row.ret_7d is not None and row.ret_7d > 7
    assert row.rel_volume == pytest.approx(3.0)
    assert row.funding == pytest.approx(0.0004)


def test_a_one_day_spike_alone_does_not_top_the_ranking() -> None:
    # Measured 2026-09-17: with |ret_24h| as the only input, a +140% name won by default.
    spike = uni.ScreenRow(
        "SPIKE", "SPIKE-USDT-SWAP", 1e8, ret_24h=140.0, ret_7d=5.0, rel_volume=1.0, funding=0.0001
    )
    steady = [
        uni.ScreenRow(
            f"S{i}",
            f"S{i}-USDT-SWAP",
            1e8,
            ret_24h=4.0,
            ret_7d=30.0 + i,
            rel_volume=3.0,
            funding=0.0008,
        )
        for i in range(3)
    ]
    quiet = [
        uni.ScreenRow(
            f"Q{i}", f"Q{i}-USDT-SWAP", 1e8, ret_24h=1.0, ret_7d=1.0, rel_volume=1.0, funding=0.0001
        )
        for i in range(4)
    ]
    ranked = uni.rank_by_attention([spike, *steady, *quiet])
    assert ranked[0].symbol.startswith("S") and ranked[0].symbol != "SPIKE"


def test_the_mania_guard_drops_names_that_already_moved() -> None:
    # owner 2026-09-17: a satellite up >50% in 24h or trading >20x normal volume is the
    # "arrive after the move" case option C was rejected for. Add it by hand if you want it.
    rows = [
        uni.ScreenRow("ONE", "ONE-USDT-SWAP", 1.5e8, ret_24h=139.7, rel_volume=148.0),
        uni.ScreenRow("PUMPED", "PUMPED-USDT-SWAP", 1e8, ret_24h=-62.0, rel_volume=3.0),
        uni.ScreenRow("VOLSPIKE", "VOL-USDT-SWAP", 1e8, ret_24h=12.0, rel_volume=25.0),
        uni.ScreenRow("NORMAL", "NORMAL-USDT-SWAP", 1e8, ret_24h=9.0, rel_volume=2.4),
    ]
    kept, dropped = uni.drop_mania(rows)
    assert [r.symbol for r in kept] == ["NORMAL"]
    assert {d[0] for d in dropped} == {"ONE", "PUMPED", "VOLSPIKE"}
    assert "24h" in dict(dropped)["ONE"]


def test_the_mania_guard_keeps_names_with_unknown_components() -> None:
    rows = [uni.ScreenRow("NEW", "NEW-USDT-SWAP", 1e8, ret_24h=None, rel_volume=None)]
    kept, dropped = uni.drop_mania(rows)
    assert [r.symbol for r in kept] == ["NEW"] and dropped == []


@pytest.mark.parametrize("symbol", ["JUP", "SYRUP", "SETUP", "BEAMX", "BEAR"])
def test_real_coins_that_end_like_a_leveraged_token_are_not_excluded(symbol: str) -> None:
    """D-18d: `(UP|DOWN|BULL|BEAR)$` excluded JUP (and any coin ending in UP) as a leveraged
    token, so it could never reach the screen."""
    assert not uni._excluded_kind(symbol)


@pytest.mark.parametrize("symbol", ["BTC3L", "ETH5S", "BTCUP", "ETHDOWN", "ETHBULL", "XRPBEAR"])
def test_leveraged_tokens_are_still_excluded(symbol: str) -> None:
    assert uni._excluded_kind(symbol)


def _knowledge(monkeypatch: pytest.MonkeyPatch, core: list[str], whitelist: list[str]) -> None:
    today = datetime.now(UTC).date()
    files = {
        "whitelist.yaml": {
            "entries": [{"symbol": s, "okx": f"{s}-USDT-SWAP", "renewed": today} for s in whitelist]
        },
        "blocklist.yaml": {"symbols": ["TRUMP", "USELESS"]},
    }
    monkeypatch.setattr(uni, "load_yaml", lambda name, *a, **k: files[name])
    monkeypatch.setattr(
        uni,
        "universe_config",
        lambda: {"core": [{"symbol": s, "okx": f"{s}-USDT-SWAP"} for s in core]},
    )


def test_a_blocklisted_symbol_on_the_whitelist_is_not_tradable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-15: the blocklist guarded only the satellite screen; a blocked symbol left on the
    whitelist (or added to core) stayed in every Manager run."""
    _knowledge(monkeypatch, core=["BTC", "USELESS"], whitelist=["HYPE", "TRUMP"])
    active, _ = uni.whitelist_assets()
    assert [a.symbol for a in active] == ["HYPE"]
    assert [a.symbol for a in uni.core_assets()] == ["BTC"]


def test_excluded_kinds_never_reach_core_or_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    """blocklist.yaml's `exclude_kinds` (stablecoin, wrapped, leveraged token) apply to every
    list, not only the screen."""
    _knowledge(monkeypatch, core=["ETH", "WBTC"], whitelist=["SOL", "USDC", "BTC3L"])
    active, _ = uni.whitelist_assets()
    assert [a.symbol for a in active] == ["SOL"]
    assert [a.symbol for a in uni.core_assets()] == ["ETH"]


def test_blocklisted_tier_c_names_never_reach_the_screen() -> None:
    blocked = uni.blocklist()
    assert {"USELESS", "TRUMP", "PUMP"} <= blocked
    instruments = [_inst(s) for s in ("USELESS", "TRUMP", "PUMP", "PEPE")]
    tickers = [_tick(s) for s in ("USELESS", "TRUMP", "PUMP", "PEPE")]
    assert _screen(instruments, tickers, blocklist=blocked) == ["PEPE"]
