#!/usr/bin/env bash
# Update ARGUS on the box, verify it there, and undo everything on failure.
#
#   sudo -u argus /opt/argus-project/deploy/update.sh              by hand: origin/main, full gate
#   deploy/update.sh --to <sha> --pretested                        what deploy/autodeploy.sh calls
#
# By hand this stays what it always was: pull origin/main, re-run the gate on the box, roll back on
# any failure. GitHub runs the same gate on every push (.github/workflows/gate.yml), so the green
# check is the signal that it is safe to run this.
#
# Hands-off, deploy/autodeploy.sh (a 5-minute timer) calls it with `--to` a commit CI has already
# promoted to `deploy/green`, and `--pretested` once it has ALSO run the whole suite in a separate
# staging checkout, outside the locks. Then this script only swaps: fast-forward, `uv sync`, a
# smoke that the entry points import, the journal migration, the dashboard restart. The locks are
# held for that, seconds rather than the minutes the full suite takes — the old shape starved the
# watchdog, the manager and the review for the whole of every deploy.
#
#   --to <sha>     fast-forward to that commit instead of origin/main; never backwards.
#   --pretested    skip ruff/mypy/pytest. Needs --to, and is a claim that is checked FIRST, before
#                  either lock or the merge: $ARGUS_HOME/staged-ok must name exactly that commit and
#                  the sha256 of its uv.lock, else nothing is touched and the exit is 76. It never
#                  falls back to the full suite under the locks — that is what the flag exists to
#                  avoid. (No flags: the full gate runs here, as it always did.)
#
# Two locks, always taken in this order and held to the end, across the re-exec below:
#   deploy.lock  EXCLUSIVE. eval-models, reflect and backup take no run lock, so their units hold
#                this one SHARED (`flock -s` in ExecStart): a swap cannot land under them, and
#                they cannot start under a swap.
#   run.lock     EXCLUSIVE. Every manager, review and watchdog run takes it.
#
# Exit codes: 0 deployed (or verified) | 75 a lock was busy, nothing touched, try again |
#             76 --pretested but the stamp does not match, nothing touched, stage it again |
#             2 bad arguments | 1 anything else: failed, and rolled back (code AND journal).
#
# A deploy that dies WITHOUT running a trap (SIGKILL, the OOM killer, a reboot) is recovered from
# $ARGUS_HOME/deploy-inflight: written atomically before the merge with the commit and the journal
# snapshot this deploy started from, removed when it succeeds or its rollback completes. If a later
# run finds it, THAT commit and snapshot are the rollback target — not HEAD, which is the half-
# deployed commit, and not a fresh snapshot, which is taken after its migration ran.
#
# `set -E` matters: without errtrace the ERR trap is not inherited into functions, and the post-
# merge `uv sync` (sync_env) is one — errexit ended the script on the new commit with the old
# dependencies and no rollback (F-159).
set -Eeuo pipefail

cd "$(dirname "$0")/.."

# The arguments are kept as given: the re-exec below must hand the newer copy the same ones, or a
# pretested deploy of an update.sh fix would silently become a deploy of main.
ARGS=("$@")
TO=""
PRETESTED=0
while [ $# -gt 0 ]; do
  case "$1" in
    --to)
      [ $# -ge 2 ] || { echo "--to needs a commit"; exit 2; }
      # A sha, never a ref or an option: this is handed to git, and `operator` may call it with any args.
      [[ "$2" =~ ^[0-9a-fA-F]{7,40}$ ]] || { echo "--to takes a commit sha, not '$2'"; exit 2; }
      TO=$2
      shift 2 ;;
    --pretested) PRETESTED=1; shift ;;
    *) echo "usage: update.sh [--to <sha> [--pretested]]"; exit 2 ;;
  esac
done
if [ "$PRETESTED" = 1 ] && [ -z "$TO" ]; then
  echo "--pretested names a staged commit, so it needs --to <sha>"
  exit 2
fi

# uv lives in the argus user's home and `sudo -u` does not load a login shell, so PATH does not
# have it. Resolve it before touching anything (F-078).
# ARGUS_UV_FALLBACK exists only so tests do not depend on the machine they run on (F-161).
UV_FALLBACK=${ARGUS_UV_FALLBACK:-/home/argus/.local/bin/uv}
UV=$(command -v uv || true)
[ -x "$UV" ] || UV=$UV_FALLBACK
if [ ! -x "$UV" ]; then
  echo "uv not found (looked on PATH and at $UV_FALLBACK)"
  exit 1
