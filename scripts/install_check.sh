#!/usr/bin/env bash
# Run on the pilot box after deploy/install.sh (and again after every update): is it really working?
#
#   sudo scripts/install_check.sh [--no-dongle] [--skip-preflight] [--backup-now] [--public-probe]
#
#   --no-dongle        skip the dongle device and chan_quectel checks (a box without the dongle yet)
#   --skip-preflight   skip `chaar-teeke preflight` (it takes a few seconds and opens one short extra ARI connection)
#   --backup-now       run the database backup once and check the file it makes
#   --public-probe     also fetch <CT_PUBLIC_URL>/api/mode from this box (a box behind NAT may not reach itself)
#   --skip-play-test   do not make Asterisk play one prompt on a throw-away local channel (it takes about a second)
#
# One line per check: PASS, FAIL or SKIP with the reason. Exit 0 when nothing failed, 1 when something did,
# 2 when it could not start. It reads settings; it changes nothing except what --backup-now says.
# It cannot tell whether the dongle registered on the phone network or whether a call has good audio:
# that is the day-1 hardware checklist in docs/TELEPHONY.md.
set -uo pipefail

ETC_DIR=/etc/chaarteeke
ENV_FILE=$ETC_DIR/env
CHOICES_FILE=$ETC_DIR/install.conf
APP_DIR=/opt/chaarteeke
STATE_DIR=/var/lib/chaarteeke
CADDYFILE=/etc/caddy/Caddyfile

DONGLE=1 PREFLIGHT=1 BACKUP_NOW=0 PUBLIC_PROBE=0 PLAY_TEST=1
for arg in "$@"; do
  case $arg in
    --no-dongle) DONGLE=0 ;;
    --skip-preflight) PREFLIGHT=0 ;;
    --backup-now) BACKUP_NOW=1 ;;
    --public-probe) PUBLIC_PROBE=1 ;;
    --skip-play-test) PLAY_TEST=0 ;;
    -h | --help)
      sed -n '2,/^set -uo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "install_check: unknown option $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "install_check: run it with sudo (it reads $ENV_FILE and runs commands as the service user)" >&2
  exit 2
fi

PASSED=0 FAILED=0 SKIPPED=0
pass() {
  PASSED=$((PASSED + 1))
  printf 'PASS  %-24s %s\n' "$1" "${2:-}"
}
fail() {
  FAILED=$((FAILED + 1))
  printf 'FAIL  %-24s %s\n' "$1" "${2:-}"
}
skip() {
  SKIPPED=$((SKIPPED + 1))
  printf 'SKIP  %-24s %s\n' "$1" "${2:-}"
}

# get_file_value FILE NAME: the value of NAME=value in FILE (text only, never executed).
get_file_value() {
  local line
  [[ -r $1 ]] || return 0
  while IFS= read -r line; do
    [[ $line == "$2="* ]] && {
      printf '%s' "${line#"$2="}"
      return 0
    }
  done <"$1"
  return 0
}
env_get() { get_file_value "$ENV_FILE" "$1"; }
choice() { get_file_value "$CHOICES_FILE" "$1"; }

SERVICE_USER=$(choice SERVICE_USER)
SERVICE_USER=${SERVICE_USER:-chaarteeke}
WITH_ASTERISK=$(choice WITH_ASTERISK)
WITH_ASTERISK=${WITH_ASTERISK:-1}
DOMAIN=$(choice DOMAIN)
TLS_INTERNAL=$(choice TLS_INTERNAL)
CADDY_ROOT=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
SERVICE_GROUP=$(id -gn "$SERVICE_USER" 2>/dev/null || echo "$SERVICE_USER")

