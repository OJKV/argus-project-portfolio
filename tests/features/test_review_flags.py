"""Facts code can compute must not be left to a model to infer.

Measured 2026-09-20: the Reviewer followed `tp1_hit` and `time_stop_breached` reliably, and
missed funding at the 98th percentile and a thesis visibly weakening — both sitting in the
dossier as raw numbers. Anything deterministic belongs here; the model is for judgement.
"""

from __future__ import annotations

from argus.features.review_flags import compute_flags


def _position(**over):
    base = {
        "side": "long",
        "entry_price": 100.0,
        "mark_price": 104.0,
        "unrealised_r": 0.4,
        "hours_in_trade": 24,
        "current_stop": 96.0,
        "liq_price": 60.0,
    }
    base.update(over)
    return base


def _plan(**over):
    base = {"time_stop_hours": 120, "targets": [{"price": 112.0, "close_pct": 50}]}
    base.update(over)
    return base


def test_a_breached_time_stop_is_flagged() -> None:
    flags = compute_flags(_position(hours_in_trade=130), _plan(), {}, {})
    assert flags["time_stop_breached"] is True


def test_hostile_funding_is_flagged_only_when_it_is_against_us() -> None:
    against = compute_flags(
        _position(), _plan(), {"funding_pctile_30d": 96.0, "funding_8h": 0.002}, {}
    )
    assert against["funding_hostile"] is True

    # The same percentile pays a short rather than costing it.
    favourable = compute_flags(
        _position(side="short"), _plan(), {"funding_pctile_30d": 96.0, "funding_8h": 0.002}, {}
    )
    assert favourable["funding_hostile"] is False


def test_ordinary_funding_is_not_hostile() -> None:
    flags = compute_flags(
        _position(), _plan(), {"funding_pctile_30d": 55.0, "funding_8h": 0.0001}, {}
    )
    assert flags["funding_hostile"] is False


def test_a_stop_close_to_liquidation_is_flagged() -> None:
    near = compute_flags(
        _position(current_stop=94.0, liq_price=93.0, mark_price=95.0), _plan(), {}, {}
    )
    assert near["stop_near_liquidation"] is True
    far = compute_flags(_position(), _plan(), {}, {})
    assert far["stop_near_liquidation"] is False


def test_thinning_participation_reads_as_a_weakening_thesis() -> None:
    weak = compute_flags(_position(), _plan(), {"oi_24h_pct": -19.0}, {"rel_vol_4h": 0.35})
    assert weak["thesis_weakening"] is True
    healthy = compute_flags(_position(), _plan(), {"oi_24h_pct": 3.0}, {"rel_vol_4h": 1.2})
    assert healthy["thesis_weakening"] is False


def test_tp1_is_flagged_once_price_reaches_it() -> None:
    hit = compute_flags(_position(mark_price=112.5), _plan(), {}, {})
    assert hit["tp1_hit"] is True


def test_a_short_reaches_tp1_going_the_other_way() -> None:
    plan = _plan(targets=[{"price": 88.0, "close_pct": 100}])
    hit = compute_flags(_position(side="short", mark_price=87.0), plan, {}, {})
    assert hit["tp1_hit"] is True


def test_stale_data_is_flagged_from_provenance() -> None:
    flags = compute_flags(_position(), _plan(), {}, {"stale": True})
    assert flags["data_stale"] is True


def test_missing_inputs_never_raise() -> None:
    """A review must still happen when a family is missing; the flag simply stays False."""
    flags = compute_flags({}, {}, {}, {})
    assert flags["time_stop_breached"] is False
    assert set(flags) >= {"tp1_hit", "funding_hostile", "thesis_weakening", "data_stale"}


# --- L-3 / D-N1: the shapes production actually hands this function ---------------------------


def test_tp1_reads_the_target_shape_the_manager_journals() -> None:
    """`_record_decision` stores targets as `[[price, close_pct], ...]`, not dicts. Once the
    Reviewer could find its plan, `targets[0].get("price")` would have raised on every review."""
    plan = _plan(targets=[[112.0, 50], [124.0, 50]])
    assert compute_flags(_position(mark_price=112.5), plan, {}, {})["tp1_hit"] is True
    assert compute_flags(_position(mark_price=104.0), plan, {}, {})["tp1_hit"] is False


