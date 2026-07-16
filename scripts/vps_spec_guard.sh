#!/usr/bin/env bash
# vps_spec_guard.sh — AI OS spec guard: the go-forward target is ONLY the V2 design, no temporary
# runtime shortcuts.
# SAFE: only reads repository files. Runs nothing, touches no secrets/tokens.
#
# Fails (exit 1) if:
#  1) a systemd unit runs the single monolithic process bot.main as a service;
#  2) a systemd unit targets a nonexistent module bot.X and is NOT marked TARGET / not-runnable;
#  3) any wiring file (systemd / Procfile / Dockerfile / entrypoint / config) contains single-process-runtime-v1;
#  4) Procfile / Dockerfile contain bot.main without a LEGACY marker (the monolith must not look go-forward);
#  5) docs ENDORSE running the single monolithic process (phrases like "monolith runtime" etc.).
#
# Run:  REPO=/path/to/repo bash scripts/vps_spec_guard.sh   (default REPO=$HOME/apps/app)
set -u
REPO="${REPO:-$HOME/apps/app}"
cd "$REPO" 2>/dev/null || { echo "FAIL: no $REPO"; exit 2; }
fail=0
bad(){ echo "FAIL: $1"; fail=1; }
ok(){ echo "PASS: $1"; }

shopt -s nullglob

