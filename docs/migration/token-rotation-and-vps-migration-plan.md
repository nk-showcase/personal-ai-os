# Plan: parallel token rotation + migration to a VPS

> Preparation can happen now. Real steps that touch tokens/VPS run only on explicit command, one at a time, with confirmation. Never print token values anywhere.

## Order
1. Create the structure in Bitwarden Secrets Manager (projects per service).
2. Create machine accounts (one per service: telegram-bot / claude-worker / integrations-worker = 3 = the free tier limit).
3. **Previous-hosting access token:** check whether it is still in use. If unused, revoke/delete it. If it is only needed for cleanup (removing leftover secrets from the previous hosting at step 13), use it temporarily and then revoke. There is no need to store the previous-hosting access token permanently (it is in `forbidden_everywhere` in secrets-map.yaml).
4. Rotate the Telegram token.
5. Rotate the Git host token (for the VPS use a separate fine-grained token, scoped strictly to the required repository).
6. Rotate the model API / notes / tasks / mail keys.
7. Put new values ONLY into Bitwarden.
8. In `config/secrets-map.yaml` keep only secret names.
9. Stand up the VPS skeleton (no real values in git).
10. Install the machine tokens on the VPS securely (file readable only by the service user, not in shell history, not in logs).
11. **Precondition:** implement the V2 entry points `bot.telegram_bot` / `bot.claude_worker` / `bot.integrations_worker` / `bot.sync_service`. Until they exist, the systemd files are TARGET templates, NOT runnable services; there is no temporary single process instead of V2 (see "V2 only — no temporary runtime solutions" in `docs/architecture/target-vps-ai-os.md`). Then start the three services via systemd.
12. Decommission the previous hosting / bridge for good.
13. Remove leftover secrets from the previous hosting — AFTER the smoke test of the new path.
14. Do NOT delete old tokens until the new path passes the smoke test, except for any that are clearly compromised.

## Smoke test of the new path (before deleting the old one)
- Telegram to task to claude-worker to edit to commit to push to the working branch to VPS pulled it.
- integrations-worker: Notion/Todoist read and write work. Mail is not part of this build; in the target design mail would be read-only, with sending gated behind a CONFIRM_SEND step that does not exist in the shipped code yet.
- Disable Bitwarden for a minute to the bot keeps working on cache and sends a warning (no degraded state).

## Operational safeguard (billing risk)
- Pay for the VPS **one year in advance** — one date per year instead of twelve.
- Set a **VPS renewal reminder** well ahead of time (task manager/calendar). Missing the VPS payment kills the whole system.

## What we do NOT do in this phase
- Do not put real tokens into chat/files.
- Do not change the actual settings of the previous hosting or the Git host.
- Do not enable systemd services without a command.
- Do not introduce a temporary single process instead of V2, the `single-process-runtime-v1` wrapper, or a temporary Bitwarden machine account "for the single process".
- Legacy previous-hosting entry points (`Procfile`/`Dockerfile` -> `bot.main`) are removed at step 12 — that is not the go-forward path.