fi

# --all-extras matches install.sh. A plain `uv sync` prunes the dev extras, which is how a deploy
# once deleted the test tooling its own gate needed and then failed on its absence (F-082).
sync_env() { "$UV" sync --locked --all-extras --quiet; }

# sha256 of a stream, on the box (coreutils) and on a laptop (macOS has shasum, not sha256sum).
sha256_stdin() {
  if command -v sha256sum >/dev/null; then sha256sum | cut -d' ' -f1; else shasum -a 256 | cut -d' ' -f1; fi
}
# Of the COMMITTED uv.lock, so it is the same bytes deploy/autodeploy.sh stamped before the swap.
lock_hash() { { git show "$1:uv.lock" 2>/dev/null || true; } | sha256_stdin; }

HOME_DIR="${ARGUS_HOME:-/var/lib/argus}"
STAMP="$HOME_DIR/staged-ok"

# ── 1. which commit, and is a --pretested claim true? — before any lock, before any change ────
NEW=""
if [ -n "$TO" ]; then
  # autodeploy fetched `deploy/green` moments ago; fetch again only if the commit is not here.
  git cat-file -e "$TO^{commit}" 2>/dev/null || git fetch --quiet origin || true
  NEW=$(git rev-parse --verify --quiet "$TO^{commit}") \
    || { echo "unknown commit $TO — nothing changed"; exit 1; }
fi

stamp_matches() {
  [ -f "$STAMP" ] || return 1
  local sha="" lock="" extra=""
  read -r sha lock extra < "$STAMP" || true
  [ -z "$extra" ] && [ "$sha" = "$1" ] && [ "$lock" = "$(lock_hash "$1")" ]
}

# (The re-exec'd copy was started by a first copy that already checked, with the commit merged.)
if [ "$PRETESTED" = 1 ] && [ "${ARGUS_DEPLOY_REEXEC:-}" != "1" ] && ! stamp_matches "$NEW"; then
  echo "--pretested, but $STAMP does not name $NEW and its uv.lock — nothing changed; stage it again"
  exit 76
fi

# ── 2. the state a rollback needs ─────────────────────────────────────────────────────────────
# OLD is read AFTER the locks (init_old below). Read first, it was stale by however long the deploy
# queued: a deploy that finished meanwhile left HEAD elsewhere, and this one rolled back to — and
# recorded as the commit it replaced — one that was no longer running.
OLD=${ARGUS_DEPLOY_OLD:-}
SNAPSHOT=${ARGUS_DEPLOY_SNAPSHOT:-}

# The journal goes back with the code (P-N4). Migrations are forward-only and checksummed, so
# resetting the code alone leaves the old code facing a schema it has never seen. The snapshot is
# taken under the run lock, so no run is writing while it is copied; `.backup`/`.restore` go
# through SQLite and are safe on a WAL database where `cp` is not.
JOURNAL="$HOME_DIR/journal.db"

init_old() {
  [ -n "$OLD" ] || OLD=$(git rev-parse HEAD)
  # Named for THIS deploy, not just the commit it starts from. A name of `pre-deploy-$OLD.db` that
  # was kept if it existed meant a second failed deploy from the same commit restored the first
  # attempt's snapshot — days old — and wiped every order, stop and ledger row written since
  # (review 2026-10-07, second pass). The re-exec'd newer copy is handed this exact path.
  [ -n "$SNAPSHOT" ] || SNAPSHOT="$HOME_DIR/backup/pre-deploy-$OLD-$(date -u +%Y%m%dT%H%M%SZ)-$$.db"
}

