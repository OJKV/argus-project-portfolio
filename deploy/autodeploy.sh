#!/usr/bin/env bash
# PORTFOLIO COPY: its CI does not create or advance deploy/green and cannot deploy code.
# This script expects that ref to be managed separately; it does not prove a ref is CI-verified.
# Do not enable deployment automation without an independently reviewed promotion mechanism.
#
# WHY IT IS SHAPED THIS WAY. ARGUS has no daemon: stops rest on OKX and every run is a short-lived
# process under $ARGUS_HOME/run.lock. The old update.sh ran `uv sync` and the full suite WHILE
# holding that lock, which starves the watchdog, manager and review for minutes. So:
#
#   test outside the locks, swap inside them
#     1. STAGE   a persistent git worktree ($ARGUS_HOME/staging, its own .venv) is checked out at
#                the target and put through `uv sync`, ruff, mypy and pytest at the lowest CPU and
#                IO priority (to reduce contention),
#                inside a 25-minute budget (a timeout is a staging failure). No lock is held.
#                pytest gets a throwaway ARGUS_HOME, so the production journal is never within
#                reach of a test. A pass writes $ARGUS_HOME/staged-ok.
#     2. SWAP    `update.sh --to <sha> --pretested` takes deploy.lock then run.lock, fast-forwards,
#                syncs, smokes the entry points, migrates, restarts the dashboard: seconds. If a
#                lock stays busy for a minute it exits 75 and this retries next tick, re-using the
#                stamp instead of re-testing. If it refuses the stamp (76) the stamp is deleted and
#                the commit is staged again; it never falls back to the suite under the locks.
#     3. UNITS   a changed deploy/systemd/* is installed through the root-owned validating
#                installer (grant-claude.sh rule 5). While that cannot run, a units-pending flag is
#                kept and the installer is retried every tick; the code stays deployed.
#
# NOTHING IS DEPLOYED UNDER A JOB THAT TAKES NO RUN LOCK. eval-models, reflect and backup read the
# journal and import the code without it. They are Type=oneshot, so while they run systemd reports
# them "activating" — `systemctl is-active` says NO for exactly the jobs that matter — and the
# check here is `show -p ActiveState`. That check is advisory (it saves a doomed attempt and is
# repeated right before the swap); the guarantee is deploy.lock, which those units hold SHARED and
# update.sh takes EXCLUSIVE, so a job starting between the check and the swap cannot be run over.
#
# QUIET BY DESIGN. A tick with nothing new is one `git ls-remote`: no fetch, no uv, no tests, no
# lock. The unit's OnFailure alert fires only when a human has something to do, once per commit
# (or, for a persistent fault such as a missing uv, once per hour):
#   exit 1  staging failed | the swap rolled back | deploy/green is not a fast-forward |
#           fetch failing for ~1 h | uv or flock missing | the owner must act on the units
#   exit 0  everything else, including a missing deploy/green, a busy lock, a deferral, and "already failed, waiting for a newer commit".
# Every outcome appends one JSON line to $ARGUS_HOME/autodeploy.log.
#
# STATE, all in $ARGUS_HOME and all writable by argus:
#   autodeploy.paused          exists -> do nothing (the pause that does not need root)
#   deploy-failed              the commit that failed; delete the file to retry that same commit
#   staged-ok                  "<sha> <sha256 of uv.lock>" of the last commit that passed staging
#   deploy-inflight            written by update.sh while a swap runs; still here = a swap died
#                              without a trap, and HEAD is verified before anything else
#   autodeploy.refused         the last non-fast-forward target already reported
#   autodeploy.mismatch        the last commit whose stamp update.sh refused (76)
#   autodeploy.verified        the HEAD whose "no deployed record" check already ran
#   autodeploy.units-pending   unit files not yet installed (holds the commit that changed them)
#   autodeploy.units-alerted   the commit the owner was already told about
#   autodeploy.alerted         "<reason> <epoch>" of the last page per persistent fault
#   autodeploy.fetch-failures  consecutive failed remote lookups
#
# A running bash reads its script as it goes, but the swap replaces files the way git always does
# (new file, then rename over the old one), so this process keeps reading the version it started.
set -euo pipefail

