# Threat Model & Boundaries

This document describes the threat model of a **single-user** personal AI operating
system: a set of Telegram-driven services that let one person queue coding tasks, run an
autonomous coding agent, and store personal context in an encrypted-at-rest store.

It is written for a reference architecture. It names, in plain language, what the system
defends against, what it deliberately does not defend against, and where the current
implementation stops short of the target design. The goal is honesty: every boundary
below is stated factually, not whitewashed. Where a boundary reflects an unbuilt or
partial mitigation, that is said outright so a reader can judge the residual risk before
adopting the pattern.

---

## 1. Trust model

The system is built for **one operator** who owns the deployment end to end: the phone
that sends commands, the server that runs the services, the Git repositories that carry
task and context data, and the secret store. There are no other human users, no
multi-tenant separation, and no adversarial insiders in the design. This single-user
assumption is load-bearing: several "no confirmation" paths below are safe only because
the operator is the sole party who can reach them.

The strong adversary this model cares about is anyone who gains **read access to
durable artifacts** — Git history, log files, backups, the secret store, or a snapshot
of a service host — without the operator's involvement.

### Assets

| Asset | Why it matters |
|---|---|
| Secret material | Telegram bot token, LLM API key, Git write token, integration credentials, and the age recovery/store private keys. Compromise gives an attacker the operator's identity or read access to stored content. |
| Personal context | Task text, dialogue transcripts, and notes the operator dictates. Sensitive because it is free-form personal data. |
| Coding-agent capability | The autonomous agent can edit code, commit, and push in the operator's name. Misdirection means unwanted changes shipped as the operator. |
| Operator identity on Telegram | Only the operator's numeric id may drive the system; the id is the authorization boundary for every privileged action. |

### Trust zones

The architecture splits the running system into zones with different privilege so that a
compromise of a lower-trust process does not immediately yield secrets or plaintext.

| Zone | Holds keys? | Holds plaintext? | Role |
|---|---|---|---|
| **Receiver / transport** | No | In transit only | Accepts operator messages from Telegram, routes them, and relays replies. Designed to be keyless. |
| **Keyed worker** | Yes | Yes | Decrypts stored context, runs the coding agent, and is the only process that sends personal replies directly to Telegram. |
| **Durable queues (Git / on-disk)** | No | See §4 | Carry tasks and replies between zones at rest. |
| **Secret store** | — | — | Source of truth for credentials; each service is limited to an allow-listed subset. |

The intended invariant is that the receiver/transport zone holds **neither the
decryption key nor durable plaintext**, so that reading its state or logs does not expose
personal content. Section 4 and Section 6 record where that invariant is currently only
partial.

---

## 2. What the system defends against

These are the properties the reference code actually enforces:

- **Operator-only authorization.** Every privileged command is gated on the operator's
  numeric Telegram id. Unknown senders are rejected before any action.
- **Least-privilege secrets.** Each service resolves secrets through a central loader
  that enforces a per-service allow-list; a service that requests a secret outside its
  set is denied. Services without keys tolerate a `SecretAccessDenied` result by design
  instead of crashing.
- **Fail-closed key handling.** Sealing and store operations fail closed: on a decryption
  or key error the system does not execute, does not delete, and surfaces the error rather
  than falling through to a plaintext path.
- **Secret redaction in logs and outbound text.** A shared redactor scrubs token-shaped
  strings (LLM keys, Git tokens, bot tokens) from log lines and from task representations
  before they are written or committed, so a dictated secret does not land in Git or logs
  verbatim.
- **Confirmation on irreversible actions.** Message sending, pushes to the main branch,
  force-pushes, deletions, destructive resets, history rewrites, and changes to
  secrets/security config each require an explicit confirmation or are blocked by default
  (see `docs/security/approval-policy.md`).
- **Structured-only persistence by default.** Raw free text is not written to the notes
  store unless the operator explicitly opts in; structural fields (numbers, enums, tags)
  are written, free text is dropped (see `docs/security/memory-safety.md`).

---

## 3. Approval and the no-confirmation coding path

This is the system's central security trade-off and deserves a plain statement.

**By default, the coding agent runs without per-tool Allow/Deny prompts.** The permission
mode resolves to `bypassPermissions` when unset (`bot/claude_policy.py:19`,
`resolve_claude_permission_mode` at `bot/claude_policy.py:26`), and the bridge runs the
agent in that mode (`bot/claude_bridge_worker.py:505-512`). This is deliberate: driving a
coding agent from a phone must not become an endless stream of confirmations for every
file edit.

