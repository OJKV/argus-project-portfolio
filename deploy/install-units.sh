#!/usr/bin/env bash
# Install or refresh the systemd units — and refuse any unit that could run as root.
#
#   sudo argus-install-units                                  the root-owned copy (no password)
#   sudo argus-install-units --smoke argus-manager.service    ... then run that oneshot once
#                                                             (asks for owner's password)
#   deploy/install-units.sh --check [DIR]                     validate only: no root, no changes
#
# Needs root, which is why `update.sh` cannot do it: that script runs as the `argus` user and
# deliberately holds no privilege it does not need. Until this is run, a unit changed in the repo
# has no effect on the box — the code deploys and the schedule does not (F-098).
#
# WHY IT VALIDATES (review 2026-10-07, P-N1). owner may run this as root without a password, so that
# deploys and unit changes need no human. The units come from a checkout the `argus` user can
# write, and a unit file decides its own privileges: `User=root`, an `ExecStart=+…` line, a
# capability, a credential PID 1 reads on its behalf. Copying them blindly made one pushed commit
# into root, `.env` and an armed run. So:
#   - sudo names /usr/local/sbin/argus-install-units, a ROOT-OWNED copy of this script installed
#     by grant-claude.sh — not this file, which argus can edit;
#   - the units are copied into a root-owned directory first and validated there, so nothing
#     can swap them between the check and the install;
#   - every key must be on the allowlist below, and the privileged ones are checked by value.
#     A unit that needs anything else needs owner: change this script, then re-run grant-claude.sh.
#
# `--smoke` then runs the named oneshot services once, immediately, and reports their exit status.
# Enabling a timer proves systemd will *call* the service; it proves nothing about whether the
# service works, because the unit carries its own User, WorkingDirectory and EnvironmentFile that
# a hand-run `argus manager` never exercises. On 2026-10-06 the manager and review timers were
# enabled having never once run under systemd, and without this their first attempt would have
# been unattended, at 12:30 UTC, with the alert channel still down (F-141, F-126).
#
# A smoked service does real work on whatever account `.env` names. That is the point — it is the
# same run the timer would start minutes later — but it is why the unit must be one we ship, and
# why this prints what it is about to start.
set -euo pipefail

# Fixed, never derived from $0 or the caller: the root copy lives in /usr/local/sbin, and sudo
# must not let anyone point it at a directory of their own units.
UNIT_SRC=/opt/argus-project/deploy/systemd
ENV_FILE=/opt/argus-project/.env

# Every key a shipped unit may use, BY SECTION. Anything else — LoadCredential,
# StandardOutput=file:, Alias, SupplementaryGroups, PermissionsStartOnly, AmbientCapabilities,
# CapabilityBoundingSet, … — is refused rather than reasoned about one by one.
#
# The section matters (review 2026-10-07, second pass). systemd reads User= only in [Service] and
# ignores it anywhere else with a log line, so a unit whose one `User=argus` sat under [Unit]
# passed a section-blind count and then ran as root. Each key is accepted only in its own section,
# and only sections that unit type has.
UNIT_KEYS=" Description Documentation After Before OnFailure StartLimitIntervalSec StartLimitBurst "
SERVICE_KEYS=" Type User Group WorkingDirectory EnvironmentFile Environment ExecStart ExecStartPre
  ExecStartPost ExecStopPost TimeoutStartSec TimeoutStopSec Restart RestartSec Nice UMask
  NoNewPrivileges PrivateTmp PrivateDevices ProtectSystem ProtectKernelTunables
  ProtectKernelModules ProtectKernelLogs ProtectControlGroups ProtectClock ProtectHostname
  RestrictSUIDSGID RestrictRealtime LockPersonality MemoryHigh CPUWeight "
TIMER_KEYS=" OnCalendar OnBootSec OnUnitActiveSec Persistent RandomizedDelaySec AccuracySec Unit "
INSTALL_KEYS=" WantedBy "
NAME_RE='^argus-[a-z0-9-]+(@[A-Za-z0-9%._-]*)?\.(service|timer)$'

refuse() { echo "refusing $1: $2" >&2; BAD=1; }

# A unit a key may start or attach to: one of ours, or the targets the units hang off.
ref_ok() {
  [[ "$1" =~ $NAME_RE ]] || [ "$1" = timers.target ] || [ "$1" = multi-user.target ]
}

