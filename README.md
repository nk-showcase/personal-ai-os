# Personal AI OS - a single-user, security-first agentic coding system

> **How this repository was made.** This public copy - the sanitized extraction from my
> private repository, the documentation, and its verification - was done end to end by
> Claude Code based on my directions.

## 1. What this is

This is a reference architecture for a **single-user personal AI OS**: an always-on service running on a dedicated server that lets one person drive an agentic coding workflow entirely from a phone. You send a task through a chat channel; the system runs a coding agent on the server, edits code, commits, and pushes to a working branch - even when your personal computer is turned off. The design is **security-first**: secrets never live in code or chat, the system is split across three services that do not trust each other with the same credentials, and the target design gates any irreversible action (sending a message, pushing to `main`, deleting data) behind an explicit Allow/Deny prompt sent to your phone. This repository publishes the architecture, policies, and a runnable demo domain - not a turnkey deployment of one person's private setup. **§2 maps which parts ship as running code and which are documented targets or off by default; check it before assuming a control is active.**

---

## 2. What runs where

The system is a mix of code you can run and documented design that a real deployment provisions out-of-band (server accounts, secret-manager machine tokens, host permissions). The table below marks which is which.

| Capability | Status | Notes |
|---|---|---|
| Telegram transport service (owner check, enqueue) | shipped-as-code | `bot/telegram_bot.py`; runs the keyless transport tier |
| Durable task queue | shipped-as-code | `bot/task_queue.py` |
| Coding worker (agentic coding on the server) | shipped-as-code | `bot/claude_worker.py`; drives a coding agent |
| Integrations worker (decrypt + send replies, external services) | shipped-as-code | `bot/integrations_worker.py`: `run_loop()` is inert on its own, but the shipped entry point `main()` (and the systemd unit) wires in a **live** Notion/Todoist applier that makes real network calls |
| Envelope-encrypted SQLite context store | shipped-as-code, **off by default** | at-rest encryption exists in code but is gated by `AIOS_CONTEXT_ENCRYPTION` (default `0`); with the flag off, note content is stored as cleartext. See §4 |
| Allow/Deny approval gate from the phone | partly-coded, **off by default** | the preflight gate and an approval queue exist in code, but the coding worker ships with `bypassPermissions` (no per-tool prompt fires) and the approval queue is not yet wired to the executor. See §4 |
| Demo `notes` domain (runnable example) | shipped-as-code | `bot/aios_notes_store.py` - see §5 |
| Auto-restart with a compile gate | shipped-as-code | `bot/auto_restart.py`, wired into the worker loops; a restart takes new code only if it byte-compiles. The systemd units in `systemd/` are deployment templates |
| `/bridge` on-server coding agent internals | documented-only | the phone-driven bridge is **described, not one-click-runnable** - it depends on a coding-agent CLI, a provisioned server account, and host wiring that live outside this repo |
| Secret manager, machine tokens, server user accounts | documented-only | provisioned out-of-band; see `docs/security/` |

**Read this as:** the control flow, policies, and the demo domain are real code; the parts that touch live external accounts and privileged host setup are documented targets, not a script you run once.

---

## 3. Three-service zero-trust split

The system never runs as one privileged process. It is split into three services under **three separate operating-system user accounts**, so a weakness in one cannot reach another's secrets.

1. **Keyless transport** (`telegram-bot`, user `aios-bot`)
   Receives messages from the chat channel, checks the sender is the owner, and puts a task on the queue. It holds **only** the chat-channel token - no coding-agent credentials, no git push key, no integration secrets. Compromising transport yields nothing but the ability to enqueue.

2. **Keyed integrations worker** (`integrations-worker`, user `aios-integrations`)
   The service that holds integration secrets. On the store-view path shipped here it **decrypts the encrypted context store itself and sends the reply to the chat itself** (using its own chat-channel token), so the transport never sees decrypted content or integration keys. Because it sends replies, its allowlist includes the chat-channel token in addition to the integration keys - by design for this path. Mail is a documented future integration (no mail code ships here); the safety policy it must satisfy - read-only first, sending only behind preview + explicit confirm, one-time codes never shown in chat - is written down ahead of the code in `docs/security/mail-integration-policy.md`. The worker runs under its own OS user, so its on-disk secrets are unreadable by the coding worker.

3. **Coding worker** (`claude-worker`, user `ai-os`)
   Runs the coding agent on the server: edits code, commits, pushes to a working branch. Its active coding-agent session file (`~/.claude/.credentials.json`) is **local-only on the server disk** - never in git or a prompt. The one-time sign-in token used to establish that session is held in the secret manager, scoped to this service. It holds no integration keys and no chat-channel token.

