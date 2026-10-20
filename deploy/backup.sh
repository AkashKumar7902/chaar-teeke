#!/usr/bin/env bash
# One consistent copy of the Chaar Teeke database, made with sqlite3 ".backup" (safe while the app runs),
# into $CT_VAR_DIR/backups. Copies older than policy retention.backup_keep_days (in days of 24 hours) are deleted.
#
# Started daily by chaarteeke-backup.timer, as the service user, with /etc/chaarteeke/env loaded.
# By hand:  sudo systemctl start chaarteeke-backup.service
#
# The copy is NOT encrypted: it holds staff password hashes and the audit log, and the phone numbers in it
# are encrypted with CT_PHONE_KEY. The folder is mode 0700 and the files 0600. Do not copy these files off
# the box; the app's own encrypted copies (CT_BACKUP_DIR) are the ones to take away. See docs/DEPLOY.md.
#
# Exit codes: 0 done; 1 a step failed (nothing partial is left behind); 2 the settings are not usable.
set -euo pipefail
umask 077

app_dir=${CHAARTEEKE_APP_DIR:-/opt/chaarteeke}
db=${CT_DB:-}
var_dir=${CT_VAR_DIR:-}

fail() {
  echo "backup: $*" >&2
  exit 1
}
unusable() {
  echo "backup: $*" >&2
  exit 2
}

[[ -n $db ]] || unusable "CT_DB is not set (it comes from /etc/chaarteeke/env)"
[[ -n $var_dir ]] || unusable "CT_VAR_DIR is not set (it comes from /etc/chaarteeke/env)"
[[ -f $db ]] || unusable "the database $db does not exist yet"
command -v sqlite3 >/dev/null || unusable "sqlite3 is not installed"
python="$app_dir/.venv/bin/python"
[[ -x $python ]] || unusable "$python is missing (run deploy/install.sh)"

# The number of days to keep comes from data/policy.json and nowhere else.
keep=$("$python" -I -c '
import json, sys
print(json.load(open(sys.argv[1]))["retention"]["backup_keep_days"])
' "$app_dir/data/policy.json") || unusable "could not read retention.backup_keep_days from data/policy.json"
[[ $keep =~ ^[1-9][0-9]*$ ]] || unusable "retention.backup_keep_days is '$keep', not a whole number of days"

dest="$var_dir/backups"
mkdir -p "$dest"
chmod 0700 "$dest"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
partial="$dest/.partial-$stamp.db"
final="$dest/chaarteeke-$stamp.db"
[[ ! -e $final ]] || fail "$final already exists"
trap 'rm -f -- "$partial" "$partial-wal" "$partial-shm" "$partial-journal"' EXIT

# ".backup" copies page by page under SQLite's own locking and includes what is still in the WAL.
sqlite3 "$db" ".timeout 30000" ".backup '$partial'" || fail "sqlite3 .backup failed"

# The copy inherits WAL mode from the live database. Switch it to the plain journal so it is one
# self-contained file (no -wal or -shm beside it); the app puts a restored file back into WAL mode itself.
mode=$(sqlite3 "$partial" "PRAGMA journal_mode=DELETE;") || fail "the copy could not be opened"
[[ $mode == delete ]] || fail "could not make the copy a single file (journal_mode is '$mode')"
result=$(sqlite3 "$partial" "PRAGMA integrity_check;") || fail "the copy could not be opened"
[[ $result == ok ]] || fail "the copy failed its integrity check: $result"

# Leave nothing behind: with the journal switched, the -wal file is gone and the -shm file is stale.
rm -f -- "$partial-wal" "$partial-shm"
chmod 0600 "$partial"
mv -- "$partial" "$final"
trap - EXIT

# Delete copies older than the policy's days, counted in minutes (find -mtime would round down and keep a copy
# for one extra day). The timer is daily, so a copy is gone at the first run after it passes that age.
deleted=$(find "$dest" -maxdepth 1 -type f -name 'chaarteeke-*.db' -mmin "+$((keep * 1440))" -print -delete | wc -l)
kept=$(find "$dest" -maxdepth 1 -type f -name 'chaarteeke-*.db' | wc -l)
echo "backup: wrote $final ($(wc -c <"$final") bytes); kept $kept, deleted $deleted older than $keep days"
