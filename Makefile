.PHONY: sync test lint type doctor gate1
sync:      ; uv sync --all-extras
test:      ; uv run pytest
lint:      ; uv run ruff check . && uv run ruff format --check .
type:      ; uv run mypy argus/gate argus/exchange argus/execution argus/journal/states.py
doctor:    ; uv run argus doctor
gate1: lint type test   # Gate 1 of the two-gate verification (AGENTS.md)
