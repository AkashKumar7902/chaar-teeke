#!/usr/bin/env bash
# The check before a demo. It prints the database it checks, then whether var/ can be written, then runs
# chaar-teeke preflight (database, prompt audio, telephony, scheduler lock, one end-to-end simulated call).
# It loads no speech model unless asked.
#
#   scripts/smoke.sh                  preflight, no server needed
#   scripts/smoke.sh --with-asr       also load the speech model once and transcribe a bundled clip (about 2.4 GB of memory)
#   scripts/smoke.sh --url [URL]      also check a running server over HTTP: page, visitor, demo label, coverage, ward
#                                     map data, map library, one stream frame, demo clock (URL: http://127.0.0.1:$CT_PORT)
set -u
cd "$(dirname "$0")/.."

extra=()
url_stage=0
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    --with-asr) extra+=(--with-asr) ;;
    --url)
      url_stage=1
      case "${2:-}" in
        http://* | https://*)
          url=$2
          shift
          ;;
      esac
      ;;
    -h | --help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "smoke: unknown argument $1" >&2
      exit 2
      ;;
  esac
  shift
done

failed=0
uv run --frozen python scripts/smoke_checks.py env || failed=1
uv run --frozen chaar-teeke preflight "${extra[@]+"${extra[@]}"}" || failed=1
if [ "$url_stage" -eq 1 ]; then
  uv run --frozen python scripts/smoke_checks.py url ${url:+"$url"} || failed=1
fi
if [ "$failed" -ne 0 ]; then
  echo "smoke: a check found a problem" >&2
  exit 1
fi
echo "smoke: all checks passed"
