#!/bin/sh
# check_setup.sh - preflight for the reference architecture.
# Verifies the toolchain, dependencies, required env vars, and git repo.
# Prints PASS/FAIL per check. NEVER prints a secret VALUE - only variable NAMES.
# POSIX sh; no bashisms.

# Resolve repo root as the parent of this script's directory.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

fail_count=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; fail_count=$((fail_count + 1)); }

# --- 1. python3.12 present ---------------------------------------------------
PY=""
for cand in python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)
        if [ "$ver" = "3.12" ]; then
            PY="$cand"
            break
        fi
    fi
done
if [ -n "$PY" ]; then
    pass "python3.12 available ($PY)"
else
    fail "python3.12 not found on PATH"
fi

# --- 2. venv dependencies importable -----------------------------------------
# Prefer a project venv interpreter if present, else the python found above.
VENV_PY="$REPO_ROOT/.venv/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$PY"

if [ -n "$VENV_PY" ]; then
    for mod in telegram dotenv httpx anthropic cryptography; do
        if "$VENV_PY" -c "import $mod" >/dev/null 2>&1; then
            pass "dependency importable: $mod"
        else
            fail "dependency NOT importable: $mod"
        fi
    done
else
    fail "no python interpreter to check dependencies"
fi

# --- 3. required env vars set (names only; values never printed) -------------
# Only names are read; the value is checked for non-emptiness and never echoed.
REQUIRED_VARS="TELEGRAM_BOT_TOKEN TELEGRAM_OWNER_ID ANTHROPIC_API_KEY"
for name in $REQUIRED_VARS; do
    # Indirect expansion in POSIX sh via eval; value is only tested, never printed.
    eval "val=\${$name:-}"
    if [ -n "$val" ]; then
        pass "env var set: $name"
    else
        fail "env var missing: $name"
    fi
    unset val
done

# --- 4. git repo present -----------------------------------------------------
if command -v git >/dev/null 2>&1 && \
   git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    pass "git repository present"
else
    fail "not a git repository (or git missing)"
fi

# --- summary -----------------------------------------------------------------
echo "----"
if [ "$fail_count" -eq 0 ]; then
    echo "ALL CHECKS PASSED"
    exit 0
fi
echo "$fail_count CHECK(S) FAILED"
exit 1
