"""Untrusted numbers: bool and non-finite values must not pass as data (Muse P1-3)."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from argus.data.envelope import finite_number
from argus.data.fetchers import kalshi
from argus.features import market
from argus.features.regime import RegimeInputs


@pytest.mark.parametrize(
    "bad", [True, False, math.nan, math.inf, -math.inf, "nan", "inf", "", None]
)
def test_finite_number_rejects(bad: object) -> None:
    assert finite_number(bad) is None


def test_finite_number_accepts_real_numbers() -> None:
    assert finite_number(3) == 3.0
    assert finite_number("0.25") == 0.25
    assert finite_number(-1.5) == -1.5


def test_market_number_rejects_bool_and_nan() -> None:
    assert market._number(True) is None
    assert market._number("nan") is None
    assert market._number("0.0001") == 0.0001


def test_regime_inputs_reject_bool() -> None:
    with pytest.raises(ValidationError):
        RegimeInputs(btc_close=True)  # type: ignore[arg-type]


def test_regime_inputs_treat_non_finite_as_missing() -> None:
    inputs = RegimeInputs(btc_close=math.nan, fear_greed=math.inf)
    assert inputs.btc_close is None and inputs.fear_greed is None


def test_regime_inputs_validate_on_assignment() -> None:
    inputs = RegimeInputs()
    inputs.btc_ema20 = math.nan
    assert inputs.btc_ema20 is None
    with pytest.raises(ValidationError):
        inputs.btc_ema50 = True  # type: ignore[assignment]


def test_kalshi_probability_ignores_bool_cents() -> None:
    assert kalshi._probability({"last_price": True}) is None
    assert kalshi._probability({"last_price": 40}) == 0.4


def test_funding_percentile_survives_a_garbage_funding_rate() -> None:
    assert market.funding_percentile(["x"], [0.1, 0.2]) is None  # type: ignore[list-item]
    assert market.funding_percentile(0.15, [0.1, 0.2]) == 50.0
