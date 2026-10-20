#!/usr/bin/env bash
# Checks the LOGIC of deploy/install.sh and deploy/backup.sh on any machine with bash 5, sqlite3, openssl and
# the repo's .venv (uv sync). Everything happens in a temporary folder; nothing is installed and nothing needs
# root. It does NOT test what needs a Debian system: apt, useradd, systemd, Asterisk, Caddy, udev.
#
#   deploy/tests/logic.sh
#
# Stubs: chown and systemctl are replaced by no-ops, `id` answers for two made-up users. Exit 0 when every
# check passes.
# The settings below are read by the sourced install.sh (invisible to shellcheck); `a && ok || bad` is meant.
# shellcheck disable=SC2034,SC2015,SC2016,SC2329,SC1091,SC2086
set -uo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
PASS=0 FAIL=0
ok() {
  PASS=$((PASS + 1))
  printf 'ok    %s\n' "$1"
}
bad() {
  FAIL=$((FAIL + 1))
  printf 'FAIL  %s\n' "$1"
  [[ -z ${2:-} ]] || printf '        %s\n' "$2"
}
# expect NAME CONDITION...: run the condition as a command
expect() {
  local name=$1
  shift
  if "$@"; then ok "$name"; else bad "$name"; fi
}
refuses() { # refuses NAME COMMAND...: the command must fail (in a subshell, since die exits)
  local name=$1 out
  shift
  if out=$( ("$@") 2>&1); then bad "$name" "it succeeded"; else ok "$name"; fi
  LAST_OUT=$out
}

# mode_of FILE: the octal mode, from GNU stat (Linux, or coreutils on a Mac) or BSD stat
mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }

[[ -x $repo/.venv/bin/python ]] || {
  echo "needs $repo/.venv (run: uv sync)" >&2
  exit 2
}

# shellcheck source=../install.sh
source "$repo/deploy/install.sh"
set +e # install.sh sets -e; the checks below expect some commands to fail

# ----- stubs and paths (all inside $tmp)
chown() { :; }
systemctl() { :; }
APP_DIR=$repo # for the app's Python and the shipped Asterisk files; only read in these tests
ETC_DIR=$tmp/etc
ENV_FILE=$ETC_DIR/env
CHOICES_FILE=$ETC_DIR/install.conf
STATE_DIR=$tmp/state
ASTERISK_ETC=$tmp/asterisk
BACKUP_ROOT=$tmp/install-backups
RSYNC_OWN=()
mkdir -p "$ETC_DIR" "$ASTERISK_ETC"
SERVICE_USER=tester
SERVICE_GROUP=staff
say() { :; } # quiet
warn() { printf 'warn: %s\n' "$*" >>"$tmp/warnings"; }

# ======================================================================= refusals
id() { # a made-up machine: "human" is uid 1001, "svc" is uid 998, anything else does not exist
  case "$*" in
    "-u human") echo 1001 ;;
    "-u svc") echo 998 ;;
    "-u root") echo 0 ;;
    *) return 1 ;;
  esac
}
SERVICE_USER=root
refuses "refuses to run the app as root" check_user_name
[[ $LAST_OUT == *"refusing to run the app as root"* ]] && ok "  ... and says why" || bad "  ... and says why" "$LAST_OUT"
SERVICE_USER=human
refuses "refuses a login account (uid 1001) as the service user" check_user_name
SERVICE_USER='Bad Name'
refuses "refuses a user name with a space" check_user_name
SERVICE_USER='x;rm'
refuses "refuses a user name with shell characters" check_user_name
SERVICE_USER=svc
expect "accepts an existing system account (uid 998)" check_user_name
SERVICE_USER=chaarteeke
expect "accepts a user that does not exist yet" check_user_name
unset -f id
refuses "refuses to run without root" check_platform
[[ $LAST_OUT == *"run it with sudo"* ]] && ok "  ... with a plain sentence" || bad "  ... with a plain sentence" "$LAST_OUT"