trim() { local v="${1#"${1%%[![:space:]]*}"}"; printf '%s' "${v%"${v##*[![:space:]]}"}"; }

# The keys a section accepts in this unit type; empty for a section the type does not have.
section_keys() {
  case "$1:$2" in
    Unit:*) printf '%s' "$UNIT_KEYS" ;;
    Install:*) printf '%s' "$INSTALL_KEYS" ;;
    Service:*.service) printf '%s' "$SERVICE_KEYS" ;;
    Timer:*.timer) printf '%s' "$TIMER_KEYS" ;;
  esac
}

validate_unit() {
  local file=$1 name users=0 raw line key value prefix ref section="" allowed
  name=$(basename "$file")
  if [ -L "$file" ] || [ ! -f "$file" ]; then refuse "$name" "not a regular file"; return; fi
  if ! [[ "$name" =~ $NAME_RE ]]; then
    refuse "$name" "only argus-*.service|timer units are installed"
    return
  fi
  while IFS= read -r raw || [ -n "$raw" ]; do
    line=$(trim "$raw")
    case "$line" in ''|'#'*|';'*) continue ;; esac
    case "$line" in *\\) refuse "$name" "line continuation: $line"; continue ;; esac
    case "$line" in
      '['*)
        section="${line#[}"
        section="${section%]}"
        case "$line" in
          '[Unit]'|'[Service]'|'[Timer]'|'[Install]') ;;
          *) refuse "$name" "section $line"; section="?"; continue ;;
        esac
        if [ -z "$(section_keys "$section" "$name")" ]; then
          refuse "$name" "a ${name##*.} has no $line section"
          section="?"
        fi
        continue ;;
    esac
    case "$line" in *=*) ;; *) refuse "$name" "not a key=value line: $line"; continue ;; esac
    key=$(trim "${line%%=*}")
    value=$(trim "${line#*=}")
    if [ -z "$section" ]; then refuse "$name" "$key= before any [section]"; continue; fi
    [ "$section" = "?" ] && continue   # already refused above
    allowed=$(section_keys "$section" "$name")
    case "$allowed" in
      *[[:space:]]"$key"[[:space:]]*) ;;
      *) refuse "$name" "$key= is not allowed in [$section]"; continue ;;
    esac
    case "$key" in
      User)
        # Only reachable inside [Service], the one section systemd reads it from.
        users=$((users + 1))
        [ "$value" = argus ] || refuse "$name" "User=$value (only argus; an empty User= is root)" ;;
      Group)
        [ "$value" = argus ] || refuse "$name" "Group=$value" ;;
      Exec*)
        # systemd's prefixes: - @ : + ! !! (and | in newer releases), in any order. "+" and "!"
        # run the line with full privileges, or without switching to User=.
        prefix="${value%%[^-@:+!|]*}"
        case "$prefix" in *[+!]*) refuse "$name" "$key prefix '$prefix' escapes User=argus" ;; esac ;;
      EnvironmentFile)
        # PID 1 reads this file as root on the unit's behalf.
        [ "$value" = "$ENV_FILE" ] || [ "$value" = "-$ENV_FILE" ] \
          || refuse "$name" "EnvironmentFile=$value (only $ENV_FILE)" ;;
      Nice)
        [[ "$value" =~ ^[0-9]+$ ]] || refuse "$name" "Nice=$value (negative nice is a root privilege)" ;;
      OnFailure|Unit|WantedBy)
        for ref in $value; do ref_ok "$ref" || refuse "$name" "$key=$ref is not an ARGUS unit"; done ;;
    esac
  done < "$file"
  case "$name" in
    *.service)
      [ "$users" -eq 1 ] \
        || refuse "$name" "$users User= lines in [Service]; exactly one User=argus, or it runs as root" ;;
  esac
}