# This portfolio copy cannot verify an independently managed deployment ref.
if [ "${ARGUS_ENABLE_PORTFOLIO_AUTODEPLOY:-}" != "I_UNDERSTAND" ]; then
  echo "portfolio auto-deploy disabled"
  exit 0
fi

TICK_START=$(date +%s)
REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

HOME_DIR="${ARGUS_HOME:-/var/lib/argus}"
export ARGUS_HOME="$HOME_DIR"      # update.sh reads the same variable
export GIT_TERMINAL_PROMPT=0       # a timer has no terminal: fail, never ask
mkdir -p "$HOME_DIR" || { echo "cannot create $HOME_DIR"; exit 1; }

LOG="$HOME_DIR/autodeploy.log"
PAUSE="$HOME_DIR/autodeploy.paused"
FAILED="$HOME_DIR/deploy-failed"
STAMP="$HOME_DIR/staged-ok"
HISTORY="$HOME_DIR/deploy-history.log"
REFUSED="$HOME_DIR/autodeploy.refused"
MISMATCH="$HOME_DIR/autodeploy.mismatch"
VERIFIED="$HOME_DIR/autodeploy.verified"
UNITS_PENDING="$HOME_DIR/autodeploy.units-pending"
UNITS_ALERTED="$HOME_DIR/autodeploy.units-alerted"
INFLIGHT="$HOME_DIR/deploy-inflight"   # written by update.sh while a swap is in progress
ALERTED="$HOME_DIR/autodeploy.alerted"
FETCH_FAILS="$HOME_DIR/autodeploy.fetch-failures"
STAGING="$HOME_DIR/staging"
FETCH_ALERT_AT=12                  # x 5 minutes ~ one hour of failing lookups before a page
PAGE_EVERY_S=3600                  # a persistent fault pages at most this often
SWAP_LOCK_WAIT_S="${ARGUS_AUTODEPLOY_LOCK_WAIT_S:-60}"   # how long the swap queues for each lock
STAGE_BUDGET_S="${ARGUS_AUTODEPLOY_STAGE_BUDGET_S:-1500}"
# Past this, what staging learned is stale. The unit allows 1800 s in all, and the swap itself may
# queue 2 x 60 s for its two locks, so a stage that ends with less than that left is not swapped.
TICK_MAX_S="${ARGUS_AUTODEPLOY_TICK_MAX_S:-1100}"
# Units that read the journal or import the code without taking run.lock.
NO_LOCK_UNITS="argus-eval-models.service argus-reflect.service argus-backup.service"
# Outcomes that repeat every tick while a condition lasts: logged once per target, not 288 x a day.
QUIET=" up-to-date ahead-of-green paused no-green-ref fetch-failed skipped-failed skipped-refused"
QUIET="$QUIET deferred lock-busy busy units-pending paged-recently "

json_str() { printf '%s' "$1" | tr '\r\n\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# log_event <result> [target] [detail] — one JSON line, and the same words on stdout for journald.
log_event() {
  local result=$1 target=${2:-} detail=${3:-} key last
  key="\"target\":\"$target\",\"result\":\"$result\""
  case "$QUIET" in
    *" $result "*)
      last=$(tail -n 1 "$LOG" 2>/dev/null || true)
      case "$last" in *"$key"*) return 0 ;; esac ;;
  esac
  printf '{"ts":"%s",%s,"detail":"%s"}\n' "$(date -u +%FT%TZ)" "$key" "$(json_str "$detail")" \
    >> "$LOG" || true
  echo "autodeploy: $result ${target:+${target:0:12}} $detail"
}

