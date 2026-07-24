#!/usr/bin/env bash
# install_systemd_units.sh -- render the unit TEMPLATES in systemd/ and install them.
#
# The units under systemd/ use ${AIOS_HOME} / ${AIOS_BOT_HOME} / ${AIOS_INTEGRATIONS_HOME}
# placeholders. systemd does NOT perform shell-style ${VAR} expansion in Environment=,
# WorkingDirectory=, or EnvironmentFile= directives, so the templates must be rendered to
# concrete paths at install time -- that is what this script does (via envsubst). Run as root.
# Idempotent. Contains no secrets (paths only).
set -Eeuo pipefail
[ "$(id -u)" = 0 ] || { echo "ERROR: run as root"; exit 1; }
command -v envsubst >/dev/null || { echo "ERROR: envsubst (gettext) is required"; exit 1; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$REPO/systemd"
DEST=/etc/systemd/system

# Take the three home paths from the ENVIRONMENT only. Do NOT source a repo-controlled .env as
# root -- if the checkout were writable by the coding worker, that would be arbitrary root code
# execution. All three MUST be set and DISTINCT: sharing the coding worker home would collapse the
# three-user secret boundary the units depend on.
: "${AIOS_HOME:?set AIOS_HOME=<coding-worker home> in the environment before running (as root)}"
: "${AIOS_BOT_HOME:?set AIOS_BOT_HOME=<transport-user home>}"
: "${AIOS_INTEGRATIONS_HOME:?set AIOS_INTEGRATIONS_HOME=<integrations-user home>}"
if [ "$AIOS_HOME" = "$AIOS_BOT_HOME" ] || [ "$AIOS_HOME" = "$AIOS_INTEGRATIONS_HOME" ] \
   || [ "$AIOS_BOT_HOME" = "$AIOS_INTEGRATIONS_HOME" ]; then
  echo "ERROR: AIOS_HOME / AIOS_BOT_HOME / AIOS_INTEGRATIONS_HOME must be three DISTINCT paths"; exit 1
fi
export AIOS_HOME AIOS_BOT_HOME AIOS_INTEGRATIONS_HOME

VARS="\${AIOS_HOME} \${AIOS_BOT_HOME} \${AIOS_INTEGRATIONS_HOME}"
for u in "$UNIT_DIR"/aios-*.service; do
  name="$(basename "$u")"
  envsubst "$VARS" < "$u" > "$DEST/$name"
  # Fail loudly if any AIOS_ placeholder survived unresolved (a missing value would ship a literal).
  if grep -q "AIOS_[A-Z_]*}" "$DEST/$name"; then
    echo "ERROR: unresolved placeholder left in $DEST/$name -- set the missing variable"; exit 2
  fi
  echo "installed $DEST/$name"
done

systemctl daemon-reload
echo "Rendered + installed units. Enable them only at the migration step, e.g.:"
echo "  systemctl enable --now aios-telegram-bot.service"
