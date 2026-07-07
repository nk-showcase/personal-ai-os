# Bitwarden Secrets Manager — setup checklist (for the operator, no secrets)

> Step by step, for a non-developer operator. Real secret values are entered ONLY into the Bitwarden window. Never into chat / git / CLAUDE.md / docs / logs.
> Bitwarden model: **projects** (secret folders) + **machine accounts** (robot users for services) + project-level access (`Can read`). Sources at the end of the file.

## Terms (short)
- **Project** — a folder where you put secrets.
- **Machine account** — a "robot" for one bot service; sees only its own folder.
- **Access token** — the single key that later gets installed on the server. Bitwarden shows it **once**.

## 1. Create 3 projects
| Project | Purpose |
|---|---|
| `chatbot-telegram` | Telegram bot secret |
| `chatbot-claude-worker` | coder secrets |
| `chatbot-integrations` | mail/notes/tasks secrets |

## 2. Create 3 machine accounts
`telegram-bot`, `claude-worker`, `integrations-worker` — one per service.
3 projects + 3 machine accounts = **exactly the Bitwarden free tier**, no headroom (a 4th service means a paid plan).
Do NOT create a temporary 4th / "monolithic" machine account — the target path is V2 only (3 services), with no temporary single process.

## 3. Create secrets (names from `config/secrets-map.yaml`), sorted into projects
| Project | Secrets (names) |
|---|---|
| `chatbot-telegram` | `TELEGRAM_BOT_TOKEN` |
| `chatbot-claude-worker` | `GITHUB_REPO_WRITE_TOKEN`, `ANTHROPIC_API_KEY` |
| `chatbot-integrations` | `NOTION_API_KEY`, `TODOIST_API_KEY` |

Values are entered **only in the Bitwarden window** and **as each token is rotated** — not necessarily all at once. A secret's name+value is created at the moment that token is actually reissued. (For a name-only inventory, see 7a.)

**Do NOT store in Bitwarden:** the Claude Code login (lives as a file on the server, local-only), and `RECOVERY_CODES`, `MASTER_PASSWORDS`, `BANKING_OTP`, `PERSONAL_PASSWORDS`, `PREVIOUS_HOSTING_MASTER_TOKEN` (`forbidden_everywhere`).

## 4. Grant access (each robot gets only its own folder)
Machine account -> **Projects** tab -> add **one** project -> permission **Can read**:
- `telegram-bot` -> `chatbot-telegram` · Can read
- `claude-worker` -> `chatbot-claude-worker` · Can read
- `integrations-worker` -> `chatbot-integrations` · Can read

No robot gets more than one folder (least privilege, as in `secrets-map.yaml`). **Verify live — see 7.**

## 5. Manual steps in Bitwarden
1. Open Bitwarden -> enable **Secrets Manager** (free organization).
2. Create 3 projects (section 1).
3. Create 3 machine accounts (section 2).
4. Grant each `Can read` on its project (section 4).
5. Create/fill secrets **as each token is rotated** (section 3) — not all 8 at once.
6. Create access tokens and install them on the VPS securely (one per service, file readable only by the service user). Values are never printed.

## 5a. Access token lifetime (expiration) and rotation
- **When they are created:** during VPS setup (created -> straight to the server, no interim storage).
- **For the reference deployment, non-expiring access tokens (`Never`) are allowed.** This is a deliberate choice: a token with an expiry that nobody remembers to renew silently takes the service down — the same "everything went dark" risk we avoid. No timers, no mandatory renewals.
- **Security rationale (in place of an expiry):** each machine access token is scoped to exactly ONE project and only with **Can read**. So the blast radius on leak is one folder's secrets, no more; lifetime is secondary here. **Confirmed by the scope check (section 7).**
- **Rotation is event-driven, not timer-driven.** Reissue a machine access token (and replace it on the server) when:
  - a token leak is suspected;
  - the VPS or a client machine is compromised;
  - the set of services changes;
  - the secret-manager architecture changes;
  - a planned manual security review occurs.
- An access token **is shown once** — save it only to a secure place, then install it on the server securely.
- Access tokens are **not written** to chat, git, `CLAUDE.md`, docs, or logs.