# page <reason> <message> — exit 1 so OnFailure alerts, but at most once per PAGE_EVERY_S per
# reason. A persistent fault (uv missing, a full disk) would otherwise be 288 identical alerts a day.
page() {
  local reason=$1 now last="" r="" t=""
  shift
  now=$(date +%s)
  if [ -f "$ALERTED" ]; then
    while read -r r t; do
      if [ "$r" = "$reason" ]; then last=$t; fi
    done < "$ALERTED" || true
  fi
  case "$last" in ''|*[!0-9]*) last="" ;; esac
  if [ -n "$last" ] && [ $((now - last)) -lt "$PAGE_EVERY_S" ]; then
    log_event paged-recently "" "$reason: $*"
    exit 0
  fi
  { grep -v "^$reason " "$ALERTED" 2>/dev/null || true; echo "$reason $now"; } > "$ALERTED.tmp" \
    && mv "$ALERTED.tmp" "$ALERTED" || true
  echo "autodeploy: $reason — $*"
  exit 1
}

sha256_stdin() {
  if command -v sha256sum >/dev/null; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}
# The COMMITTED uv.lock, so it is the same bytes update.sh compares before the merge.
lock_hash() { { git show "$1:uv.lock" 2>/dev/null || true; } | sha256_stdin; }

first_word() { local w=""; [ -f "$1" ] && read -r w _ < "$1" || true; printf '%s' "$w"; }

# Every network and test step is bounded: a hung git or uv must not hold the tick lock for the
# unit's whole 30 minutes (or, for a stuck stage, fail to say so).
TIMEOUT_BIN=$(command -v timeout || command -v gtimeout || true)
bounded() {
  local secs=$1
  shift
  # -k 30: TERM at the deadline, KILL 30 s later. A pytest that ignores TERM would otherwise hold
  # the tick lock until systemd's 1800 s ran out.
  if [ -n "$TIMEOUT_BIN" ]; then "$TIMEOUT_BIN" -k 30 "$secs" "$@"; else "$@"; fi
}

# ── 0. paused? ───────────────────────────────────────────────────────────────────────────────
if [ -e "$PAUSE" ]; then
  log_event paused "" "$PAUSE exists — remove it to resume"
  exit 0
fi

# ── 1. one tick at a time ────────────────────────────────────────────────────────────────────
# Not run.lock: this lock is held for the whole staging (minutes) and must never starve a trade.
command -v flock >/dev/null || page flock-missing "flock not found — refusing to run without a tick lock"
exec 8>>"$HOME_DIR/autodeploy.lock" || page state-unwritable "cannot open $HOME_DIR/autodeploy.lock"
if ! flock -n 8; then
  log_event busy "" "the previous tick is still running"
  exit 0
fi

# ── 2. what has CI passed? ───────────────────────────────────────────────────────────────────
OLD=$(git rev-parse HEAD)

note_remote_failure() {
  local n=0
  [ -f "$FETCH_FAILS" ] && read -r n < "$FETCH_FAILS" || true
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  n=$((n + 1))
  echo "$n" > "$FETCH_FAILS"
  if [ "$n" -eq "$FETCH_ALERT_AT" ]; then
    log_event fetch-stuck "" "$n consecutive failures: ${1:0:200}"
    page fetch-stuck "git has failed $n times in a row — is the deploy key, GitHub or the network down?"
  fi
  log_event fetch-failed "" "attempt $n: ${1:0:200}"
  exit 0
}

# One cheap question first. A missing branch is not a network failure: an operator may create it
# the first green run after the merge, which can take longer than the failure alert tolerates.
if out=$(bounded 60 git ls-remote --exit-code origin refs/heads/deploy/green 2>&1); then
  ls_rc=0
else
  ls_rc=$?
fi
case "$ls_rc" in
  0)
    # stderr is merged into $out (a ssh warning must reach the failure log), so take the sha from
    # the line that is one, not from the first word of whatever came first.
    REMOTE_SHA=$(printf '%s\n' "$out" | sed -n 's/^\([0-9a-f]\{40\}\)[[:space:]].*/\1/p' | head -n 1)
    [ -n "$REMOTE_SHA" ] || note_remote_failure "ls-remote printed no sha: $out" ;;
  2)
    rm -f "$FETCH_FAILS"
    log_event no-green-ref "" "origin has no deploy/green; portfolio CI does not create it"
    exit 0 ;;
  *) note_remote_failure "$out" ;;
esac

