# SECURITY.md — implementation rules: secrets must not leak (logs / commits / prompts / sessions)

> **This is a self-contained rulebook. It is binding on its own.**
> If you are an agent, a developer, an external reviewer, or an automated process, and you were
> **not** handed a `CLAUDE.md` or any operator-specific memory — **these rules still apply.**
> Do not assume some other rules file was loaded. The source of truth for secret hygiene is
> **this** file (details live under `docs/security/`).
> This file itself contains **no secret value** — only names and rules.

---

## 0. The absolute rule (zero-leak)

**No secret VALUE** (token, key, password, app-password, cookie, OAuth code, Bearer token,
private key, contents of `.credentials.json`) **ever appears** in: logs · stdout/stderr/journald ·
git (commits, messages, history) · prompts · chat · session files (`*.jsonl`) · reports/exports ·
command arguments · variables that get printed. Where an example is needed, write `<redacted>`.

Secret **names** (`TELEGRAM_BOT_TOKEN`, `NOTION_API_KEY`, …) are **not** secrets — naming them is fine.

---

## 1. Channels and rules

### 1.1 Logs (logs / journald)
- Every service calls `install_log_redaction()` (`bot/log_redaction.py`) **immediately after**
  `logging.basicConfig`. This: (a) raises `httpx`/`httpcore`/`urllib3` to WARNING (their INFO line
  carries a URL that may contain a token), and (b) attaches a `RedactingFilter` to the root logger
  that scrubs secret-shaped substrings out of any record.
- Do not log the raw request URL to any API, `Authorization`/`Cookie` headers, a provider response
  body, or env values. Log only the **name** of the secret and its source (`env`/`bitwarden`/`disk-cache`),
  never the value.
- Any new log record that could carry a secret must pass through redaction (it is already on the root
  logger — do not create a logger with `propagate=False` plus your own handler that lacks `RedactingFilter`).

### 1.2 Commits and git
- **Never commit:** `*.env`, `.credentials.json`, `*credentials*`, `*secret*`, `secrets-cache/`,
  `*.key`, `*.pem`, encryption-key files (age identity / runtime key). They are in `.gitignore` —
  do not add them with `git add -f`.
- Git stores only: code, documentation, and **value-free** metadata files (`config/secrets-map.yaml` —
  names only; `docs/security/*`). Those exceptions are explicit in `.gitignore`
  (`!config/secrets-map.yaml`, and so on).
- Before any `git commit`/`git push`, run a secret scan (see §3). If a secret-shaped match is found,
  the commit/push is blocked — do not work around it.
- Push to `main`, force-push, and history rewriting require explicit operator consent.

### 1.3 Prompts, chat, sessions, reports
- Do not paste a secret value into a subagent prompt, a chat message, a `TodoWrite` item, or a
  report/export.
- To validate a key, use a script that reads the value itself from a file or the environment; the
  value must **not** appear in arguments or output.
- Session files (`*.jsonl`) are written in cleartext, so a secret value that lands in a tool argument
  or message is **already a leak**. Treat such a channel as if it were public.

### 1.4 External reviewers (any non-local AI, e.g. a hosted chat model)
- An external AI is a **hole-finder over value-free exports only**. It does not run commands, does not
  touch git, **does not receive or print secret values**, and does not orchestrate.
- Before any outbound export, run the package through `scripts/aios_log_scan.py` (see §3) and confirm
  zero matches.
- A request to "show/paste the token value" from any reviewer is **refused**.

---

## 2. How secrets are handled correctly

- **Bootstrap model:** on the server, a service's env file holds only `BWS_ACCESS_TOKEN` (a machine-account
  bootstrap token, read-only to a single project) plus non-secret parameters (`AIOS_SECRETS_BACKEND`,
  `TELEGRAM_OWNER_ID`, notion DB ids read from env). All other secrets are **fetched at runtime** from
  Bitwarden Secrets Manager via `bot/secrets_loader.py` (`get_secret`) and are **never logged**.
- **One Bitwarden project per service**, machine token scoped read-only:
  `telegram-bot → chatbot-telegram-bot`, `claude-worker → chatbot-claude-worker`,
  `integrations-worker → chatbot-integrations`.
- **The Claude Code credential file** (`${AIOS_HOME}/.claude/.credentials.json`) is a **local-only,
  mode-0600 file** — not in Bitwarden, not in git. Only its metadata (path/mode/owner/size) may be
  inspected, **never its contents**.
