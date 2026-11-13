#!/usr/bin/env bash
# Update ARGUS on the box, verify it there, and undo everything on failure.
#
# Deployment is deliberately manual: a push to main does NOT reach the server. GitHub runs the
# same gate on every push (.github/workflows/gate.yml), so the green check is the signal that it
# is safe to run this; the box then re-runs the tests itself before trusting the new code.
#
#   sudo -u argus /opt/argus-project/deploy/update.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

# uv lives in the argus user's home and `sudo -u` does not load a login shell, so PATH does not
# have it. Resolve it before touching anything (F-078).
UV=$(command -v uv || true)
[ -x "$UV" ] || UV=/home/argus/.local/bin/uv
if [ ! -x "$UV" ]; then
  echo "uv not found (looked on PATH and at /home/argus/.local/bin/uv)"
  exit 1
fi

# --all-extras matches install.sh. A plain `uv sync` prunes the dev extras, which is how a deploy
# once deleted the test tooling its own gate needed and then failed on its absence (F-082).
sync_env() { "$UV" sync --locked --all-extras --quiet; }

OLD=${ARGUS_DEPLOY_OLD:-$(git rev-parse HEAD)}
HOME_DIR="${ARGUS_HOME:-/var/lib/argus}"

# The journal goes back with the code (P-N4). Migrations are forward-only and checksummed, so
# resetting the code alone leaves the old code facing a schema it has never seen. The snapshot is
# taken under the run lock, so no run is writing while it is copied; `.backup`/`.restore` go
# through SQLite and are safe on a WAL database where `cp` is not.
JOURNAL="$HOME_DIR/journal.db"
# Named for THIS deploy, not just the commit it starts from. A name of `pre-deploy-$OLD.db` that
# was kept if it existed meant a second failed deploy from the same commit restored the first
# attempt's snapshot — days old — and wiped every order, stop and ledger row written since
# (review 2026-10-07, second pass). The re-exec'd newer copy is handed this exact path.
SNAPSHOT="${ARGUS_DEPLOY_SNAPSHOT:-$HOME_DIR/backup/pre-deploy-$OLD-$(date -u +%Y%m%dT%H%M%SZ)-$$.db}"

