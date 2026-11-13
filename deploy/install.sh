#!/usr/bin/env bash
# Linux installation example. Run as root only after reviewing it and AFTER the repo
# has been cloned to /opt/argus-project by the `argus` user with its deploy key.
# Idempotent: safe to re-run after environment-specific review.
set -euo pipefail
REPO_DIR=/opt/argus-project
STATE_DIR=/var/lib/argus

id -u argus >/dev/null 2>&1 || adduser --disabled-password --gecos "" argus
apt-get update -y && apt-get install -y git sqlite3 curl ufw unattended-upgrades
ufw allow OpenSSH && ufw --force enable

if [ ! -d "$REPO_DIR/.git" ]; then
  echo "Clone first (as argus, with the deploy key):"
  echo "  sudo -iu argus git clone <repository-url> $REPO_DIR"
  exit 1
fi

# A pinned uv, the same one CI runs (.github/workflows/gate.yml; a test keeps them equal), and the
# hash-locked dependency set CI tested. Unpinned, a box rebuilt after a VPS loss would resolve
# fresh versions nothing had ever run (P-12). --all-extras matches update.sh, whose on-box gate
# needs the dev tools (F-082).
UV_VERSION=0.12.5
[ -x /home/argus/.local/bin/uv ] || sudo -iu argus sh -c "curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sh"
mkdir -p "$STATE_DIR" && chown -R argus:argus "$REPO_DIR" "$STATE_DIR" && chmod 750 "$STATE_DIR"
sudo -iu argus sh -c "cd $REPO_DIR && /home/argus/.local/bin/uv sync --locked --all-extras"

if [ ! -f "$REPO_DIR/.env" ]; then
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  sed -i "s|^ARGUS_HOME=.*|ARGUS_HOME=$STATE_DIR|; s|^OKX_PUBLIC_HOST_OVERRIDE=.*|OKX_PUBLIC_HOST_OVERRIDE=|" "$REPO_DIR/.env"
  echo "Created $REPO_DIR/.env — fill it in"
fi
chmod 600 "$REPO_DIR/.env" && chown argus:argus "$REPO_DIR/.env"

# Same validation the password-free installer applies (P-N1): no unit that could run as root.
bash "$REPO_DIR/deploy/install-units.sh" --check "$REPO_DIR/deploy/systemd"
cp "$REPO_DIR"/deploy/systemd/*.service "$REPO_DIR"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload

echo "Next: sudo -u argus nano $REPO_DIR/.env"
echo "      cd $REPO_DIR && sudo -u argus .venv/bin/argus doctor --universe --auth"
echo "      sudo systemctl enable --now argus-watchdog.timer   (Manager/Review timers after Phase 4)"
