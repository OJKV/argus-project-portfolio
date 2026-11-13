#!/usr/bin/env bash
# Read-only health checks on the box. Places nothing, changes nothing, prints no secret — with
# one named exception, `demo-tests`, which opens and closes a position on the OKX DEMO account.
#
# Exists as its own script so it can be allowed in sudoers without also granting
# `argus manager`, which places orders whenever MODE=approve|auto (trade.sh is that grant).
#
#   sudo -u argus /opt/argus-project/deploy/checks.sh [keys|models|universe|auth|all]
#
set -euo pipefail
cd "$(dirname "$0")/.."   # logfire reads pyproject.toml from the CWD at import time

BIN=.venv/bin/argus
UV=$(command -v uv || true)
[ -x "$UV" ] || UV=/home/argus/.local/bin/uv

case "${1:-keys}" in
  keys)     exec "$BIN" doctor --no-network ;;
  models)   PYDANTIC_AI_NO_BANNER=1 exec "$BIN" doctor --no-network --models ;;
  universe) exec "$BIN" doctor --universe ;;
  auth)     exec "$BIN" doctor --auth ;;
  live)     exec "$BIN" doctor --live-keys ;;
  # Sends one test alert and names the cause in words. Never prints the token.
  telegram) exec "$BIN" doctor --telegram ;;
  tests)    exec "$UV" run pytest -q ;;
  # CLAUDE.md rule 4's gate: opens a minimum-size position on the OKX DEMO account without a
  # stop, proves the watchdog attaches one, and closes it in `finally`. Demo only — the settings
  # interlock refuses to run it against a live key. Required after any change to place_bracket,
  # amend_stop or reconcile.
  demo-tests) exec "$UV" run pytest -q -m demo ;;
  # Restores the dev extras. `uv sync` without --all-extras prunes them, which is how a
  # deploy once uninstalled the test tooling its own gate needed (F-082).
  repair)   exec "$UV" sync --locked --all-extras ;;
  # Research, gate and journal only. Safe because of the --shadow flag itself: a shadow run is
  # built with no executor, so it cannot place an order whatever .env says. The manager WITHOUT
  # --shadow is armed whenever MODE=approve|auto (it has been since Phase 5), so never drop the
  # flag here — tests/deploy/test_checks_script.py fails if any arm loses it (P-N10).
  shadow)   exec "$BIN" manager --shadow ;;
  # Same run, but the Trader agent decides instead of the stub. Still places nothing.
  shadow-llm) exec "$BIN" manager --shadow --llm ;;
  # Judges every open position and sends NOTHING. The armed form lives in trade.sh's sibling
  # `argus review` (no --dry-run), which is deliberately not reachable from this script.
  review)   PYDANTIC_AI_NO_BANNER=1 exec "$BIN" review --dry-run ;;
  replay)   exec "$BIN" replay --last ;;
  evals)    exec "$BIN" eval-report ;;
  # The lessons memory as the next run will see it, plus the closed trades behind it. Read-only:
  # `--dry-run` makes no model call and writes nothing.
  lessons)  exec "$BIN" reflect --dry-run ;;
  # The read-only dashboard. Run through this script rather than calling the binary directly:
  # logfire reads pyproject.toml from the CWD at import time, so starting it from anywhere the
  # `argus` user cannot read dies with PermissionError before main() runs. The `cd` above fixes
  # it for every subcommand. Ctrl-C to stop; binds 127.0.0.1 only.
  ui)       exec "$BIN" ui ;;
  # One role's own golden suite against its leader, or every role with `agents` alone. Read-only:
  # golden fixtures in, model calls out, nothing touches the venue or the journal's plan tables.
  agents)   PYDANTIC_AI_NO_BANNER=1 exec "$BIN" eval-agents ${2:+--agent "$2"} ;;
  # Bookkeeping only: re-records checksums after a knowing migration reconciliation.
  reconcile) exec "$BIN" doctor --reconcile-migrations --no-network ;;
  # Every ledger hash chain and its insert-only guards; exit 1 on any break. Copy the printed
  # row counts and head hashes off the box: only an anchor kept elsewhere shows a truncated tail.
  audit)    exec "$BIN" audit ;;
  all)      PYDANTIC_AI_NO_BANNER=1 exec "$BIN" doctor --universe --auth --models ;;
  *)        echo "usage: checks.sh [keys|models|universe|auth|live|telegram|tests|demo-tests|repair|shadow|shadow-llm|review|replay|evals|agents [role]|lessons|ui|reconcile|audit|all]" >&2; exit 2 ;;
esac
