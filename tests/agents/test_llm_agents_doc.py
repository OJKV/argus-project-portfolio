"""docs/LLM_AGENTS.md describes the agents the code actually has (L-13, L-4).

The roster was the design-era plan: `TraderDecision`, `RedTeamNote`, three Risk Debate personas,
an `invalidation_hit` flag — none of which exist — and three agents listed as if they ran that
no production code calls. A spec that disagrees with the code gets trusted exactly when someone
is trying to understand a live decision. These tests tie the document to the code it describes.
"""

from __future__ import annotations

import re
from pathlib import Path

import argus.llm.schemas as schemas
from argus.config import REPO_ROOT
from argus.features.review_flags import compute_flags

DOC = REPO_ROOT / "docs" / "LLM_AGENTS.md"
AGENTS_DIR = REPO_ROOT / "argus" / "llm" / "agents"


def _section(title: str) -> str:
    text = DOC.read_text()
    start = text.index(title)
    following = re.search(r"\n#{2,3} ", text[start + len(title) :])
    return text[start : start + len(title) + following.start()] if following else text[start:]


def _roster() -> list[str]:
    return [line for line in _section("## 2. Agent roster").splitlines() if line.startswith("| ")]


def _live_agents() -> set[str]:
    """Agent modules imported by production code outside `argus/llm/` itself."""
    pattern = re.compile(r"argus\.llm\.agents(?:\.(\w+)| import ([\w ,]+))")
    live: set[str] = set()
    for path in (REPO_ROOT / "argus").rglob("*.py"):
        if AGENTS_DIR.parent in path.parents:
            continue
        for dotted, imported in pattern.findall(path.read_text()):
            names = [dotted] if dotted else [n.split(" as ")[0] for n in imported.split(",")]
            live.update(name.strip() for name in names if name.strip())
    return live


def test_every_output_schema_in_the_roster_exists() -> None:
    named = {
        name
        for row in _roster()[2:]
        for name in re.findall(r"`([A-Z][A-Za-z]+)", row.split("|")[-2])
    }
    assert named, "the roster names no output schema"
    missing = sorted(name for name in named if not hasattr(schemas, name))
    assert not missing, f"LLM_AGENTS.md roster names schemas that do not exist: {missing}"


def test_every_agent_module_has_a_row_saying_whether_it_is_live() -> None:
    modules = {p.stem for p in AGENTS_DIR.glob("*.py") if p.stem != "__init__"}
    live = _live_agents()
    rows = {
        module: row
        for row in _roster()
        for module in re.findall(r"`agents/(\w+)\.py`", row.split("|")[1])
    }
    assert set(rows) == modules, f"rows for {sorted(set(rows))}, modules {sorted(modules)}"
    for module, row in rows.items():
        marked_not_live = "not in the live path" in row
        assert marked_not_live == (module not in live), (
            f"{module}: live={module in live} but the roster says otherwise"
        )


def test_the_reviewer_flags_named_in_the_doc_are_the_ones_code_computes() -> None:
    computed = set(compute_flags({}, {}, {}, {}))
    named = set(re.findall(r"`(\w+)`", _section("### 3.6 Reviewer")))
    flag_like = {n for n in named if n.endswith(("_hit", "_breached", "_hostile", "_stale"))}
    assert flag_like, "§3.6 names no flags"
    assert flag_like <= computed, f"flags the code never computes: {sorted(flag_like - computed)}"


def test_the_risk_debate_is_described_as_the_one_call_it_is() -> None:
    from argus.llm.agents.riskdebate import SIZE_FACTOR

    row = next(r for r in _roster() if "agents/riskdebate.py" in r)
    assert "persona" not in row.lower()
    for recommendation, factor in SIZE_FACTOR.items():
        assert recommendation in row and f"{factor:g}" in row


def test_a_family_separation_the_roster_claims_is_configured() -> None:
    """A roster row saying "never the Trader's family" is a promise; the pair is what keeps it.

    The Reviewer row made it while nothing passed the Trader's family to the Reviewer (L-13);
    `test_reviewer.py` checks the plumbing, this checks the doc and the config agree.
    """
    from argus.llm.router import ADVERSARIAL_PAIRS

    claims = 0
    for row in _roster():
        claim = re.search(r"never the (\w+)'s family", row)
        module = re.search(r"agents/(\w+)\.py", row)
        if claim and module:
            claims += 1
            assert ADVERSARIAL_PAIRS.get(module.group(1)) == claim.group(1).lower(), row[:60]
    assert claims >= 3, "the roster stopped stating which families each check must avoid"


def test_the_doc_points_at_files_that_exist() -> None:
    for module in re.findall(r"`agents/(\w+)\.py`", DOC.read_text()):
        assert (AGENTS_DIR / f"{module}.py").exists(), module
    assert Path(REPO_ROOT / "argus" / "prompts" / "trader_batch.md").exists()
