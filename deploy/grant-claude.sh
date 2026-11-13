#!/usr/bin/env bash
# Run as root:   sudo /opt/argus-project/deploy/grant-claude.sh <operator-username>
# Re-run it whenever deploy/install-units.sh changes — that refreshes the root-owned copy.
#
# Grants five things, so deploys, unit changes and demo trading runs stop needing a password:
#
#   1. operator    -> may run deploy/trade.sh as argus          (a manager run on the DEMO account only;
#                                                           trade.sh refuses the live venue)
#   2. operator    -> may run deploy/update.sh + checks.sh as argus   (deploy, and read-only health)
#   3. operator    -> may run /usr/local/sbin/argus-install-units as root, WITH NO ARGUMENTS
#                                                         (install/enable the units; --smoke
#                                                          still asks for owner's password)
#   4. argus -> may restart argus-ui.service as root      (so a deploy refreshes the dashboard)
#   5. argus -> may run /usr/local/sbin/argus-install-units as root, WITH NO ARGUMENTS
#                                                         (deploy/autodeploy.sh applies a changed
#                                                          unit file; the same root-owned validator
#                                                          as rule 3, nothing wider)
#
# Rule 3, and why it no longer hands out root (review 2026-10-07, P-N1). It used to name the repo's
# deploy/install-units.sh, which the `argus` user can edit, and that script copied whatever units
# the checkout held — a unit can say `User=root` or `ExecStart=+…`. One pushed commit was root,
# `.env` and an armed run with no human in the loop. Now:
#   - sudo names a ROOT-OWNED copy, installed by this script into /usr/local/sbin. Editing the repo
#     copy changes nothing until owner runs this script again, by hand, with his password.
#   - that copy refuses any unit not named argus-*.service|timer, without exactly one User=argus
#     in [Service] (systemd ignores User= in any other section), with
#     User=root, an Exec "+"/"!"/"!!" prefix, PermissionsStartOnly, AmbientCapabilities,
#     CapabilityBoundingSet, or any key outside its allowlist (`--check` shows the verdict).
#   - the rule allows no arguments (sudoers `""`). With any arguments allowed, a password-free
#     `--smoke argus-manager.service` started an armed manager on whatever account .env names —
#     live included — around trade.sh's demo-only check (review 2026-10-07, second pass).
#     Smoking a unit is how owner proves one by hand, so it asks for his password.
# What remains is what the argus user could already do: run code as argus.
#
# Rule 5, hands-off deploys. argus-autodeploy.timer (deploy/autodeploy.sh) runs as `argus` every
# five minutes and follows `origin/deploy/green` only when explicitly enabled. Portfolio CI never
# advances that ref, which requires a separately reviewed mechanism. When a deploy changes
# deploy/systemd it needs the installer, so `argus` gets the same rule `operator` has: the root-owned
# validating copy, no arguments. The unit that runs it has NoNewPrivileges off, and every setting
# that makes systemd turn it on (ProtectKernelTunables, RestrictSUIDSGID, LockPersonality, …), for
# exactly this reason: sudo is setuid (see argus-autodeploy.service). Two things stay manual and
# are reported, never guessed at:
# this script, when deploy/install-units.sh changes (the root copy does not follow the repo), and
# `--smoke`. After this script runs, `sudo -u argus sudo -n -l` should list rules 4 and 5.
#
# PORTFOLIO COPY: its CI has no deployment-promotion job and does not advance deploy/green.
# A ref is not proof of a passing CI run. Independently review and configure a trusted promotion
# mechanism before using automatic deployment, and restrict repository write access.
#
# WHAT THIS DOES NOT DO: it never chooses an account. MODE, OKX_DEMO and LIVE_VENUE in .env decide
# whether a run trades the demo account or real money, and editing those stays a deliberate act by
# owner. Only the systemd timers may run against the live account; trade.sh refuses it.
#
# Revoke at any time:  sudo rm /etc/sudoers.d/argus-claude /usr/local/sbin/argus-install-units
set -euo pipefail

# Require a local account explicitly; never interpolate arbitrary text into sudoers.
preview=false
if [ "${1:-}" = "--print-rules" ]; then
  preview=true
  shift
fi
if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] ||
   [[ "$1" = root || "$1" = argus || "$1" = nobody || "$1" = daemon ||
      "$1" = bin || "$1" = sys || "$1" = sync || "$1" = halt ||
      "$1" = shutdown || "$1" = systemd-* ]]; then
  echo "usage: $0 [--print-rules] <operator-username>" >&2
  exit 2
fi
operator_user=$1

generate_rules() {
  cat <<RULES
# ARGUS — operator grants and service grants.
$operator_user ALL=(argus) NOPASSWD: /opt/argus-project/deploy/trade.sh
$operator_user ALL=(argus) NOPASSWD: /opt/argus-project/deploy/update.sh, /opt/argus-project/deploy/checks.sh
$operator_user ALL=(root) NOPASSWD: /usr/local/sbin/argus-install-units ""
argus ALL=(root) NOPASSWD: /usr/bin/systemctl restart argus-ui.service
argus ALL=(root) NOPASSWD: /usr/local/sbin/argus-install-units ""
RULES
}
if [ "$preview" = true ]; then
  generate_rules
  exit 0
fi

if ! operator_uid=$(id -u "$operator_user" 2>/dev/null) ||
   [[ ! "$operator_uid" =~ ^[0-9]+$ ]] || [ "$operator_uid" -lt 1000 ]; then
  echo "unknown operator or system account: $operator_user" >&2
  exit 2
fi
if ! account=$(getent passwd "$operator_user"); then
  echo "cannot verify operator account" >&2
  exit 2
fi
IFS=: read -r _ _ _ _ _ _ operator_shell <<< "$account"
case "$operator_shell" in
  ""|*/nologin|*/false) echo "operator must have a login shell" >&2; exit 2 ;;
esac
if ! current_uid=$(id -u) || [ "$current_uid" != 0 ]; then
  echo "run with sudo: sudo $0" >&2
  exit 1
fi

REPO=/opt/argus-project
INSTALLER_SRC="$REPO/deploy/install-units.sh"
INSTALLER=/usr/local/sbin/argus-install-units
TARGET=/etc/sudoers.d/argus-claude
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# The copy sudo will run, owned by root so the argus user cannot change it afterwards. Check the
# source parses and accepts the shipped units BEFORE it becomes the thing sudo trusts.
bash -n "$INSTALLER_SRC"
bash "$INSTALLER_SRC" --check "$REPO/deploy/systemd"
install -m 755 -o root -g root "$INSTALLER_SRC" "$INSTALLER"
echo "installed $INSTALLER (sha256 $(sha256sum "$INSTALLER" | cut -c1-16)) from" \
     "$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo 'an unknown commit')"

generate_rules > "$TMP"

chmod 440 "$TMP"
# Validate BEFORE installing. A malformed file in sudoers.d can lock everyone out of sudo, so it
# is checked in place and only then moved.
if ! visudo -c -f "$TMP" >/dev/null; then
  echo "refusing to install: the generated rules did not parse" >&2
  exit 1
fi

install -m 440 -o root -g root "$TMP" "$TARGET"

if ! visudo -c >/dev/null; then
  rm -f "$TARGET"
  echo "installed rules broke the sudoers set — reverted, nothing changed" >&2
  exit 1
fi

echo "granted:"
sed 's/^/  /' "$TARGET" | grep -v '^  #'
echo
echo "verify:  sudo -u $operator_user sudo -n -l | grep argus-install-units"
echo "revoke:  sudo rm $TARGET $INSTALLER"
