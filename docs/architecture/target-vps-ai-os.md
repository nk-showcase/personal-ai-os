# Target architecture: VPS-based AI OS

## Flow
```
Telegram (phone)
   -> VPS: telegram-bot (owner check, puts a task on the queue)
      -> queue
         -> VPS: claude-worker (runs Claude Code on the VPS)
            -> code edits -> commit -> push to a WORKING branch (no confirmation)
GitHub (source of truth) <- push ; the VPS automatically pulls changes (git pull --ff-only)
integrations-worker: Notion / Todoist / Anthropic API (separate from coding)
Secret manager: real key values (workers fetch them by machine access)
```

## Components
- **telegram-bot** — transport. Holds only `TELEGRAM_BOT_TOKEN`. No Claude OAuth, no GitHub token, no integration tokens.
- **claude-worker** — coding through Claude Code. The Claude Code credential is local-only on the VPS disk. Push to a working branch without confirmation; push to main / force-push / changing secrets require confirmation.
- **integrations-worker** — Notion/Todoist/Anthropic/classifier. In the shipped build, Notion/Todoist write-backs execute directly, with no preview/confirm step. Preview + CONFIRM_SEND in front of an external write-back is a target-design goal, not wired in this build.
- **secret manager** — a machine-account secret manager (primary), with a service-account fallback.
- **VPS** — always-on executor. Minimum 4 GB RAM / 2 vCPU, 8 GB recommended. 1 GB is NOT allowed.

## How the VPS differs from a managed PaaS
- A managed PaaS gives you ephemeral containers, platform restarts, and auto-redeploy from git. There is no persistent disk for the Claude credential.
- The VPS is your own persistent machine: you control the OS, disk, and network; the Claude credential can live on disk; there is no auto-redeploy-from-git as a mandatory coupling.

## Why the cloud-container bridge is not reused
The earlier cloud-container bridge ran Claude Code in a container that auto-deployed from git, held Claude OAuth + a GitHub token, and could rewrite its own code and immediately deploy it. That is a design defect (a self-modifying, self-deploying execution path with credentials) and is deliberately not repeated.

## Why the VPS bridge executor STILL carries bridge risk
The bridge scenario is **intentionally recreated** as a controlled VPS bridge executor. The risk does not disappear — it is **moved** onto a dedicated VPS and must be explicitly defended (guards, secret separation, a separate VPS GitHub token, no auto-deploy-from-git, owner-only entry). claude-worker is still an always-on executor with access to code and to the Claude credential.

## Why this is accepted deliberately
The primary requirement: coding from a phone while the operator's computer is off. That requires an always-on executor. You cannot both "code while the computer is off" and "not keep the Claude credential on an always-on machine". The price of the scenario is that the Claude credential lives on the VPS 24/7. Accepted deliberately; compensated by hardening the VPS.

## V2 only — no temporary runtime shortcuts (hard rule)
- **The target architecture is V2 ONLY:** three separate services telegram-bot / claude-worker / integrations-worker. A single-process deployment path is REJECTED.
- **No temporary single-process launch** in place of V2; no `chatbot-runtime-v1`-style scaffolding; no temporary single-process machine account.
- **Until V2 entrypoints exist, the system does NOT start.** An unimplemented component is marked "not implemented" and is NOT wired as a runnable service: its systemd files are TARGET templates, not active units.
- **Temporary shortcuts are forbidden** — except when ALL three hold: (1) explicitly approved; (2) recorded who approved it and why; (3) an expiry/cleanup condition is stated. Missing any one — do not introduce it.
- **Reason:** temporary shortcuts lose context, leave litter, and turn into permanent crutches.
- **Legacy PaaS files** (`Procfile`, `Dockerfile` -> `bot.main`) are NOT the target path; marked legacy and removed when the PaaS is retired.

## Permission policy (short)
`claude-worker` runs owner-originated coding tasks from Telegram without per-tool prompts, because coding from a phone is a product requirement. Safety comes from a fail-closed policy gate (`bot/claude_policy.validate_task_execution_policy`): a durable queue + an owner-only route + an allowlisted project alias + a known mode (ask/fix/continue) + the claude-worker service identity. Dangerous operations (external-integration write-back, secret rotation, systemctl, push to main, force-push, destructive delete, changing the Claude credential) are confirmed separately — see `docs/security/approval-policy.md`. A per-tool gating mode is available as an option but is not the default.