# curl with a user name and password that never appear in the process list.
ari_get() { # ari_get PATH -> prints the HTTP status
  local user pass url
  user=$(env_get CT_ARI_USER)
  pass=$(env_get CT_ARI_PASSWORD)
  url=$(env_get CT_ARI_URL)
  user=${user//\\/\\\\}
  user=${user//\"/\\\"}
  pass=${pass//\\/\\\\}
  pass=${pass//\"/\\\"}
  printf 'user = "%s:%s"\n' "$user" "$pass" |
    curl -sS -o /dev/null -w '%{http_code}' --max-time 5 -K - "${url%/}$1" 2>/dev/null
}

# asterisk_sounds_dir: where the running Asterisk looks for sound:NAME (its data directory plus /sounds).
asterisk_sounds_dir() {
  local data
  data=$(asterisk -rx 'core show settings' 2>/dev/null | sed -n 's/^[[:space:]]*Data directory:[[:space:]]*//p' | head -n 1)
  [[ $data == /* ]] || return 1
  printf '%s/sounds\n' "${data%/}"
}

# ari_body PATH: the body of an ARI GET, with the app's user and password kept out of the process list.
ari_body() {
  local user pass url
  user=$(env_get CT_ARI_USER)
  pass=$(env_get CT_ARI_PASSWORD)
  url=$(env_get CT_ARI_URL)
  user=${user//\\/\\\\}
  user=${user//\"/\\\"}
  pass=${pass//\\/\\\\}
  pass=${pass//\"/\\\"}
  printf 'user = "%s:%s"\n' "$user" "$pass" |
    curl -sS --max-time 5 -K - "${url%/}$1" 2>/dev/null
}

as_service() {
  systemd-run --quiet --wait --collect --pipe \
    -p "User=$SERVICE_USER" -p "Group=$SERVICE_GROUP" \
    -p "EnvironmentFile=$ENV_FILE" -p "WorkingDirectory=$APP_DIR" \
    -p UMask=0077 -p Environment=PYTHONDONTWRITEBYTECODE=1 "$@"
}

# listening ADDR_PORT_PATTERN: the local addresses something listens on for the port.
listeners() { ss -H -ltn "sport = :$1" 2>/dev/null | awk '{print $4}'; }

# ----------------------------------------------------------------------------------------------- settings
check_settings() {
  if [[ ! -r $ENV_FILE ]]; then
    fail env-file "cannot read $ENV_FILE (run deploy/install.sh)"
    return
  fi
  local mode owner problems=()
  mode=$(stat -c '%a' "$ENV_FILE")
  owner=$(stat -c '%U' "$ENV_FILE")
  [[ $mode == 600 ]] || problems+=("mode is $mode, should be 600")
  [[ $owner == "$SERVICE_USER" ]] || problems+=("owner is $owner, should be $SERVICE_USER")
  [[ $(env_get CT_MODE) == pilot ]] || problems+=("CT_MODE is not pilot")
  [[ $(env_get CT_TELEPHONY) == ari ]] || problems+=("CT_TELEPHONY is '$(env_get CT_TELEPHONY)'; a pilot box uses 'ari' (add ',sim' only for the browser phone)")
  local secret
  secret=$(env_get CT_SECRET_KEY)
  ((${#secret} >= 32)) || problems+=("CT_SECRET_KEY is shorter than 32 characters")
  [[ -n $(env_get CT_PHONE_KEY) ]] || problems+=("CT_PHONE_KEY is empty")
  [[ $(env_get CT_ARI_PASSWORD) != *CHANGE-ME* && -n $(env_get CT_ARI_PASSWORD) ]] || problems+=("CT_ARI_PASSWORD is empty or the development one")
  [[ $(env_get CT_PUBLIC_URL) == http* ]] || problems+=("CT_PUBLIC_URL is not an http(s) address")
  [[ $(env_get CT_HOST) == 127.0.0.1 ]] || problems+=("CT_HOST is '$(env_get CT_HOST)', should be 127.0.0.1")
  if ((${#problems[@]})); then
    fail env-file "$(
      IFS=';'
      echo "${problems[*]}"
    )"
  else
    pass env-file "mode 600, owner $SERVICE_USER, pilot mode, keys present (values not read out)"
  fi

  if [[ $(env_get CT_ASR) == off ]]; then
    skip speech-model "CT_ASR=off: no transcripts, on purpose"
  else
    local model
    model=$(env_get CT_ASR_MODEL_DIR)
    if [[ -z $model ]]; then
      fail speech-model "CT_ASR is $(env_get CT_ASR) but CT_ASR_MODEL_DIR is not set"
    elif runuser -u "$SERVICE_USER" -- test -d "$model" -a -r "$model" 2>/dev/null; then
      pass speech-model "$model is readable by $SERVICE_USER (the model itself is not loaded here; preflight does that)"
    else
      fail speech-model "$SERVICE_USER cannot read $model (missing, wrong permissions, or under /home, which the service cannot see)"
    fi
  fi

  local sm
  sm=$(stat -c '%a %U' "$STATE_DIR" 2>/dev/null || echo missing)
  if [[ $sm == "700 $SERVICE_USER" ]]; then
    pass data-folder "$STATE_DIR is mode 700, owner $SERVICE_USER"
  else
    fail data-folder "$STATE_DIR is '$sm', should be '700 $SERVICE_USER'"
  fi
}

# ----------------------------------------------------------------------------------------------- app
check_app() {
  if systemctl is-active --quiet chaarteeke.service; then
    pass service "chaarteeke.service is active"
  else
    fail service "chaarteeke.service is not active: journalctl -u chaarteeke -n 50 --no-pager"
  fi
  if systemctl is-enabled --quiet chaarteeke.service; then
    pass service-enabled "starts at boot"
  else
    fail service-enabled "chaarteeke.service is not enabled"
  fi

  local port addrs
  port=$(env_get CT_PORT)
  port=${port:-8080}
  addrs=$(listeners "$port")
  if [[ -z $addrs ]]; then
    fail app-listen "nothing listens on port $port"
  elif [[ $addrs == "127.0.0.1:$port" ]]; then
    pass app-listen "listens on 127.0.0.1:$port only"
  else
    fail app-listen "listens on $(echo "$addrs" | tr '\n' ' '), not loopback only"
  fi

  local body
  body=$(curl -fsS --max-time 5 "http://127.0.0.1:$port/api/mode" 2>/dev/null || true)
  if [[ $body == *'"mode":"pilot"'* ]]; then
    pass app-mode "/api/mode answers: pilot mode"
  else
    fail app-mode "/api/mode did not answer in pilot mode"
  fi

  local admins
  admins=$(as_service sqlite3 -cmd '.timeout 5000' "$(env_get CT_DB)" "SELECT COUNT(*) FROM staff WHERE role='admin'" 2>/dev/null || true)
  if [[ $admins =~ ^[1-9][0-9]*$ ]]; then
    pass first-admin "an administrator exists"
  else
    fail first-admin "no administrator yet: systemd-run --pty --wait -p User=$SERVICE_USER -p EnvironmentFile=$ENV_FILE -p WorkingDirectory=$APP_DIR $APP_DIR/.venv/bin/chaar-teeke create-admin"
  fi

  if [[ $(timedatectl show -p NTPSynchronized --value 2>/dev/null) == yes ]]; then
    pass clock "the system clock is synchronised (reminders go out at fixed local times)"
  else
    fail clock "the clock is not synchronised (timedatectl). A Raspberry Pi has no battery clock; calls would go out at the wrong time"
  fi

  local need free
  need=$("$APP_DIR/.venv/bin/python" -I -c '
import json, sys
print(json.load(open(sys.argv[1]))["storage"]["min_free_mb"])
' "$APP_DIR/data/policy.json" 2>/dev/null || true)
  free=$(df -Pm "$STATE_DIR" 2>/dev/null | awk 'NR==2 {print $4}')
  if [[ $need =~ ^[0-9]+$ && $free =~ ^[0-9]+$ ]]; then
    if ((free >= need)); then
      pass disk-free "$free MB free under $STATE_DIR (policy needs $need MB)"
    else
      fail disk-free "only $free MB free under $STATE_DIR; policy storage.min_free_mb is $need"
    fi
  else
    fail disk-free "could not read the free space or policy storage.min_free_mb"
  fi
}

# check_play: have Asterisk itself play the shortest shipped prompt on a throw-away Local channel and read
# PLAYBACKSTATUS. This is the one check that proves "sound:chaar/hi/NAME" resolves the way a call needs.
check_play() {
  if ((!PLAY_TEST)); then
    skip prompt-play "--skip-play-test"
    return
  fi
  local pick name ctx=ct_install_check ext=9001 body=""
  pick=$(find "$APP_DIR/data/prompts/audio/hi" -maxdepth 1 -name '*.wav' -printf '%s %p\n' 2>/dev/null | sort -n | head -n 1 | cut -d' ' -f2-)
  if [[ -z $pick ]]; then
    fail prompt-play "no .wav file in $APP_DIR/data/prompts/audio/hi"
    return
  fi
  name=$(basename "$pick" .wav)
  asterisk -rx "dialplan remove context $ctx" >/dev/null 2>&1
  asterisk -rx "dialplan set global CT_INSTALL_CHECK PENDING" >/dev/null 2>&1
  asterisk -rx "dialplan add extension $ext,1,Answer into $ctx" >/dev/null 2>&1
  asterisk -rx "dialplan add extension $ext,2,Playback(chaar/hi/$name) into $ctx" >/dev/null 2>&1
  # shellcheck disable=SC2016  # ${PLAYBACKSTATUS} is Asterisk's variable, not the shell's
  asterisk -rx "dialplan add extension $ext,3,Set(GLOBAL(CT_INSTALL_CHECK)=\${PLAYBACKSTATUS}) into $ctx" >/dev/null 2>&1
  asterisk -rx "dialplan add extension $ext,4,Hangup into $ctx" >/dev/null 2>&1
  asterisk -rx "channel originate Local/$ext@$ctx extension $ext@$ctx" >/dev/null 2>&1
  for _ in $(seq 1 20); do
    body=$(ari_body "/ari/asterisk/variable?variable=CT_INSTALL_CHECK")
    [[ $body != *PENDING* ]] && break
    sleep 1
  done
  asterisk -rx "dialplan remove context $ctx" >/dev/null 2>&1
  case $body in
    *'"SUCCESS"'*) pass prompt-play "Asterisk played chaar/hi/$name (a call can find and play the prompt audio)" ;;
    *PENDING*) fail prompt-play "Asterisk did not finish playing chaar/hi/$name within 20 s" ;;
    *) fail prompt-play "Asterisk could not play chaar/hi/$name (PLAYBACKSTATUS $body); its log: /var/log/asterisk/messages.log" ;;
  esac
}

# ----------------------------------------------------------------------------------------------- telephony
check_telephony() {
  if ((!WITH_ASTERISK)); then
    skip asterisk "installed with --no-asterisk: ARI is elsewhere, check it there"
  else
    if systemctl is-active --quiet asterisk.service; then
      pass asterisk "asterisk.service is active"
    else
      fail asterisk "asterisk.service is not active: journalctl -u asterisk -n 50 --no-pager"
    fi
    local addrs
    addrs=$(listeners 8088)
    if [[ $addrs == "127.0.0.1:8088" ]]; then
      pass ari-listen "Asterisk's HTTP/ARI listens on 127.0.0.1:8088 only"
    else
      fail ari-listen "port 8088 is served on '$(echo "$addrs" | tr '\n' ' ')', expected 127.0.0.1:8088 only"
    fi
    if grep -q 'CHANGE-ME' /etc/asterisk/ari.conf 2>/dev/null; then
      fail ari-conf "/etc/asterisk/ari.conf still has the development password"
    elif grep -q '^password_format=crypt' /etc/asterisk/ari.conf 2>/dev/null; then
      pass ari-conf "ari.conf stores a hashed password"
    else
      fail ari-conf "/etc/asterisk/ari.conf is missing or does not use password_format=crypt"
    fi
    # Asterisk's own sockets, with the address and port split. TCP and UDP on a fixed port must be on loopback.
    # res_pjsip (which cannot be unloaded) also opens two UDP client sockets on every address, on random
    # ports, for its DNS resolver; they are accepted only while the kernel filter in the drop-in is on.
    local open_ports ephemeral_lo ephemeral_hi stray=() client=() line proto addr port
    read -r ephemeral_lo ephemeral_hi < /proc/sys/net/ipv4/ip_local_port_range 2>/dev/null || ephemeral_lo=""
    open_ports=$(ss -H -lntup 2>/dev/null | awk '/"asterisk"/ {print $1 " " $5}' | grep -v -E ' (127\.0\.0\.1|\[::1\]):[0-9]+$' || true)
    while read -r proto addr; do
      [[ -n $proto ]] || continue
      port=${addr##*:}
      if [[ $proto == udp && $ephemeral_lo =~ ^[0-9]+$ && $port =~ ^[0-9]+$ ]] && ((port >= ephemeral_lo && port <= ephemeral_hi)); then
        client+=("$proto $addr")
      else
        stray+=("$proto $addr")
      fi
    done <<<"$open_ports"
    local ipdeny
    ipdeny=$(systemctl show asterisk.service -p IPAddressDeny --value 2>/dev/null || true)
    if ((${#stray[@]})); then
      fail asterisk-sockets "Asterisk listens beyond loopback: $(
        IFS=';'
        echo "${stray[*]}"
      ). Find the module (asterisk -rx 'module show') and add noload to modules.conf"
    elif ((${#client[@]})) && [[ $ipdeny != *0.0.0.0/0* ]]; then
      fail asterisk-sockets "Asterisk has UDP sockets on every address (${client[*]}) and the kernel filter is off: IPAddressDeny=any is missing from the asterisk.service drop-in (run deploy/install.sh)"
    elif ((${#client[@]})); then
      pass asterisk-sockets "no fixed-port socket beyond loopback; ${#client[@]} UDP client socket(s) on random ports (on Ubuntu 24.04: res_pjsip's DNS resolver) are cut off by IPAddressDeny=any in the asterisk.service drop-in"
    else
      pass asterisk-sockets "every socket Asterisk listens on is on loopback"
    fi
    if grep -q '^\[softphone-auth\]' /etc/asterisk/pjsip.conf 2>/dev/null; then
      fail dev-sip-endpoint "/etc/asterisk/pjsip.conf still has the development 'softphone' endpoint (known password, loopback only): remove it from telephony/asterisk/etc/asterisk/pjsip.conf and run deploy/install.sh"
    else
      pass dev-sip-endpoint "no development SIP endpoint in pjsip.conf"
    fi
    local sounds link target
    if sounds=$(asterisk_sounds_dir); then
      link=$sounds/chaar
      target=$(readlink "$link" 2>/dev/null || true)
      if [[ $target == "$APP_DIR/data/prompts/audio" ]]; then
        if runuser -u asterisk -- test -r "$link/hi/FC_NOTICE.wav" 2>/dev/null; then
          pass prompt-audio "$link links to the prompt audio (Asterisk's own sounds folder) and the asterisk user can read it"
        else
          fail prompt-audio "the asterisk user cannot read $link/hi/FC_NOTICE.wav (folder permissions?)"
        fi
      else
        fail prompt-audio "$link is not a link to $APP_DIR/data/prompts/audio (Asterisk looks for sounds in $sounds): run deploy/install.sh"
      fi
    else
      fail prompt-audio "could not ask Asterisk where its sounds folder is (asterisk -rx 'core show settings' gave no data directory)"
    fi
    check_play
  fi

  local code app
  code=$(ari_get /ari/asterisk/info)
  if [[ $code == 200 ]]; then
    pass ari-reachable "ARI answered 200 to the app's user and password"
  else
    fail ari-reachable "ARI answered '${code:-nothing}' at $(env_get CT_ARI_URL) (401 = wrong password, nothing = Asterisk is down)"
  fi
  app=$(env_get CT_ARI_APP)
  app=${app:-chaar}
  code=$(ari_get "/ari/applications/$app")
  if [[ $code == 200 ]]; then
    pass ari-app "the app is connected to ARI as application '$app'"
  else
    fail ari-app "ARI has no application '$app' (HTTP '${code:-nothing}'): the app's telephony link is down; see the admin system tab"
  fi

  if ((WITH_ASTERISK)); then
    # The CDR engine itself, app_cdr, func_cdr and app_forkcdr are core modules that always load (Asterisk 20
    # lists them under 'module show like cdr'). What writes a record is a backend (cdr_*.so, cel_*.so,
    # res_cdrel_custom.so), and the engines must say logging is off.
    local backends cdr_status cel_status problems=()
    backends=$(
      for m in cdr_ cel_ res_cdrel; do asterisk -rx "module show like $m" 2>/dev/null; done |
        awk '$1 ~ /^(cdr_|cel_|res_cdrel)/ && $0 !~ /Not Running/ && $0 ~ /Running/ {print $1}' | sort -u | tr '\n' ' '
    )
    cdr_status=$(asterisk -rx 'cdr show status' 2>/dev/null || true)
    cel_status=$(asterisk -rx 'cel show status' 2>/dev/null || true)
    [[ -z $backends ]] || problems+=("record backends running: $backends")
    grep -q 'Logging:[[:space:]]*Disabled' <<<"$cdr_status" || problems+=("'cdr show status' does not say Logging: Disabled")
    grep -q 'CEL Logging:[[:space:]]*Disabled' <<<"$cel_status" || problems+=("'cel show status' does not say CEL Logging: Disabled")
    if ((${#problems[@]})); then
      fail no-call-records "$(
        IFS=';'
        echo "${problems[*]}"
      )"
    else
      pass no-call-records "no CDR or CEL backend is running and both engines say logging is disabled"
    fi
  fi

  if ((!DONGLE)); then
    skip dongle "--no-dongle"
    return
  fi
  local quectel
  quectel=$(asterisk -rx 'module show like chan_quectel' 2>/dev/null || true)
  if [[ $quectel == *chan_quectel.so* ]]; then
    pass chan_quectel "chan_quectel is loaded in Asterisk"
  else
    fail chan_quectel "chan_quectel is not loaded (it is built separately: docs/DEPLOY.md)"
  fi
  if [[ -e /dev/sim7600-at && -e /dev/sim7600-audio ]]; then
    pass dongle-ports "/dev/sim7600-at and /dev/sim7600-audio exist"
  else
    fail dongle-ports "/dev/sim7600-at or /dev/sim7600-audio is missing (dongle not plugged in, or the udev rule did not match: docs/TELEPHONY.md day 1)"
  fi
}

# ----------------------------------------------------------------------------------------------- backups
check_backups() {
  local dir
  dir=$(env_get CT_VAR_DIR)/backups
  if systemctl is-active --quiet chaarteeke-backup.timer; then
    pass backup-timer "chaarteeke-backup.timer is active; next: $(systemctl show chaarteeke-backup.timer -p NextElapseUSecRealtime --value 2>/dev/null)"
  else
    fail backup-timer "chaarteeke-backup.timer is not active"
  fi
  if ((BACKUP_NOW)); then
    if systemctl start chaarteeke-backup.service; then
      pass backup-run "chaarteeke-backup.service ran and exited 0"
    else
      fail backup-run "chaarteeke-backup.service failed: journalctl -u chaarteeke-backup -n 20 --no-pager"
    fi
  fi
  local newest
  newest=$(find "$dir" -maxdepth 1 -type f -name 'chaarteeke-*.db' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -n 1 | cut -d' ' -f2-)
  if [[ -z $newest ]]; then
    skip backup-file "no backup yet (the timer has not fired; use --backup-now to test one)"
    return
  fi
  local result age_h
  result=$(as_service sqlite3 -readonly "$newest" "PRAGMA integrity_check;" 2>/dev/null || true)
  age_h=$((($(date +%s) - $(stat -c %Y "$newest")) / 3600))
  if [[ $result == ok ]]; then
    if ((age_h <= 36)); then
      pass backup-file "$(basename "$newest") passes integrity_check, $age_h h old"
    else
      fail backup-file "newest backup is $age_h h old (timer is daily): the backup job is failing; journalctl -u chaarteeke-backup"
    fi
  else
    fail backup-file "$(basename "$newest") did not pass integrity_check"
  fi
  local enc
  enc=$(env_get CT_BACKUP_DIR)
  if [[ -n $enc ]]; then
    if find "$enc" -maxdepth 1 -type f -name 'chaarteeke-*.db.enc' 2>/dev/null | grep -q .; then
      pass app-backup "the app has written encrypted backups to $enc"
    else
      skip app-backup "no encrypted backup in $enc yet (the app writes one a day, after 00:00 local time, while it runs)"
    fi
  fi
}

# ----------------------------------------------------------------------------------------------- proxy
check_proxy() {
  if [[ -z $DOMAIN ]]; then
    skip https-proxy "no --domain was given at install: your own proxy fronts the app, check it there"
    return
  fi
  if systemctl is-active --quiet caddy.service; then
    pass caddy "caddy.service is active"
  else
    fail caddy "caddy.service is not active: journalctl -u caddy -n 50 --no-pager"
  fi
  # throw-away data folder: validate would otherwise make a certificate authority under root's home
  local scratch
  scratch=$(mktemp -d)
  if command -v caddy >/dev/null && HOME=$scratch XDG_DATA_HOME=$scratch XDG_CONFIG_HOME=$scratch caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
    pass caddyfile "caddy validate accepts $CADDYFILE"
  else
    fail caddyfile "caddy validate rejects $CADDYFILE (or caddy is missing)"
  fi
  rm -rf -- "$scratch"
  if ((PUBLIC_PROBE)); then
    local url body trust=()
    url=$(env_get CT_PUBLIC_URL)
    # With --tls-internal the certificate is signed by Caddy's own authority, which no system trusts: check
    # against that root, so the name and the chain are still verified.
    if [[ $TLS_INTERNAL == 1 ]]; then
      if [[ -r $CADDY_ROOT ]]; then
        trust=(--cacert "$CADDY_ROOT")
      else
        fail public-url "tls internal is on but Caddy's root certificate is not at $CADDY_ROOT (Caddy has not made it yet?)"
        return
      fi
    fi
    body=$(curl -fsS --max-time 10 ${trust[@]+"${trust[@]}"} "${url%/}/api/mode" 2>/dev/null || true)
    if [[ $body == *'"mode":"pilot"'* ]]; then
      pass public-url "$url answers over HTTPS"
    else
      fail public-url "$url did not answer (DNS, ports 80 and 443, or this box cannot reach its own public address)"
    fi
  else
    skip public-url "pass --public-probe to fetch the public address from this box"
  fi
}

# ----------------------------------------------------------------------------------------------- preflight
check_preflight() {
  if ((!PREFLIGHT)); then
    skip preflight "--skip-preflight"
    return
  fi
  local out rc
  out=$(as_service "$APP_DIR/.venv/bin/chaar-teeke" preflight 2>&1)
  rc=$?
  if ((rc == 0)); then
    pass preflight "chaar-teeke preflight: $(echo "$out" | tail -n 1)"
  else
    fail preflight "chaar-teeke preflight exited $rc; its output follows"
    # shellcheck disable=SC2001
    echo "$out" | sed 's/^/      | /'
  fi
}

check_settings
check_app
check_telephony
check_backups
check_proxy
check_preflight

printf '\n%d passed, %d failed, %d skipped\n' "$PASSED" "$FAILED" "$SKIPPED"
((FAILED == 0))