# ======================================================================= arguments and choices
reset_choices() {
  SERVICE_USER="" DOMAIN="" TLS_INTERNAL="" PUBLIC_URL="" WITH_ASR="" MODEL_DIR="" WITH_ASTERISK=""
  GIT_URL="" GIT_REF="" SOURCE_DIR="" UV_BIN_ARG="" REFRESH_PROMPTS=0
}
reset_choices
refuses "unknown option is refused" parse_args --nonsense
reset_choices
parse_args --model-dir /srv/model
expect "--model-dir implies --asr" test "$WITH_ASR" = 1
reset_choices
refuses "no address at all is refused" settle_choices
reset_choices
DOMAIN="a b"
refuses "domain with a space is refused" settle_choices
reset_choices
DOMAIN='x;rm -rf'
refuses "domain with shell characters is refused" settle_choices
reset_choices
PUBLIC_URL=https://example.org/path
refuses "public URL with a path is refused" settle_choices
reset_choices
PUBLIC_URL=ftp://example.org
refuses "public URL that is not http(s) is refused" settle_choices
reset_choices
DOMAIN=a.example.org PUBLIC_URL=https://b.example.org
refuses "domain and a different public URL are refused" settle_choices
reset_choices
PUBLIC_URL=https://a.example.org TLS_INTERNAL=1
refuses "--tls-internal without --domain is refused" settle_choices
reset_choices
DOMAIN=reminders.example.org
settle_choices
expect "domain gives the https public URL" test "$PUBLIC_URL" = https://reminders.example.org
expect "defaults: user chaarteeke, asterisk on, asr off" test "$SERVICE_USER/$WITH_ASTERISK/$WITH_ASR" = chaarteeke/1/0
reset_choices
PUBLIC_URL=http://192.0.2.10:8080
: >"$tmp/warnings"
settle_choices
grep -q "not https" "$tmp/warnings" && ok "an http address is allowed but warned about" || bad "an http address is allowed but warned about"
reset_choices
DOMAIN=a.example.org
MODEL_DIR='/srv/model; rm -rf /'
refuses "model dir with shell characters is refused" settle_choices
reset_choices
DOMAIN=a.example.org
MODEL_DIR=relative/model
refuses "relative model dir is refused" settle_choices
reset_choices
DOMAIN=a.example.org
MODEL_DIR=/home/someone/model
refuses "model dir under /home is refused (the service cannot see it)" settle_choices
[[ $LAST_OUT == *ProtectHome* ]] && ok "  ... and says why" || bad "  ... and says why" "$LAST_OUT"
reset_choices
DOMAIN=a.example.org
MODEL_DIR=/srv/indicconformer
settle_choices
expect "model dir under /srv is accepted" test "$MODEL_DIR" = /srv/indicconformer

# saved choices: round trip; flags win over the file; the file never executes anything
reset_choices
DOMAIN=a.example.org TLS_INTERNAL=1 WITH_ASR=1 MODEL_DIR=/srv/model WITH_ASTERISK=0 SERVICE_USER=svc
PUBLIC_URL=https://a.example.org
save_choices
reset_choices
printf 'EVIL=$(touch %s/pwned)\n' "$tmp" >>"$CHOICES_FILE"
load_choices 2>/dev/null
expect "saved choices come back (domain, tls, asr, model, asterisk off, user)" \
  test "$DOMAIN|$TLS_INTERNAL|$WITH_ASR|$MODEL_DIR|$WITH_ASTERISK|$SERVICE_USER" = "a.example.org|1|1|/srv/model|0|svc"
expect "a line in the choices file is never executed" test ! -e "$tmp/pwned"
reset_choices
DOMAIN=b.example.org WITH_ASTERISK=1
load_choices 2>/dev/null
expect "a flag beats the saved value" test "$DOMAIN/$WITH_ASTERISK" = "b.example.org/1"
rm -f "$CHOICES_FILE"

# ======================================================================= templates
printf 'a @@ONE@@ b\nsecond @@TWO@@ & more\n' >"$tmp/t1"
TPL_ONE="x&y" TPL_TWO=$'l1\nl2'
out=$(render_template "$tmp/t1")
[[ $out == $'a x&y b\nsecond l1\nl2 & more' ]] && ok "template: values are inserted verbatim (& and newlines)" || bad "template: values inserted verbatim" "$out"
unset TPL_ONE TPL_TWO
printf 'needs @@MISSING@@\n' >"$tmp/t2"
refuses "template: a placeholder nobody filled is an error" render_template "$tmp/t2"

