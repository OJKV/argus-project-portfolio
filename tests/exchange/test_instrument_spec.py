"""Review 2026-09-18 X-14: `instrument_spec` silently defaulted safety-critical values.

`ctVal or 1` on BTC (real 0.01) is a 100x mis-size; `lever or 1`, `min or 0` and `tick or 0` are
the same shape. A missing field is the venue saying something we did not expect, and F-030's
rule is that we refuse to do arithmetic on it rather than guess.
"""

from __future__ import annotations

from typing import Any

import pytest

from argus.exchange.okx import ExchangeResponseError, OKXAdapter


def _market(**info_over: Any) -> dict[str, Any]:
    info = {"ctVal": "0.01", "lever": "100", "ctValCcy": "BTC", "state": "live"}
    info.update(info_over)
    return {
        "id": "BTC-USDT-SWAP",
        "info": {k: v for k, v in info.items() if v is not None},
        "limits": {"amount": {"min": 0.01}},
        "precision": {"price": 0.1},
    }


def _adapter(monkeypatch: pytest.MonkeyPatch, market: dict[str, Any]) -> OKXAdapter:
    instance = OKXAdapter.__new__(OKXAdapter)
    monkeypatch.setattr(OKXAdapter, "_market", lambda _s, _sym: market)
    return instance


def test_a_complete_spec_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _adapter(monkeypatch, _market()).instrument_spec("BTC/USDT:USDT")
    assert str(spec["ct_val"]) == "0.01" and spec["max_leverage"] == 100


@pytest.mark.parametrize("field", ["ctVal", "lever"])
def test_a_missing_contract_field_is_an_error_not_a_default(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    with pytest.raises(ExchangeResponseError, match=field):
        _adapter(monkeypatch, _market(**{field: None})).instrument_spec("BTC/USDT:USDT")


def test_a_zero_contract_value_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ExchangeResponseError, match="ctVal"):
        _adapter(monkeypatch, _market(ctVal="0")).instrument_spec("BTC/USDT:USDT")


def test_a_missing_tick_or_minimum_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    market = _market()
    market["precision"]["price"] = None
    with pytest.raises(ExchangeResponseError, match="tick"):
        _adapter(monkeypatch, market).instrument_spec("BTC/USDT:USDT")
    market = _market()
    market["limits"]["amount"]["min"] = None
    with pytest.raises(ExchangeResponseError, match="min"):
        _adapter(monkeypatch, market).instrument_spec("BTC/USDT:USDT")


def test_an_instrument_that_is_not_live_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ExchangeResponseError, match="suspend"):
        _adapter(monkeypatch, _market(state="suspend")).instrument_spec("BTC/USDT:USDT")
