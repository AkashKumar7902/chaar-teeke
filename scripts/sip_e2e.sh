#!/usr/bin/env bash
# The real exchange path with no phone hardware: SIP signalling, RTP audio, RFC 4733 key presses and a
# recording, through the app's own AriAdapter and CallRunner. A softphone (baresip) is the patient's phone.
#
#   scripts/sip_e2e.sh [--lang hi|doi|ur] [--clip FILE.wav] [--asr [--expect-ward N]]
#
# --asr (or CT_TEST_SIP_ASR=1) runs the app with the speech model; it needs CT_ASR_MODEL_DIR (no default) and
# about 2.2 GB of memory. Off by default. After it, scripts/sip_e2e_heard.py shows what the model heard.
#
# Needs: uv, ffmpeg, baresip (brew install baresip), and a private Asterisk 22 build with ARI and PJSIP at
# $ASTERISK_PREFIX (default ~/chaar-teeke-build/work/asterisk-prefix; see docs/TELEPHONY.md).
# Everything runs on this machine on ports 14444, 15060, 15070, 18080, 18088 and 31000+ (override with the
# CT_SIP_E2E_*_PORT variables). Nothing is installed, no outside service is called, no real phone is rung.
# It starts Asterisk, baresip and the app, and stops each of them by its own PID at the end. Exit code 0 only
# when every check passed. CT_SIP_E2E_KEEP=1 keeps the run folder (logs, database, clip) after a pass.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${ASTERISK_PREFIX:=$HOME/chaar-teeke-build/work/asterisk-prefix}"
export ASTERISK_PREFIX
base="${CT_SIP_E2E_DIR:-$PWD/var/sip-e2e}"
export CT_SIP_E2E_DIR="$base"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "sip_e2e: $1 is not installed${2:+ ($2)}" >&2
    exit 2
  }
}
need uv
need ffmpeg "brew install ffmpeg"
need baresip "brew install baresip"
[ -x "$ASTERISK_PREFIX/sbin/asterisk" ] || {
  echo "sip_e2e: no Asterisk at $ASTERISK_PREFIX/sbin/asterisk (set ASTERISK_PREFIX to a private Asterisk 22 build)" >&2
  exit 2
}

mkdir -p "$base"
marker="$(mktemp "$base/.started.XXXXXX")"

# If the driver is killed, stop what it started: the PIDs it wrote down, and only if the process still looks
# like what it was (a PID can be reused).
cleanup() {
  local pidfile pid name
  for pidfile in $(find "$base" -mindepth 2 -maxdepth 2 -name pids -newer "$marker" 2>/dev/null); do
    while read -r pid name; do
      [ -n "${pid:-}" ] || continue
      if kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o command= | grep -q -E "asterisk|baresip|chaarteeke.main"; then
        kill "$pid" 2>/dev/null || true
      fi
    done <"$pidfile"
  done
  rm -f "$marker"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

uv run --frozen python scripts/sip_e2e.py "$@"
