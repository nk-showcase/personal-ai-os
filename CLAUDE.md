# AI OS — Agent Rules (hard rules, not recommendations)

> This file is the "brain" of the system. The coding agent reads it at the start of every session on the server.
> Everything below is a HARD RULE, not a suggestion. It must not be violated.
> The source of truth for this file, skills, and hooks is git. On the server they are wired in via symlinks (see docs/sync/auto-sync-rules.md).

## System role
A personal, single-user AI OS running on a dedicated always-on server. It receives tasks from a chat channel, performs coding through a coding agent, and works with external integrations (e.g. email, notes, and task-manager services). Primary use case: coding from a phone, even when the operator's personal computer is turned off.

## Where the rules live
- Behavioral rules: this CLAUDE.md (in git).
- Secret access map (names only, no values): `config/secrets-map.yaml`.
- Technical guards: `claude/hooks/` plus the guard scripts (see docs/security/guards-and-hooks.md).
- Architecture and policies: `docs/`.

## Hard security rules (HARD)
1. NEVER print the values of tokens/keys/passwords — not in chat, not in logs, not in a prompt, not in git, not in this file.
2. Real secret values live ONLY in the secret manager. The agent works with secret NAMES, not values.
3. The coding agent's own credentials (`~/.claude/`) on the server are local-only. Do not put them in git, a prompt, or the secret manager (unless this is deliberately reconsidered).
4. A secret must never be written into the text of a task/reminder/prompt. Before any log/commit/prompt, run a secret scan (a guard blocks it).
5. Every secret is read under the principle of least privilege — only what a given service is allowed to access per `secrets-map.yaml`.

## Approval policy (summary; full in docs/security/approval-policy.md)
- WITHOUT confirmation: code edits, commit, push to a working (non-main) branch, creating a branch, running tests, ordinary git pull/fetch/rebase (no destructive reset).
- REQUIRES confirmation: force-push, push to main, deleting files/branches/data, reset --hard, rewriting history, changing secrets/auth/security/deploy config, disabling guards/hooks, changing secret-manager rules.

## Git policy
- Source of truth is git. The personal computer pushes with its own local access; the server pushes with a separate server-side access credential. The two never share git keys with each other.
- The server automatically pulls changes from the remote (`git pull --ff-only`). Push to main requires confirmation. Force-push requires confirmation and is blocked by default.

## Sync policy
- Change CLAUDE.md/skills/hooks → the coding worker restarts / starts a new session with the up-to-date rules. A manual `/sync_config` is only an emergency fallback, not the main path.

## Resilience (so nothing silently drifts out of sync)
- Secrets are cached locally after the first fetch. If the secret manager is unreachable, keep operating on the last known values and send a warning — do NOT crash.
- Pay for the server prepaid for a year plus a renewal reminder. A missed server payment kills the whole system; that is the main billing risk.

## Communicating with the operator
The operator may be non-technical. Answer concisely and clearly, without unnecessary architectural detail. Explain technical terms. Do not redirect the operator to a browser for something the agent can do itself.