snapshot_journal() {
  [ -f "$JOURNAL" ] || return 0
  # Only the re-exec reuses a snapshot, and only the one its first copy took moments ago, under
  # the same locks, before the merge. A newer copy started by an older one that predates this
  # hand-over gets none, so it takes its own: no run can have written since the locks were taken.
  if [ "${ARGUS_DEPLOY_REEXEC:-}" = "1" ] && [ -n "${ARGUS_DEPLOY_SNAPSHOT:-}" ] \
     && [ -f "$SNAPSHOT" ]; then
    return 0
  fi
  # A recovery keeps the snapshot the dead deploy took. A new one would be taken after that
  # deploy's migration and, restored, would roll back to the half-migrated journal.
  if [ "$RECOVERING" = "1" ] && [ -f "$SNAPSHOT" ]; then
    echo "journal snapshot: $SNAPSHOT (the dead deploy's)"
    return 0
  fi
  mkdir -p "$HOME_DIR/backup"
  sqlite3 "$JOURNAL" ".backup '$SNAPSHOT'"
  # Keep the last five; each is a whole journal and this box shares its disk.
  ls -1t "$HOME_DIR"/backup/pre-deploy-*.db 2>/dev/null | tail -n +6 | xargs rm -f --
  echo "journal snapshot: $SNAPSHOT"
}

# One line per deploy: when, from which commit, to which, and how it ended. This is the record a
# manual rollback starts from ("which sha was running before?") — there were no deployed-SHA tags,
# so it had to be dug out of the reflog on the box (P-N11).
HISTORY="$HOME_DIR/deploy-history.log"
INFLIGHT="$HOME_DIR/deploy-inflight"
RECOVERING=0
record() { mkdir -p "$HOME_DIR"; echo "$(date -u +%FT%TZ) $OLD -> $1 $2" >> "$HISTORY" || true; }

# The commit and snapshot a dead deploy started from, if one left them (RECOVERING=1 then).
recover_inflight() {
  [ -f "$INFLIGHT" ] || return 0
  local old="" snapshot=""
  read -r old snapshot < "$INFLIGHT" || true
  if [[ "$old" =~ ^[0-9a-f]{40}$ ]] && [ -n "$snapshot" ] && git cat-file -e "$old^{commit}" 2>/dev/null; then
    OLD=$old
    SNAPSHOT=$snapshot
    RECOVERING=1
    echo "a previous deploy died part-way ($INFLIGHT): a failure now rolls back to $OLD," \
         "the last commit known to be complete, and restores its journal snapshot"
  else
    echo "WARNING: ignoring $INFLIGHT — it does not name a commit and a snapshot"
  fi
}

# Atomic: a deploy killed while writing it must not leave half a line that names the wrong commit.
write_inflight() {
  printf '%s %s\n' "$OLD" "$SNAPSHOT" > "$INFLIGHT.tmp.$$" && mv "$INFLIGHT.tmp.$$" "$INFLIGHT"
}

rollback() {
  # Disarmed first: this runs on a failure OR a signal, and must not be interrupted by, or trip,
  # the very traps that brought it here.
  trap - ERR TERM INT HUP
  echo "FAILED — rolling back to $OLD"
  record "$(git rev-parse HEAD 2>/dev/null || echo unknown)" "rolled back"
  git reset --hard --quiet "$OLD"
  local complete=1
  sync_env || { echo "WARNING: rollback sync failed; run deploy/checks.sh repair"; complete=0; }
  # The restore replaces the journal wholesale. After an interrupted deploy (marker recovery), runs
  # on the half-deployed code may have written orders since the snapshot, so keep what is about to
  # be overwritten — and if that copy cannot be made, do not restore at all (F-160).
  local kept="$HOME_DIR/backup/pre-rollback-$(date -u +%Y%m%dT%H%M%SZ)-$$.db"
  if [ -f "$SNAPSHOT" ] && [ -f "$JOURNAL" ] && ! sqlite3 "$JOURNAL" ".backup '$kept'"; then
    echo "WARNING: could not copy the journal aside — NOT restoring $SNAPSHOT; do it by hand (local recovery procedure)"
    SNAPSHOT=""
    complete=0
  fi
  # `|| true`: with no copy made, ls exits non-zero (2 on GNU) and set -e would end the rollback.
  ls -1t "$HOME_DIR"/backup/pre-rollback-*.db 2>/dev/null | tail -n +6 | xargs rm -f -- || true
  if [ -n "$SNAPSHOT" ] && [ -f "$SNAPSHOT" ]; then
    if sqlite3 "$JOURNAL" ".restore '$SNAPSHOT'"; then
      echo "journal restored from $SNAPSHOT"
    else
      echo "WARNING: journal restore failed — restore $SNAPSHOT by hand (local recovery procedure)"
      complete=0
    fi
  fi
  # Only a rollback that finished clears the marker: an unfinished one is still what the next run
  # has to finish.
  if [ "$complete" = 1 ]; then rm -f "$INFLIGHT"; fi
  exit 1
}

