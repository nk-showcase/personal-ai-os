#!/usr/bin/env bash
# vps_pre_secret_sanity_check.sh — checks to run BEFORE adding any secrets on the VPS.
# SAFE: does not read secrets, does not print values. Only verifies environment readiness.
# Run on the VPS:  bash scripts/vps_pre_secret_sanity_check.sh
set -u
REPO="${REPO:-$HOME/apps/app}"
fail=0
ok(){ echo "PASS: $1"; }
bad(){ echo "FAIL: $1"; fail=1; }
cd "$REPO" 2>/dev/null || { echo "FAIL: no $REPO"; exit 2; }

# 1. settings.json — a real local file, not a symlink, mode 600
S="$HOME/.claude/settings.json"
if [ -L "$S" ]; then bad "settings.json is a symlink (must be a real local file)"
elif [ -f "$S" ]; then
  perm=$(stat -c '%a' "$S")
  [ "$perm" = "600" ] && ok "settings.json real file, mode 600" || bad "settings.json mode $perm (need 600)"
else bad "settings.json missing"; fi

# 2. ~/.ai-os/secrets-cache exists and is 700
SC="$HOME/.ai-os/secrets-cache"
if [ -d "$SC" ]; then
  perm=$(stat -c '%a' "$SC")
  [ "$perm" = "700" ] && ok "secrets-cache exists, mode 700" || bad "secrets-cache mode $perm (need 700)"
else bad "secrets-cache missing ($SC)"; fi

# 3. no .env with real values in the repo
envf=$(find "$REPO" -maxdepth 2 -name '.env' -not -path '*/.git/*' 2>/dev/null)
[ -z "$envf" ] && ok "no .env in the repo" || bad ".env found: $envf"

# 4. .gitignore covers secret patterns
miss=""
for pat in '.env' '*.key' '*.pem' '*credentials*' 'secrets-cache/'; do
  grep -qF "$pat" .gitignore 2>/dev/null || miss="$miss $pat"
done
[ -z "$miss" ] && ok ".gitignore covers secret patterns" || bad ".gitignore does not contain:$miss"

# 5. no keys / credential files tracked in git
tracked=$(git ls-files | grep -iE '\.(pem|key)$|credentials|id_ed25519|(^|/)\.env$')
[ -z "$tracked" ] && ok "no keys/credential files in git" || bad "found in git: $tracked"

# 6. secrets layer present and the cache directory is outside the repository
if [ -f bot/secrets_loader.py ]; then ok "bot/secrets_loader.py present"; else bad "bot/secrets_loader.py missing"; fi
case "$SC" in
  "$REPO"/*) bad "secrets-cache inside the repository ($SC) — must be outside" ;;
  *) ok "secrets-cache outside the repository ($SC)" ;;
esac

echo ""
[ "$fail" = "0" ] && { echo "RESULT: SAFE TO PROCEED (ALL PASS)"; exit 0; } || { echo "RESULT: NOT SAFE — fix the FAILs above"; exit 1; }