The OS-user boundary is a hard precondition, not a fallback: if the separate integration account does not exist, the integrations unit refuses to start rather than silently sharing the coding worker's account.

---

## 4. Security model (summary)

- **Secret manager + per-service allowlist.** In the production backend, real secret values live only in a machine-account secret manager (Bitwarden Secrets Manager as the primary choice), where each service gets a read-only machine token scoped to exactly one project, so a leak's blast radius is one folder. The backend is selectable and **defaults to `env`** (a local `.env` file) for dev/test; switch it to Bitwarden for production. Either way the agent and workers operate on secret **names** (`config/secrets-map.yaml`), never hard-coded values.
- **Secrets lock.** Log redaction is active in every service - a filter scrubs secret-shaped values before anything reaches logs. A value-free secret-shape scanner (`scripts/aios_log_scan.py`) ships for the operator to run over logs/journald output. An automatic pre-commit / pre-prompt secret scan is a documented target, not wired in this build.
- **Envelope-encrypted SQLite context store (off by default).** The at-rest encryption path exists in code (per-row data key wrapped by a master key; only the keyed worker holds the key). It is gated by `AIOS_CONTEXT_ENCRYPTION` and ships **disabled** — with the flag off, note content is written as cleartext. Turn the flag on and provision the keys to get encryption at rest.
- **Log redaction.** Sensitive values are redacted before anything reaches logs.
- **Durable queues.** Tasks and pending actions survive restarts so a crash does not silently drop work.
- **Allow/Deny from the phone (off by default in the shipped config).** The intent: irreversible or security-sensitive actions - sending a message, force-push, push to `main`, deleting files/branches/data, destructive reset, rewriting history, changing secrets/auth/deploy config, disabling guards - proceed only after an explicit Allow, while routine coding (edits, commits, pushes to a working branch, running tests) is never interrupted. The preflight gate and an approval queue are coded, but the coding worker ships with `bypassPermissions` (no per-tool prompt fires) and the approval queue is not yet wired to the executor; enabling this is a documented target. See `docs/security/approval-policy.md`.
- **Auto-restart with a compile gate.** Services restart on failure, but a restart only takes new code that byte-compiles, so a bad edit cannot wedge the system into a crash loop of broken code.

For the full threat model and current boundaries, see `docs/security/`.

---

## 5. The demo `notes` domain

Every "domain" in this architecture is a self-contained feature the operator drives by chat. The published, runnable example is a plain **notes** domain (`bot/aios_notes_store.py`): capture a note, list notes, delete a note. It exercises the same machinery as any real domain - enqueue, worker, context store, replies - without depending on any private external account. Use it to see the control flow end to end and as the template when adding your own domain.

---

## 6. Architecture diagrams

**Components and trust boundaries** - the three services, the shared queue, and what each OS user can and cannot read.

![Components and trust boundaries](diagrams/components-trust-boundaries.png)

**Agentic task path** - the life of one coding task, from a chat message on a phone to the service restarting itself on the new code.

![Agentic task path](diagrams/agentic-task-path.png)

**Secret path** - how each service gets only its own secrets, by name, from the secret manager.

![Secret path](diagrams/secret-path.png)

**Encryption envelope** - how personal free text is stored encrypted at rest (per-row data key wrapped by a master key). Off by default; see §4.

![Encryption envelope](diagrams/encryption-envelope.png)

The editable sources (`.drawio`) and a written description of each diagram are in [`diagrams/`](diagrams/).

---

## 7. Setup

Setup is documented rather than fully automated, because a real deployment provisions server accounts, secret-manager machine tokens, and host permissions that cannot live in a public repo.

- Run `scripts/check_setup.sh` to verify local prerequisites.
- Follow `docs/security/` for the secret manager, service-user split, and approval policy.
- Follow `docs/vps/` and `systemd/` for the always-on service units.

---

## 8. Runnable demo and scope

A **minimal runnable demo** repository accompanies this reference architecture - it stands up the transport, queue, worker, and the `notes` domain locally so you can watch the flow work without provisioning a server or any external accounts. See the demo repo link in the project description.

> **This is a reference architecture, not a one-click-run product.** It documents the design, policies, and control flow of a real single-user system, plus a runnable demo domain. The parts that touch live external accounts and privileged host setup are documented targets you adapt to your own environment. It contains no real hostnames, secrets, or personal data.