Instead of per-tool prompts, the routine coding path is constrained by a **fail-closed
policy gate** (`bot/claude_policy.validate_task_execution_policy`), which requires all of:

- a durable queue as the task source,
- an operator-only Telegram route,
- an allow-listed project alias (the repository is derived from the alias, never taken
  as an arbitrary path from the queue),
- a known permission mode (an unknown mode fails), and
- execution on the dedicated coding-worker service, not the receiver.

Dangerous operations are still confirmed by their own mechanisms (send-preview
confirmation, push/force-push guards), not by per-tool gating.

**Boundary — per-tool approval is skipped by default.** Per-tool Allow/Deny is an
**optional, non-default mode** controlled by `AIOS_CLAUDE_PERMISSION_MODE`. A per-tool
approval queue exists in the codebase (`make_queue_approver`,
`bot/claude_worker_runner.py:64`) but is **not wired into the default executor**; in the
default configuration the agent's own tool calls are not individually confirmed. When that
approver *is* used, an approval **timeout resolves to a safe deny** (returns `False` and
closes the approval row, `bot/claude_worker_runner.py:86-95`), and a valid operator
fix/edit/continue task otherwise runs in write mode without an interactive prompt. Adopters
who do not accept the single-user trust assumption should enable the gating mode and
wire the approver into the executor. (Exact skip conditions and bypass steps are
intentionally not published.)

---

## 4. Data-at-rest boundaries

### 4.1 Legacy plaintext in Git — real and not yet closed

Task and dialogue bodies still travel **in plaintext inside Git-committed files**. Only
the file **names** and **commit messages** are neutralized (see
`docs/security/memory-safety.md`, Block -1A); the body itself remains cleartext in the
file until an encrypted body format lands. Scrubbing old Git history and encrypting the
body are **deferred** (Block -1B) and are not implemented in this reference tree.

**Consequence:** anyone with read access to a repository that carries these files can read
the queued task and dialogue text. A secret-pattern scrubber runs before write, so a
dictated *credential* does not land verbatim, but ordinary free text does.

### 4.2 Laptop-task handoff body is plaintext

For the phone-to-computer task handoff, the task body is written as **plaintext** inside
`_pc_tasks/<id>.md` in the inbox repository; only the file name and commit message are
neutral ids (`bot/pc_tasks.py`). A secret-pattern redactor (`_redact`) runs first, but the
free-text command still lands in Git history in cleartext. Encrypting the file **body** is
a deployment option, **not implemented** in the reference code. Anyone with read access to
the inbox repository can read queued task text.

### 4.3 Reply payload is not encrypted at rest

The reply payload that transits the reply queue is **not encrypted at rest**. Combined
with §4.4, this means personal content is briefly present in cleartext in the transport
zone in the default configuration. This is a documented boundary of the current transport
design, not a solved property.

### 4.4 Gate-0 leak surface — keyless receiver briefly handles personal content

The setting that makes the keyed worker send personal views **directly** to Telegram
(`worker_sends.views`) defaults to **`off`** (`bot/aios_config.py:105-106`,
`get_worker_sends_views` at line 109). While it is off, decrypted personal views **transit
the reply queue to the keyless receiver**, which momentarily handles personal content on
its way to Telegram. The "receiver holds no plaintext" invariant (§1) is therefore only
fully true once the operator flips this setting **on**. Until then the leak surface — a
keyless process momentarily in contact with personal content — exists in the default state.

---

## 5. Queue semantics and idempotency

The task/reply transport is an **at-least-once** channel. Two boundaries follow.

### 5.1 Write duplicate-apply depends on stable business keys

On a `WRITE_ACCEPTED` result the caller must **not** blindly retry. A retry issued with a
**fresh** business key / correlation id creates a **second** queue row, and the worker
applies the write **twice** (double apply). Idempotency depends entirely on callers
passing a **stable** `business_key` so a retried logical write reuses the same row
(`bot/integrations_proxy.py:234-237`). This is an idempotency contract, not an automatic
guarantee: a caller that violates it can cause duplicate side effects.

### 5.2 A due row whose kind has no registered handler is dropped

The scheduler claims a due batch atomically and dispatches each row to a handler keyed by
its `kind`. A due row whose `kind` has **no registered handler is dropped** — no per-kind
error is surfaced and the row is not retried (`bot/schedule_jobs.py`, HANDLERS registry).
**Consequence:** a mis-seeded or unregistered scheduled kind is silently lost rather than
recovered. This is an operational gap: registering a handler for every seeded kind is a
requirement the deployment must uphold, not a property the code enforces at seed time.

