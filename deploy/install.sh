#!/usr/bin/env bash
# Chaar Teeke: install or update the pilot box (Raspberry Pi OS 64-bit, or Ubuntu 24.04 on x86_64).
#
#   sudo deploy/install.sh --domain reminders.example.org      first install, with HTTPS by Caddy
#   sudo deploy/install.sh --public-url https://reminders.example.org   first install, TLS handled elsewhere
#   sudo deploy/install.sh                                     update: repeats the choices of the last run
#
# It is safe to run again. It never overwrites /etc/chaarteeke/env (the keys), never deletes data, and keeps
# a copy of every config file it replaces under /var/backups/chaarteeke-install/<time>/.
# The app runs as a dedicated, login-less system user. This script needs root to set that up, and refuses
# to make the app run as root. Secrets are generated on the box, written only to /etc/chaarteeke/env
# (mode 0600) and never printed. Read docs/DEPLOY.md first.
#
# Options (all optional after the first run; the choices are saved in /etc/chaarteeke/install.conf):
#   --domain NAME        install Caddy and serve https://NAME (public URL becomes https://NAME)
#   --tls-internal       with --domain: Caddy's own certificate authority, for a private-network name
#   --public-url URL     the address people reach, https://host[:port]; required unless --domain
#   --asr                install the speech model's libraries (uv sync --extra asr); CT_ASR=indicconformer
#   --model-dir DIR      CT_ASR_MODEL_DIR (the model is not downloaded here); implies --asr
#   --no-asterisk        do not install Asterisk or touch /etc/asterisk (ARI is somewhere else)
#   --with-asterisk      undo a saved --no-asterisk
#   --user NAME          the service user (default chaarteeke); must be a system account, never root
#   --source DIR         copy the app from DIR (default: the checkout this script is in)
#   --git URL [--ref R]  clone the app instead (a branch or tag R; default the remote's default branch)
#   --uv-bin PATH        use this uv instead of downloading the pinned one
#   --refresh-prompts    overwrite prompt audio with the shipped files (replaces uploaded recordings)
#   -h, --help
#
# Exit codes: 0 everything asked for is done; 1 refused or a step failed; 3 installed, but some steps
# could not run and are listed at the end as NOT DONE (nothing stands in for them).
# The TPL_* variables are read by render_template through indirect expansion, which shellcheck cannot follow.
# shellcheck disable=SC2034
set -euo pipefail

# ---------------------------------------------------------------------------------------------- constants
# Plain variables, not readonly: deploy/tests/logic.sh sources this file and points them at temporary folders.
APP_DIR=/opt/chaarteeke
STATE_DIR=/var/lib/chaarteeke
ETC_DIR=/etc/chaarteeke
ENV_FILE=$ETC_DIR/env
CHOICES_FILE=$ETC_DIR/install.conf
SYSTEMD_DIR=/etc/systemd/system
ASTERISK_ETC=/etc/asterisk
# Asterisk's sounds folder differs between builds (/usr/share/asterisk/sounds in the Debian and Ubuntu package,
# /var/lib/asterisk/sounds in a source build), so the link is made where Asterisk itself says (asterisk_sounds_dir).
# Tests set SOUNDS_LINK to a path of their own.
SOUNDS_LINK=""
UDEV_DIR=/etc/udev/rules.d
LOGROTATE_DIR=/etc/logrotate.d
CADDYFILE=/etc/caddy/Caddyfile
BACKUP_ROOT=/var/backups/chaarteeke-install
UV_CACHE=/var/cache/chaarteeke/uv
# Pinned uv. The installer script for exactly this version is downloaded over HTTPS (--uv-bin skips it).
UV_VERSION=0.11.23
UV_INSTALLER_URL="https://astral.sh/uv/${UV_VERSION}/install.sh"
PYTHON_VERSION=3.12
# rsync options that set the owner of the copied files; the tests (which do not run as root) empty this.
RSYNC_OWN=(--chown=root:root)

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
STEP_NO=0
STEP_TOTAL=12
NOT_DONE=()
ASTERISK_CHANGED=0
CLONE_DIR=""

# Choices (defaults; saved ones and flags override).
# An empty value means "no flag gave it": the saved choice applies, then the default (see main).
SERVICE_USER=""
SERVICE_GROUP=""
DOMAIN=""
TLS_INTERNAL=""
PUBLIC_URL=""
WITH_ASR=""
MODEL_DIR=""
WITH_ASTERISK=""
SOURCE_DIR=""
GIT_URL=""
GIT_REF=""
UV_BIN_ARG=""
REFRESH_PROMPTS=0

# ---------------------------------------------------------------------------------------------- output
say() { printf '      %s\n' "$*"; }
warn() { printf '      WARNING: %s\n' "$*" >&2; }
die() {
  printf 'install: %s\n' "$*" >&2
  exit 1
}
step() {
  STEP_NO=$((STEP_NO + 1))
  printf '\n[%d/%d] %s\n' "$STEP_NO" "$STEP_TOTAL" "$*"
}
# A step that cannot run says so here and in the summary; nothing stands in for it.
not_done() {
  NOT_DONE+=("$1")
  printf '      NOT DONE: %s\n' "$1" >&2
}

