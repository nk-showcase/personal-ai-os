#!/usr/bin/env bash
# vps_bitwarden_inventory_check.sh - inventory of bot configuration BY NAME (present/missing).
#
# SECURITY (the point):
#  - NEVER prints secret/config values and NEVER prints BWS_ACCESS_TOKEN.
#  - The default and --env-check modes do NOT read values (names / key presence only).
#  - The --live mode does "parse-and-discard": it takes the output of `bws secret list`, keeps
#    ONLY the key names (.key) and immediately discards the values; prints present/missing.
#    NOTE: bws 2.1.0 has NO "names only" mode - `bws secret list` always returns
#    values in the payload. In --live the values pass through process memory on the host, but
#    are NOT displayed / logged / written to disk. Run deliberately only.
#
# Modes:
#   bash scripts/vps_bitwarden_inventory_check.sh             # SAFE: expected secret names + required non-secrets (no Bitwarden)
#   bash scripts/vps_bitwarden_inventory_check.sh --env-check  # SAFE: whether required non-secrets exist in env files (by name, no values)
#   bash scripts/vps_bitwarden_inventory_check.sh --live       # present/missing of secrets via bws (values NOT printed)
#
# Does NOT start the bot, touch systemd, or change permissions/files.
set -u

REPO="${REPO:-$HOME/apps/ai-os}"
MAP="$REPO/config/secrets-map.yaml"
ENV_DIR="${AIOS_ENV_DIR:-$HOME/.ai-os/env}"
BWS="${BWS_BIN:-$HOME/.local/bin/bws}"

MODE="safe"
case "${1:-}" in
  --live) MODE="live" ;;
  --env-check) MODE="env-check" ;;
  "" ) MODE="safe" ;;
  *) echo "FAIL: unknown argument '$1' (expected --live | --env-check | no argument)"; exit 2 ;;
esac

# service -> project (as in docs/security/bitwarden-setup-checklist.md)
declare -A PROJECT=(
  [telegram-bot]=chatbot-telegram
  [claude-worker]=chatbot-claude-worker
  [integrations-worker]=chatbot-integrations
)
SERVICES="telegram-bot claude-worker integrations-worker"

