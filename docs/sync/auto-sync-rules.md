# Automatic synchronization (no manual operations)

## Principles
- **Source of truth is the Git repository.** Edits to CLAUDE.md / skills / hooks / docs are made in the repository.
- The VPS **pulls changes automatically** after a push. A manual `/sync_config` is an emergency fallback only, NOT the primary path.
- Only safe pulls are allowed: `git pull --ff-only` (no overwrite of local state, no destructive reset).

## Mechanism (primary then fallback)
1. **Primary:** a Git webhook triggers a lightweight pull service on the VPS that runs `git pull --ff-only`.
2. **Fallback:** a systemd timer (periodic polling) runs `git pull --ff-only`. NOT the basis of the coding flow, only a safety net if the webhook is unavailable.

## Symlinks on the VPS (skills/hooks from the repo ONLY)
```
~/.claude/skills -> symlink to repo/claude/skills
~/.claude/hooks  -> symlink to repo/claude/hooks
```
**`~/.claude/settings.json` is NOT symlinked** — it is a real local file with the operator's runtime/UI settings (for example, `theme`), local-only, not in git. `repo/claude/settings.json` is only a template/reference. (Symlinking settings.json to the repository was a mistake: Claude Code writes local settings there, and that would dirty the repository.)
This means: editing skills/hooks in git takes effect on the VPS immediately after a pull, with no manual copying.

## After a rules update
If `CLAUDE.md` / `claude/skills` / `claude/hooks` changed, then `claude-worker` restarts (systemd) or starts a new session to pick up the current rules. Otherwise it runs on stale rules.

## What we do NOT do
- Do not make manual `/sync_config` the primary scenario.
- Do not use cron as the basis of the coding flow (cron is only for future scheduled jobs: backup, morning digest, healthcheck).
- Do not pull changes that overwrite local edits (ff-only only).
