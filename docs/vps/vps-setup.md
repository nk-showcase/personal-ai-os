# VPS setup

> This is a checklist of WHAT to do, not step-by-step navigation of any provider's panel. The exact clicks in a given cloud console are provider-specific and change over time; confirm them against the provider's current documentation at setup time.

## Choice and size
- Pick any reputable VPS provider that offers an always-on instance. Have a fallback provider ready in case sign-up or payment fails with the first one.
- **Hard rule: 1 GB is FORBIDDEN.** Minimum **4 GB RAM / 2 vCPU**, **8 GB RAM** recommended when available at an acceptable price. A 1 GB instance runs out of memory under this workload.
- OS: **Ubuntu LTS**.
- Billing: **prepay for a year** plus a renewal reminder (see the migration plan). A lapsed VPS payment kills the whole system.

## Baseline hardening (standard practices)
- Log in by **SSH key**; **disable password login**; disable root login.
- Create an unprivileged user (services run as this user, not as root).
- **Firewall**: open only what is needed (SSH inbound plus outbound for the bot); do not leave extra inbound ports open.
- Keep secrets out of shell history (`HISTCONTROL`; never pass tokens as command arguments).

## Install
- Python (3.12), Node.js, git.
- Claude Code CLI.
- A secrets-manager CLI/SDK (to fetch secrets via a machine account).

## Lay out
- Repository: clone into `${AIOS_HOME}/apps/app` (working tree). Virtualenv at `${AIOS_HOME}/apps/app/.venv`. This path matches the `REPO=$HOME/apps/app` default used by every `scripts/vps_*.sh`, the `EnvironmentFile` in the systemd units, and the actual deploy — one path, not `/opt/ai-os/repo`.
- `~/.claude/skills`, `~/.claude/hooks`, `~/.claude/settings.json` — symlinks into the repo (see auto-sync-rules.md).
- `~/.claude/.credentials.json` — Claude Code credentials, local-only, readable only by the service user.
- systemd services (see `systemd/`), enabled only at the migration step that turns them on.

## Operations
- **Logs**: journalctl per service.
- **Backups**: regular backup of `data/` plus configs (not secret values).
- **Healthcheck**: periodic liveness check of the services (cron is acceptable for healthcheck/backup, not for the coding flow).

## Open (confirm in practice, no guessing)
- The exact panel steps for a given provider (create instance, add SSH key, firewall) — from that provider's current documentation at setup time.
- Availability of the required size/region and final price — verify at sign-up; if payment/registration does not go through, switch to the fallback provider at 4 GB.
