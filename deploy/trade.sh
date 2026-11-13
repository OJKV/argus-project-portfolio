#!/usr/bin/env bash
# Start a manager run that MAY PLACE ORDERS.
#
# Separate from checks.sh on purpose. checks.sh is read-only by intent and safe to grant widely;
# this one moves money, so it gets its own sudoers line, its own audit trail, and a name nobody
# runs by accident.
#
# DEMO ACCOUNT ONLY (review 2026-10-07, X-N10 / P-N1). `operator` may run this without a password, so it
# must not be able to reach real money: it refuses unless .env names the demo venue, and refuses
# when the settings cannot be read at all. Live trading runs only from the systemd timers.
#
#   ./trade.sh            one armed run, LLM proposer
#   ./trade.sh --shadow   the same run placing nothing
#   (--equity is refused unless --shadow: an armed run sizes on the account's real equity)
#   ./trade.sh manual ARGS   one deliberate manual DEMO trade: `argus manual-trade ARGS` (--yes places)
#
# `manual` slightly widens what the passwordless grant can do: the same sudoers line that starts a
# manager run now also starts a single minimum-size order that is NOT proposed by the agents
# (CLAUDE.md #1's manual-trade exception, which owner invokes in a session). It stays demo-only: it
# sits behind the venue check below, and `argus manual-trade` refuses the live account again itself.
#
# The account is decided by .env (MODE + OKX_DEMO), never by this script. It refuses to guess.
set -euo pipefail
cd "$(dirname "$0")/.."   # logfire reads pyproject.toml from the CWD at import time (F-124)

BIN=.venv/bin/argus

# Asked of argus.config.Settings — the interlock that every run applies — rather than parsed out
# of .env here, so the answer cannot differ from the one the manager would act on. A config that
# does not validate prints nothing, and anything but "demo" is a refusal.
VENUE=$(.venv/bin/python -c 'from argus.config import get_settings; print(get_settings().venue)' \
        2>/dev/null || true)
if [ "$VENUE" != "demo" ]; then
  echo "refusing: .env names the ${VENUE:-unreadable} account. trade.sh runs the demo account" \
       "only; the live account is traded by the systemd timers alone." >&2
  exit 3
fi
export PYDANTIC_AI_NO_BANNER=1   # pydantic-ai's own banner says "observability: off" about ITS
                                 # instrumentation, which reads as a broken setup and is not one.

# Say what is about to happen, from the settings rather than from an assumption.
"$BIN" doctor --no-network 2>/dev/null | grep -E '^\│ mode' || true

if [ "${1:-}" = "manual" ]; then
  shift
  exec "$BIN" manual-trade "$@"
fi

exec "$BIN" manager --llm "$@"