def test_a_plan_invalidation_that_fired_is_flagged() -> None:
    """`parse_rule` / `evaluate_plan_rules` had no caller; the plan's own invalidation was left
    for the model to spot in a JSON blob — the exact thing F-102 measured it missing."""
    plan = _plan(
        invalidation_rules=[{"kind": "price_close_below", "level": 92.0, "timeframe": "4h"}]
    )
    fired = compute_flags(_position(), plan, {}, {"price": 91.0})
    assert fired["invalidation_hit"] is True
    holding = compute_flags(_position(), plan, {}, {"price": 95.0})
    assert holding["invalidation_hit"] is False
    assert "invalidation_unchecked" not in holding


def test_a_daily_rule_reads_the_daily_close() -> None:
    plan = _plan(invalidation_rules=[{"kind": "price_close_below", "level": 92.0}])  # 1d default
    technical = {"price": 95.0, "ema_1d": {"close": 90.0}}
    assert compute_flags(_position(), plan, {}, technical)["invalidation_hit"] is True


def test_a_rule_code_cannot_check_is_named_not_passed() -> None:
    """An unparseable rule, or one whose input is missing, is not "not hit" — it is unchecked,
    and the model is told so instead of reading a False it might trust (F-013)."""
    plan = _plan(
        invalidation_rules=[
            {"kind": "vibes_turn_bad"},
            {"kind": "price_close_below", "level": 92.0, "timeframe": "1d"},
        ]
    )
    flags = compute_flags(_position(), plan, {}, {"price": 91.0})
    assert flags["invalidation_hit"] is None
    assert len(flags["invalidation_unchecked"]) == 2


def test_rules_code_cannot_evaluate_leave_invalidation_unknown_not_false() -> None:
    """The weekly close is 90, under the plan's 95: the rule has really fired, but no weekly
    close reaches a review. A False here is the F-013 pattern — a code-computed "not hit" the
    Reviewer is told to trust, resolving step 6 to HOLD on a broken thesis."""
    plan = _plan(
        invalidation_rules=[
            {"kind": "price_close_below", "level": 95.0, "timeframe": "1w"},
            {"kind": "event", "event": "ETF decision", "outcome": "denied"},
        ]
    )
    flags = compute_flags(_position(), plan, {}, {"price": 90.0})
    assert flags["invalidation_hit"] is None
    assert len(flags["invalidation_unchecked"]) == 2


def test_a_checked_rule_that_fired_is_reported_beside_an_unchecked_one() -> None:
    plan = _plan(
        invalidation_rules=[
            {"kind": "event", "event": "ETF decision"},
            {"kind": "price_close_below", "level": 92.0, "timeframe": "4h"},
        ]
    )
    flags = compute_flags(_position(), plan, {}, {"price": 91.0})
    assert flags["invalidation_hit"] is True
    assert len(flags["invalidation_unchecked"]) == 1


def test_a_plan_with_no_typed_rules_gets_no_invalidation_flag() -> None:
    """Nothing to evaluate is not "not hit": the model reads the plan's own words, as before."""
    assert "invalidation_hit" not in compute_flags(_position(), _plan(), {}, {"price": 91.0})


def test_the_golden_reviewer_prompts_stay_as_measured() -> None:
    """The Reviewer's 18/20 was measured on flags carrying `invalidation_hit` only where a case
    declared it. Always emitting it changed 18 of the 20 prompts — each gaining an explicit
    "invalidation not hit" — with no new measurement behind them."""
    from argus.llm.golden import REVIEW_CASES, review_flags

    for case in REVIEW_CASES:
        declared = case.payload.get("flags")  # a dict, or "AUTO" for whatever code computes
        expected = (
            declared.get("invalidation_hit", "absent") if isinstance(declared, dict) else "absent"
        )
        assert review_flags(case).get("invalidation_hit", "absent") == expected, case.name


def test_a_funding_flip_rule_is_evaluated() -> None:
    plan = _plan(invalidation_rules=[{"kind": "funding_flip", "sign": "negative"}])
    assert compute_flags(_position(), plan, {"funding_8h": -0.0001}, {})["invalidation_hit"]
    assert not compute_flags(_position(), plan, {"funding_8h": 0.0001}, {})["invalidation_hit"]
