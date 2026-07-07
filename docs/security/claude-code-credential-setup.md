# Claude Code: credential setup on the VPS (local-only)

> Setup plan. NO values: the credential contents never appear in this file, in chat, in git, or in logs.
> The Claude Code credential is NOT an ordinary secret from the secret manager. It lives ONLY as a file on the VPS disk.

## What it is and why it is separate from the secret manager
`claude-worker` runs Claude Code on the VPS. Claude Code reads its OAuth credential from a local file
`~/.claude/.credentials.json`. By design (see `config/secrets-map.yaml` -> `local_only`) this file is
**local-only**: not in the secret manager, not in env, not in a cloud host, not over Telegram, not in git, not in the on-disk secrets cache.

Reason: carrying the Claude credential through env/Telegram (`CLAUDE_CREDENTIALS_JSON`) is an OAuth
leak/injection vector. That path is hard-disabled and **forbidden** (forbidden legacy). The only valid
source of the credential is a file placed on the VPS outside the application.

## Target file properties
| Property | Value |
|---|---|
| Path | `${AIOS_HOME}/.claude/.credentials.json` |
| Directory | `${AIOS_HOME}/.claude/` |
| Owner | the coding-worker service user |
| Directory mode | `0700` |
| File mode | `0600` |
| Who reads it | only `claude-worker` (running as the coding-worker user, with `HOME=${AIOS_HOME}`) |
| Who does NOT read it | `telegram-bot` (no `HOME=` on this folder; `setup_claude_auth()` is a no-op) |

Why `${AIOS_HOME}/.claude/`: the `aios-claude-worker.service` unit sets `Environment=HOME=${AIOS_HOME}`
and `User=<coding-worker user>`, so `~/.claude` for the worker resolves to `${AIOS_HOME}/.claude`. The
`aios-telegram-bot.service` unit **intentionally** does not set `HOME` to this folder and receives no
Claude secrets.

## Manual steps (performed by the operator on the VPS; interactive Claude login cannot be done from chat)
The assistant CANNOT perform these steps: logging in to Claude Code requires an interactive OAuth session
on the VPS itself. So the steps are for the operator, over SSH to the VPS. No credential values are ever printed.

1. SSH to the VPS under an account with sudo.
2. Create the directory with the right mode and owner:
   `sudo install -d -m 700 -o <coding-worker-user> -g <coding-worker-user> ${AIOS_HOME}/.claude`
3. Place the credential at `${AIOS_HOME}/.claude/.credentials.json` by ONE of these methods (the value never passes through chat/git):
   - **Preferred:** on the VPS itself, as the coding-worker user, run the interactive Claude Code login
     (`claude setup-token` / login), so the file is written locally and the value never leaves the machine.
   - **Or:** copy an existing `.credentials.json` directly over SSH (`scp`/`rsync` in a secure channel)
     straight to this path. Never via Telegram, chat, git, or a chat clipboard.
4. Set owner and mode:
   `sudo chown <coding-worker-user>:<coding-worker-user> ${AIOS_HOME}/.claude/.credentials.json && sudo chmod 600 ${AIOS_HOME}/.claude/.credentials.json`
5. **Check WITHOUT reading the value** (presence + mode + owner only; do not open the contents):
   `sudo -u <coding-worker-user> test -f ${AIOS_HOME}/.claude/.credentials.json && stat -c '%U:%G %a' ${AIOS_HOME}/.claude/.credentials.json`
   Expected: `<coding-worker-user>:<coding-worker-user> 600`. Do NOT run `cat`/`less`/print the contents.
6. Confirm that `telegram-bot` receives no Claude credential: `scripts/vps_spec_guard.sh` (check 6) should show
   `telegram-bot.service without Claude/GitHub/integration secrets`.

## Forbidden (hard)
- `CLAUDE_CREDENTIALS_JSON` as a setup path — **forbidden legacy** (OAuth leak vector). Do not reintroduce.
- Loading the credential over Telegram (`intercept_credentials_document`) — disabled, do not reintroduce.
- The credential in the secret manager as an ordinary secret, in a service's env, in a cloud host, in git, in the on-disk secrets cache
  (`CLAUDE_OAUTH` in `NO_DISK_CACHE`), in `CLAUDE.md`, in logs, in task text.
- Printing/committing/forwarding the contents of `.credentials.json`.

## Rotation
- Event-driven, not on a timer (like the other secrets — see `docs/security/secrets-management.md`).
- Reissue on: suspected leak, VPS/operator-machine compromise, architecture change.
- Because the `CLAUDE_CREDENTIALS_JSON` path historically existed, **rotating the Claude OAuth credential is
  recommended** during the initial setup.

## Fixed rule: credentials are a local file only
The Claude Code credential is sourced **only** from the local file `~/.claude/.credentials.json`
(`${AIOS_HOME}/.claude/.credentials.json` for the coding worker). Any alternative env path (for example a
`CLAUDE_CODE_OAUTH_TOKEN` env variable that some plans allow before launching the CLI) is NOT a supported
target and is not documented as a valid source: the file model fully replaces the env-token model. This is a
decided rule, not an open question.