if [ "$REMOTE_SHA" = "$OLD" ]; then
  TARGET=$OLD                      # nothing to fetch
else
  if err=$(bounded 120 git fetch --quiet origin '+refs/heads/deploy/green:refs/remotes/origin/deploy/green' 2>&1); then
    :
  else
    note_remote_failure "$err"
  fi
  TARGET=$(git rev-parse --verify --quiet 'refs/remotes/origin/deploy/green^{commit}') \
    || note_remote_failure "deploy/green fetched but not found"
fi
rm -f "$FETCH_FAILS"

# ── 3. nothing new: the common case, and it costs one lookup ─────────────────────────────────
VERIFYING=0
mark_verified() { if [ "$VERIFYING" = 1 ]; then echo "$OLD" > "$VERIFIED"; fi; }

deployed_recorded() { grep -qF -- " -> $1 deployed" "$HISTORY" 2>/dev/null; }

if [ "$OLD" = "$TARGET" ]; then
  if [ -f "$INFLIGHT" ] && [ "$(first_word "$FAILED")" != "$OLD" ]; then
    # update.sh leaves this marker while a swap runs and removes it when the swap succeeds or its
    # rollback completes. Still here: a swap died without running a trap (SIGKILL, the OOM killer,
    # a reboot). It outranks a "deployed" record for HEAD — it is newer. The swap below finds the
    # marker and, if the verification fails, rolls back to where THAT deploy started.
    VERIFYING=1
    log_event recovering-interrupted-deploy "$OLD" "$INFLIGHT exists: a swap died part-way; verifying it"
  elif deployed_recorded "$OLD" || [ "$(first_word "$VERIFIED")" = "$OLD" ]; then
    if [ -f "$UNITS_PENDING" ]; then
      # Unit files from an earlier deploy are still not installed. Retry; page only for a new
      # commit (the deploy tick that first failed already did).
      if sudo -n /usr/local/sbin/argus-install-units; then
        rm -f "$UNITS_PENDING"
        log_event units-installed "$OLD" "the pending unit files are installed"
      else
        log_event units-pending "$OLD" "the installer is still refused; retrying each tick"
      fi
    else
      log_event up-to-date "$OLD"
    fi
    exit 0
  fi
  if [ "$VERIFYING" = 0 ]; then
    # HEAD is the target but nothing says a deploy of it finished: one died part-way (the
    # checkout is on the new commit, the dependencies may be half-synced). Verify it once, through
    # the same stage-and-swap path; the attempt is remembered per HEAD below so it cannot loop.
    VERIFYING=1
    echo "autodeploy: no 'deployed' record for ${OLD:0:12} — verifying it once"
  fi
fi

# ── 4. only ever forward ─────────────────────────────────────────────────────────────────────
if ! git merge-base --is-ancestor "$OLD" "$TARGET"; then
  if git merge-base --is-ancestor "$TARGET" "$OLD"; then
    # A manual update.sh ran ahead of the configured deployment ref; no page.
    log_event ahead-of-green "$TARGET" "production runs $OLD, which is newer"
    exit 0
  fi
  if [ "$(first_word "$REFUSED")" = "$TARGET" ]; then
    log_event skipped-refused "$TARGET"
    exit 0
  fi
  echo "$TARGET" > "$REFUSED"
  log_event refused-non-ff "$TARGET" "deploy/green is not a fast-forward of $OLD"
  echo "deploy/green ($TARGET) is not a fast-forward of what runs ($OLD) — refusing to move sideways." \
       "Someone rewrote the branch; look before you reset it."
  exit 1
fi

# ── 5. a commit that already failed waits for a newer one ────────────────────────────────────
if [ "$(first_word "$FAILED")" = "$TARGET" ]; then
  log_event skipped-failed "$TARGET" "failed earlier; delete $FAILED to retry it"
  exit 0
fi