# ======================================================================= shipped templates render completely
TPL_USER=svc TPL_GROUP=svcgrp
for u in chaarteeke.service chaarteeke-backup.service; do
  out=$(render_template "$repo/deploy/systemd/$u")
  [[ $out != *@@* && $out == *"User=svc"* && $out == *"Group=svcgrp"* ]] && ok "unit $u renders with the user" || bad "unit $u renders with the user"
done
TPL_SITE=reminders.example.org TPL_TLS_LINE=""
out=$(render_template "$repo/deploy/caddy/Caddyfile")
[[ $out == *"reminders.example.org {"* && $out != *@@* && $out != *"tls internal"$'\n'* ]] && ok "Caddyfile: public certificate variant" || bad "Caddyfile: public certificate variant"
TPL_TLS_LINE="tls internal"
out=$(render_template "$repo/deploy/caddy/Caddyfile")
[[ $out == *$'\ttls internal'* ]] && ok "Caddyfile: tls internal variant" || bad "Caddyfile: tls internal variant"
unset TPL_USER TPL_GROUP TPL_SITE TPL_TLS_LINE

# ======================================================================= env file
STATE_DIR=$tmp/state
PUBLIC_URL=https://reminders.example.org
WITH_ASR=0 MODEL_DIR=""
log=$tmp/env-run.log
step_env >"$log" 2>&1
expect "env: file written" test -f "$ENV_FILE"
mode=$(mode_of "$ENV_FILE")
expect "env: mode is 600" test "$mode" = 600
leaks=0
for name in CT_SECRET_KEY CT_PHONE_KEY CT_BACKUP_KEY CT_ARI_PASSWORD; do
  v=$(get_env "$name")
  [[ -n $v ]] || {
    bad "env: $name is set"
    continue
  }
  grep -qF -- "$v" "$log" && leaks=$((leaks + 1))
done
expect "env: no key or password appears in the installer's output" test "$leaks" = 0
secret=$(get_env CT_SECRET_KEY)
expect "env: CT_SECRET_KEY is at least 32 characters" test "${#secret}" -ge 32
for name in CT_PHONE_KEY CT_BACKUP_KEY; do
  if "$repo/.venv/bin/python" -I -c 'import sys; from cryptography.fernet import Fernet; Fernet(sys.argv[1].encode())' "$(get_env "$name")" 2>/dev/null; then
    ok "env: $name is a valid Fernet key"
  else
    bad "env: $name is a valid Fernet key"
  fi
