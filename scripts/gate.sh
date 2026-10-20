#!/usr/bin/env bash
# The gate: every check a change must pass. One line per step, exit 1 if any step fails.
#
#   scripts/gate.sh          lint, format, JS syntax and unit tests, shell syntax, personal contacts in old commits (a warning), pytest
#   scripts/gate.sh --ui     also node scripts/ui_check.mjs --quick (needs Chrome; fails if Chrome is missing)
set -u
cd "$(dirname "$0")/.."

ui=0
for arg in "$@"; do
  case "$arg" in
    --ui) ui=1 ;;
    -h | --help)
      sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "gate: unknown argument $arg" >&2
      exit 2
      ;;
  esac
done

log_dir=$(mktemp -d "${TMPDIR:-/tmp}/ct-gate.XXXXXX")
trap 'rm -rf "$log_dir"' EXIT
failed=0
steps=0
warned=()

# step NAME COMMAND...  runs the command, prints one line, shows the output of a failed step.
step() {
  local name=$1
  shift
  local log="$log_dir/$steps.log"
  local start=$SECONDS
  steps=$((steps + 1))
  if "$@" >"$log" 2>&1; then
    printf 'ok    %-26s %3ss\n' "$name" "$((SECONDS - start))"
  else
    failed=$((failed + 1))
    printf 'FAIL  %-26s %3ss\n' "$name" "$((SECONDS - start))"
    sed 's/^/      | /' "$log" | tail -n 40
  fi
}

# warn_step NAME COMMAND...  like step, but findings in the output are a WARNING that does not fail the gate.
# The command is expected to print "personal contact detail" when it has findings; any other failure
# (it crashed, git is missing) is a real failure. Used for the history scan: commits already pushed
# cannot be fixed by a change, and rewriting history is the owner's decision.
warn_step() {
  local name=$1
  shift
  local log="$log_dir/$steps.log"
  local start=$SECONDS
  steps=$((steps + 1))
  if "$@" >"$log" 2>&1; then
    printf 'ok    %-26s %3ss\n' "$name" "$((SECONDS - start))"
  elif grep -q 'personal contact detail' "$log"; then
    warned+=("$name")
    printf 'WARN  %-26s %3ss\n' "$name" "$((SECONDS - start))"
    sed 's/^/      | /' "$log"
    echo "      | Warning only: the gate does not fail on this. The README commit 10ec4f2 is already on origin/main,"
    echo "      | and the rest are in local commits. Rewriting history is the owner's decision; until then the"
    echo "      | working tree (tests/test_repo_rules.py) still refuses any new contact detail."
  else
    failed=$((failed + 1))
    printf 'FAIL  %-26s %3ss\n' "$name" "$((SECONDS - start))"
    sed 's/^/      | /' "$log" | tail -n 40
  fi
}

# web/vendor is third-party code (checked by checksum in the tests), not ours to syntax-check
js_files() { find web scripts \( -name node_modules -o -path web/vendor \) -prune -o \( -name '*.js' -o -name '*.mjs' \) -type f -print | sort; }
js_tests() { find web \( -name node_modules -o -path web/vendor \) -prune -o -name '*.test.mjs' -type f -print | sort; }
sh_files() { find scripts -name node_modules -prune -o -name '*.sh' -type f -print | sort; }

check_js_syntax() {
  local f rc=0
  while IFS= read -r f; do
    node --check "$f" || { echo "syntax error in $f"; rc=1; }
  done < <(js_files)
  return "$rc"
}

check_sh_syntax() {
  local f rc=0
  while IFS= read -r f; do
    bash -n "$f" || { echo "syntax error in $f"; rc=1; }
  done < <(sh_files)
  return "$rc"
}

run_js_tests() {
  local files
  files=$(js_tests)
  if [ -z "$files" ]; then
    echo "no web/**/*.test.mjs files found"
    return 1
  fi
  # shellcheck disable=SC2086
  node --test $files
}

find_chrome() {
  local c
  for c in "${CHROME_PATH:-}" "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    /usr/bin/google-chrome /usr/bin/google-chrome-stable /usr/bin/chromium /usr/bin/chromium-browser; do
    if [ -n "$c" ] && [ -x "$c" ]; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

run_ui() {
  if ! find_chrome >/dev/null; then
    echo "--ui asked for, but Chrome was not found (set CHROME_PATH)"
    return 1
  fi
  node scripts/ui_check.mjs --quick
}

step "ruff check" uv run --frozen ruff check .
step "ruff format --check" uv run --frozen ruff format --check .
step "node --check (web, scripts)" check_js_syntax
step "node --test (web)" run_js_tests
step "bash -n (scripts)" check_sh_syntax
warn_step "history (no contacts)" uv run --frozen python scripts/forbidden.py history
step "pytest" uv run --frozen pytest -q
if [ "$ui" -eq 1 ]; then
  step "ui_check --quick" run_ui
else
  printf 'skip  %-26s (pass --ui to run it)\n' "ui_check --quick"
fi

if [ "$failed" -gt 0 ]; then
  echo "gate: $failed of $steps steps failed"
  exit 1
fi
if [ "${#warned[@]}" -gt 0 ]; then
  echo "gate: all $steps steps passed, ${#warned[@]} with a warning: ${warned[*]}"
else
  echo "gate: all $steps steps passed"
fi
