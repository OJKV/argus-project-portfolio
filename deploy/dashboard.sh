#!/usr/bin/env bash
# Forward a remote loopback dashboard. Configure the SSH destination separately.
set -uo pipefail

HOST=${ARGUS_HOST:-}
PORT=${ARGUS_PORT:-8787}
if [ -z "$HOST" ]; then
  echo "set ARGUS_HOST to a configured SSH destination" >&2
  exit 2
fi
if [[ "$HOST" == -* || "$HOST" =~ [[:space:]] ]]; then
  echo "ARGUS_HOST must be a single SSH destination, not an option" >&2
  exit 2
fi
if [[ ! "$PORT" =~ ^[0-9]{1,5}$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
  echo "ARGUS_PORT must be between 1 and 65535" >&2
  exit 2
fi
set --
if [ -n "${ARGUS_KEY:-}" ]; then
  set -- -i "$ARGUS_KEY"
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "port $PORT is already forwarded — open http://localhost:$PORT"
  exit 0
fi

echo "forwarding localhost:$PORT -> $HOST  (ctrl-c to stop)"
echo "open http://localhost:$PORT"

while true; do
  # ServerAliveInterval makes a dead link fail in ~60s instead of hanging silently.
  # ExitOnForwardFailure stops a "connected" session that never actually bound the port.
  ssh "$@" -N \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=2 \
      -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new \
      -L "$PORT:127.0.0.1:$PORT" "$HOST" || true
  echo "tunnel dropped — reconnecting in 5s (ctrl-c to stop)"
  sleep 5
done