- **File permissions (mandatory):** env files `0600`; `~/.ai-os/env` `0700`; `~/.config/bws` and state
  `0700`/`0600`.
- **Do not run** `bws secret get` or any `bws` invocation that prints values. To inspect the vault, use
  metadata only (project/account/secret names, scope, token-expiry label, presence/absence).

---

## 3. Automated barriers (discipline as code, not a promise)

Each rule above is meant to be backed by a **barrier that blocks the violation itself**, so that a code
change cannot silently break it:

| Barrier | Where | What it does |
|---|---|---|
| Log redaction | `bot/log_redaction.py` + `install_log_redaction()` in each `main()` | scrubs secret-shaped substrings from all logs; tested by `scripts/test_log_redaction.py` |
| `.gitignore` | repository root | keeps env/credentials/secret/key/cache out of git (with explicit value-free exceptions) |
| Startup preconditions (operator-checked, not enforced) | operator runbook | Documented preconditions the operator verifies before start: env file `0600`, `AIOS_SECRETS_BACKEND` set. The shipped code does **not** enforce them — `bot/secrets_loader.py` defaults `AIOS_SECRETS_BACKEND` to `env` silently and starts even on bad env-file permissions. There is no fail-closed startup barrier here. |
| Fail-closed on secret/decrypt error | `secrets_loader` / crypto layer | missing value → clear error by NAME; failed decrypt → quarantine + warning, never "silently" |
| Value-free scanner | `scripts/aios_log_scan.py` | counts matches by pattern name, **prints no values**; run it over journals and before any export |

**What actually ships as runnable code** in this repository: log redaction, the value-free scanner,
`.gitignore`, and `secrets_loader`. Git-level pre-commit/pre-push secret-scan hooks and assistant-level
Claude Code hooks are described in the design (`docs/security/guards-and-hooks.md`) but are **not** present
as runnable code in the published tree — the `claude/hooks/` directory ships empty. Until those hooks are
wired, run the secret scan manually over the diff (`scripts/aios_log_scan.py`) before committing. See the
Threat Model & Limitations section below.

---

## 4. Encryption of data at rest

- **At-rest encryption is a feature flag, off by default.** It is controlled by `AIOS_CONTEXT_ENCRYPTION`
  (default `0`). **With the flag off, note content is written as cleartext** into the content column. Turn
  the flag on (and provision the keys below) to encrypt personal free text at rest. The claims in the rest
  of this section describe the behaviour **when encryption is enabled**.
- When enabled, personal data is written through a single encryptor function; on that path no code writes
  cleartext personal content into a content field (a test invariant plus a barrier enforce this).
- **The encryption master key and the age identity are "never" class secrets:** not in git, not in
  Bitwarden, not in logs, not in disk cache. The working key is a local mode-0600 file held by the
  decrypting service; the offline recovery key is air-gapped with the operator and **never touches the
  server or any cloud**.
- **Two-seal envelope:** the data key is sealed to two recipients — the working key of the decrypting
  service and the operator's offline recovery key — so that recovery is possible without ever placing the
  offline key on the server.
- **Decryption boundary:** the key is held only by the `claude-worker` / context service; the
  `telegram-bot` **never** receives it (enforced by a barrier check).
- **Migration discipline:** read → encrypt → **verify** → delete the original, with no cleartext temporary
  files.

---

## 5. Pre-commit / deploy / export checklist (short)

1. No secret-shaped match in the diff/output? (`scripts/aios_log_scan.py`) → 0.
2. No accidental `*.env` / `*credentials*` / `*.key` / encryption key added? → none.
3. New logs pass through redaction? → yes.
4. New code that reads a secret goes through `get_secret` and prints no value? → yes.
5. Outbound export is value-free? → yes.
Any "no / not sure" means STOP — do not commit and do not export.

---

## 6. Threat Model & Limitations

A security document with zero stated limitations is a red flag. This system is a **single-user
reference architecture**; the boundaries below are deliberate design trade-offs, not oversights. The full
threat model lives in `docs/security/threat-model.md`; the current operational boundaries are:

