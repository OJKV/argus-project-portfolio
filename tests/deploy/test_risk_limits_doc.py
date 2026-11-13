"""The operator docs state the risk limits the code enforces (review 2026-09-18 P-9, OPEN on
2026-10-07).

The drawdown kill was 15 in code, 20 in the aggressive yaml profile and "≤12 %" in STATUS; the
position cap was 4 in code and 2 in yaml and DECISION_LOGIC; HUMAN_GUIDE called `conservative` the
default while the manager always picks `aggressive`, and offered a `/risk moderate` that no
profile backs. An owner deciding whether to go live reads the docs, not `gate/rules.py`. These
tests pin RUNBOOK's "Risk limits in force" table to the code's own constants, so the table moves
when the code does — no number here is a target, only a reading.
"""

from __future__ import annotations

import re

import pytest

from .systemd_units import REPO

RUNBOOK = REPO / "docs" / "SAFETY.md"
HUMAN_GUIDE = REPO / "docs" / "SAFETY.md"
ENV_EXAMPLE = REPO / ".env.example"


def _row(limit: str) -> str:
    text = RUNBOOK.read_text()
    assert "## Risk limits in force" in text, "RUNBOOK has no 'Risk limits in force' section"
    section = text[text.index("## Risk limits in force") :].split("\n## ", 1)[0]
    for line in section.splitlines():
        if line.startswith(f"| {limit} |"):
            return line.split("|")[2]
    raise AssertionError(f"no row for {limit!r}")


def _number(cell: str) -> float:
    found = re.search(r"\d+(?:\.\d+)?", cell)
    assert found, cell
    return float(found.group(0))


def test_drawdown_kill_matches_both_places_the_code_holds_it() -> None:
    from argus.gate.killswitch import PORTFOLIO_DD_LIMIT_PCT
    from argus.runs.watchdog import DD_HALT_PCT

    assert float(DD_HALT_PCT) == PORTFOLIO_DD_LIMIT_PCT  # the doc can only state one number
    assert _number(_row("Portfolio drawdown kill")) == PORTFOLIO_DD_LIMIT_PCT


@pytest.mark.parametrize(
    "limit,constant",
    [("Daily loss halt", "DAILY_LOSS_LIMIT_PCT"), ("Weekly loss halt", "WEEKLY_LOSS_LIMIT_PCT")],
)
def test_loss_halts_match_the_protections(limit: str, constant: str) -> None:
    from argus.gate import protections

    assert _number(_row(limit)) == abs(getattr(protections, constant))


def test_position_cap_matches_the_gate() -> None:
    from argus.gate.rules import MAX_POSITIONS

    assert _number(_row("Max open positions")) == MAX_POSITIONS


def _selection() -> tuple[str, str]:
    """What the code picks with RISK_PROFILE empty: (small account, account that held $500 a week).

    Read from `select_profile` itself, so the docs are checked against the code rather than
    against a copy of it (review G-5 replaced the fixed `_default_profile`).
    """
    from argus.config import risk_config
    from argus.gate.risk_config import parse, select_profile

    config = parse(risk_config())
    return (
        select_profile(config, 150.0),
        select_profile(config, 2000.0, lowest_recent_equity=2000.0),
    )


def test_default_profile_matches_the_manager() -> None:
    small, grown = _selection()
    assert (small, grown) == ("aggressive", "conservative")
    row = _row("Default profile")
    assert f"`{small}` below $500" in row and f"`{grown}`" in row


def test_human_guide_offers_only_profiles_that_exist() -> None:
    from typing import get_args

    from argus.config import Settings

    real = {p for p in get_args(Settings.model_fields["risk_profile"].annotation) if p}
    text = HUMAN_GUIDE.read_text()
    for named in re.findall(r"/risk ([a-z|]+)", text):
        assert set(named.split("|")) <= real, named
    assert "moderate" not in text
    small, grown = _selection()
    assert f"`{small}` below $500" in text and f"`{grown}` once equity" in text


def test_env_example_names_the_real_default() -> None:
    line = next(x for x in ENV_EXAMPLE.read_text().splitlines() if x.startswith("# RISK_PROFILE"))
    small, grown = _selection()
    assert f"empty = {small} below $500, {grown} once equity" in line, line
