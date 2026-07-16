# Secrets management

## Storage choice
- **Primary: Bitwarden Secrets Manager.** Free tier: unlimited secrets, up to 2 users, 3 projects, **3 machine accounts**. Advantage for this system: no billing date to miss, which lowers the risk of "everything went down because a payment lapsed".
- **Fallback: 1Password Service Account** — only if Bitwarden does not fit. Note: 1Password has no free tier (a paid subscription means a billing date that can be missed). Adopt only deliberately.
- **Bitwarden free-tier limit:** 3 machine accounts = exactly 3 services, with zero headroom. A 4th service requires a paid plan. Plan for this in advance.

## Base rules
1. Real secret values live ONLY in Bitwarden. Not in git, agent rules, README, prompts, logs, or a repository `.env`.
2. The coding agent and workers operate on secret NAMES (`config/secrets-map.yaml`), never on values.
3. Access follows least privilege: a service receives only the secrets it is allowed.
4. A new token goes straight into Bitwarden and nowhere else.
5. The coding-agent credentials (`${AIOS_HOME}/.claude/.credentials.json`) are local-only on the host. NOT in Bitwarden, NOT in git, NOT in the cache as an ordinary secret.

## Machine accounts (Bitwarden)
- One machine account per service: telegram-bot, claude-worker, integrations-worker. Total 3 = the free-tier limit.
- Each has access strictly to its own secrets from `secrets-map.yaml`. A service's machine token is stored on the host with tight permissions (readable only by the relevant service user, not in shell history, not in logs).

### Lifetime and rotation of machine access tokens
- **Non-expiring access tokens (`Never`) are permitted.** A deliberate choice: a token that expires and whose renewal is forgotten silently takes the service down, which is the same "everything went down" failure class we are trying to remove. No renewal timers.
- **Security rests on scope, not on lifetime:** each machine access token is scoped to exactly ONE project and to `Can read` only. The blast radius of a leak is the secrets of a single folder.
- **Rotation is event-driven, not timer-driven.** Reissue a token (and replace it on the host) on: suspicion of a leak; host or workstation compromise; a change in the set of services; a change in the secret-manager architecture; a scheduled manual security review.
- An access token is shown once, goes only to a secure location, and then onto the host with tight permissions. Never write it to chat, git, agent rules, docs, or logs.

## last-known-good cache (availability fallback)
Purpose: a temporary Bitwarden outage must NOT take the bot down. This is **only an availability fallback**, not the primary store.

| # | Rule |
|---|---|
| 1 | Bitwarden remains the source of truth. The cache is not a replacement. |
| 2 | The cache is not the primary secret store. |
| 3 | An in-memory cache (inside the process) is allowed. |
| 4 | An on-disk cache is only for runtime secrets of ordinary services: Telegram, Notion, task manager, LLM API, classifier backup. |
| 5 | An on-disk cache is FORBIDDEN for: master passwords, recovery codes, banking OTP, personal passwords, the platform master token. |
| 6 | The coding-agent OAuth credentials are NOT cached as an ordinary secret — they live as local agent credentials on the host. |
| 7 | The cache is per-service (one file per service), NOT one shared file with all secrets. |
| 8 | Cache files do not enter git, logs, prompts, or agent rules. |
| 9 | A cache file is accessible only to the relevant service user (file owner/permissions). |
| 10 | If the bot uses the cache instead of Bitwarden, it MUST send a warning to the operator. |
| 11 | The cache stores the timestamp of the last successful refresh from Bitwarden. |
| 12 | If the cache is too old, the service enters degraded mode and warns the operator. |

**Trade-off (explicit):** the cache improves reliability (the bot does not crash when Bitwarden is unavailable) but increases risk, since some secrets end up on the host's disk. This is a deliberate exchange of a little more attack surface for reliability. Therefore the on-disk cache is only for non-critical runtime secrets (rules 4-5) and never for critical ones.

**Hard rule for on-disk cache files:** on-disk cache files must be outside the repo, per-service, owned by the service user only, `chmod 600`, never committed, and covered by `.gitignore` patterns.

## Operational safeguard (billing)
The host is a paid subscription with a billing date. Missing that payment kills the whole system (a bigger risk than the secret store). Measures:
- Pay for the host **a year in advance** (one date per year instead of twelve).
- Set a **renewal reminder** well ahead (task manager / calendar).

## Forbidden everywhere
Secrets in output, logs, prompts, git, or agent rules. Before any log/commit/prompt, run a secret-scan (guard, see docs/security/guards-and-hooks.md).

## Secret-loading layer (`bot/secrets_loader.py`)
Code obtains secrets ONLY through `get_secret(name, required=…, default=…)`. Resolution order (first hit wins):
1. **in-memory cache** of the process;
2. **environment variable** — the main source for local/dev/test;
3. **Bitwarden** via the `bws` CLI — production runtime fetch. A **safe no-op** if `bws` is not installed or `BWS_ACCESS_TOKEN` is missing (returns None, the import does NOT fail);
4. **on-disk last-known-good cache** (`${AIOS_HOME}/.ai-os/secrets-cache`) — availability fallback, only for non-critical runtime secrets (never for the no-disk-cache set: master/recovery/banking/personal/platform-token/agent-OAuth).