# ── 3. the locks ──────────────────────────────────────────────────────────────────────────────
# The run lock name must be argus.runs.lock.LOCK_NAME — a test holds the two equal. This used to
# test `manager.lock`, a file no code creates, so the guard never fired and a deploy could rewrite
# .venv under a manager mid-`place_bracket`; the CLI imports lazily, so that run would have
# finished on half-replaced modules (P-N3).
LOCK_FILE="$HOME_DIR/run.lock"
DEPLOY_LOCK="$HOME_DIR/deploy.lock"
LOCK_WAIT_S="${ARGUS_DEPLOY_LOCK_WAIT_S:-120}"

# A lock that cannot be taken. In the first copy nothing has changed yet, so it is just a refusal.
# In the re-exec'd copy the new commit is ALREADY merged — the copy that exec'd us may predate the
# lock and not have handed it over — so a refusal there has to undo the deploy, not strand it.
lock_failed() {
  if [ "${ARGUS_DEPLOY_REEXEC:-}" = "1" ]; then rollback; fi
  exit "$1"
}

need_flock() {
  # Fail closed: without flock there is no way to know a trade or an eval is not running.
  command -v flock >/dev/null || { echo "flock not found — refusing to deploy without the locks"; lock_failed 1; }
  mkdir -p "$HOME_DIR"
}

take_deploy_lock() {
  need_flock
  # fd 7 stays open, and so stays locked, for the rest of this process — across the `exec` of a
  # newer copy of this script below — and the kernel drops it when the deploy exits.
  exec 7>>"$DEPLOY_LOCK"
  if ! flock -w "$LOCK_WAIT_S" 7; then
    echo "a job that takes no run lock (eval-models, reflect, backup) is running — refusing to" \
         "swap code underneath it; try again in a few minutes"
    lock_failed 75
  fi
}

take_run_lock() {
  need_flock
  # Same for fd 9.
  exec 9>>"$LOCK_FILE"
  if ! flock -w "$LOCK_WAIT_S" 9; then
    echo "a run is in progress — refusing to swap code underneath it; try again in a few minutes"
    # EX_TEMPFAIL, not 1: nothing was touched and a retry is expected to work, so the auto-deploy
    # timer must not raise a failure alert for what is just a trade in progress.
    lock_failed 75
  fi
  # What a manager that queues behind us prints as the holder.
  printf 'deploy pid=%s since=%s' "$$" "$(date +%s)" > "$LOCK_FILE"
}

# Below, `trap rollback ERR TERM INT HUP`: a kill (systemd's TimeoutStartSec, a reboot, `kill`)
# between the merge and the end must undo the deploy like any other failure, not leave it where it
# stood. Bash runs a signal trap once the foreground child (uv) returns.

if [ "${ARGUS_DEPLOY_REEXEC:-}" = "1" ]; then
  init_old
  echo "running the updated deploy script (was $OLD)"
  trap rollback ERR TERM INT HUP
  # The copy that exec'd us normally hands both locks over on fds 7 and 9. One that predates a
  # lock does not, so take it here rather than run unguarded.
  flock -n 7 2>/dev/null || take_deploy_lock
  flock -n 9 2>/dev/null || take_run_lock
  snapshot_journal
  # A copy older than the marker started this deploy and wrote none; cover it from here on.
  [ -f "$INFLIGHT" ] || write_inflight
