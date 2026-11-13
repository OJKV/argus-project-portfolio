from argus.config import universe_config


def test_core_is_twelve_unique_okx_swaps() -> None:
    core = universe_config()["core"]
    assert len(core) == 12
    assert len({a["symbol"] for a in core}) == 12
    assert all(a["okx"].endswith("-USDT-SWAP") for a in core)


def test_whitelist_entries_are_complete_and_dated() -> None:
    from datetime import date

    from argus.config import KNOWLEDGE_DIR, load_yaml

    entries = load_yaml("whitelist.yaml", KNOWLEDGE_DIR)["entries"]
    assert {e["symbol"] for e in entries} == {"FIL", "ENA", "AAVE", "PEPE", "NEAR", "BCH"}
    for entry in entries:
        assert entry["okx"] == f"{entry['symbol']}-USDT-SWAP"
        assert entry["coingecko"] and entry["reason"]
        assert isinstance(entry["renewed"], date)  # expiry is computed from this


def test_whitelist_never_duplicates_core() -> None:
    from argus.config import KNOWLEDGE_DIR, load_yaml, universe_config

    core = {a["symbol"] for a in universe_config()["core"]}
    listed = {e["symbol"] for e in load_yaml("whitelist.yaml", KNOWLEDGE_DIR)["entries"]}
    assert not core & listed


def test_dossier_budget_fits_core_whitelist_and_satellites() -> None:
    from argus.config import KNOWLEDGE_DIR, load_yaml, universe_config

    universe = universe_config()
    whitelist = load_yaml("whitelist.yaml", KNOWLEDGE_DIR)["entries"]
    needed = len(universe["core"]) + len(whitelist) + universe["satellites"]["max"]
    assert universe["max_dossiers_per_run"] >= needed


def test_core_liquidity_uses_the_seven_day_median_not_one_day() -> None:
    # LINK read $53M on 2026-09-16 and $47M on 2026-09-17: a single-day floor makes core
    # membership flap on noise. UNIVERSE_OPTIONS.md specifies the 7-day median.
    from argus.doctor import median_daily_turnover

    closed = [
        ["t", "o", "h", "l", "c", "v", "vc", str(q), "1"]
        for q in (60e6, 55e6, 47e6, 52e6, 58e6, 61e6, 49e6)
    ]
    forming = ["t", "o", "h", "l", "c", "v", "vc", "1000", "0"]  # today's partial candle
    rows = [forming, *closed]  # OKX returns newest first
    assert median_daily_turnover(rows) == 55e6


def test_median_turnover_needs_closed_candles() -> None:
    from argus.doctor import median_daily_turnover

    assert median_daily_turnover([["t", "o", "h", "l", "c", "v", "vc", "1", "0"]]) is None
    assert median_daily_turnover([]) is None


def test_floors_admit_the_approved_borderline_names() -> None:
    # owner 2026-09-17: keep BNB ($49M 7d median), LINK ($43M) in core and AAVE ($29M) on the
    # whitelist. Floors sit ~20% below the weakest approved name so one soft week doesn't evict it.
    from argus.config import universe_config

    universe = universe_config()
    assert universe["core_liquidity_floor"]["min_turnover_24h_usd"] <= 43_000_000 * 0.85
    assert universe["whitelist_liquidity_floor"]["min_turnover_24h_usd"] <= 29_000_000 * 0.85


def test_the_automatic_satellite_screen_is_not_loosened_by_the_whitelist_floor() -> None:
    # The whitelist is human-curated; satellites are picked by code and keep the stricter bar.
    from argus.config import universe_config

    universe = universe_config()
    whitelist_floor = universe["whitelist_liquidity_floor"]["min_turnover_24h_usd"]
    assert universe["satellites"]["screen"]["min_vol_24h_usd"] > whitelist_floor
