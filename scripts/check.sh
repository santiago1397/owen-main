#!/usr/bin/env bash
# Pre-deploy gate. Everything here is fast, offline, and needs no database — so there is no
# excuse to skip it, which is the point: the checks that get skipped are the ones that never
# run at all.
#
# WHY THIS EXISTS. Three bugs shipped to production in the agent build, all of the same
# shape — code written, never once executed:
#   * api/calls.py referenced `call.id` in a function whose variable is `call_id`, so EVERY
#     call-detail request 500'd for five days (the block was copied from flows/runtime.py,
#     where `call` does exist).
#   * owen-voice `_dispatch_tools` used `args` before it was assigned, so custom HTTP tools
#     silently never ran.
#   * test_ai_api asserted on a source literal that a later, correct change had moved on from.
# `pyflakes` finds the first two in under a second. The test run finds the third.
set -uo pipefail
cd "$(dirname "$0")/.."
fail=0
step() { printf '\n==> %s\n' "$1"; }
bad()  { printf '    FAIL: %s\n' "$1"; fail=1; }

PY="${PY:-python}"
[ -x backend/.venv/Scripts/python.exe ] && PY="backend/.venv/Scripts/python.exe"
[ -x backend/.venv/bin/python ] && PY="backend/.venv/bin/python"

step "pyflakes (undefined names, unreachable imports)"
if "$PY" -m pyflakes --version >/dev/null 2>&1; then
  # Only the errors that are always bugs. Unused imports are style; an undefined name is a
  # 500 waiting for the first request.
  out=$("$PY" -m pyflakes backend/app owen-voice/app 2>&1 \
        | grep -E "undefined name|local variable .* referenced before assignment" || true)
  if [ -n "$out" ]; then echo "$out"; bad "undefined names"; else echo "    clean"; fi
else
  echo "    pyflakes not installed — pip install -r backend/requirements-dev.txt"
  bad "pyflakes missing (this gate is the cheapest one you have)"
fi

step "backend tests"
for t in backend/tests/test_*.py; do
  name=$(basename "$t" .py)
  if (cd backend && PYTHONIOENCODING=utf-8 "../$PY" -m "tests.$name" >/tmp/owen-check.$name 2>&1); then
    printf '    ok   %s\n' "$name"
  else
    printf '    FAIL %s\n' "$name"; tail -5 /tmp/owen-check.$name | sed 's/^/         /'; fail=1
  fi
done

step "owen-voice tests"
for t in owen-voice/tests/test_*.py; do
  name=$(basename "$t" .py)
  if (cd owen-voice && PYTHONIOENCODING=utf-8 "../$PY" -m "tests.$name" >/tmp/owen-check.v.$name 2>&1); then
    printf '    ok   %s\n' "$name"
  else
    printf '    FAIL %s\n' "$name"; tail -5 /tmp/owen-check.v.$name | sed 's/^/         /'; fail=1
  fi
done

step "frontend typecheck"
if [ -d frontend/node_modules ]; then
  if (cd frontend && npx --no-install tsc --noEmit -p tsconfig.json); then
    echo "    clean"
  else
    bad "tsc"
  fi
else
  echo "    node_modules absent — skipped (run npm ci in frontend/ to enable)"
fi

echo
if [ "$fail" -ne 0 ]; then
  echo "CHECKS FAILED — not deployable."
  exit 1
fi
echo "All checks passed."