Behaviour and rules:
- A required secret not found raises `MissingSecretError("Missing required secret: <NAME>")` — **name only, never the value**.
- Only **names and source** are logged (`env`/`bitwarden`/`disk-cache`); values never.
- Switches via env: `AIOS_SECRETS_BACKEND` = `env` (default) | `bitwarden`; `AIOS_SECRETS_CACHE_DIR` (default `${AIOS_HOME}/.ai-os/secrets-cache`); `BWS_ACCESS_TOKEN` (for production).
- `config.py` uses this loader; it is compatible with a dummy-env check:
  `TELEGRAM_BOT_TOKEN="000:dummy" TELEGRAM_OWNER_ID="1" python -c "import bot.config; import bot.main"`.

## Non-secret runtime settings
Not everything the bot needs is a secret. Identifiers and parameters whose leak grants no access are ordinary service env variables, NOT Bitwarden entries. The full list of names is in `config/secrets-map.yaml` → `non_secret_env`.
- **`TELEGRAM_OWNER_ID` is NOT a secret, but is REQUIRED.** It is the operator's numeric Telegram id (access control, not a credential — knowing the id grants no login). `bot/config.py` raises `ValueError` if it is unset. The source is an ordinary service env variable of the telegram-bot service (systemd `Environment=` / `EnvironmentFile=`), not Bitwarden. Presence check by name: `scripts/vps_bitwarden_inventory_check.sh --env-check`.
- The other non-secrets (`NOTION_*_DB`, `BRIDGE_*`) have defaults in code, so their absence does not block startup.

## Coding-agent OAuth: local-only file
- The coding-agent OAuth source is a local-only file `${AIOS_HOME}/.claude/.credentials.json` (provisioned OUTSIDE the application). The full setup plan (owner, path, permissions, manual steps, verification without reading the value) is in `docs/security/claude-code-credential-setup.md`.
- `CLAUDE_CREDENTIALS_JSON` is a **forbidden legacy** environment variable — a known OAuth leak/injection vector. Its consumption is hard-disabled in `bot/claude_bridge_worker.py`: the worker never reads `CLAUDE_CREDENTIALS_JSON` and never writes the credentials file. Loading credentials through Telegram is disabled. This is NOT a valid setup path.

## Per-service secret boundaries
Least privilege from `config/secrets-map.yaml`, condensed into "what a service receives / what it must NOT receive". The source of truth for the composition is `secrets-map.yaml`; the guard `scripts/vps_spec_guard.sh` (check 6) verifies the boundaries against the systemd files.

| Service | Receives | Must NOT receive |
|---|---|---|
| `telegram-bot` | only `TELEGRAM_BOT_TOKEN` (+ the non-secret `TELEGRAM_OWNER_ID`) | agent credentials, `GITHUB_*`, `NOTION_API_KEY`, `TODOIST_API_KEY`, `ANTHROPIC_API_KEY`, the context-store decryption identity (`CONTEXT_STORE_IDENTITY`) |
| `claude-worker` | agent credentials (local-only file, NOT Bitwarden), `GITHUB_REPO_WRITE_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` (one-time sign-in) | `TELEGRAM_BOT_TOKEN`, `NOTION_API_KEY`, `TODOIST_API_KEY` |
| `integrations-worker` | `NOTION_API_KEY`, `TODOIST_API_KEY`, `TELEGRAM_BOT_TOKEN` (it sends personal store views itself — by design, see SECURITY.md §6), `ANTHROPIC_API_KEY` (in-worker LLM calls) | agent credentials, `GITHUB_*` |
| `sync` | nothing (only `git pull --ff-only`) | any secrets; in particular agent credentials and `TELEGRAM_BOT_TOKEN` |

## Context-encryption key (implemented; deliberately NOT a secret-manager secret)
The encrypted context store ships in this tree (`bot/context_store.py`, `bot/context_key.py`, `bot/context_cipher.py`) and runs enabled in the production deployment. Its master key is handled differently from every other secret — **by design it never enters the secret manager**:
- The working master key is an **on-disk mode-0600 age identity file** held by the decrypting (keyed) worker; the offline recovery key stays air-gapped with the operator. See `docs/security/adr-encryption-notion-migration.md` (which supersedes the earlier "store it in Bitwarden" placeholder) and `docs/security/key-custody-model.md`.
- The name `CONTEXT_STORE_IDENTITY` remains reserved in `config/secrets-map.yaml` only to state the boundary in the value-free map: it is a "never" class secret — not in git, not in Bitwarden, not in logs.
- **Decryption boundary:** only the keyed worker decrypts. `telegram-bot` **never** receives the decryption key (neither via env nor via secret-manager access).