usage() { sed -n '2,/^# The TPL_/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------------------------------------- arguments
parse_args() {
  while (($#)); do
    case $1 in
      --domain) DOMAIN=${2:?--domain needs a host name}; shift 2 ;;
      --tls-internal) TLS_INTERNAL=1; shift ;;
      --public-url) PUBLIC_URL=${2:?--public-url needs an address}; shift 2 ;;
      --asr) WITH_ASR=1; shift ;;
      --model-dir) MODEL_DIR=${2:?--model-dir needs a folder}; WITH_ASR=1; shift 2 ;;
      --no-asterisk) WITH_ASTERISK=0; shift ;;
      --with-asterisk) WITH_ASTERISK=1; shift ;;
      --user) SERVICE_USER=${2:?--user needs a name}; shift 2 ;;
      --source) SOURCE_DIR=${2:?--source needs a folder}; shift 2 ;;
      --git) GIT_URL=${2:?--git needs a URL}; shift 2 ;;
      --ref) GIT_REF=${2:?--ref needs a branch or tag}; shift 2 ;;
      --uv-bin) UV_BIN_ARG=${2:?--uv-bin needs a path}; shift 2 ;;
      --refresh-prompts) REFRESH_PROMPTS=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown option $1 (try --help)" ;;
    esac
  done
}

# Saved choices: plain NAME=value lines, read without executing anything.
load_choices() {
  [[ -f $CHOICES_FILE ]] || return 0
  local key value
  while IFS='=' read -r key value; do
    case $key in
      DOMAIN) [[ -n $DOMAIN ]] || DOMAIN=$value ;;
      TLS_INTERNAL) [[ -n $TLS_INTERNAL ]] || TLS_INTERNAL=$value ;;
      PUBLIC_URL) [[ -n $PUBLIC_URL ]] || PUBLIC_URL=$value ;;
      WITH_ASR) [[ -n $WITH_ASR ]] || WITH_ASR=$value ;;
      MODEL_DIR) [[ -n $MODEL_DIR ]] || MODEL_DIR=$value ;;
      WITH_ASTERISK) [[ -n $WITH_ASTERISK ]] || WITH_ASTERISK=$value ;;
      SERVICE_USER) [[ -n $SERVICE_USER ]] || SERVICE_USER=$value ;;
      '' | \#*) ;;
      *) warn "ignoring unknown line '$key' in $CHOICES_FILE" ;;
    esac
  done <"$CHOICES_FILE"
}

save_choices() {
  local tmp="$CHOICES_FILE.tmp"
  {
    echo "# Written by deploy/install.sh; the choices it repeats on the next run. No secrets."
    echo "DOMAIN=$DOMAIN"
    echo "TLS_INTERNAL=$TLS_INTERNAL"
    echo "PUBLIC_URL=$PUBLIC_URL"
    echo "WITH_ASR=$WITH_ASR"
    echo "MODEL_DIR=$MODEL_DIR"
    echo "WITH_ASTERISK=$WITH_ASTERISK"
    echo "SERVICE_USER=$SERVICE_USER"
  } >"$tmp"
  chmod 0644 "$tmp"
  mv -- "$tmp" "$CHOICES_FILE"
}