snapshot_journal() {
  [ -f "$JOURNAL" ] || return 0
  # Only the re-exec reuses a snapshot, and only the one its first copy took moments ago, under
  # the same lock, before the merge. A newer copy started by an older one that predates this
  # hand-over gets none, so it takes its own: no run can have written since the lock was taken.
  if [ "${ARGUS_DEPLOY_REEXEC:-}" = "1" ] && [ -n "${ARGUS_DEPLOY_SNAPSHOT:-}" ] \
     && [ -f "$SNAPSHOT" ]; then
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
record() { mkdir -p "$HOME_DIR"; echo "$(date -u +%FT%TZ) $OLD -> $1 $2" >> "$HISTORY" || true; }

rollback() {
  echo "FAILED — rolling back to $OLD"
  record "$(git rev-parse HEAD 2>/dev/null || echo unknown)" "rolled back"
  git reset --hard --quiet "$OLD"
  sync_env || echo "WARNING: rollback sync failed; run deploy/checks.sh repair"
  if [ -f "$SNAPSHOT" ]; then
    sqlite3 "$JOURNAL" ".restore '$SNAPSHOT'" \
      && echo "journal restored from $SNAPSHOT" \
      || echo "WARNING: journal restore failed — restore $SNAPSHOT by hand (local recovery procedure)"
  fi
  exit 1
}

# The run lock, held for the whole update. The name must be argus.runs.lock.LOCK_NAME — a test
# holds the two equal. This used to test `manager.lock`, a file no code creates, so the guard
# never fired and a deploy could rewrite .venv under a manager mid-`place_bracket`; the CLI
# imports lazily, so that run would have finished on half-replaced modules (P-N3).
LOCK_FILE="$HOME_DIR/run.lock"
LOCK_WAIT_S="${ARGUS_DEPLOY_LOCK_WAIT_S:-120}"

take_run_lock() {
  # Fail closed: without flock there is no way to know a trade is not running.
  command -v flock >/dev/null || { echo "flock not found — refusing to deploy without the run lock"; exit 1; }
  mkdir -p "$HOME_DIR"
  # fd 9 stays open, and so stays locked, for the rest of this process — across the `exec` of a
  # newer copy of this script below — and the kernel drops it when the deploy exits.
  exec 9>>"$LOCK_FILE"
  if ! flock -w "$LOCK_WAIT_S" 9; then
    echo "a run is in progress — refusing to swap code underneath it; try again in a few minutes"
    exit 1
  fi
  # What a manager that queues behind us prints as the holder.
  printf 'deploy pid=%s since=%s' "$$" "$(date +%s)" > "$LOCK_FILE"
}

if [ "${ARGUS_DEPLOY_REEXEC:-}" = "1" ]; then
  echo "running the updated deploy script (was $OLD)"
  trap rollback ERR
  # The copy that exec'd us normally hands the lock over on fd 9. One that predates the lock
  # (the first deploy of this change) does not, so take it here rather than run unguarded.
  flock -n 9 2>/dev/null || take_run_lock
  snapshot_journal
else
  take_run_lock

  git fetch --quiet origin main
  NEW=$(git rev-parse origin/main)
  if [ "$OLD" = "$NEW" ]; then
    # Verify rather than trust: a previous run that died during the sync leaves the checkout new
    # and the dependencies old, and the old early-exit reported success over that (F-081).
    echo "already at $OLD — verifying the installed environment"
    sync_env
    "$UV" run pytest -q
    exit 0
  fi

  echo "updating $OLD -> $NEW"
  snapshot_journal
  # Armed before the merge: a failure during the merge or sync must still be undone (F-078).
  trap rollback ERR
  git merge --ff-only origin/main
  sync_env

  # If this script changed in that merge, the copy currently running is stale. Re-exec the new
  # one so a fix to the deploy takes effect on the same run — otherwise a broken update.sh keeps
  # pulling its own fix, failing, and rolling it away again, forever (F-083).
  if ! git diff --quiet "$OLD" HEAD -- deploy/update.sh; then
    # `exec` replaces this process, so a syntax error in the new copy would kill the deploy with
    # no rollback. Check it first — the trap is still armed here.
    bash -n deploy/update.sh
    export ARGUS_DEPLOY_REEXEC=1 ARGUS_DEPLOY_OLD="$OLD" ARGUS_DEPLOY_SNAPSHOT="$SNAPSHOT"
    exec "$(pwd)/deploy/update.sh"
  fi
fi

# pytest is the check that earns its place here — "does this work on this box". ruff and mypy
# already ran on GitHub for this exact commit, so they are a bonus and never a blocker.
if [ -x .venv/bin/ruff ]; then "$UV" run ruff check . >/dev/null; fi
if [ -x .venv/bin/mypy ]; then
  "$UV" run mypy argus/gate argus/exchange argus/execution argus/journal/states.py >/dev/null
fi
"$UV" run pytest -q
trap - ERR

# Timers need no restart: each tick starts a fresh process that already has the new code.
#
# The dashboard is the exception — it is a long-running service, so it keeps serving the OLD code
# until something restarts it, and a deploy that silently leaves the UI stale is a deploy that
# lies about having happened. Restart it if the grant exists; say so plainly if it does not,
# rather than failing the deploy over a page.
# The dashboard refuses to start on a journal schema that is behind the code, and nothing else
# migrates before the next timer tick. Migrate now (open_journal applies them) so the restart
# below finds a current schema instead of crash-looping until a run happens.
"$UV" run python -c "from argus.journal.db import open_journal; open_journal()" \
  || echo "NOTE: journal migration failed — argus-ui may not start until a run migrates it"

if sudo -n systemctl restart argus-ui.service 2>/dev/null; then
  echo "restarted argus-ui"
elif systemctl is-active --quiet argus-ui.service; then
  echo "NOTE: argus-ui is running OLD code — restart it with:"
  echo "      sudo systemctl restart argus-ui.service"
fi

record "$(git rev-parse HEAD)" deployed
echo "deployed $(git rev-parse --short HEAD) — the next timer tick runs it"