else
  take_deploy_lock
  take_run_lock
  # CURRENT is what is checked out now; OLD is what a failure rolls back to. Normally the same —
  # unless a dead deploy left a marker, and then OLD is where THAT deploy started.
  CURRENT=$(git rev-parse HEAD)
  recover_inflight
  init_old

  if [ -n "$TO" ]; then
    if ! git merge-base --is-ancestor "$CURRENT" "$NEW"; then
      echo "$NEW is not a fast-forward of $CURRENT — refusing to move the checkout sideways or back"
      exit 1
    fi
  else
    git fetch --quiet origin main
    NEW=$(git rev-parse origin/main)
  fi
  if [ "$CURRENT" = "$NEW" ] && [ "$PRETESTED" != 1 ] && [ "$RECOVERING" != 1 ]; then
    # Verify rather than trust: a previous run that died during the sync leaves the checkout new
    # and the dependencies old, and the old early-exit reported success over that (F-081).
    echo "already at $CURRENT — verifying the installed environment"
    sync_env
    "$UV" run pytest -q
    exit 0
  fi

  if [ "$CURRENT" = "$NEW" ]; then
    # autodeploy found HEAD current but with no "deployed" record: a deploy died part-way.
    # Re-verify the environment and the journal without re-testing (the stamp was checked above).
    echo "re-verifying $CURRENT — nothing to merge"
  else
    echo "updating $CURRENT -> $NEW"
  fi
  snapshot_journal
  # Armed before the merge: a failure during the merge or sync must still be undone (F-078).
  trap rollback ERR TERM INT HUP
  write_inflight
  git merge --ff-only "$NEW"
  sync_env

  # If this script changed in that merge, the copy currently running is stale. Re-exec the new
  # one so a fix to the deploy takes effect on the same run — otherwise a broken update.sh keeps
  # pulling its own fix, failing, and rolling it away again, forever (F-083).
  if ! git diff --quiet "$CURRENT" HEAD -- deploy/update.sh; then
    # `exec` replaces this process, so a syntax error in the new copy would kill the deploy with
    # no rollback. Check it first — the trap is still armed here.
    bash -n deploy/update.sh
    export ARGUS_DEPLOY_REEXEC=1 ARGUS_DEPLOY_OLD="$OLD" ARGUS_DEPLOY_SNAPSHOT="$SNAPSHOT"
    exec "$(pwd)/deploy/update.sh" ${ARGS[@]+"${ARGS[@]}"}
  fi
fi

# ── 4. the gate ───────────────────────────────────────────────────────────────────────────────
if [ "$PRETESTED" = 1 ]; then
  echo "gate: $(git rev-parse --short HEAD) passed ruff, mypy and pytest in the staging checkout — not re-running"
else
  # pytest is the check that earns its place here — "does this work on this box". ruff and mypy
  # already ran on GitHub for this exact commit, so they are a bonus and never a blocker.
  if [ -x .venv/bin/ruff ]; then "$UV" run ruff check . >/dev/null; fi
  if [ -x .venv/bin/mypy ]; then
    "$UV" run mypy argus/gate argus/exchange argus/execution argus/journal/states.py >/dev/null
  fi
  "$UV" run pytest -q
fi

# The smoke runs either way, under the trap: the staged tests ran in ANOTHER venv, so this is the
# only check that THIS environment, after THIS sync, can import the runs and start the CLI.
"$UV" run python -c "import argus.cli, argus.runs.manager, argus.runs.review, argus.runs.watchdog"
"$UV" run argus --help >/dev/null

# The dashboard refuses to start on a journal schema that is behind the code, and nothing else
# migrates before the next timer tick. Migrate now (open_journal applies them) so the restart
# below finds a current schema instead of crash-looping until a run happens. This sits INSIDE the
# trap: it used to run after `trap - ERR` with a NOTE on failure, so a migration that failed left
# new code on a half-migrated journal and the deploy reported success. Now it fails the deploy,
# and the rollback restores the snapshot taken before this run touched anything.
"$UV" run python -c "from argus.journal.db import open_journal; open_journal()"
trap - ERR TERM INT HUP
# The deploy is complete. Cleared BEFORE the record: dying between them costs one re-verification;
# the other order would leave a marker that sends the next failure to a commit that is too old.
rm -f "$INFLIGHT"

# Timers need no restart: each tick starts a fresh process that already has the new code.
#
# The dashboard is the exception — it is a long-running service, so it keeps serving the OLD code
# until something restarts it, and a deploy that silently leaves the UI stale is a deploy that
# lies about having happened. Restart it if the grant exists; say so plainly if it does not,
# rather than failing the deploy over a page.

if sudo -n systemctl restart argus-ui.service 2>/dev/null; then
  echo "restarted argus-ui"
elif systemctl is-active --quiet argus-ui.service; then
  echo "NOTE: argus-ui is running OLD code — restart it with:"
  echo "      sudo systemctl restart argus-ui.service"
fi

record "$(git rev-parse HEAD)" deployed
echo "deployed $(git rev-parse --short HEAD) — the next timer tick runs it"
