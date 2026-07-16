#!/usr/bin/env bash
# vps_status_check.sh — read-only VPS state for the AI OS.
# SAFE: runs nothing, neither reads nor prints secrets, does not start the bot.
# Run on the VPS:  bash scripts/vps_status_check.sh
set -u
REPO="${REPO:-$HOME/apps/app}"
fail=0
ok(){ echo "PASS: $1"; }
bad(){ echo "FAIL: $1"; fail=1; }

echo "== identity =="
echo "user=$(whoami) host=$(hostname)"

echo "== repo =="
if [ -d "$REPO/.git" ]; then
  cd "$REPO" || exit 2
  echo "branch=$(git branch --show-current)"
  echo "--- git status --short ---"; git status --short
  ok "repo found: $REPO"
else
  bad "repo not found: $REPO"
fi

echo "== ~/.claude layout =="
S="$HOME/.claude/settings.json"
if [ -L "$S" ]; then bad "settings.json is a SYMLINK (should be a real local file)"
elif [ -f "$S" ]; then ok "settings.json is a real local file (perms $(stat -c '%a' "$S"))"
else bad "settings.json missing"; fi
for d in skills hooks; do
  p="$HOME/.claude/$d"
  if [ -L "$p" ]; then ok "$d is a symlink -> $(readlink "$p")"; else bad "$d is not a symlink (expected a symlink into repo/claude/$d)"; fi
done

echo "== venv =="
if [ -x "$REPO/.venv/bin/python" ]; then ok "venv: $("$REPO/.venv/bin/python" --version 2>&1)"; else bad "venv not found"; fi

echo "== ~/.ai-os perms (expected 700) =="
if [ -d "$HOME/.ai-os" ]; then
  for d in "$HOME/.ai-os" "$HOME/.ai-os"/*/; do
    [ -d "$d" ] || continue
    perm=$(stat -c '%a' "$d")
    [ "$perm" = "700" ] && ok "$d = $perm" || bad "$d = $perm (not 700)"
  done
else bad "~/.ai-os missing"; fi

echo "== .env in repo (must not exist) =="
envf=$(find "$REPO" -maxdepth 2 -name '.env' -not -path '*/.git/*' 2>/dev/null)
[ -z "$envf" ] && ok "no .env in repo" || bad ".env found in repo: $envf"

echo ""
[ "$fail" = "0" ] && { echo "RESULT: ALL PASS"; exit 0; } || { echo "RESULT: SOME FAIL"; exit 1; }