## 6. What is forbidden (hard)
- Do not paste real secret values or access tokens into chat.
- Do not store them in git or in a repository `.env`.
- Do not write them into `CLAUDE.md`, docs, logs, or task text.
- Secret values live only in Bitwarden; access tokens live only in a secure place -> server.

## 7. Check: a robot sees ONLY its own secrets
- **In the window (the operator does this):** machine account -> **Projects** tab -> exactly **one** project with `Can read`, the other two absent. Same for all three.
- **Live check (no token printed):** using each robot's access token, request the **project** list (`bws project list` — it does not return secret values). Expected result: each token sees exactly its one project (`telegram-bot -> chatbot-telegram`, `claude-worker -> chatbot-claude-worker`, `integrations-worker -> chatbot-integrations`). Tokens and values never reach chat/log.

## 7a. Secret inventory by name (present/missing)
- **Tool limitation:** `bws` version 2.1.0 has NO "names only" mode. `bws secret list` in every output format (json/yaml/env/table/tsv) includes the secret **value**. So you cannot list secret NAMES without simultaneously reading their VALUES through the standard command.
- **Therefore automatic live inventory does NOT run by default** (rule: do not read values when names alone are enough to check).
- **Safe ways to learn which names exist:**
  1. **Bitwarden window (operator):** open each project -> secret names are visible (values masked by asterisks) -> reconcile with the table in section 3.
  2. **parse-and-discard (by explicit decision):** the script `scripts/vps_bitwarden_inventory_check.sh --live` takes the output of `bws secret list`, keeps ONLY the key names and immediately discards the values — it prints `present/missing` but NOT the values. Values pass only through the process memory on the VPS and are never displayed/logged. Run only deliberately.
- **Expected state early on:** most secrets are not yet created (filled as tokens rotate), so a name inventory will mostly show `missing` — that is normal, not an error.

## 8. Connecting to the bot (env on the VPS)
On the VPS each service has variables set (values NOT in git/chat):
- `AIOS_SECRETS_BACKEND=bitwarden` — enables runtime fetch from Bitwarden (default `env` for dev/test).
- `BWS_ACCESS_TOKEN=<this service's token>` — the Bitwarden machine token (stored securely, file readable only by the service user).
- (opt.) `AIOS_SECRETS_CACHE_DIR` — default `~/.ai-os/secrets-cache`.

**Before starting telegram-bot — add the non-secret `TELEGRAM_OWNER_ID`** (the operator's numeric id; NOT a secret, but required — without it `bot/config.py` fails with `ValueError`). Operator step on the server (the operator supplies the value directly, it is not printed to chat/git/docs):
```
printf 'TELEGRAM_OWNER_ID=<numeric-id>\n' >> ${AIOS_HOME}/.ai-os/env/telegram-bot.env
```
Do NOT leave it empty (empty value = the same `ValueError`). Presence check by name (without values): `bash scripts/vps_bitwarden_inventory_check.sh --env-check`.

Before starting the bot you must fill the secret values in Bitwarden (as tokens rotate) and then enable the services. The `bot/secrets_loader.py` layer safely tolerates missing values (see docs/security/secrets-management.md).

## 9. Future: context encryption key (do NOT create during initial setup)
- Placeholder name of the future secret: `CONTEXT_STORE_IDENTITY` (aka `CONTEXT_KDB_MASTER_KEY`) — the private decryption identity of the future encrypted context store (KDB).
- **Not created now** — it is only a reserved name (`config/secrets-map.yaml` -> `planned_future`). The KDB is not built during initial setup.
- When implementation starts: put it in the `chatbot-claude-worker` project and grant access ONLY to the `claude-worker` machine account (reuse the existing one, without a paid 4th account). The `telegram-bot` machine account is never given the decryption key.

---
Sources: [Machine Accounts](https://bitwarden.com/help/machine-accounts/) · [Access Tokens](https://bitwarden.com/help/access-tokens/) · [Secrets Manager Quick Start](https://bitwarden.com/help/secrets-manager-quick-start/) · [Secrets Manager Plans](https://bitwarden.com/help/secrets-manager-plans/) · [Secrets Manager CLI (bws)](https://bitwarden.com/help/secrets-manager-cli/)
