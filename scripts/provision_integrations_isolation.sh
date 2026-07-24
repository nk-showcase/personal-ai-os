#!/usr/bin/env bash
# provision_integrations_isolation.sh — create the ROOT-OWNED code tree that the keyed integrations
# worker runs from, so the coding-worker OS user cannot modify the code or Python interpreter that
# the keyed worker executes on its next restart. Without this, a compromised coding worker could
# swap in code and, after a restart, obtain the integration keys + store key.
#
# Run as root. Idempotent. Pairs with systemd/aios-integrations-worker.service (which runs from
# the path created here) and README §4. Contains no secrets; it only relocates + locks code.
set -Eeuo pipefail

SRC="${1:-$HOME/apps/app}"                  # source checkout (the coding worker's working tree)
DEST="/opt/aios-integrations/app"           # root-owned tree the integrations unit runs from
CODING_USER="${AIOS_CODING_USER:-ai-os}"    # coding-worker OS user — MUST NOT be able to write DEST
PYBIN="${AIOS_PYTHON:-/usr/bin/python3}"

[ "$(id -u)" = 0 ] || { echo "ERROR: run as root"; exit 1; }
[ -d "$SRC/bot" ] || { echo "ERROR: source tree $SRC/bot not found"; exit 1; }
command -v "$PYBIN" >/dev/null || { echo "ERROR: python ($PYBIN) not found"; exit 1; }
id "$CODING_USER" >/dev/null 2>&1 || { echo "ERROR: coding user '$CODING_USER' not found (set AIOS_CODING_USER)"; exit 1; }

install -d -o root -g root -m 0755 /opt/aios-integrations

# 1. Snapshot ONLY code into the root-owned tree. The destination is world-readable (go=rX), so we
#    deliberately EXCLUDE anything secret or deployment-local (.env, keys, identities, databases,
#    caches, backups, runtime state) — only source code + requirements should land here. Atomic swap.
#    NOTE: `pip install` below runs as root against this code's requirements.txt; run this only from
#    a trusted source checkout (it executes each package's install logic).
rsync -a --delete \
  --exclude '.venv/' --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.env' --exclude '.env.*' --exclude '*.key' --exclude '*.pem' --exclude '*.identity' \
  --exclude '*.age' --exclude '*.db' --exclude '*.sqlite3' --exclude '*.sqlite3-*' \
  --exclude '.ai-os/' --exclude '_backups/' --exclude 'secrets-cache/' --exclude '.credentials.json' \
  "$SRC/" "$DEST.new/"
[ -d "$DEST" ] && { rm -rf "$DEST.old"; mv "$DEST" "$DEST.old"; }
mv "$DEST.new" "$DEST"
rm -rf "$DEST.old"

# 2. Fresh venv built AT its final path (a venv bakes its absolute path into pip/scripts, so it
#    must be built in place, never built elsewhere and moved).
rm -rf "$DEST/.venv"
"$PYBIN" -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q --upgrade pip
[ -f "$DEST/requirements.txt" ] && "$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt"

# 3. Lock: root owns everything; group/other may read + traverse but NEVER write.
chown -R root:root "$DEST"
chmod -R u=rwX,go=rX "$DEST"

# 4. Verify (required) the coding worker cannot write the executed code OR the interpreter.
if sudo -u "$CODING_USER" sh -c "printf x >> '$DEST/bot/integrations_worker.py'" 2>/dev/null; then
  echo "FAIL: $CODING_USER can still write the code in $DEST — check ownership/permissions"; exit 2
fi
if sudo -u "$CODING_USER" sh -c "touch '$DEST/.venv/bin/_probe'" 2>/dev/null; then
  echo "FAIL: $CODING_USER can still write the venv in $DEST"; exit 2
fi
echo "verified: $CODING_USER cannot write the code or venv in $DEST"
echo "Provisioned root-owned integrations code tree at $DEST"
echo "Point systemd/aios-integrations-worker.service at $DEST (it already does) and restart it."