# --- 1 + 2: systemd units (monolith-as-service / nonexistent module without a marker) ---
svc_found=0
for sf in systemd/*.service; do
  svc_found=1
  mod="$(grep -E '^ExecStart=' "$sf" | grep -oE '\-m[[:space:]]+bot\.[A-Za-z0-9_]+' | grep -oE 'bot\.[A-Za-z0-9_]+' | head -1)"
  base="$(basename "$sf")"
  if [ -z "$mod" ]; then ok "$base: ExecStart without '-m bot.<module>' (skip)"; continue; fi
  if [ "$mod" = "bot.main" ]; then bad "$base: runs the single monolithic process bot.main as a service (forbidden — V2 only)"; continue; fi
  modpath="bot/${mod#bot.}.py"
  if [ -f "$modpath" ]; then
    ok "$base: module $mod implemented ($modpath)"
  elif grep -qiE 'TARGET|pending|not runnable|not-runnable|not implemented' "$sf"; then
    ok "$base: module $mod not implemented, but marked TARGET / not-runnable"
  else
    bad "$base: targets nonexistent $mod WITHOUT a TARGET / not-runnable marker"
  fi
done
[ "$svc_found" = "1" ] || echo "NOTE: no systemd/*.service found"

# --- 3: single-process-runtime-v1 in wiring files ---
wiring=(systemd/*.service Procfile Dockerfile entrypoint.sh)
[ -d config ] && wiring+=(config/*)
hit=0
for f in "${wiring[@]}"; do
  [ -f "$f" ] || continue
  grep -qF 'single-process-runtime-v1' "$f" && { echo "  -> $f"; hit=1; }
done
[ "$hit" = "0" ] && ok "no single-process-runtime-v1 in wiring files" || bad "single-process-runtime-v1 present in wiring (forbidden)"

# --- 4: Procfile / Dockerfile containing bot.main must carry a LEGACY marker ---
for f in Procfile Dockerfile; do
  [ -f "$f" ] || continue
  if grep -qE 'bot\.main' "$f"; then
    if grep -qiE 'LEGACY|retired' "$f"; then ok "$f: contains bot.main, but marked LEGACY"
    else bad "$f: contains bot.main WITHOUT a LEGACY marker"; fi
  fi
done

# --- 5: docs ENDORSE running the single monolithic process (endorsing phrases only) ---
if grep -rniE 'MVP monolith run|run the monolith|monolith runtime|temporary monolith' docs 2>/dev/null > /tmp/specguard_docs.txt; then
  echo "  -> docs endorsing the single process:"; sed 's/^/    /' /tmp/specguard_docs.txt
  bad "docs endorse running the single monolithic process"
else
  ok "docs do not endorse running the single monolithic process"
fi
rm -f /tmp/specguard_docs.txt

# --- 6: per-service secret hygiene in systemd (Block 4/8 + Block 5/8 boundaries) ---
# Checked only by secret NAMES in the systemd file (no values are written here). The real boundary
# is enforced by each service's secret-manager machine-account scope; this is a backstop against an
# accidental reference to another service's secret.
tb="systemd/aios-telegram-bot.service"
if [ -f "$tb" ]; then
  if grep -qE 'ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|CLAUDE_CREDENTIALS_JSON|GITHUB_TOKEN|GITHUB_REPO_WRITE_TOKEN|GMAIL_[A-Z_]+|NOTION_API_KEY|TODOIST_API_KEY|CONTEXT_STORE_IDENTITY|CONTEXT_KDB_MASTER_KEY' "$tb"; then
    bad "telegram-bot.service references a non-transport secret (Claude/GitHub/Gmail/Notion/Todoist/context-key — forbidden for transport)"
  else ok "telegram-bot.service has no non-transport secrets (Telegram only)"; fi
fi
cw="systemd/aios-claude-worker.service"
if [ -f "$cw" ]; then
  if grep -qE 'TELEGRAM_BOT_TOKEN|GMAIL_[A-Z_]+|NOTION_API_KEY|TODOIST_API_KEY' "$cw"; then
    bad "claude-worker.service references a Telegram/Gmail/Notion/Todoist secret (forbidden)"
  else ok "claude-worker.service has no Telegram/integration secrets"; fi
fi
# integrations-worker and sync: do NOT get the Claude account, Telegram token, GitHub token, context-key (Block 5/8)
for svc in integrations-worker sync; do
  sf="systemd/aios-$svc.service"
  [ -f "$sf" ] || continue
  if grep -qE 'CLAUDE_CODE_OAUTH_TOKEN|CLAUDE_CREDENTIALS_JSON|\.credentials\.json|TELEGRAM_BOT_TOKEN|GITHUB_TOKEN|GITHUB_REPO_WRITE_TOKEN|CONTEXT_STORE_IDENTITY|CONTEXT_KDB_MASTER_KEY' "$sf"; then
    bad "$svc.service references the Claude account/Telegram/GitHub/context-key (forbidden without explicit justification)"
  else ok "$svc.service has no Claude account/Telegram/GitHub/context-key"; fi
done

# --- 7: CLAUDE_CREDENTIALS_JSON — forbidden legacy (an OAuth-leak vector) ---
if grep -rnE 'os\.(environ\.get|getenv)\([[:space:]]*["'"'"']CLAUDE_CREDENTIALS_JSON' bot 2>/dev/null > /tmp/specguard_creds.txt; then
  echo "  -> active consumer:"; sed 's/^/    /' /tmp/specguard_creds.txt
  bad "active consumer of CLAUDE_CREDENTIALS_JSON in bot/*.py (forbidden legacy)"
else ok "no active consumer of CLAUDE_CREDENTIALS_JSON in code"; fi
rm -f /tmp/specguard_creds.txt
# A setup-instruction signal = a line where CLAUDE_CREDENTIALS_JSON sits next to a hosting dashboard
# reference (locale-independent, no brittle {0,40}; does not catch the substring "set" in "_setup_claude_auth").
if grep -rnE 'CLAUDE_CREDENTIALS_JSON' . --include='*.py' --include='*.md' --include='*.service' 2>/dev/null \
   | grep -iE 'dashboard' > /tmp/specguard_creds2.txt; then
  echo "  -> setup instruction:"; sed 's/^/    /' /tmp/specguard_creds2.txt
  bad "found a CLAUDE_CREDENTIALS_JSON setup instruction (dashboard; forbidden legacy)"
else ok "no CLAUDE_CREDENTIALS_JSON setup instruction (dashboard)"; fi
rm -f /tmp/specguard_creds2.txt

# --- 8: bypassPermissions — only on an approved path with a policy marker (Block 4.1/8 no-confirm policy) ---
for f in $(grep -rlF --include='*.py' 'bypassPermissions' bot 2>/dev/null); do
  case "$f" in
    bot/claude_policy.py|bot/claude_bridge.py|bot/claude_bridge_worker.py|bot/claude_worker.py)
      if grep -qiE 'no-confirm|policy|AIOS_CLAUDE_PERMISSION_MODE|owner' "$f"; then
        ok "$f: bypassPermissions with a policy marker (no-confirm chatbot)"
      else bad "$f: bypassPermissions without a policy comment"; fi ;;
    *) bad "$f: bypassPermissions outside the approved path (claude_policy/claude_bridge/claude_worker)" ;;
  esac
done
sys_bad=0
for sf in systemd/*.service; do
  [ -f "$sf" ] || continue
  if grep -qiE 'bypassPermissions|NoNewPrivileges[[:space:]]*=[[:space:]]*false' "$sf"; then
    bad "$(basename "$sf"): dangerous system-operation bypass flag"; sys_bad=1
  fi
done
[ "$sys_bad" = "0" ] && ok "systemd: no dangerous system-operation bypass flags"

# --- 9: forbid "set secret in the hosting dashboard" instructions (V2 hygiene, Block 4.2/8) ---
# A UI-instruction signal = the word "dashboard" on the same line as a secret name. Legitimate
# hosting references (volume/logs/decommission) do not contain "dashboard" and are not flagged.
if grep -rniE 'dashboard' . --include='*.py' --include='*.md' --include='*.service' 2>/dev/null \
   | grep -iE 'GMAIL_|_TOKEN|API_KEY|PASSWORD|CREDENTIALS|SECRET' > /tmp/specguard_rw.txt; then
  echo "  -> dashboard+secret instruction:"; sed 's/^/    /' /tmp/specguard_rw.txt
  bad "instruction to set a secret via the hosting dashboard (forbidden V2)"
else ok "no instruction to set secrets via the hosting dashboard"; fi
rm -f /tmp/specguard_rw.txt

# --- 10: systemd units do not reference a nonexistent /opt/ai-os (Block 5.2/8) ---
# The real deploy is ${AIOS_HOME}/apps/app (the same REPO=$HOME/apps/app default across all
# vps_*.sh and in the units' EnvironmentFile). The /opt/ai-os/{repo,venv} path was never created -> a
# unit pointing there would not start. Static literal check (does not touch the FS; works locally and on the VPS).
optref=0
for sf in systemd/*.service; do
  [ -f "$sf" ] || continue
  grep -qF '/opt/ai-os' "$sf" && { echo "  -> $(basename "$sf") references /opt/ai-os"; optref=1; }
done
[ "$optref" = "0" ] && ok "systemd units do not reference a nonexistent /opt/ai-os" \
                     || bad "a systemd unit references /opt/ai-os (absent on the VPS; the path is \${AIOS_HOME}/apps/app)"

# --- 11: pc_tasks does not emit the task body into a file name or commit message (Block -1A) ---
# Neutral metadata: file name = timestamp+random id, commit = "pc-task: queued <id>".
# Catch EXACTLY the old leak forms (a slug of the body in the file name; body interpolation in the
# commit), not any string — a neutral {task_id} does NOT match the pattern (no false positives).
pt="bot/pc_tasks.py"
if [ -f "$pt" ]; then
  pt_leak=0
  grep -qE '_slug\([[:space:]]*body' "$pt" && { echo "  -> file name from body: _slug(body...)"; pt_leak=1; }
  grep -qE 'pc-task:[^"]*\{(body|task_text)\b' "$pt" && { echo "  -> body in the pc-task commit message"; pt_leak=1; }
  [ "$pt_leak" = "0" ] && ok "pc_tasks: task body does not reach the file name/commit message (Block -1A)" \
                       || bad "pc_tasks: task body leaks into the file name or commit (Block -1A)"
fi

# --- 12: bridge bot-commits do not embed the task text in the commit message (Block -1A) ---
# Old templates: f"claude: {snippet}" / f"bridge: {snippet}". The neutral replacement is string
# constants with no interpolation. This check catches the return of interpolation EXACTLY in these
# commit prefixes; a log line (formatting via %r) does not match -> no false positives.
cb_f="bot/claude_bridge.py"
if [ -f "$cb_f" ]; then
  if grep -nE 'f"(claude|bridge): \{' "$cb_f" > /tmp/specguard_bridge.txt; then
    echo "  -> task interpolation in the commit:"; sed 's/^/    /' /tmp/specguard_bridge.txt
    bad "claude_bridge: the commit message interpolates the task text (Block -1A)"
  else ok "claude_bridge: bridge commit messages are neutral (no task interpolation)"; fi
  rm -f /tmp/specguard_bridge.txt
fi

# --- 13: Notion raw-persistence gate in place + default OFF (Block -1C) ---
# Structural check on the safety catch: the gate actually sits in the storage layer, and the default
# is a safe OFF. This is NOT a call block-list (brittle) — it asserts the backstop exists.
gate_bad=0
cfg="bot/config.py"
if [ -f "$cfg" ]; then
  grep -qE 'def[[:space:]]+notion_raw_allowed' "$cfg" || { echo "  -> config: no notion_raw_allowed()"; gate_bad=1; }
  grep -qF 'RAW_NOTION_PERSIST = _is_truthy(os.getenv("AIOS_RAW_NOTION_PERSIST"' "$cfg" \
    || { echo "  -> config: RAW_NOTION_PERSIST does not default OFF via _is_truthy(os.getenv)"; gate_bad=1; }
fi
if [ -f bot/claude_storage.py ]; then
  grep -qF 'notion_raw_allowed' bot/claude_storage.py || { echo "  -> claude_storage.py: no notion_raw_allowed gate"; gate_bad=1; }
fi
[ "$gate_bad" = "0" ] && ok "Notion raw-persistence gate in place (config + claude_storage), default OFF (Block -1C)" \
                      || bad "Notion raw-persistence gate missing/weakened (Block -1C)"

# --- 14: single encryptor (ADR barrier #1) — the crypto binding lives only in context_store,
# the bridge to the primitive lives only in context_cipher. Does not touch tests (scripts/); reads only bot/. ---
enc_bad=0
for f in $(grep -rlE 'from[[:space:]]+cryptography|ChaCha20Poly1305' bot --include='*.py' 2>/dev/null); do
  case "$f" in
    bot/context_store.py) : ;;
    *) echo "  -> $f imports the crypto binding outside context_store"; enc_bad=1 ;;
  esac
done
for f in $(grep -rlE 'from[[:space:]]+\.?context_store[[:space:]]+import|import[[:space:]]+context_store|bot\.context_store' bot --include='*.py' 2>/dev/null); do
  case "$f" in
    bot/context_store.py|bot/context_cipher.py) : ;;
    *) echo "  -> $f reaches the context_store primitive outside context_cipher"; enc_bad=1 ;;
  esac
done
[ "$enc_bad" = "0" ] && ok "single encryptor: crypto binding only in context_store, bridge to the primitive only in context_cipher (ADR barrier #1)" \
                     || bad "single-encryptor violated: crypto/primitive used outside the allowed modules (ADR barrier #1)"

echo ""
[ "$fail" = "0" ] && { echo "RESULT: SPEC CLEAN (V2-only, no temporary runtime shortcuts)"; exit 0; } \
                  || { echo "RESULT: SPEC VIOLATION — fix the FAILs above"; exit 1; }
