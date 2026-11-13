# ARGUS

ARGUS is a Python research and engineering project for LLM-assisted analysis of crypto perpetual markets. Deterministic code validates proposed trades, controls risk and handles execution; the model does not set position size or bypass the risk gate.

The project includes data-quality checks, a journal, an exchange adapter, an offline test suite and a read-only dashboard. Demo and live execution are separate modes with explicit safety interlocks. Historical experiments and design goals should not be interpreted as proof of profitable live trading.

## Local verification

Install Python 3.12 and [uv](https://docs.astral.sh/uv/), then run:

```sh
uv sync --all-extras
uv run ruff check .
uv run mypy argus/gate argus/exchange argus/execution
uv run pytest -q
```

The offline golden-set tests check fixtures and scoring logic; they do not measure model performance without model calls.

Deployment privilege configuration requires an explicit operator username; `deploy/grant-claude.sh --print-rules <operator-username>` previews the generated rules without installing them.

This portfolio copy does not create or advance a deployment ref. Its CI runs checks only; automatic deployment is disabled by default and requires a separately reviewed promotion mechanism plus an explicit opt-in.
