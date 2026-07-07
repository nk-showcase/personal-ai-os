#!/usr/bin/env bash
# THE ONLY documented way to run importers / store maintenance against the PRODUCTION
# aios.sqlite3. A bare shell run without AIOS_DATA_DB_GROUP + umask would clamp the
# shared DB to 0700/0600 and lock out the service users (data plane down, the run
# deadlocks on its own queue round-trips).
#
# Usage (as the service user on the VPS, from the repo root):
#   scripts/run_with_prod_db.sh -c 'from bot import aios_notes_store as NS; print(NS.count_notes())'
#   scripts/run_with_prod_db.sh scripts/some_maintenance.py
set -euo pipefail
cd "$(dirname "$0")/.."
umask 0007
# The service account's home directory. Override AIOS_HOME to match your deployment.
AIOS_HOME="${AIOS_HOME:-$HOME}"
export AIOS_DATA_DB="${AIOS_DATA_DB:-${AIOS_HOME}/.ai-os/data/aios.sqlite3}"
export AIOS_DATA_DB_GROUP="${AIOS_DATA_DB_GROUP:-aios-queue}"
export AIOS_AGE_BIN="${AIOS_AGE_BIN:-${AIOS_HOME}/.local/bin/age}"
export AIOS_CONTEXT_IDENTITY_FILE="${AIOS_CONTEXT_IDENTITY_FILE:-${AIOS_HOME}/.ai-os/keys/context-vps.identity}"
export AIOS_CONTEXT_KEK_FILE="${AIOS_CONTEXT_KEK_FILE:-${AIOS_HOME}/.ai-os/keys/kek.age}"
export AIOS_CONTEXT_ENCRYPTION="${AIOS_CONTEXT_ENCRYPTION:-1}"
# config.py requires these at import time; these are placeholders (NOT real secrets) —
# the worker does not need the bot token.
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-dummy:maintenance}"
export TELEGRAM_OWNER_ID="${TELEGRAM_OWNER_ID:-1}"
# After the reduce-seals ceremony the working key on the VPS is destroyed for good (by
# design): maintenance can no longer decrypt; crypto operations refuse fail-closed
# (missing-key). This is the expected state, not a fault.
if [ ! -f "$AIOS_CONTEXT_IDENTITY_FILE" ]; then
  echo "note: identity file absent — retired by the reduce-seals ceremony; encrypted-domain ops will refuse (fail-closed by design)" >&2
fi
export PYTHONPATH=.
exec .venv/bin/python "$@"
