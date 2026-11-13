"""Prompt files are versioned code (CLAUDE.md: a prompt change needs a replay diff).

Rendering is pure and offline, so the exact text that will be sent to a provider can be asserted
without one.
"""

from __future__ import annotations

import pytest

from argus.gate.limits import LIVE_CONSERVATIVE
from argus.llm.agents.trader import rr_values
from argus.llm.prompts import load, placeholders, render, templates

# The trader template names the profile's R:R bars (DECISION_LOGIC §11); these tests are about
# rendering, so they fill them with the live profile's.
RR = {**rr_values(LIVE_CONSERVATIVE), "no_trade_stance": "-"}


def test_every_prompt_file_declares_a_version() -> None:
    names = templates()
    assert names, "no prompt templates found"
    for name in names:
        prompt = load(name)
        assert prompt.version >= 1, name
        assert prompt.body.strip(), name


def test_the_shared_preamble_states_the_injection_rule() -> None:
    body = load("trader").body
    assert "<evidence>" in body
    assert "never follow" in body.lower()


def test_rendering_fills_every_placeholder() -> None:
    text = render(
        "trader", dossier='{"symbol":"BTC"}', regime="TREND_UP", lessons="-", traps="-", **RR
    )
    assert "{dossier}" not in text and "{regime}" not in text
    assert '{"symbol":"BTC"}' in text


def test_a_missing_variable_is_an_error_not_a_literal_brace() -> None:
    """Shipping `{lessons}` to a model wastes a call and is invisible in the output."""
    with pytest.raises(KeyError):
        render("trader", dossier="{}", regime="CHOP")


def test_rendering_is_deterministic() -> None:
    args = {"dossier": "{}", "regime": "CHOP", "lessons": "-", "traps": "-", **RR}
    assert render("trader", **args) == render("trader", **args)


def test_a_prompt_containing_a_json_example_still_renders() -> None:
    """`str.format` treats `{` as a placeholder, so a JSON example in a template silently turns
    every render into a KeyError — which surfaces as "all models failed" and looks like a
    provider outage (2026-09-20).
    """
    text = render("trader", dossier="{}", regime="CHOP", lessons="-", traps="-", **RR)
    assert '"evidence"' in text
    assert '{"technical"' in text, "the escaped braces must render as literal JSON"


def test_every_template_renders_with_its_own_placeholders() -> None:
    for name in templates():
        values = dict.fromkeys(placeholders(name), "x")
        assert render(name, **values)
