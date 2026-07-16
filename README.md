# Personal AI OS - a single-user, security-first agentic coding system

> **How this repository was made.** This public copy - the sanitized extraction from my
> private repository, the documentation, and its verification - was done end to end by
> Claude Code based on my directions.

## 1. What this is

This is a reference architecture for a **single-user personal AI OS**: an always-on service running on a dedicated server that lets one person drive an agentic coding workflow entirely from a phone. You send a task through a chat channel; the system runs a coding agent on the server, edits code, commits, and pushes to a working branch - even when your personal computer is turned off. The design is **security-first**: secrets never live in code or chat, the system is split across three services that do not trust each other with the same credentials, and the coding agent's irreversible tool calls (pushing to `main`, deleting data, download-and-execute, sending data out) are gated behind an explicit Allow/Deny prompt sent to your phone — a per-tool, fail-closed gate that ships enabled by default. This repository publishes the architecture, policies, and a runnable demo domain - not a turnkey deployment of one person's private setup. **§2 maps which parts ship as running code, which are flag-gated in the demo, and which are documented targets; check it before assuming a control is active.**

---

## 2. What runs where

The system is a mix of code you can run and documented design that a real deployment provisions out-of-band (server accounts, secret-manager machine tokens, host permissions). The table below marks which is which.

| Capability | Status | Notes |
|---|---|---|
| Telegram transport service (owner check, enqueue) | shipped-as-code | `bot/telegram_bot.py`; runs the keyless transport tier |
| Durable task queue | shipped-as-code | `bot/task_queue.py` |
| Coding worker (agentic coding on the server) | shipped-as-code | `bot/claude_worker.py`; drives a coding agent |
| Integrations worker (decrypt + send replies, external services) | shipped-as-code | `bot/integrations_worker.py`: `run_loop()` is inert on its own, but the shipped entry point `main()` (and the systemd unit) wires in a **live** Notion/Todoist applier that makes real network calls |
| Envelope-encrypted SQLite context store | shipped-as-code, **flag-gated** | at-rest encryption is gated by `AIOS_CONTEXT_ENCRYPTION` (code default `0` so the tree imports and the test suite runs without provisioning production keys; **the production deployment runs with it on** — every domain that stores personal free text writes encrypted). The keyed worker's queue applier is fail-closed: with the flag off it refuses store operations rather than serving them keyless — so the notes round-trip (§5) needs encryption enabled (production keys, or the dev test-KEK documented in `.env.example`). See §4 |
| Allow/Deny approval gate from the phone | shipped-as-code, **enabled by default** | a per-tool `can_use_tool` gate: dangerous actions wait for the operator's Allow/Deny on the phone (5-minute timeout = safe deny), routine coding is not interrupted by design (the classifier errs conservative — a command that merely mentions `rm` can trip a prompt). Fail-closed; kill-switch `AIOS_TOOL_APPROVALS=0`. See §4 |
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
- **Envelope-encrypted SQLite context store (on in production, flag-gated in this tree).** At-rest encryption (per-row data key wrapped by a master key; only the keyed worker holds the key) is gated by `AIOS_CONTEXT_ENCRYPTION`. The code default is `0` so the tree imports and the test suite runs without provisioning production keys (encryption-aware tests inject a throwaway dev KEK). **In the production deployment the flag is on and every domain that stores personal free text writes encrypted**; the keyed worker's queue applier is fail-closed and refuses store operations when the flag is off — so a mis-deployed worker cannot silently fall back to cleartext, and the notes round-trip (§5) needs encryption enabled to complete. Turn the flag on and provision the keys (production) or the documented dev test-KEK to exercise the store.
- **Log redaction.** Sensitive values are redacted before anything reaches logs.
- **Durable queues.** Tasks and pending actions survive restarts so a crash does not silently drop work.
- **Allow/Deny from the phone (enabled by default, fail-closed).** Irreversible or security-sensitive actions - force-push, push to `main`, deleting files/branches/data, destructive reset, rewriting history, download-and-execute from the internet, package installs, outbound data sends, touching secrets/auth/deploy config or the guards themselves - wait for an explicit Allow on the operator's phone, while routine coding (edits, commits, pushes to a working branch, running tests) is not interrupted by design (the classifier errs conservative — a command that merely mentions `rm` can trip a prompt). Mechanism: the coding agent runs with a per-tool `can_use_tool` callback; a pure policy function (`bot/claude_policy.approval_reason`) classifies each tool call, dangerous ones go onto the durable approval queue, and the operator gets a card with Allow/Deny buttons. No answer within 5 minutes = a safe deny; a policy error = deny; an SDK without callback support = a loud task failure, never a silent pass. Kill-switch: `AIOS_TOOL_APPROVALS=0` restores the no-confirm mode. Honest boundary: the classifier is a regex speed bump against accidental self-harm and the obviously dangerous, not a hermetic anti-exfiltration barrier - the first line of defence remains the operator-only task route. See `docs/security/approval-policy.md`.
- **Auto-restart with a compile gate.** Services restart on failure, but a restart only takes new code that byte-compiles, so a bad edit cannot wedge the system into a crash loop of broken code.

For the full threat model and current boundaries, see `docs/security/`.

---

## 5. The demo `notes` domain

Every "domain" in this architecture is a self-contained feature the operator drives by chat. The published example is a plain **notes** domain (`bot/aios_notes_store.py`): capture a note, list notes, delete a note. It exercises the same machinery as any real domain - enqueue, worker, context store, replies - without depending on any private external account. Use it to see the control flow end to end and as the template when adding your own domain. Note: the store is fail-closed, so the round-trip through the keyed worker requires encryption enabled — provision the production keys or the dev test-KEK (`AIOS_CONTEXT_ALLOW_TEST_KEK`, see `.env.example` and `bot/context_key.py`) before driving it; the shipped code default leaves encryption off.

---

## 6. Architecture diagrams

**Components and trust boundaries** - the three services, the shared queue, and what each OS user can and cannot read.

![Components and trust boundaries](diagrams/components-trust-boundaries.png)

**Agentic task path** - the life of one coding task, from a chat message on a phone to the service restarting itself on the new code.

![Agentic task path](diagrams/agentic-task-path.png)

**Secret path** - how each service gets only its own secrets, by name, from the secret manager.

![Secret path](diagrams/secret-path.png)

**Encryption envelope** - how personal free text is stored encrypted at rest (per-row data key wrapped by a master key). On in the production deployment, flag-gated in the demo; see §4.

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