validate_dir() {
  local dir=$1 file found=0
  BAD=0
  for file in "$dir"/*; do
    [ -e "$file" ] || [ -L "$file" ] || continue
    found=1
    validate_unit "$file"
  done
  [ "$found" = 1 ] || refuse "$dir" "no units found"
  [ "$BAD" = 0 ]
}

if [ "${1:-}" = "--check" ]; then
  dir="${2:-$(dirname "$0")/systemd}"
  validate_dir "$dir" || exit 1
  echo "units OK: $dir"
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "installing units needs root: sudo $0  (--check DIR validates without it)" >&2
  exit 1
fi

SMOKE=()
if [ "${1:-}" = "--smoke" ]; then
  shift
  SMOKE=("$@")
  [ ${#SMOKE[@]} -gt 0 ] || { echo "--smoke needs at least one unit name" >&2; exit 2; }
fi

# Copy first, into a directory only root can write, and validate the copies: what is checked is
# exactly what is installed. `cp -P` copies a symlink as a symlink, which the check then refuses.
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
cp -P "$UNIT_SRC"/* "$STAGE"/
validate_dir "$STAGE" || { echo "nothing installed" >&2; exit 1; }

for named in ${SMOKE[@]+"${SMOKE[@]}"}; do
  if ! [[ "$named" =~ $NAME_RE ]] || [ ! -f "$STAGE/$named" ]; then
    echo "refusing to smoke $named: not shipped here" >&2
    exit 2
  fi
done

install -m 644 -o root -g root "$STAGE"/*.service "$STAGE"/*.timer /etc/systemd/system/
systemctl daemon-reload

# Timers we want running.
#
# manager and review were held back here while the manager was shadow-only, and that line was
# never revisited when Phase 5 armed it — so for eleven days the core of the system ran only when
# someone typed the command, and the alert that said so went to a dead Telegram channel (F-141).
# owner enabled them on 2026-10-06.
#
# They go on together on purpose: the manager opens positions and the review is what tends them
# (hold / trim / close / tighten the stop). A manager without a review opens trades nothing then
# manages. argus-watchdog is the layer below both — it guarantees the exchange-side stop — which
# is why it was always enabled and must stay that way.
#
# argus-reflect reads the journal, makes one model call and appends to `lessons`. It places
# nothing, and it exits without calling anything until three trades close.
#
# argus-autodeploy follows `deploy/green` (an operator-configured ref) every five minutes and, when
# a deploy changes deploy/systemd, runs THIS script through sudo to apply it. It pauses with
# `touch /var/lib/argus/autodeploy.paused`; `sudo systemctl stop` is the root way.
for unit in argus-watchdog.timer argus-backup.timer argus-eval-models.timer argus-reflect.timer \
            argus-live-keys.timer argus-manager.timer argus-review.timer argus-autodeploy.timer; do
  systemctl enable --now "$unit"
done

# The dashboard is a service, not a timer: it should be up whenever the box is, so a closed SSH
# session does not take it down.
systemctl enable --now argus-ui.service
systemctl --no-pager --lines=0 status argus-ui.service | head -3 || true

# Expose the dashboard on the tailnet, with TLS, without the app ever leaving 127.0.0.1.
#
# `tailscale serve` terminates TLS with a real certificate for the MagicDNS name and applies the
# tailnet's own device authentication, so the page is reachable from owner's machines and phone and
# from nowhere else. The alternative — binding the tailnet address inside the app — failed
# silently as the `argus` user, which could not reach the tailscaled socket, leaving a service
# that reported healthy while being unreachable (F-134).
if command -v tailscale >/dev/null 2>&1 && tailscale status >/dev/null 2>&1; then
  tailscale serve --bg --https=443 http://127.0.0.1:8787 >/dev/null 2>&1 || true
  DNS=$(tailscale status --json 2>/dev/null \
        | sed -n 's/.*"DNSName" *: *"\([^"]*\)\.".*/\1/p' | head -1)
  [ -n "$DNS" ] && echo "dashboard: https://$DNS"
else
  echo "NOTE: tailscale not up — dashboard stays on 127.0.0.1 only"
fi

systemctl list-timers --no-pager | grep argus || true

# Prove the unit wrapper, not just the schedule. `Type=oneshot` makes `systemctl start` block
# until the run finishes, so ExecMainStatus is the real exit code.
for named in ${SMOKE[@]+"${SMOKE[@]}"}; do
  echo
  echo "smoke: starting $named now (this does real work on the account .env names)"
  systemctl start "$named" || true
  status=$(systemctl show -p ExecMainStatus --value "$named")
  if [ "$status" = "0" ]; then
    echo "smoke: $named exited 0"
  else
    echo "smoke: $named FAILED, exit $status — last lines:" >&2
    journalctl -u "$named" -n 15 --no-pager -o cat >&2 || true
    exit 1
  fi
done