---

## 6. Transport-thinning plan is a plan, not an implementation

The design to thin the transport process so it holds neither keys nor plaintext (see
`docs/architecture/s6-thin-transport-plan.md`) is a **plan, not yet implemented**. The
following prerequisites are **unbuilt**:

- inbound-plaintext encryption (so inbound task text is not persisted in cleartext),
- per-service `get_secret` enforcement at the transport,
- the worker scheduler and durable due-timestamp store,
- the fail-closed startup self-check.

Until these land, the transport process resolves integration/LLM/Git keys as an **import
side-effect at startup** and persists inbound task text in **cleartext**. The intended
"transport holds neither the decryption key nor plaintext" boundary is therefore currently
**nominal**, not achieved. These are stated as requirements the design must satisfy, not as
properties already in place.

---

## 7. Guard coverage gaps

The design calls for **intentional duplication** of critical prohibitions (blocking pushes
to the main branch, force-push, and printing secret values) in two independent layers, so
that bypassing one layer does not open the action.

**Boundary — the duplication is partial in this reference tree.** Only the **worker-side
Python guards** actually ship as runnable code: `email-sensitive-filter`,
`confirm-before-email-send`, and `cache-fallback-warning` (see
`docs/security/guards-and-hooks.md`). The **assistant-level Claude Code hooks**
(secret-scan-before-output, block-secrets-in-prompts, block-destructive-delete,
block-direct-push-to-main, block-print-secrets) and the **git `pre-push` hooks**
(block-force-push, block-direct-push-to-main) are described in the design but are **not
present as runnable code** — the hooks directory (`claude/hooks/`) ships empty (only a
`.gitkeep`). For the critical prohibitions, the "two independent places" property is
therefore **aspirational** in this tree: a deployment must provision the assistant-side and
git-level layers itself to achieve it.

---

## 8. Secret-store boundaries

- **Unused-but-allow-listed secret names (least-privilege residue).** The integrations
  worker's allow-list still contains legacy/alternate mailbox-secret **names** that no
  surviving code path uses (`bot/secrets_loader.py`). They are retained only because the
  secret-map config and a drift-check test compare against this exact set. This is a
  factual current-state boundary: names remain allow-listed beyond what the running code
  needs, which is least-privilege residue rather than an active exposure.
- **Mailbox send is not implemented.** A mailbox **send** token is present in the
  allow-list, but sending is **not implemented** (a later phase). A send would go through a
  preview-and-confirm mechanism; that mechanism is a documented pending boundary, not
  present here.
- **Config discrepancies between design and runnable code.** In this reference tree the
  V2 service entrypoints are **target templates, not runnable services**; the shipped
  service units do not read an environment file, so some documented environment variables
  are not picked up as written; and there are name-level mismatches between the design
  text and the map (a Git write-token name, mailbox app-password vs. token names, and which
  worker an LLM key is attributed to). These are unreconciled gaps between the documented
  design and the code as it stands, not verified working configuration.

---

## 9. Explicit non-goals

This model does **not** attempt to defend against:

- a compromised operator device or operator account (the operator is fully trusted),
- a compromised keyed-worker host (that host legitimately holds keys and plaintext),
- multi-user isolation of any kind (there is one user by design),
- confidentiality of task/dialogue bodies already written to Git in cleartext under the
  current boundary (§4.1, §4.2), pending the deferred encrypted-body work.

---

## 10. Summary of current boundaries

| # | Boundary | Status |
|---|---|---|
| 3 | Per-tool Allow/Deny skipped by default for operator coding tasks; approver exists but is unwired | By design; gating is optional |
| 4.1 | Task/dialogue bodies plaintext in Git; only names/commits neutralized | Deferred (Block -1B) |
| 4.2 | Laptop-task handoff body plaintext in inbox repo | Body encryption not implemented |
| 4.3 | Reply payload not encrypted at rest | Current transport boundary |
| 4.4 | Keyless receiver briefly handles personal content while `worker_sends.views` is off (default) | Leak surface in default state |
| 5.1 | Write double-apply if caller retries without a stable business key | Idempotency contract on callers |
| 5.2 | Due row with unregistered kind is dropped, not retried | Operational gap |
| 6 | Thin-transport isolation is planned, not built; transport resolves keys at import and persists inbound text plaintext | Nominal, not achieved |
| 7 | Assistant-side and git-level guards not present as runnable code (hooks dir empty) | Partial duplication |
| 8 | Unused allow-listed secret names; mailbox send unimplemented; design/code config discrepancies | Residue / pending / unreconciled |