[ -f "$MAP" ] || { echo "FAIL: missing map $MAP"; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 is required to parse secrets-map.yaml"; exit 2; }

# Expected secret names for a service from secrets-map.yaml -> services -> <svc> -> allowed_secrets (NAMES ONLY).
expected_names() {  # $1 = service
  python3 - "$MAP" "$1" <<'PY'
import sys
path, svc = sys.argv[1], sys.argv[2]
names, in_services, cur, in_allowed = [], False, None, False
for raw in open(path, encoding="utf-8"):
    line = raw.rstrip("\n")
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    indent = len(line) - len(line.lstrip(" "))
    key = line.strip().split("#", 1)[0].rstrip()  # strip inline comment before structure checks
    if not key:
        continue
    if indent == 0:
        in_services = (key == "services:")
        cur, in_allowed = None, False
        continue
    if not in_services:
        continue
    if indent == 2 and key.endswith(":"):
        cur, in_allowed = key[:-1], False
        continue
    if cur == svc and indent == 4 and key == "allowed_secrets:":
        in_allowed = True
        continue
    if cur == svc and in_allowed and key.startswith("- "):
        names.append(key[2:].strip())
for n in names:
    print(n)
PY
}

# Required NON-secret names from secrets-map.yaml -> non_secret_env -> required (NAMES ONLY).
required_non_secret_names() {
  python3 - "$MAP" <<'PY'
import sys
path = sys.argv[1]
names, in_nse, in_req = [], False, False
for raw in open(path, encoding="utf-8"):
    line = raw.rstrip("\n")
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    indent = len(line) - len(line.lstrip(" "))
    key = line.strip().split("#", 1)[0].rstrip()  # strip inline comment before structure checks
    if not key:
        continue
    if indent == 0:
        in_nse = (key == "non_secret_env:")
        in_req = False
        continue
    if not in_nse:
        continue
    if indent == 2 and key.endswith(":"):
        in_req = (key == "required:")
        continue
    if in_req and indent >= 4 and key.startswith("- "):
        names.append(key[2:].strip())
for n in names:
    print(n)
PY
}

echo "=== bot config inventory (NAMES ONLY) ==="
echo "mode: $MODE"
echo

warn=0

if [ "$MODE" = "env-check" ]; then
  echo "--- required non-secret settings: presence in env files (by name, no values) ---"
  mapfile -t req < <(required_non_secret_names)
  if [ "${#req[@]}" -eq 0 ]; then echo "  (no non_secret_env.required in secrets-map.yaml)"; fi
  for name in "${req[@]}"; do
    found_in=""
    for ef in "$ENV_DIR"/*.env; do
      [ -f "$ef" ] || continue
      if grep -Eq "^[[:space:]]*${name}=" "$ef"; then found_in="$found_in $(basename "$ef")"; fi
    done
    if [ -n "$found_in" ]; then echo "  PRESENT: $name (in:$found_in)"; else echo "  MISSING: $name"; warn=1; fi
  done
  echo
  [ "$warn" = "0" ] && echo "RESULT: PASS (all required non-secrets are set)" \
                    || echo "RESULT: WARN (required non-secrets are unset - see MISSING; add them to the service env before starting the bot, not via this script)"
  exit 0
fi

# safe / live: per service
for svc in $SERVICES; do
  proj="${PROJECT[$svc]}"
  echo "----- $svc -> $proj -----"
  mapfile -t exp < <(expected_names "$svc")
  if [ "${#exp[@]}" -eq 0 ]; then
    echo "  (no allowed_secrets for $svc in secrets-map.yaml)"; echo; continue
  fi

  if [ "$MODE" = "safe" ]; then
    echo "  expected secrets (${#exp[@]}):"
    for n in "${exp[@]}"; do echo "    - $n"; done
    echo; continue
  fi

  # --- live: parse-and-discard (values dropped inside python, only names surface) ---
  f="$ENV_DIR/$svc.env"
  [ -f "$f" ] || { echo "  FAIL: missing env file $f"; warn=1; echo; continue; }
  [ -x "$BWS" ] || { echo "  FAIL: bws not found ($BWS)"; warn=1; echo; continue; }

  present_raw="$(
    ( set -a; . "$f" >/dev/null 2>&1; set +a; "$BWS" secret list -o json 2>/dev/null ) \
    | python3 -c "import sys,json
try:
    d=json.load(sys.stdin)
except Exception:
    sys.exit(3)
for it in d:
    k=it.get('key')
    if k: print(k)"
  )"
  rc=$?
  if [ "$rc" != "0" ]; then
    echo "  WARN: list not obtained (rc=$rc) - token / Bitwarden connectivity? No values were printed."; warn=1; echo; continue
  fi

  for n in "${exp[@]}"; do
    if printf '%s\n' "$present_raw" | grep -qxF "$n"; then echo "  PRESENT: $n"; else echo "  MISSING: $n"; warn=1; fi
  done
  while IFS= read -r k; do
    [ -z "$k" ] && continue
    printf '%s\n' "${exp[@]}" | grep -qxF "$k" || { echo "  EXTRA (not in secrets-map): $k"; warn=1; }
  done <<< "$present_raw"
  echo
done

# safe mode: additionally show the required non-secret settings
if [ "$MODE" = "safe" ]; then
  echo "----- required non-secret settings (non_secret_env.required) -----"
  mapfile -t req < <(required_non_secret_names)
  if [ "${#req[@]}" -eq 0 ]; then
    echo "  (none)"
  else
    for n in "${req[@]}"; do echo "    - $n   (ordinary service env variable, NOT Bitwarden)"; done
  fi
  echo
  echo "RESULT: SAFE LISTING ONLY (non-secret presence in env -> --env-check; present/missing of secrets -> --live, deliberately)"
  exit 0
fi

# live summary
[ "$warn" = "0" ] && echo "RESULT: PASS (all expected names present, no extras)" \
                  || echo "RESULT: WARN (see MISSING/EXTRA above - no values were printed)"
exit 0
