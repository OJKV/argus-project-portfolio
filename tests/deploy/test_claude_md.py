"""The agent and operator docs describe the repo that exists (review 2026-09-18 P-7, PARTIAL on
2026-10-07).

CLAUDE.md's layout listed `features/screen.py`, `exchange/bybit.py` and `journal/models|
calibration|shadow.py`; its commands block listed `argus audit` and `replay --since`; HUMAN_GUIDE
told the operator to run `doctor --slugs --keys`. None of them exist. A coding agent following
the layout builds a second copy of something that lives elsewhere; one running `argus audit` to
"verify the ledger" gets an error and may conclude the ledger is fine; an operator running a
flag that does not exist learns nothing about key age. Anything not built yet stays listed,
marked `(planned`, so the plan is not lost — the tests skip those lines and nothing else.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import typer

from .systemd_units import REPO

CLAUDE = REPO / "CLAUDE.md"
AGENTS = REPO / "AGENTS.md"
# The docs an agent or the operator copies commands from. LLM_AGENTS.md belongs to the LLM slice
# and STATUS/FAILURE_LOG are history, so they are not policed here.
OPERATOR_DOCS = [
    CLAUDE,
    AGENTS,
    REPO / "docs" / "SAFETY.md",
]


def _block(heading: str) -> list[str]:
    text = CLAUDE.read_text()
    section = text[text.index(heading) :]
    return section.split("```")[1].splitlines()


def _layout_files() -> list[tuple[str, str]]:
    """(directory, file) for every `*.py` the layout names outside a `(planned)` note."""
    found = []
    for line in _block("## Repo layout"):
        match = re.match(r"^  ([\w/]+)/\s+#\s*(.*)$", line)
        if not match:
            continue
        folder, notes = match.groups()
        for item in re.split(r",\s*(?![^{]*\})", notes):
            if "(planned" in item:
                continue
            name = re.match(r"\s*([\w/{},<>]+\.py)", item)
            if not name or "<" in name.group(1):
                continue
            stem = name.group(1)
            braces = re.match(r"(.*)\{(.*)\}(.*)", stem)
            options = braces.group(2).split(",") if braces else [""]
            for option in options:
                path = f"{braces.group(1)}{option}{braces.group(3)}" if braces else stem
                found.append((folder, path))
    return found


@pytest.mark.parametrize("folder,name", _layout_files())
def test_every_file_the_layout_names_exists(folder: str, name: str) -> None:
    assert (REPO / "argus" / folder / name).exists(), f"argus/{folder}/{name}"


def test_the_layout_parser_reads_something() -> None:
    assert len(_layout_files()) > 30


def _cli() -> Any:
    """The click group behind the typer app (typer may ship its own click, so no isinstance)."""
    import argus.cli as cli

    return typer.main.get_command(cli.app)


def _code_spans(text: str) -> list[str]:
    """Each line of every fenced block, and every inline `code` span outside them."""
    spans: list[str] = []
    for i, chunk in enumerate(text.split("```")):
        if i % 2:
            spans.extend(chunk.splitlines())
        else:
            spans.extend(re.findall(r"`([^`\n]+)`", chunk))
    return spans


_INVOCATION = re.compile(r"(?:^|(?<=\s))argus ([a-z][a-z-]*)(.*?)(?=$|[;|&]|\bargus )")


def _invocations(doc: Path) -> list[tuple[str, str, list[str]]]:
    """(line, command, flags) for every `argus <command> [--flag ...]` a doc tells you to run.

    Only code is read — prose saying "argus reads .env" is not an instruction. A line or span
    carrying `(planned` is skipped: that is how a doc keeps an unbuilt command on the record.
    """
    found = []
    text = doc.read_text()
    for span in _code_spans(text):
        line = next((full for full in text.splitlines() if span in full), span)
        if "(planned" in span or "(planned" in line:
            continue
        code = span.split(" #", 1)[0]
        for match in _INVOCATION.finditer(code):
            before = code[: match.start()].split()
            if before and before[-1] in {"-u", "--user", "chown", "id", "su"}:
                continue  # `sudo -u argus ...` names the user, not a subcommand
            flags = re.findall(r"(?<!\S)(--[a-z][a-z-]*)", match.group(2))
            found.append((span, match.group(1), flags))
    return found


def test_the_invocation_parser_reads_something() -> None:
    assert len(_invocations(CLAUDE)) > 10
    assert any(cmd == "doctor" for _, cmd, _ in _invocations(REPO / "docs" / "SAFETY.md"))


@pytest.mark.parametrize("doc", OPERATOR_DOCS, ids=lambda d: d.name)
def test_every_argus_command_and_flag_a_doc_names_exists(doc: Path) -> None:
    group = _cli()
    for span, name, flags in _invocations(doc):
        assert name in group.commands, f"{doc.name}: `argus {name}` does not exist: {span}"
        command = group.commands[name]
        known = {opt for param in command.params for opt in (*param.opts, *param.secondary_opts)}
        for flag in flags:
            if flag == "--help":
                continue
            assert flag in known, f"{doc.name}: `argus {name} {flag}` does not exist: {span}"


def test_stub_commands_are_marked_planned() -> None:
    """A command that exists only as `_not_yet` must not be listed as if it worked."""
    import inspect

    group = _cli()
    stubs = {
        name
        for name, command in group.commands.items()
        if command.callback is not None and "_not_yet(" in inspect.getsource(command.callback)
    }
    assert stubs, "the stub detector found nothing — has backtest-rules been built?"
    for doc in OPERATOR_DOCS:
        for span, name, _ in _invocations(doc):
            assert name not in stubs, f"{doc.name}: `argus {name}` is a stub: {span}"


def test_replay_prompt_version_is_not_presented_as_working() -> None:
    """`replay --prompt-version` exits 2 ("landing in Phase 4"); `--since` was never written."""
    for doc in (CLAUDE, AGENTS):
        for line in doc.read_text().splitlines():
            if "--prompt-version" in line or "replay --since" in line:
                assert "(planned" in line, f"{doc.name}: {line}"