# ── 6. do not even try under a job that takes no run lock ────────────────────────────────────
busy_unit() {
  command -v systemctl >/dev/null || return 1
  local unit state
  for unit in $NO_LOCK_UNITS; do
    state=$(systemctl show -p ActiveState --value "$unit" 2>/dev/null || true)
    case "$state" in
      activating|active|reloading|deactivating) echo "$unit"; return 0 ;;
    esac
  done
  return 1
}
if unit=$(busy_unit); then
  log_event deferred "$TARGET" "$unit is running"
  exit 0
fi

# ── 7. stage and test, outside the locks ─────────────────────────────────────────────────────
# systemd gives the unit a bare PATH; uv is installed in the argus user's ~/.local/bin. The fallback
# is overridable only so tests do not depend on the machine they run on (F-161).
UV_FALLBACK=${ARGUS_UV_FALLBACK:-/home/argus/.local/bin/uv}
UV=$(command -v uv || true)
[ -x "$UV" ] || UV=$UV_FALLBACK
[ -x "$UV" ] || page uv-missing "uv not found (looked on PATH and at $UV_FALLBACK)"

LOW=(nice -n 19)
if command -v ionice >/dev/null 2>&1; then LOW+=(ionice -c3); fi

LOCK_HASH=$(lock_hash "$TARGET")

stamped() {
  [ -f "$STAMP" ] || return 1
  local sha="" lock="" extra=""
  read -r sha lock extra < "$STAMP" || true
  [ "$sha" = "$TARGET" ] && [ "$lock" = "$LOCK_HASH" ] && [ -z "$extra" ]
}

STAGE_DEADLINE=$(( $(date +%s) + STAGE_BUDGET_S ))

# One staging step at the lowest priority, inside what is left of the budget (124 = timed out).
staged() {
  local left=$((STAGE_DEADLINE - $(date +%s)))
  [ "$left" -gt 0 ] || return 124
  bounded "$left" "${LOW[@]}" "$UV" "$@"
}

# Explicit `|| return 1`, not `set -e`: this runs inside an `if`, where errexit is suspended.
stage_and_test() {
  local scratch rc
  rm -f "$STAMP"   # a half-finished stage must never leave a stamp that update.sh would honour
  # Reuse the worktree (and its .venv, the slow part); if it is missing or unusable — the repo was
  # re-cloned, a kill left it half-written — rebuild it rather than fail every commit forever.
  if [ -e "$STAGING/.git" ] \
     && git -C "$STAGING" checkout --quiet --detach --force "$TARGET" 2>/dev/null \
     && git -C "$STAGING" clean -fdxq -e .venv 2>/dev/null; then
    :
  else
    rm -rf "$STAGING"
    git worktree prune || return 1   # after the rm: prune only forgets a worktree whose dir is gone
    git worktree add --quiet --detach "$STAGING" "$TARGET" || return 1
  fi
  scratch=$(mktemp -d) || return 1
  (
    cd "$STAGING" || exit 1
    # The tests get a journal of their own. ARGUS_HOME is also where run.lock lives, and there
    # is no .env in the worktree: nothing production-shaped is within reach of a test.
    export ARGUS_HOME="$scratch"
    staged sync --locked --all-extras || exit $?
    staged run ruff check . || exit $?
    staged run mypy argus/gate argus/exchange argus/execution argus/journal/states.py || exit $?
    staged run pytest -q || exit $?
  )
  rc=$?
  rm -rf "$scratch"
  return $rc
}

if stamped; then
  echo "autodeploy: ${TARGET:0:12} already passed staging — not testing again"
else
  echo "autodeploy: staging ${TARGET:0:12} (was ${OLD:0:12})"
  if stage_and_test; then
    stage_rc=0
  else
    stage_rc=$?
  fi
  if [ "$stage_rc" -ne 0 ]; then
    # Nothing was swapped, so this is NOT written to deploy-history.log: its mtime is what a
    # queued manager or review reads as "the code changed under me, restart" (lock.deployed_since),
    # and this runs outside the run lock.
    echo "$TARGET" > "$FAILED"
    mark_verified
    why="ruff, mypy or pytest failed in $STAGING"
    if [ "$stage_rc" -eq 124 ]; then why="staging timed out after ${STAGE_BUDGET_S}s in $STAGING"; fi
    log_event staging-failed "$TARGET" "$why; production untouched"
    echo "autodeploy: ${TARGET:0:12} FAILED staging. Production still runs ${OLD:0:12}." \
         "Nothing retries this commit; the next green one is tried. Details above."
    exit 1
  fi
  printf '%s %s\n' "$TARGET" "$LOCK_HASH" > "$STAMP.tmp"
  mv "$STAMP.tmp" "$STAMP"
