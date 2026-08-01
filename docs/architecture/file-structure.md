# Where things live

## In git (source of truth, published in the repository)
- `CLAUDE.md` — system rules.
- `docs/` — architecture, security, synchronization, migration, VPS notes.
- `config/secrets-map.yaml` — secret-access map (NAMES only).
- `claude/skills/`, `claude/hooks/` — Claude Code skills and guards (source of truth, symlinked into `~/.claude`). **Present only in the private installation** — this public tree ships just the empty `claude/hooks/` placeholder; the skill/guard contents are private.
- `claude/settings.json` — template/reference ONLY. It is NOT the live user settings file and is NOT symlinked into `~/.claude`. **Present only in the private installation** (not in this public tree).
- `systemd/` — service unit templates.
- `bot/` — bot and worker code.
- `scripts/` — tests and operator utilities.

## On the VPS (on the machine's disk, NOT in git)
- Running services (systemd).
- `~/.claude/` — Claude Code credential (local-only).
- Local cache of secret values (so the system does not fail when the secret manager is unreachable).
- Logs, and the working clone of the repository.

## Inside ~/.claude on the VPS (private installation; the symlink targets are not part of this public tree)
- `~/.claude/skills` -> symlink to `repo/claude/skills` (source of truth in git).
- `~/.claude/hooks` -> symlink to `repo/claude/hooks` (source of truth in git).
- `~/.claude/settings.json` — a REAL local file, NOT a symlink: user runtime/UI settings (for example, `theme`). Local-only, not in git, does NOT point at the repository.
- `~/.claude/.credentials.json` — NOT a symlink, local-only, not in git.

## In the secret manager (real values)
- TELEGRAM_BOT_TOKEN, GITHUB_REPO_WRITE_TOKEN, ANTHROPIC_API_KEY, NOTION_API_KEY, TODOIST_API_KEY.

## What NEVER goes into git
- `.env` with real values.
- Any token/key/password values.
- `~/.claude/.credentials.json`.
- The local secret cache.
- Logs with sensitive data.
- Personal Claude Code UI settings (`theme`, etc.) — they live ONLY in the local `~/.claude/settings.json`; the repository stays clean of them.
