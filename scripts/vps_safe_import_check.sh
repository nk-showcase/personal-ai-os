#!/usr/bin/env bash
# vps_safe_import_check.sh — import/compile the code with DUMMY env + test the secrets layer.
# SAFE: dummy values only, does NOT start the bot, does not read/print real secrets.
# Run on the server:  bash scripts/vps_safe_import_check.sh
set -u
REPO="${REPO:-${AIOS_HOME}/apps/ChatBot}"
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || { echo "FAIL: venv python not found ($PY)"; exit 1; }
cd "$REPO" || { echo "FAIL: no $REPO"; exit 2; }

# Dummy env — NOT real secrets (placeholder token format 000:dummy).
export TELEGRAM_BOT_TOKEN="000:dummy"
export TELEGRAM_OWNER_ID="1"

out=$(PYTHONPATH=. "$PY" -c "import bot.config; import bot.main; import bot.task_queue; import bot.claude_worker_core; import bot.claude_worker_runner; import bot.bridge_queue; import bot.claude_policy; import bot.telegram_bot; import bot.claude_worker; import bot.integrations_worker; import bot.aios_storage; print('IMPORT_OK')" 2>&1)
if ! echo "$out" | grep -q "IMPORT_OK"; then
  echo "FAIL: importing bot.config / bot.main failed:"; echo "$out" | tail -8; exit 1
fi
echo "PASS: bot.config + bot.main import with dummy env"

if PYTHONPATH=. "$PY" -m py_compile bot/*.py 2>/tmp/aios_pyc.err; then
  echo "PASS: py_compile bot/*.py with no syntax errors"; rm -f /tmp/aios_pyc.err
else
  echo "FAIL: py_compile found errors:"; tail -5 /tmp/aios_pyc.err; rm -f /tmp/aios_pyc.err; exit 1
fi

if [ -f scripts/test_secrets_loader.py ]; then
  echo "--- test_secrets_loader.py ---"
  if PYTHONPATH=. "$PY" scripts/test_secrets_loader.py; then echo "PASS: secrets-layer test"; else echo "FAIL: secrets-layer test"; exit 1; fi
fi

echo "RESULT: ALL PASS"; exit 0
