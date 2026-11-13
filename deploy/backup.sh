#!/usr/bin/env bash
# Nightly journal snapshot. `.backup` is safe on a live WAL database; `cp` is not.
#
# Compressed on purpose: 30 daily copies of an uncompressed journal is the largest single
# consumer of disk on this box by a wide margin. Once the LLM call log fills up, the journal is
# projected at ~0.6 GB/year, which would make the backup directory ~18 GB — over half the free
# space — while gzip on SQLite text typically wins 5-10x.
set -euo pipefail
HOME_DIR="${ARGUS_HOME:-/var/lib/argus}"
mkdir -p "$HOME_DIR/backup"
stamp=$(date -u +%F)
tmp="$HOME_DIR/backup/journal-$stamp.db"
sqlite3 "$HOME_DIR/journal.db" ".backup '$tmp'"

# Prove the copy before keeping it (P-6). `.backup` copies pages faithfully, corrupt ones too, so
# without this a damaged journal became thirty days of damaged backups, each reported as success.
# A failure exits non-zero — the unit's OnFailure= sends the Telegram alert — and returns BEFORE
# the rotation below, so a bad night never deletes an older good copy. The bad copy is kept,
# renamed, for the post-mortem: it is evidence that the live journal itself is damaged.
verdict=$(sqlite3 "$tmp" "PRAGMA integrity_check;" 2>&1 || true)
if [ "$verdict" != "ok" ]; then
  mv -f "$tmp" "$tmp.corrupt"
  echo "backup FAILED integrity check — kept as $tmp.corrupt; the live journal may be damaged:" >&2
  echo "$verdict" | head -5 >&2
  exit 1
fi
gzip -f "$tmp"
gzip -t "$tmp.gz"

find "$HOME_DIR/backup" -name 'journal-*.db.gz' -mtime +30 -delete
find "$HOME_DIR/backup" -name 'journal-*.db' -mtime +1 -delete   # leftovers from before gzip
# Off-box copy: NOT configured. It needs a remote and credentials only owner can create (P-6); until
# then every backup lives on the same disk as the journal, and VPS loss is journal loss.
# Once a remote exists:  rclone copy "$HOME_DIR/backup" remote:argus-backups