fi

# Staging took minutes. The 08:30 eval may have started inside them (and the target may have moved).
if unit=$(busy_unit); then
  log_event deferred "$TARGET" "$unit started while staging; tested, will swap later"
  exit 0
fi
if [ $(( $(date +%s) - TICK_START )) -gt "$TICK_MAX_S" ]; then
  log_event stale-tick "$TARGET" "this tick ran past ${TICK_MAX_S}s; the stamp is kept, the next tick swaps"
  exit 0
fi

# ── 8. swap, inside the locks ────────────────────────────────────────────────────────────────
set +e
ARGUS_DEPLOY_LOCK_WAIT_S="$SWAP_LOCK_WAIT_S" "$REPO/deploy/update.sh" --to "$TARGET" --pretested
rc=$?
set -e
case "$rc" in
  0) ;;
  75)
    log_event lock-busy "$TARGET" "a lock was held for ${SWAP_LOCK_WAIT_S}s; the next tick retries"
    exit 0 ;;
  76)
    # update.sh refused the stamp before touching anything. Stage again — but a stamp refused
    # twice for one commit is a disagreement that staging will not fix, so it fails the commit.
    rm -f "$STAMP"
    if [ "$(first_word "$MISMATCH")" = "$TARGET" ]; then
      echo "$TARGET" > "$FAILED"
      mark_verified
      log_event swap-failed "$TARGET" "update.sh refused the stamp twice"
      exit 1
    fi
    echo "$TARGET" > "$MISMATCH"
    log_event stamp-mismatch "$TARGET" "update.sh refused the stamp (76); staging again next tick"
    exit 0 ;;
  *)
    # update.sh has already rolled the code and journal back and recorded it.
    echo "$TARGET" > "$FAILED"
    mark_verified
    log_event swap-failed "$TARGET" "update.sh exited $rc and rolled back"
    exit 1 ;;
esac
mark_verified
log_event deployed "$TARGET" "was $OLD"

# ── 9. unit files ────────────────────────────────────────────────────────────────────────────
# update.sh syncs code only (F-098). The root-owned installer validates every unit before it
# installs one, so the argus user can run it with no password and still not become root.
if ! git diff --quiet "$OLD" "$TARGET" -- deploy/systemd; then
  echo "$TARGET" > "$UNITS_PENDING"
fi
OWNER=""
if [ -f "$UNITS_PENDING" ]; then
  if sudo -n /usr/local/sbin/argus-install-units; then
    rm -f "$UNITS_PENDING"
  else
    OWNER="the unit files changed and sudo argus-install-units did not run or was refused"
  fi
fi
# The installer that sudo runs is a ROOT-OWNED COPY made by grant-claude.sh. It does not follow
# the repo, so a changed deploy/install-units.sh leaves the validator on the box stale.
if ! git diff --quiet "$OLD" "$TARGET" -- deploy/install-units.sh; then
  OWNER="${OWNER:+$OWNER; }deploy/install-units.sh changed, so the root-owned copy is stale"
fi
if [ -n "$OWNER" ]; then
  if [ "$(first_word "$UNITS_ALERTED")" = "$TARGET" ]; then
    log_event units-pending "$TARGET" "$OWNER"
    exit 0
  fi
  echo "$TARGET" > "$UNITS_ALERTED"
  log_event owner-action "$TARGET" "$OWNER"
  echo "OWNER ACTION: run sudo /opt/argus-project/deploy/grant-claude.sh <operator-username> ($OWNER)." \
       "Then, if units changed: sudo /usr/local/sbin/argus-install-units." \
       "The new code is deployed; only the systemd side is waiting."
  exit 1
fi