done
expect "env: the three random keys differ" test "$(get_env CT_PHONE_KEY)" != "$(get_env CT_BACKUP_KEY)" -a "$(get_env CT_SECRET_KEY)" != "$(get_env CT_PHONE_KEY)"
expect "env: pilot mode, ari telephony, loopback host" test "$(get_env CT_MODE)/$(get_env CT_TELEPHONY)/$(get_env CT_HOST)" = pilot/ari/127.0.0.1
expect "env: public URL and data paths filled in" test "$(get_env CT_PUBLIC_URL)|$(get_env CT_DB)|$(get_env CT_VAR_DIR)" = "https://reminders.example.org|$tmp/state/var/chaarteeke.db|$tmp/state/var"
expect "env: speech model off is explicit" test "$(get_env CT_ASR)" = off
expect "env: no placeholder is left" test "$(grep -c '@@' "$ENV_FILE")" = 0
expect "env: no empty value that pydantic would read as a path (CT_ASR_MODEL_DIR=)" test "$(grep -c '^CT_ASR_MODEL_DIR=$' "$ENV_FILE")" = 0
sum1=$(shasum "$ENV_FILE")
NOT_DONE=()
step_env >"$tmp/env-run2.log" 2>&1
expect "env: a second run leaves the file and the keys alone" test "$(shasum "$ENV_FILE")" = "$sum1"
expect "env: a complete existing file reports nothing missing" test "${#NOT_DONE[@]}" = 0
grep -v '^CT_BACKUP_KEY=' "$ENV_FILE" >"$tmp/env.cut" && cp "$tmp/env.cut" "$ENV_FILE"
step_env >"$tmp/env-run3.log" 2>&1
[[ ${NOT_DONE[*]:-} == *"CT_BACKUP_KEY"* ]] && ok "env: a missing name is reported, not invented" || bad "env: a missing name is reported, not invented" "${NOT_DONE[*]:-}"
expect "env: ... and the file is still not rewritten" test "$(get_env CT_BACKUP_KEY)" = ""
rm -f "$ENV_FILE"
WITH_ASR=1 MODEL_DIR=/srv/model
step_env >/dev/null 2>&1
expect "env: --asr --model-dir sets CT_ASR and CT_ASR_MODEL_DIR" test "$(get_env CT_ASR)|$(get_env CT_ASR_MODEL_DIR)" = "indicconformer|/srv/model"
rm -f "$ENV_FILE"
WITH_ASR=1 MODEL_DIR=""
step_env >/dev/null 2>&1
expect "env: --asr without a model dir sets no CT_ASR_MODEL_DIR" test "$(get_env CT_ASR_MODEL_DIR)" = ""
WITH_ASR=0
NOT_DONE=()
# the app reads this file: load it the way the app would and see that pilot mode accepts it
rm -f "$ENV_FILE"
step_env >/dev/null 2>&1
if (
  cd "$tmp" &&
    env -i PATH="$PATH" bash -c 'set -a; . "$1"; set +a; exec "$2" -I -c "
from chaarteeke.config import Settings
from chaarteeke.api.app import _secrets
s = Settings(_env_file=None)
assert s.mode == \"pilot\" and s.telephony == \"ari\" and s.asr == \"off\", s
assert str(s.db).endswith(\"state/var/chaarteeke.db\"), s.db
assert s.backup_dir is not None and s.backup_key and s.ari_user == \"chaar\"
_secrets(s)
"' _ "$ENV_FILE" "$repo/.venv/bin/python"
) >"$tmp/load.log" 2>&1; then
  ok "env: the app's Settings accept the file and pilot mode takes its keys"
else
  bad "env: the app's Settings accept the file and pilot mode takes its keys" "$(tail -n 3 "$tmp/load.log")"
fi

# ======================================================================= install_managed
src=$tmp/src.conf dest=$tmp/dest/x.conf
echo one >"$src"
install_managed "$src" "$dest" 0640 root asterisk && ok "managed: a new file is installed" || bad "managed: a new file is installed"
install_managed "$src" "$dest" 0640 root asterisk && bad "managed: an identical file is left" || ok "managed: an identical file is left (returns 1)"
echo two >"$src"
install_managed "$src" "$dest" 0640 root asterisk && ok "managed: a changed file is replaced" || bad "managed: a changed file is replaced"
old=$(find "$BACKUP_ROOT" -type f -name x.conf | head -n 1)
expect "managed: the old content is kept under BACKUP_ROOT" test "$(cat "$old")" = one
expect "managed: the new content is in place" test "$(cat "$dest")" = two
expect "managed: the backup is not next to the file" test "$(ls "$tmp/dest")" = x.conf
mode=$(mode_of "$dest")
expect "managed: mode applied" test "$mode" = 640

# ======================================================================= ari.conf
[[ -f $ENV_FILE ]] || step_env >/dev/null 2>&1
rm -f "$ASTERISK_ETC/ari.conf"
NOT_DONE=() ASTERISK_CHANGED=0
render_ari_conf
conf=$ASTERISK_ETC/ari.conf
pw=$(get_env CT_ARI_PASSWORD)
expect "ari.conf: written, flagged as a change" test -f "$conf" -a "$ASTERISK_CHANGED" = 1
expect "ari.conf: no development password, no plain password" test "$(grep -c -e CHANGE-ME -e "$pw" "$conf")" = 0
expect "ari.conf: crypt format and pretty=no" test "$(grep -c -e '^password_format=crypt' -e '^pretty=no' "$conf")" = 2
hash=$(sed -n 's/^password=//p' "$conf")
salt=$(echo "$hash" | cut -d'$' -f3)
expect "ari.conf: the stored hash verifies against the env password" test "$(printf '%s' "$pw" | openssl passwd -6 -salt "$salt" -stdin)" = "$hash"
expect "ari.conf: the rest of the shipped file is unchanged" test "$(diff <(grep -v -e '^password' -e '^pretty' "$conf") <(grep -v -e '^password' -e '^pretty' "$repo/telephony/asterisk/etc/asterisk/ari.conf") | wc -l)" -eq 0
sum1=$(shasum "$conf")
ASTERISK_CHANGED=0
render_ari_conf
expect "ari.conf: a second run changes nothing" test "$(shasum "$conf")" = "$sum1" -a "$ASTERISK_CHANGED" = 0
# rotate the password in the env file: the next run re-syncs ari.conf and keeps the old file
{
  grep -v '^CT_ARI_PASSWORD=' "$ENV_FILE"
  echo "CT_ARI_PASSWORD=a-new-password-for-the-test"
} >"$tmp/env.new" && cp "$tmp/env.new" "$ENV_FILE"
render_ari_conf
hash2=$(sed -n 's/^password=//p' "$conf")
expect "ari.conf: a changed env password is re-hashed into ari.conf" test "$hash2" != "$hash" -a "$ASTERISK_CHANGED" = 1
salt2=$(echo "$hash2" | cut -d'$' -f3)
expect "ari.conf: ... and verifies" test "$(printf '%s' 'a-new-password-for-the-test' | openssl passwd -6 -salt "$salt2" -stdin)" = "$hash2"
expect "ari.conf: ... and the old file was kept" test -n "$(find "$BACKUP_ROOT" -type f -name ari.conf)"
expect "ari.conf: no failure was recorded" test "${#NOT_DONE[@]}" = 0
# the shipped file gains a line while the password stays: the line must arrive and the hash must not change
APP_REAL=$APP_DIR
APP_DIR=$tmp/app-shipped
mkdir -p "$APP_DIR/telephony/asterisk/etc/asterisk"
{
  cat "$APP_REAL/telephony/asterisk/etc/asterisk/ari.conf"
  echo "; added by a later release"
} >"$APP_DIR/telephony/asterisk/etc/asterisk/ari.conf"
ASTERISK_CHANGED=0
render_ari_conf
expect "ari.conf: a changed shipped file reaches the box" test "$(grep -c 'added by a later release' "$conf")" = 1 -a "$ASTERISK_CHANGED" = 1
expect "ari.conf: ... with the same hash (the password did not change)" test "$(sed -n 's/^password=//p' "$conf")" = "$hash2"
ASTERISK_CHANGED=0
render_ari_conf
expect "ari.conf: ... and the next run changes nothing" test "$ASTERISK_CHANGED" = 0
APP_DIR=$APP_REAL
sed -i.bak 's/^CT_ARI_USER=.*/CT_ARI_USER=someone/' "$ENV_FILE" && rm -f "$ENV_FILE.bak"
render_ari_conf
[[ ${NOT_DONE[*]:-} == *"someone"* ]] && ok "ari.conf: a user name other than chaar is reported, not guessed" || bad "ari.conf: a user name other than chaar is reported" "${NOT_DONE[*]:-}"
NOT_DONE=()

# ======================================================================= copying the app
if rsync --help 2>&1 | grep -q -e '--chmod' && rsync --help 2>&1 | grep -q -e '--ignore-existing'; then
  APP_DIR=$tmp/opt
  mkdir -p "$APP_DIR/.venv" "$APP_DIR/.python" "$APP_DIR/.uv"
  echo marker >"$APP_DIR/.venv/keep"
  echo marker >"$APP_DIR/.python/keep"
  SOURCE_DIR=$repo GIT_URL="" REFRESH_PROMPTS=0 SERVICE_USER=tester SERVICE_GROUP=staff
  step_app_files >"$tmp/app.log" 2>&1 || bad "app: step_app_files ran" "$(tail -n 3 "$tmp/app.log")"
  expect "app: code and policy copied" test -f "$APP_DIR/chaarteeke/main.py" -a -f "$APP_DIR/data/policy.json" -a -f "$APP_DIR/deploy/backup.sh"
  expect "app: .git, var and tests' caches are not copied" test ! -e "$APP_DIR/.git" -a ! -e "$APP_DIR/var" -a ! -e "$APP_DIR/.pytest_cache"
  expect "app: the checkout's .venv is not copied over the installed one" test "$(cat "$APP_DIR/.venv/keep")" = marker -a ! -e "$APP_DIR/.venv/bin"
  expect "app: the installed Python survives" test -f "$APP_DIR/.python/keep"
  expect "app: prompt audio copied" test -f "$APP_DIR/data/prompts/audio/hi/FC_NOTICE.wav"
  expect "app: DEPLOYED written" test -s "$APP_DIR/DEPLOYED"
  echo "native recording" >"$APP_DIR/data/prompts/audio/hi/FC_NOTICE.wav"
  : >"$APP_DIR/stale-file-from-an-old-release"
  step_app_files >"$tmp/app2.log" 2>&1
  expect "app: an update never overwrites an existing prompt file" test "$(cat "$APP_DIR/data/prompts/audio/hi/FC_NOTICE.wav")" = "native recording"
  expect "app: an update removes code files the release no longer has" test ! -e "$APP_DIR/stale-file-from-an-old-release"
  REFRESH_PROMPTS=1
  step_app_files >"$tmp/app3.log" 2>&1
  expect "app: --refresh-prompts overwrites them" test "$(cmp -s "$APP_DIR/data/prompts/audio/hi/FC_NOTICE.wav" "$repo/data/prompts/audio/hi/FC_NOTICE.wav" && echo same)" = same
else
  printf 'skip  app copy tests: this rsync (%s) lacks --chmod or --ignore-existing; the Linux rsync has both\n' "$(command -v rsync)"
fi

# ======================================================================= backup.sh
db=$tmp/live/chaarteeke.db
mkdir -p "$tmp/live" "$tmp/var"
sqlite3 "$db" "PRAGMA journal_mode=WAL; CREATE TABLE t(x); INSERT INTO t VALUES (1),(2),(3);" >/dev/null
# A second process keeps its connection open with auto-checkpointing off and writes one more row, so that row
# exists only in the -wal file while the backup runs (closing the last connection would fold it into the db).
holder_log=$tmp/holder.log
"$repo/.venv/bin/python" -I -c '
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("INSERT INTO t VALUES (4)")
print("ready", flush=True)
time.sleep(60)' "$db" >"$holder_log" 2>&1 &
holder=$!
for _ in $(seq 1 50); do grep -q ready "$holder_log" && break; sleep 0.1; done
expect "backup: the test holds a row that is only in the -wal file" test "$(sqlite3 -readonly "file:$db?immutable=1" 'SELECT COUNT(*) FROM t' 2>/dev/null)" = 3 -a -s "$db-wal"
run_backup() { CHAARTEEKE_APP_DIR=$repo CT_DB=$db CT_VAR_DIR=$tmp/var "$repo/deploy/backup.sh" 2>&1; }
out=$(run_backup); rc=$?
expect "backup: runs and exits 0" test "$rc" = 0
b=$(find "$tmp/var/backups" -name 'chaarteeke-*.db' | head -n 1)
expect "backup: one file written" test -n "$b"
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null
expect "backup: the copy holds the rows (4), one of them from the -wal file" test "$(sqlite3 "$b" 'SELECT COUNT(*) FROM t')" = 4
expect "backup: the copy passes integrity_check" test "$(sqlite3 "$b" 'PRAGMA integrity_check')" = ok
perm_dir=$(mode_of "$tmp/var/backups")
perm_file=$(mode_of "$b")
expect "backup: folder is 700 and the file is 600" test "$perm_dir/$perm_file" = 700/600
expect "backup: no partial file left behind" test -z "$(find "$tmp/var/backups" -name '.partial-*')"
keep=$("$repo/.venv/bin/python" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["retention"]["backup_keep_days"])' "$repo/data/policy.json")
old=$tmp/var/backups/chaarteeke-20000101T000000Z.db
cp "$b" "$old"
touch -t 200001010000 "$old"
expect "backup: the copy is one self-contained file (no -wal or -shm beside it)" test "$(find "$tmp/var/backups" -name '*-wal' -o -name '*-shm' | wc -l | tr -d ' ')" = 0
expect "backup: the copy is not in WAL mode" test "$(sqlite3 "$b" 'PRAGMA journal_mode')" = delete
young=$tmp/var/backups/chaarteeke-20990101T000000Z.db
cp "$b" "$young"
touch -t "$(date -v-$((keep - 1))d +%Y%m%d%H%M 2>/dev/null || date -d "-$((keep - 1)) days" +%Y%m%d%H%M)" "$young"
hours_ago() { date -v-"$1"H +%Y%m%d%H%M 2>/dev/null || date -d "-$1 hours" +%Y%m%d%H%M; }
just_over=$tmp/var/backups/chaarteeke-20990102T000000Z.db
just_under=$tmp/var/backups/chaarteeke-20990103T000000Z.db
cp "$b" "$just_over"
cp "$b" "$just_under"
touch -t "$(hours_ago $((keep * 24 + 3)))" "$just_over"
touch -t "$(hours_ago $((keep * 24 - 3)))" "$just_under"
unrelated=$tmp/var/backups/notes.txt
echo keep >"$unrelated"
touch -t 200001010000 "$unrelated"
out=$(run_backup); rc=$?
expect "backup: a second run in the same second refuses to overwrite" test "$rc" = 1 -a "${out#*already exists}" != "$out"
sleep 1.2
run_backup >/dev/null
expect "backup: a copy older than policy backup_keep_days ($keep) is deleted" test ! -e "$old"
expect "backup: a copy younger than that is kept" test -e "$young"
expect "backup: a copy 3 hours past the limit is deleted (not kept an extra day)" test ! -e "$just_over"
expect "backup: a copy 3 hours inside the limit is kept" test -e "$just_under"
expect "backup: a file that is not a backup is never touched" test -e "$unrelated"
out=$(CHAARTEEKE_APP_DIR=$repo CT_DB=$tmp/nope.db CT_VAR_DIR=$tmp/var "$repo/deploy/backup.sh" 2>&1); rc=$?
expect "backup: a missing database exits 2 and says so" test "$rc" = 2 -a "${out#*does not exist}" != "$out"
out=$(CHAARTEEKE_APP_DIR=$tmp/no-such-app CT_DB=$db CT_VAR_DIR=$tmp/var "$repo/deploy/backup.sh" 2>&1); rc=$?
expect "backup: with no app folder it exits 2 and makes no copy" test "$rc" = 2
out=$(env -u CT_DB CT_VAR_DIR=$tmp/var CHAARTEEKE_APP_DIR=$repo "$repo/deploy/backup.sh" 2>&1); rc=$?
expect "backup: CT_DB unset exits 2" test "$rc" = 2
echo "not a database" >"$tmp/broken.db"
out=$(CHAARTEEKE_APP_DIR=$repo CT_DB=$tmp/broken.db CT_VAR_DIR=$tmp/var "$repo/deploy/backup.sh" 2>&1); rc=$?
expect "backup: a damaged database fails (exit 1) and leaves no partial file" test "$rc" = 1 -a -z "$(find "$tmp/var/backups" -name '.partial-*')"

# ======================================================================= Asterisk's sounds folder (found on Ubuntu 24.04)
# The Ubuntu package keeps its sounds under /usr/share/asterisk; a source build under /var/lib/asterisk. The installer
# asks Asterisk instead of assuming (it once linked /var/lib/asterisk/sounds/chaar and no prompt played).
asterisk() { printf '* Directories\n  Configuration directory:     /etc/asterisk\n  Data directory:              /usr/share/asterisk\n  VarLib directory:            /var/lib/asterisk\n'; }
expect "sounds folder: taken from Asterisk's data directory (Ubuntu package)" test "$(asterisk_sounds_dir)" = /usr/share/asterisk/sounds
asterisk() { printf '  Data directory:              /var/lib/asterisk/\n'; }
expect "sounds folder: a source build's data directory, trailing slash removed" test "$(asterisk_sounds_dir)" = /var/lib/asterisk/sounds
asterisk() { printf 'Unable to connect to remote asterisk\n'; return 1; }
refuses "sounds folder: when Asterisk cannot be asked, nothing is guessed" asterisk_sounds_dir
unset -f asterisk
expect "install_check.sh asks Asterisk for the sounds folder too (no fixed path)" test "$(grep -c '/var/lib/asterisk/sounds' "$repo/scripts/install_check.sh")" = 0

# ======================================================================= Asterisk drop-in (found on Ubuntu 24.04)
dropin=$repo/deploy/systemd/asterisk-dropin.conf
expect "asterisk drop-in: the kernel drops every packet that is not loopback" \
  test "$(grep -c '^IPAddressDeny=any$' "$dropin")" = 1 -a "$(grep -c '^IPAddressAllow=localhost$' "$dropin")" = 1
expect "asterisk drop-in: a changed drop-in restarts Asterisk (installer sets ASTERISK_CHANGED)" \
  grep -q 'asterisk-dropin.conf.*; then ASTERISK_CHANGED=1; fi' "$repo/deploy/install.sh"
expect "asterisk modules.conf: chan_sip is not loaded" grep -q '^noload => chan_sip.so$' "$repo/telephony/asterisk/etc/asterisk/modules.conf"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
((FAIL == 0))