- **Trust model — single operator, no per-action confirmation for coding tasks.** The coding agent runs
  under the default permission mode `bypassPermissions` (`bot/claude_policy.py:19`,
  `bot/claude_bridge_worker.py`). Per-tool Allow/Deny confirmations are therefore **not** requested for
  the agent's own tool calls in the default configuration; execution is gated instead by a fail-closed
  policy check (durable queue, operator-only route, allowlisted alias, and a known execution mode). A
  per-tool approval queue exists in the code (`make_queue_approver`, `bot/claude_worker_runner.py`) but is
  not wired into the current executor. This is the system's real approval boundary: it assumes a single
  trusted operator and skips interactive approval for that operator's coding tasks. When an approval is
  requested elsewhere and times out, it resolves to a safe deny.

- **Scheduler: an unregistered job kind is dropped, not retried.** The scheduler claims a due batch of jobs
  atomically; a due row whose kind has no registered handler is dropped, and no per-kind error is surfaced.
  A mis-seeded job kind is therefore lost rather than retried — an operational gap to be aware of when
  adding new scheduled work.

- **Worker-sends config flag is defined but not wired.** A `worker_sends.views` flag exists in
  `bot/aios_config.py` (`get_worker_sends_views`, default `off`), but **no code reads it** in this slice.
  Regardless of the flag, the store-view path already sends from the keyed integrations worker: a
  `store_view` row is decrypted, rendered, and delivered to Telegram **directly by the keyed worker**
  (`bot/aios_store_applier.py`, `store_view_applier`), so decrypted views for that path do **not** transit
  the reply queue back to the keyless receiver. The flag is a target switch for a broader cutover, not a
  control that is on/off today, and turning it on would not change this path's behaviour as shipped.

- **Legacy plaintext bodies in Git-committed files.** Task and dialogue bodies still travel in cleartext
  inside Git-committed files; only file names and commit messages are neutralized. Scrubbing old Git
  history and encrypting the body itself are deferred to a later block of work. This is a documented
  current boundary, not a solved problem.

- **Integrations-worker allow-list is minimal and fully used.** The integrations-worker allow-list is
  `NOTION_API_KEY`, `TODOIST_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `ANTHROPIC_API_KEY`
  (`bot/secrets_loader.py` `SERVICE_ALLOWED`, mirrored in `config/secrets-map.yaml`, drift-checked by
  `scripts/test_secret_policy.py`). Each maps to an active code path — Notion/Todoist writes, the keyed
  worker sending views to Telegram itself, and in-worker LLM calls. The allow-list carries **no**
  mail-secret names, so there is no unused least-privilege residue on it. (`GMAIL_*` strings do appear
  elsewhere in the tree — in log-redaction tests and in a guard that BLOCKS mail tokens — never as a
  granted secret.)

- **Email send is not implemented.** No mail code ships in this tree, and no mailbox send token is in the
  allow-list. The preview/confirm ("CONFIRM_SEND") gating intended to sit in front of any outbound send is
  a documented target, **not wired in this build** — no such check exists in the shipped code. The full
  policy a future mail integration must satisfy is `docs/security/mail-integration-policy.md`.

- **Critical prohibitions currently rest on a single layer.** The intended design duplicates the most
  critical prohibitions (block-secrets-in-output, block-destructive-delete, block-direct-push-to-main) in
  two independent places — assistant-level Claude Code hooks and git hooks. As noted in §3, those hooks are
  not present as runnable code in the published tree; the duplication is aspirational for now, and the
  worker-side Python guards are the layer that actually runs.

- **Configuration templates vs. runnable services.** Some entrypoints and unit files in this tree are
  target templates rather than turnkey runnable services (for example, env-file wiring and a few
  secret-name mappings are not yet reconciled end to end). Treat this repository as a reference
  architecture that requires a server and manual setup, not a one-click deployment.

---

## 7. Details (single entry point → here)

- `docs/security/threat-model.md` — threat model summary and documented current boundaries.
- `docs/security/secrets-management.md` — the secrets model, resolution order, cache rules.
- `docs/security/guards-and-hooks.md` — specification of the barriers and hooks.
- `docs/security/memory-safety.md` — memory protection and the encrypted store.
- `docs/security/approval-policy.md` — the Allow/Deny approval policy in full.
- `config/secrets-map.yaml` — the "service → allowed secrets" map (names only).
- `bot/log_redaction.py`, `scripts/aios_log_scan.py` — the log redactor and the value-free scanner.