valid_host() { [[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]; }
valid_url() { [[ $1 =~ ^https?://[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:[0-9]{1,5})?$ ]]; }

# Validate the choices and settle the derived ones. Dies with a plain sentence.
settle_choices() {
  : "${SERVICE_USER:=chaarteeke}" "${TLS_INTERNAL:=0}" "${WITH_ASR:=0}" "${WITH_ASTERISK:=1}"
  if [[ -n $DOMAIN ]]; then
    valid_host "$DOMAIN" || die "--domain '$DOMAIN' is not a host name (letters, digits, dots and dashes)"
    [[ -z $PUBLIC_URL || $PUBLIC_URL == "https://$DOMAIN" ]] ||
      die "--domain $DOMAIN and --public-url $PUBLIC_URL disagree; give one of them"
    PUBLIC_URL=https://$DOMAIN
  fi
  [[ -n $PUBLIC_URL ]] || die "say where people reach this box: --domain NAME (with Caddy) or --public-url https://NAME"
  valid_url "$PUBLIC_URL" || die "--public-url '$PUBLIC_URL' must look like https://name or https://name:port (no path)"
  [[ $TLS_INTERNAL -eq 0 || -n $DOMAIN ]] || die "--tls-internal needs --domain"
  [[ $TLS_INTERNAL =~ ^[01]$ && $WITH_ASR =~ ^[01]$ && $WITH_ASTERISK =~ ^[01]$ ]] || die "$CHOICES_FILE has a damaged value"
  if [[ -n $MODEL_DIR ]]; then
    [[ $MODEL_DIR == /* ]] || die "--model-dir must be an absolute path"
    [[ $MODEL_DIR =~ ^/[A-Za-z0-9._/+-]+$ ]] || die "--model-dir may use only letters, digits and . _ / + -"
    # chaarteeke.service sets ProtectHome=yes: /home, /root and /run/user are empty to it.
    [[ $MODEL_DIR != /home/* && $MODEL_DIR != /root/* && $MODEL_DIR != /run/user/* && $MODEL_DIR != /home && $MODEL_DIR != /root ]] ||
      die "--model-dir $MODEL_DIR is under a home folder, which the service cannot see (ProtectHome=yes); put the model in /srv or /opt"
  fi
  # No fallback on a public address that is not https: say it, do not decide for the deployer.
  if [[ $PUBLIC_URL == http://* ]]; then
    warn "$PUBLIC_URL is not https: the browser microphone and the Secure login cookie need https"
  fi
}

# ---------------------------------------------------------------------------------------------- checks
check_user_name() {
  [[ $SERVICE_USER =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || die "'$SERVICE_USER' is not a usable user name"
  [[ $SERVICE_USER != root ]] || die "refusing to run the app as root: give a dedicated user with --user"
  local uid
  if uid=$(id -u "$SERVICE_USER" 2>/dev/null); then
    if ((uid == 0 || uid >= 1000)); then
      die "user '$SERVICE_USER' (uid $uid) is not a system account; the app needs its own login-less user"
    fi
  fi
}

check_platform() {
  [[ ${EUID} -eq 0 ]] || die "this changes the system: run it with sudo"
  [[ -r /etc/os-release ]] || die "no /etc/os-release: this installer supports Raspberry Pi OS and Ubuntu"
  local id version
  # shellcheck source=/dev/null
  id=$(. /etc/os-release && echo "${ID:-}")
  # shellcheck source=/dev/null
  version=$(. /etc/os-release && echo "${VERSION_ID:-}")
  case $id in
    debian | raspbian | ubuntu) ;;
    *) die "'$id' is not supported; use Raspberry Pi OS (64-bit) or Ubuntu 24.04" ;;
  esac
  case "$id:$version" in
    debian:12 | raspbian:12 | ubuntu:24.04) ;;
    *) warn "$id $version is not one of the documented systems (Debian/Raspberry Pi OS 12, Ubuntu 24.04)" ;;
  esac
  case $(uname -m) in
    aarch64 | x86_64) ;;
    *) die "$(uname -m) is not supported: the speech libraries and Python 3.12 builds need a 64-bit system" ;;
  esac
  [[ -d /run/systemd/system ]] || die "systemd is not running here; the services need it"
  command -v apt-get >/dev/null || die "apt-get is missing; this installer is for Debian-family systems"
}

validate_source_dir() {
  local f
  for f in pyproject.toml uv.lock chaarteeke/main.py data/policy.json deploy/install.sh telephony/asterisk/etc/asterisk/ari.conf; do
    [[ -e $1/$f ]] || die "$1 does not look like the Chaar Teeke checkout (no $f)"
  done
}

check_source() {
  if [[ -n $GIT_URL ]]; then
    [[ -z $SOURCE_DIR ]] || die "give --source or --git, not both"
    return 0
  fi
  [[ -n $SOURCE_DIR ]] || SOURCE_DIR=$(dirname "$SCRIPT_DIR")
  SOURCE_DIR=$(cd "$SOURCE_DIR" && pwd) || die "--source folder not found"
  validate_source_dir "$SOURCE_DIR"
}

# ---------------------------------------------------------------------------------------------- helpers
# put_file SRC DEST MODE OWNER GROUP: copy through a temporary file in the same folder, then rename.
put_file() {
  local src=$1 dest=$2 mode=$3 owner=$4 group=$5 tmp
  tmp="$(dirname "$dest")/.$(basename "$dest").new.$$"
  cp -- "$src" "$tmp"
  chmod "$mode" "$tmp"
  chown "$owner:$group" "$tmp"
  mv -- "$tmp" "$dest"
}

# keep_copy DEST: a copy of an existing file, under BACKUP_ROOT/<time>/<its full path>. Never next to it:
# /etc/logrotate.d and friends read every file in their folder.
keep_copy() {
  local dest=$1 to
  to="$BACKUP_ROOT/$STAMP$dest"
  mkdir -p "$BACKUP_ROOT" "$(dirname "$to")"
  chmod 0700 "$BACKUP_ROOT"
  cp -p -- "$dest" "$to"
  printf '%s\n' "$to"
}

# install_managed SRC DEST MODE OWNER GROUP: put SRC at DEST unless it is already identical; keep a copy of
# a different file first. Returns 0 when it changed something, 1 when nothing changed.
install_managed() {
  local src=$1 dest=$2 mode=$3 owner=$4 group=$5 old
  if [[ -f $dest ]] && cmp -s -- "$src" "$dest"; then
    # content is the same; make sure mode and owner are too
    chmod "$mode" "$dest"
    chown "$owner:$group" "$dest"
    say "unchanged   $dest"
    return 1
  fi
  if [[ -e $dest ]]; then
    old=$(keep_copy "$dest")
    say "replaced    $dest (old copy: $old)"
  else
    say "installed   $dest"
  fi
  mkdir -p "$(dirname "$dest")"
  put_file "$src" "$dest" "$mode" "$owner" "$group"
  return 0
}

# make_dir MODE OWNER GROUP PATH...: create folders and set mode and owner (existing content is left alone).
make_dir() {
  local mode=$1 owner=$2 group=$3 d
  shift 3
  for d in "$@"; do
    mkdir -p -- "$d"
    chmod "$mode" "$d"
    chown "$owner:$group" "$d"
  done
}

# render_template SRC: print SRC with every @@NAME@@ replaced by the variable TPL_NAME. A name with no
# variable is an error (a template that asks for something nobody gave must not produce a file).
render_template() {
  local src=$1 line name var
  local re='@@([A-Z_]+)@@'
  while IFS= read -r line || [[ -n $line ]]; do
    while [[ $line =~ $re ]]; do
      name=${BASH_REMATCH[1]}
      var=TPL_$name
      [[ -n ${!var+set} ]] || {
        echo "install: template $src needs @@$name@@ and nothing gave it" >&2
        return 1
      }
      line=${line//"@@$name@@"/"${!var}"}
    done
    printf '%s\n' "$line"
  done <"$src"
}

# get_env NAME [FILE]: the value of NAME in the env file, or nothing. Reads text; never executes it.
get_env() {
  local file=${2:-$ENV_FILE} line
  [[ -r $file ]] || return 0
  while IFS= read -r line; do
    [[ $line == "$1="* ]] && {
      printf '%s' "${line#"$1="}"
      return 0
    }
  done <"$file"
  return 0
}

app_python() { "$APP_DIR/.venv/bin/python" -I "$@"; }
gen_secret() { app_python -c 'import secrets; print(secrets.token_urlsafe(48))'; }
gen_short_secret() { app_python -c 'import secrets; print(secrets.token_urlsafe(24))'; }
gen_fernet() { app_python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'; }

# asterisk_sounds_dir: the folder Asterisk looks for sound:NAME in, asked of the running Asterisk (its data
# directory plus /sounds). Prints nothing and fails when Asterisk cannot be asked.
asterisk_sounds_dir() {
  local data
  data=$(asterisk -rx 'core show settings' 2>/dev/null | sed -n 's/^[[:space:]]*Data directory:[[:space:]]*//p' | head -n 1)
  [[ $data == /* ]] || return 1
  printf '%s/sounds\n' "${data%/}"
}

# as_service [--pty] COMMAND...: run COMMAND as the service user with the env file loaded, the way the
# unit would (same parser for the env file). --pty gives it the terminal.
as_service() {
  local mode=--pipe
  if [[ ${1:-} == --pty ]]; then
    mode=--pty
    shift
  fi
  systemd-run --quiet --wait --collect "$mode" \
    -p "User=$SERVICE_USER" -p "Group=$SERVICE_GROUP" \
    -p "EnvironmentFile=$ENV_FILE" -p "WorkingDirectory=$APP_DIR" \
    -p UMask=0077 -p Environment=PYTHONDONTWRITEBYTECODE=1 \
    "$@"
}

# ---------------------------------------------------------------------------------------------- steps
step_checks() {
  step "Checking this machine and the choices"
  check_platform
  settle_choices
  check_user_name
  check_source
  # shellcheck source=/dev/null
  say "system: $(. /etc/os-release && echo "${PRETTY_NAME:-unknown}") on $(uname -m)"
  say "public address: $PUBLIC_URL${DOMAIN:+ (Caddy will serve it)}"
  say "app files from: ${SOURCE_DIR:-$GIT_URL}"
  say "service user: $SERVICE_USER"
}

step_packages() {
  step "Installing system packages with apt"
  local pkgs=(ca-certificates curl rsync sqlite3 openssl logrotate)
  ((WITH_ASTERISK)) && pkgs+=(asterisk)
  [[ -n $DOMAIN ]] && pkgs+=(caddy)
  [[ -n $GIT_URL ]] && pkgs+=(git)
  export DEBIAN_FRONTEND=noninteractive
  # At a terminal dpkg prints hundreds of progress lines; keep them in a log and show it only when apt fails.
  # A fresh Ubuntu may be running unattended-upgrades, which holds the dpkg lock: wait for it (up to 5 minutes).
  local log
  log=$(mktemp)
  if ! { apt-get -o DPkg::Lock::Timeout=300 update -qq && apt-get -o DPkg::Lock::Timeout=300 install -y -qq --no-install-recommends "${pkgs[@]}"; } >"$log" 2>&1; then
    cat -- "$log" >&2
    rm -f -- "$log"
    die "apt-get failed (its output is above)"
  fi
  rm -f -- "$log"
  say "installed or already present: ${pkgs[*]}"
}

step_user() {
  step "Making the service user and its folders"
  if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    say "user $SERVICE_USER exists"
  else
    useradd --system --user-group --home-dir "$STATE_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    say "created system user $SERVICE_USER (no login, no password)"
  fi
  SERVICE_GROUP=$(id -gn "$SERVICE_USER")
  make_dir 0755 root root "$APP_DIR"
  make_dir 0750 root "$SERVICE_GROUP" "$ETC_DIR"
  make_dir 0700 "$SERVICE_USER" "$SERVICE_GROUP" "$STATE_DIR" "$STATE_DIR/var" "$STATE_DIR/var/backups" "$STATE_DIR/var/backups-encrypted"
  make_dir 0755 root root "$(dirname "$UV_CACHE")" "$UV_CACHE"
  say "data folder $STATE_DIR (mode 0700, $SERVICE_USER only)"
}

step_app_files() {
  step "Copying the app to $APP_DIR"
  local src=$SOURCE_DIR
  if [[ -n $GIT_URL ]]; then
    CLONE_DIR=$(mktemp -d)
    trap 'rm -rf -- "$CLONE_DIR"' EXIT
    local refargs=()
    [[ -n $GIT_REF ]] && refargs=(--branch "$GIT_REF")
    git clone --quiet --depth 1 ${refargs[@]+"${refargs[@]}"} -- "$GIT_URL" "$CLONE_DIR/src"
    src=$CLONE_DIR/src
    validate_source_dir "$src"
  fi
  # Anything excluded here is also left alone at the destination (no --delete-excluded): the environment,
  # the Python install, the uv binary and the prompt audio survive an update.
  rsync -a --delete ${RSYNC_OWN[@]+"${RSYNC_OWN[@]}"} --chmod=u=rwX,go=rX \
    --exclude=/.git/ --exclude=/.venv/ --exclude=/.python/ --exclude=/.uv/ --exclude=/var/ \
    --exclude=/node_modules/ --exclude=__pycache__/ --include=/.env.example --exclude='/.env*' --exclude='*.db' --exclude='*.db-wal' \
    --exclude='*.db-shm' --exclude=/.pytest_cache/ --exclude=/.ruff_cache/ --exclude=/DEPLOYED \
    --exclude=/data/prompts/audio/ \
    "$src"/ "$APP_DIR"/
  say "app code and data copied (the .git folder, .venv and var/ are not copied)"

  # Prompt audio: the admin page writes uploaded recordings here and Asterisk plays from here, so the
  # folder belongs to the service user. A normal run adds new shipped files and never overwrites one.
  make_dir 0755 "$SERVICE_USER" "$SERVICE_GROUP" "$APP_DIR/data/prompts" "$APP_DIR/data/prompts/audio"
  local ignore=(--ignore-existing)
  ((REFRESH_PROMPTS)) && ignore=()
  rsync -a ${ignore[@]+"${ignore[@]}"} --chmod=u=rwX,go=rX "$src/data/prompts/audio/" "$APP_DIR/data/prompts/audio/"
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/data/prompts/audio"
  if ((REFRESH_PROMPTS)); then
    say "prompt audio: every shipped file written, uploaded recordings with the same names replaced"
  else
    say "prompt audio: new shipped files added, existing files (possibly native recordings) left as they are"
  fi
  local rev=unknown
  if [[ -d $src/.git ]] && command -v git >/dev/null; then
    rev=$(git -C "$src" rev-parse --short HEAD 2>/dev/null || echo unknown)
  fi
  printf 'installed %s from %s, commit %s\n' "$STAMP" "$src" "$rev" >"$APP_DIR/DEPLOYED"
  chmod 0644 "$APP_DIR/DEPLOYED"
  chown root:root "$APP_DIR/DEPLOYED"
  say "recorded in $APP_DIR/DEPLOYED (commit $rev)"
}

step_python() {
  step "Installing uv, Python $PYTHON_VERSION and the app's libraries"
  local uv
  if [[ -n $UV_BIN_ARG ]]; then
    [[ -x $UV_BIN_ARG ]] || die "--uv-bin $UV_BIN_ARG is not an executable file"
    uv=$UV_BIN_ARG
    say "using $uv ($("$uv" --version))"
  else
    uv=$APP_DIR/.uv/uv
    if [[ -x $uv && $("$uv" --version) == "uv $UV_VERSION"* ]]; then
      say "uv $UV_VERSION already installed"
    else
      local tmp
      tmp=$(mktemp -d)
      curl -fsSL --proto '=https' --tlsv1.2 -o "$tmp/uv-install.sh" "$UV_INSTALLER_URL"
      say "downloaded $UV_INSTALLER_URL"
      UV_UNMANAGED_INSTALL="$APP_DIR/.uv" sh "$tmp/uv-install.sh" >/dev/null
      rm -rf -- "$tmp"
      [[ $("$uv" --version) == "uv $UV_VERSION"* ]] || die "the uv installed here says '$("$uv" --version)', not $UV_VERSION"
      say "installed uv $UV_VERSION in $APP_DIR/.uv"
    fi
  fi
  export UV_PYTHON_INSTALL_DIR=$APP_DIR/.python
  export UV_PROJECT_ENVIRONMENT=$APP_DIR/.venv
  export UV_CACHE_DIR=$UV_CACHE
  "$uv" --no-progress python install --no-bin "$PYTHON_VERSION"
  local extra=()
  ((WITH_ASR)) && extra=(--extra asr)
  # --frozen: install exactly what uv.lock says. The project is installed editable, on purpose: the app finds
  # data/ and web/ next to its own code, so it must run from $APP_DIR.
  (cd "$APP_DIR" && "$uv" --no-progress sync --frozen --no-dev --managed-python --python "$PYTHON_VERSION" ${extra[@]+"${extra[@]}"})
  local got
  got=$(app_python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  [[ $got == "$PYTHON_VERSION" ]] || die "the app's Python is $got, not $PYTHON_VERSION"
  app_python -m compileall -q "$APP_DIR/chaarteeke" || die "compiling the app's code failed"
  chmod -R go+rX "$APP_DIR/.venv" "$APP_DIR/.python" || die "could not make the Python install readable by the service user"
  say "Python $got and the libraries are in $APP_DIR/.venv${extra[*]:+ (with the speech libraries)}"
}

# Write /etc/chaarteeke/env once. An existing file is checked, never rewritten.
step_env() {
  step "Writing the settings and keys ($ENV_FILE)"
  { set +x; } 2>/dev/null
  if [[ -e $ENV_FILE ]]; then
    chmod 0600 "$ENV_FILE"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$ENV_FILE"
    say "exists: not rewritten, keys untouched (mode 0600, owner $SERVICE_USER)"
    local name missing=()
    for name in CT_MODE CT_TELEPHONY CT_ASR CT_DB CT_VAR_DIR CT_HOST CT_PORT CT_PUBLIC_URL CT_SECRET_KEY CT_PHONE_KEY \
      CT_BACKUP_DIR CT_BACKUP_KEY CT_ARI_URL CT_ARI_USER CT_ARI_PASSWORD CT_ARI_ENDPOINT; do
      [[ -n $(get_env "$name") ]] || missing+=("$name")
    done
    if ((${#missing[@]})); then
      not_done "$ENV_FILE has no value for: ${missing[*]}. Add them by hand (see deploy/env.template); the installer never edits this file"
    fi
    [[ $(get_env CT_MODE) == pilot ]] || not_done "CT_MODE in $ENV_FILE is not 'pilot'"
    local have
    have=$(get_env CT_PUBLIC_URL)
    [[ $have == "$PUBLIC_URL" ]] || warn "CT_PUBLIC_URL in $ENV_FILE is '$have' but this run was given '$PUBLIC_URL'; edit the file if the address changed"
    return 0
  fi

  local TPL_STATE_DIR=$STATE_DIR TPL_PUBLIC_URL=$PUBLIC_URL TPL_SECRET_KEY TPL_PHONE_KEY TPL_BACKUP_KEY TPL_ARI_PASSWORD TPL_ASR_LINES
  TPL_SECRET_KEY=$(gen_secret)
  TPL_PHONE_KEY=$(gen_fernet)
  TPL_BACKUP_KEY=$(gen_fernet)
  TPL_ARI_PASSWORD=$(gen_short_secret)
  if ((WITH_ASR)); then
    TPL_ASR_LINES="CT_ASR=indicconformer"
    if [[ -n $MODEL_DIR ]]; then
      TPL_ASR_LINES+=$'\n'"CT_ASR_MODEL_DIR=$MODEL_DIR"
    else
      TPL_ASR_LINES+=$'\n'"#CT_ASR_MODEL_DIR=/path/to/the/model/folder"
    fi
  else
    TPL_ASR_LINES="# The speech model is off, on purpose: every place a patient says goes to the review queue, which says so."$'\n'"CT_ASR=off"
  fi
  local old_umask tmp
  old_umask=$(umask)
  umask 077
  tmp="$ETC_DIR/.env.new.$$"
  if ! render_template "$SCRIPT_DIR/env.template" >"$tmp"; then
    rm -f -- "$tmp"
    umask "$old_umask"
    die "could not write the settings file"
  fi
  umask "$old_umask"
  chmod 0600 "$tmp"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$tmp"
  mv -- "$tmp" "$ENV_FILE"
  say "written with new random keys (mode 0600, owner $SERVICE_USER). The keys are not shown."
  say "KEEP A COPY of this file somewhere safe and off this box: without CT_PHONE_KEY the stored numbers cannot be read."
}

# The ARI user's password goes into ari.conf as a crypt hash. The env file is the source of truth. The file is
# rendered from the shipped one every run; if the installed hash already matches the env password it is kept
# (same salt), so a run changes the file only when the shipped file or the password changed.
render_ari_conf() {
  local src=$APP_DIR/telephony/asterisk/etc/asterisk/ari.conf dest=$ASTERISK_ETC/ari.conf
  local user pw stored salt hash tmp line n_format=0 n_pass=0 n_pretty=0
  user=$(get_env CT_ARI_USER)
  pw=$(get_env CT_ARI_PASSWORD)
  [[ -n $user && -n $pw ]] || {
    not_done "CT_ARI_USER and CT_ARI_PASSWORD are not both in $ENV_FILE, so ari.conf was not written"
    return 0
  }
  [[ $user == chaar ]] || {
    not_done "CT_ARI_USER is '$user' but the shipped ari.conf defines the user [chaar]; ari.conf was not written"
    return 0
  }
  # The shipped file must still hold the three lines we replace; if it does not, the installer is out of date.
  grep -q '^password_format=plain' "$src" && n_format=1
  grep -q '^password=CHANGE-ME-dev-only' "$src" && n_pass=1
  grep -q '^pretty=yes' "$src" && n_pretty=1
  if ((n_format + n_pass + n_pretty != 3)); then
    not_done "telephony/asterisk/etc/asterisk/ari.conf changed shape (expected password_format=plain, the dev password and pretty=yes lines); ari.conf was not written, fix the installer"
    return 0
  fi
  hash=""
  if [[ -f $dest ]]; then
    # shellcheck disable=SC2016  # the $ signs are literal: they are the crypt hash's own separators
    stored=$(sed -n 's/^password=\(\$6\$[^ ;]*\).*$/\1/p' "$dest" | head -n 1)
    if [[ -n $stored ]]; then
      IFS='$' read -r _ _ salt _ <<<"$stored"
      if [[ -n $salt ]] && [[ $(printf '%s' "$pw" | openssl passwd -6 -salt "$salt" -stdin) == "$stored" ]]; then
        hash=$stored
      fi
    fi
  fi
  [[ -n $hash ]] || hash=$(printf '%s' "$pw" | openssl passwd -6 -stdin)
  tmp="$ASTERISK_ETC/.ari.conf.new.$$"
  (
    umask 077
    while IFS= read -r line || [[ -n $line ]]; do
      case $line in
        password_format=plain) echo "password_format=crypt" ;;
        password=CHANGE-ME-dev-only) echo "password=$hash" ;;
        pretty=yes*) echo "pretty=no" ;;
        *) echo "$line" ;;
      esac
    done <"$src" >"$tmp"
  )
  if [[ -f $dest ]] && cmp -s -- "$tmp" "$dest"; then
    rm -f -- "$tmp"
    chmod 0640 "$dest"
    chown root:asterisk "$dest"
    say "unchanged   $dest (matches the shipped file and the env password)"
    return 0
  fi
  if [[ -e $dest ]]; then
    say "replaced    $dest (old copy: $(keep_copy "$dest"))"
  else
    say "installed   $dest (password stored as a hash)"
  fi
  chmod 0640 "$tmp"
  chown root:asterisk "$tmp"
  mv -- "$tmp" "$dest"
  ASTERISK_CHANGED=1
}

step_asterisk() {
  step "Installing the Asterisk configuration, udev rule and log rotation"
  if ((!WITH_ASTERISK)); then
    say "skipped: --no-asterisk"
    return 0
  fi
  local cfg=$APP_DIR/telephony/asterisk/etc f name
  for f in "$cfg"/asterisk/*.conf; do
    name=$(basename "$f")
    [[ $name == ari.conf ]] && continue
    if install_managed "$f" "$ASTERISK_ETC/$name" 0640 root asterisk; then ASTERISK_CHANGED=1; fi
  done
  render_ari_conf
  if grep -q '^\[softphone-auth\]' "$ASTERISK_ETC/pjsip.conf" 2>/dev/null; then
    warn "$ASTERISK_ETC/pjsip.conf still has the development 'softphone' endpoint with a known password (loopback only). Remove it from telephony/asterisk/etc/asterisk/pjsip.conf before the pilot, then run this again"
  fi
  install_managed "$cfg/udev/rules.d/99-sim7600.rules" "$UDEV_DIR/99-sim7600.rules" 0644 root root || true
  if command -v udevadm >/dev/null; then
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=tty --subsystem-match=usb || true
  fi
  install_managed "$cfg/logrotate.d/asterisk" "$LOGROTATE_DIR/asterisk" 0644 root root || true
  if command -v logrotate >/dev/null && ! logrotate --debug "$LOGROTATE_DIR/asterisk" >/dev/null 2>&1; then
    not_done "logrotate rejects $LOGROTATE_DIR/asterisk; run 'logrotate --debug $LOGROTATE_DIR/asterisk' and read it"
  fi

  # Prompt audio for Asterisk: sound:chaar/<lang>/<name> is looked up under Asterisk's sounds folder.
  local target=$APP_DIR/data/prompts/audio link=$SOUNDS_LINK sounds
  if [[ -z $link ]]; then
    # The package starts Asterisk when it is installed; make sure it is up, then ask it where its sounds are.
    systemctl start asterisk || true
    timeout 60 asterisk -rx 'core waitfullybooted' >/dev/null 2>&1 || true
    if sounds=$(asterisk_sounds_dir); then
      link=$sounds/chaar
    fi
  fi
  if [[ -z $link ]]; then
    not_done "could not ask Asterisk where its sounds folder is ('asterisk -rx \"core show settings\"' gave no data directory), so the link for the prompt audio was not made and no prompt will play. Link $target to <data directory>/sounds/chaar by hand"
  else
    local old=/var/lib/asterisk/sounds/chaar
    # An earlier version of this installer linked here on every system; on the Debian/Ubuntu package that is the wrong folder.
    if [[ $old != "$link" && -L $old && $(readlink "$old") == "$target" ]]; then
      rm -- "$old"
      say "removed     $old (the link of an earlier installer, in a folder Asterisk does not read here)"
    fi
    mkdir -p "$(dirname "$link")"
    if [[ -L $link ]]; then
      if [[ $(readlink "$link") == "$target" ]]; then
        say "unchanged   $link -> $target"
      else
        ln -sfn "$target" "$link"
        say "relinked    $link -> $target"
      fi
    elif [[ -e $link ]]; then
      not_done "$link exists and is not a link; move it away, then run the installer again (Asterisk cannot find the prompt audio)"
    else
      ln -s "$target" "$link"
      say "linked      $link -> $target"
    fi
  fi

  mkdir -p "$SYSTEMD_DIR/asterisk.service.d"
  # A changed drop-in (restart delay, address filter) only reaches a running Asterisk by restarting it.
  if install_managed "$SCRIPT_DIR/systemd/asterisk-dropin.conf" "$SYSTEMD_DIR/asterisk.service.d/chaarteeke.conf" 0644 root root; then ASTERISK_CHANGED=1; fi

  local module
  module=$(find /usr/lib /usr/local/lib -name chan_quectel.so 2>/dev/null | head -n 1)
  if [[ -n $module ]]; then
    say "chan_quectel found: $module"
  else
    not_done "chan_quectel.so is not installed. The SIM dongle cannot place or take calls until it is built; this installer does not build it (docs/DEPLOY.md, 'Asterisk and the dongle driver')"
  fi
}

step_units() {
  step "Installing the systemd units"
  local TPL_USER=$SERVICE_USER TPL_GROUP=$SERVICE_GROUP unit tmp
  tmp=$(mktemp -d)
  for unit in chaarteeke.service chaarteeke-backup.service; do
    render_template "$SCRIPT_DIR/systemd/$unit" >"$tmp/$unit"
    install_managed "$tmp/$unit" "$SYSTEMD_DIR/$unit" 0644 root root || true
  done
  install_managed "$SCRIPT_DIR/systemd/chaarteeke-backup.timer" "$SYSTEMD_DIR/chaarteeke-backup.timer" 0644 root root || true
  rm -rf -- "$tmp"
  chmod 0755 "$APP_DIR/deploy/backup.sh"
  systemctl daemon-reload
  if command -v systemd-analyze >/dev/null; then
    if ! systemd-analyze verify "$SYSTEMD_DIR/chaarteeke.service" "$SYSTEMD_DIR/chaarteeke-backup.service" "$SYSTEMD_DIR/chaarteeke-backup.timer"; then
      not_done "systemd-analyze verify reported problems with the Chaar Teeke units (see above)"
    fi
  else
    not_done "systemd-analyze is not available here, so the units were not verified"
  fi
}

step_caddy() {
  step "Configuring the HTTPS front door (Caddy)"
  if [[ -z $DOMAIN ]]; then
    say "skipped: no --domain. The app listens on 127.0.0.1:8080 only; put your own HTTPS proxy in front of it for $PUBLIC_URL"
    return 0
  fi
  local TPL_SITE=$DOMAIN TPL_TLS_LINE="" tmp
  ((TLS_INTERNAL)) && TPL_TLS_LINE="tls internal"
  tmp=$(mktemp)
  render_template "$SCRIPT_DIR/caddy/Caddyfile" >"$tmp"
  mkdir -p "$(dirname "$CADDYFILE")"
  if install_managed "$tmp" "$CADDYFILE" 0644 root root; then
    # validate provisions the certificate authority for "tls internal"; give it a throw-away data folder so it
    # does not leave a second authority and its key in root's home (found on Ubuntu 24.04).
    local scratch
    scratch=$(mktemp -d)
    if command -v caddy >/dev/null && ! HOME=$scratch XDG_DATA_HOME=$scratch XDG_CONFIG_HOME=$scratch caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
      rm -rf -- "$scratch"
      not_done "caddy validate rejects $CADDYFILE; run 'caddy validate --config $CADDYFILE --adapter caddyfile'. Caddy was not reloaded"
      rm -f -- "$tmp"
      return 0
    fi
    rm -rf -- "$scratch"
    systemctl enable caddy >/dev/null 2>&1 || true
    systemctl restart caddy
    say "caddy restarted with the new Caddyfile"
  else
    systemctl enable --now caddy >/dev/null 2>&1 || true
  fi
  rm -f -- "$tmp"
  if ((TLS_INTERNAL)); then
    say "tls internal: each browser must trust Caddy's root certificate (docs/DEPLOY.md)"
  else
    say "Caddy asks for a public certificate: $DOMAIN must resolve to this box and ports 80 and 443 must reach it. The installer changes no firewall."
  fi
}

step_admin() {
  step "The first administrator"
  local db=$STATE_DIR/var/chaarteeke.db count=""
  if [[ -e $db ]]; then
    count=$(as_service sqlite3 -cmd '.timeout 5000' "$db" "SELECT COUNT(*) FROM staff WHERE role='admin'" 2>/dev/null || true)
  fi
  if [[ $count =~ ^[1-9][0-9]*$ ]]; then
    say "an administrator already exists: nothing to do"
    return 0
  fi
  if [[ -t 0 && -t 1 ]]; then
    say "chaar-teeke create-admin asks for a username and a password. This script never sees them."
    if as_service --pty "$APP_DIR/.venv/bin/chaar-teeke" create-admin; then
      say "administrator created"
    else
      not_done "create-admin did not finish. Run it again: sudo systemd-run --pty --wait -p User=$SERVICE_USER -p EnvironmentFile=$ENV_FILE -p WorkingDirectory=$APP_DIR $APP_DIR/.venv/bin/chaar-teeke create-admin"
    fi
  else
    not_done "no terminal, so the first administrator was not made. Run this at a terminal: sudo systemd-run --pty --wait -p User=$SERVICE_USER -p EnvironmentFile=$ENV_FILE -p WorkingDirectory=$APP_DIR $APP_DIR/.venv/bin/chaar-teeke create-admin"
  fi
}

step_start() {
  step "Starting the services"
  systemctl daemon-reload
  if ((WITH_ASTERISK)); then
    systemctl enable asterisk >/dev/null 2>&1 || true
    if ((ASTERISK_CHANGED)) || ! systemctl is-active --quiet asterisk; then
      systemctl restart asterisk
      say "asterisk restarted (its configuration changed)"
    else
      say "asterisk left running (no configuration change)"
    fi
  fi
  systemctl enable chaarteeke.service chaarteeke-backup.timer >/dev/null 2>&1
  systemctl restart chaarteeke.service
  systemctl start chaarteeke-backup.timer
  say "chaarteeke.service restarted; chaarteeke-backup.timer started"
  local body=""
  for _ in $(seq 1 30); do
    body=$(curl -fsS --max-time 2 http://127.0.0.1:8080/api/mode 2>/dev/null || true)
    [[ $body == *'"mode":"pilot"'* ]] && break
    sleep 1
  done
  if [[ $body == *'"mode":"pilot"'* ]]; then
    say "the app answers on 127.0.0.1:8080 in pilot mode"
  else
    not_done "the app did not answer on 127.0.0.1:8080 within 30 s. Read: journalctl -u chaarteeke -n 50 --no-pager"
  fi
}

step_summary() {
  step "Summary"
  save_choices
  say "choices saved in $CHOICES_FILE (a later run needs no options)"
  if ((${#NOT_DONE[@]})); then
    printf '\n  Installed, but %d step(s) could not run:\n' "${#NOT_DONE[@]}"
    local item
    for item in "${NOT_DONE[@]}"; do
      printf '    - %s\n' "$item"
    done
  else
    printf '\n  Everything asked for is done.\n'
  fi
  cat <<EOF

  Next:
    sudo $APP_DIR/scripts/install_check.sh      checks the whole box (service, ARI, preflight, backups)
    $PUBLIC_URL                                  sign in as the administrator
    docs/DEPLOY.md                               update, back up, restore, rotate keys, what is untested
EOF
  ((${#NOT_DONE[@]} == 0))
}

main() {
  umask 022 # files the installer makes are readable; secrets and private folders set their own mode
  parse_args "$@"
  load_choices
  step_checks
  step_packages
  step_user
  step_app_files
  step_python
  step_env
  step_asterisk
  step_units
  step_caddy
  step_admin
  step_start
  step_summary || exit 3
}

# Sourced by deploy/tests/logic.sh: define the functions and stop.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
