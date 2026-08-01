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

## 3. Approval: routine coding uninterrupted, dangerous tool calls gated

This is the system's central security trade-off and deserves a plain statement.

**Routine coding runs without per-tool prompts; dangerous tool calls wait for the
operator's Allow/Deny.** The per-tool gate is **enabled by default**
(`bot/claude_policy.tool_approvals_enabled`): with the gate on, the permission mode
resolves to `default` and the agent runs with a `can_use_tool` callback
(`bot/claude_bridge_worker.make_gated_can_use_tool`). A pure policy function
(`bot/claude_policy.approval_reason`) classifies each tool call: routine work (edits,
commits, pushes to a working branch, tests, ordinary git) is allowed instantly — driving
a coding agent from a phone must not become an endless stream of confirmations — while
dangerous/irreversible/outbound actions (force-push, push to main, deletions, destructive
reset, history rewrites, service control, download-and-execute, package installs, outbound
data sends, touching secrets/security/deploy files) are placed on the durable approval
queue (`make_queue_approver`, `bot/claude_worker_runner.py`) and wait for the operator's
decision on the phone.

The gate is **fail-closed** at every joint: a **timeout resolves to a safe deny** (returns
`False` and closes the approval row), an error inside the policy function resolves to
deny, and an SDK without `can_use_tool` support fails the task loudly instead of running
ungated. The kill-switch `AIOS_TOOL_APPROVALS=0` restores the old no-confirm
`bypassPermissions` mode; an explicit `AIOS_CLAUDE_PERMISSION_MODE` always wins (note that
an explicit `bypassPermissions` also disables the gate — the SDK does not call the
callback in bypass mode).

Independently of the per-tool gate, the coding path is constrained by a **fail-closed
policy gate** (`bot/claude_policy.validate_task_execution_policy`), which requires all of:

- a durable queue as the task source,
- an operator-only Telegram route,
- an allow-listed project alias (the repository is derived from the alias, never taken
  as an arbitrary path from the queue),
- a known execution mode (an unknown mode fails), and
- execution on the dedicated coding-worker service, not the receiver.

**Boundary — the classifier is a speed bump, not a hermetic barrier.** `approval_reason`
is a regex filter over the tool name and input. It catches accidental self-harm and the
obviously dangerous; it does **not** catch deliberate or injected exfiltration through an
arbitrary network client (a GET with a secret in the query string, a python socket, an
encoded nonstandard channel), and it does not claim to.

Two self-protection cases are on the sensitive-path list, so they *are* gated (verified by
`scripts/test_tool_approval_gate.py`):

- **The gate protects its own modules.** Editing `bot/claude_policy.py`,
  `claude_bridge_worker.py`, `claude_worker.py`, or `claude_worker_runner.py` requires an
  Allow/Deny, so an injected task cannot silently rewrite `approval_reason` to always-allow and
  ride the auto-deploy loop (routine commit/push → `git pull --ff-only` → self-restart) into the
  running worker. The model cannot skip the callback at runtime (the gate lives in the SDK's
  control protocol) *and* cannot quietly edit the code the callback consults.
- **The gate covers the coding-agent credential file.** Reading or copying
  `~/.claude/.credentials.json` (the coder's login) is gated.

The honest residual limit stands: a regex over the command string is still a speed bump, not a
hermetic barrier — a determined injection can encode a payload into a form the pattern does not
recognise. The real first line of defence remains the operator-only task route and the
single-user trust assumption, backed (per deployment) by the independent git-level and
assistant-level hook layers.

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
with §4.5, this means personal content is briefly present in cleartext in the transport
zone in the default configuration. This is a documented boundary of the current transport
design, not a solved property.

### 4.4 Framework conversation state is encrypted at rest

The Telegram framework keeps its own conversation state (`chat_data`, `user_data`,
`bot_data`, callback data, conversation states) in a pickle file so an auto-restart does
not drop an in-flight dialogue. That file is written by the framework, not by the storage
layer, so it used to bypass the at-rest cipher and hold message text in cleartext.
`bot/ptb_state_cipher.EncryptedPicklePersistence` encrypts it: the framework's own picklers
and protocol are reused unchanged, and the resulting bytes go through the same
single-encryptor chokepoint as every other store (scheme-3 AEAD). The three-state contract
is the system-wide one — flag off writes a plain pickle, flag on with the key writes an
envelope, flag on without a key raises and writes nothing (no partial or temp file is left
behind). A cleartext file from an earlier deployment is read once and re-encrypted **in the
same load call**, so the migration window does not extend to the next update; with the flag
on and no key that migration raises instead of running half-protected. Covered by
`scripts/test_ptb_state_encryption.py` (mandatory in CI — the suite fails rather than skips
when the runtime dependencies are absent).

Limits of this boundary, stated rather than implied:

- **AAD binds the absolute path**, so an envelope cannot be decrypted from a different
  location, including a same-named file in another directory. It does **not** carry a
  monotonic version: an attacker with write access to the 0700 state directory can restore
  an **older envelope of the same path** and it will authenticate. That is rollback of the
  operator's own dialogue state, exposing no new plaintext; it is out of scope here.
- **Key loss is a startup failure, not silent data loss.** If the KEK becomes unavailable or
  is genuinely rotated (as opposed to resealed to the same key material), the existing state
  file cannot be decrypted, the transport fails to initialise, and systemd restarts it in a
  loop until the operator removes or re-provisions the state. Rotating the KEK for this file
  is not supported; delete the state file (losing only in-flight dialogue context) instead.
- **Pickle remains pickle.** Encryption authenticates the file against tampering by anyone
  without the key, but a legacy cleartext file — or any file an attacker who already holds
  the key can forge — is still deserialised by `pickle`. The 0700/0600 owner-only directory
  is the control that keeps that reachable only by the account that already runs the code.

### 4.5 Personal views are sent by the keyed worker — single route

Decrypted personal views are rendered and delivered to Telegram **directly by the keyed
worker** (`bot/aios_store_applier.py`, `store_view_applier`); they do **not** transit the
reply queue back to the keyless receiver. This is the only route — there is no
receiver-side render fallback and no config switch (a legacy `worker_sends.views` flag
from the staged cutover has been removed). When the keyed worker is unavailable, the
receiver answers with a neutral "busy, try again" message instead of falling back to a
plaintext path. Note the reply-queue boundary of §4.3 still applies to non-view replies
(task results, status lines) that the transport delivers.

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

## 6. Transport thinning is in progress, not complete

The design to thin the transport process so it holds neither keys nor plaintext (see
`docs/architecture/s6-thin-transport-plan.md`) is **partially implemented**.

Landed in this tree:

- the import-side-effect key resolution is gone — importing the transport surface resolves
  only the transport's own chat token (plus the non-secret owner id); integration/LLM/Git
  secrets are fetched lazily by the worker-side modules that use them (enforced by
  `scripts/test_s6_0a_config_boundary.py`),
- per-service allow-list enforcement in the secrets loader (activated per service via
  `AIOS_SERVICE_NAME`; simulated by `scripts/test_service_lock.py`),
- the worker scheduler and durable schedule store (`bot/schedule_*`).

Still **unbuilt**:

- inbound-plaintext encryption — inbound task text is still persisted in **cleartext** in
  the durable queue (the `store_seal` machinery ships as optional hardening for store
  payloads, but the task queue itself is not sealed),
- the fail-closed startup self-check.

The intended "transport holds neither the decryption key nor durable plaintext" boundary is
therefore **partial**: the key half largely holds; the plaintext half does not yet.

---

## 7. Guard coverage gaps

The design calls for **intentional duplication** of critical prohibitions (blocking pushes
to the main branch, force-push, and printing secret values) in two independent layers, so
that bypassing one layer does not open the action.

**Boundary — the duplication is partial in this reference tree.** One runnable layer for the
critical git prohibitions ships: the **per-tool approval gate** (§3) intercepts force-push,
push-to-main, deletions, destructive resets, and history rewrites at the coding agent's tool
boundary, enabled by default and fail-closed (within the two documented classifier gaps of
§3). The mail-related guards named in `docs/security/guards-and-hooks.md`
(`email-sensitive-filter`, `confirm-before-email-send`) do **not** ship — no mail code ships
in this tree — and `cache-fallback-warning` ships only as a plain cache-fallback log line, not
the operator-warning/degraded-mode guard the design describes. The **second, independent
layer** — the assistant-level Claude Code file hooks (secret-scan-before-output,
block-secrets-in-prompts, block-destructive-delete, block-direct-push-to-main,
block-print-secrets) and the **git `pre-push` hooks** (block-force-push,
block-direct-push-to-main) — is described in the design but **not present as runnable code**:
the hooks directory (`claude/hooks/`) ships empty (only a `.gitkeep`). For the critical
prohibitions, the "two independent places" property therefore still requires a deployment to
provision the git-level and file-hook layers itself.

---

## 8. Secret-store boundaries

- **Allow-lists are minimal and fully used.** Every name in `bot/secrets_loader.py`
  `SERVICE_ALLOWED` maps to an active code path (mirrored in `config/secrets-map.yaml` and
  drift-checked by `scripts/test_secret_policy.py`); no mailbox-secret names are granted to
  any service. See SECURITY.md §6 for the per-name justification.
- **Mailbox send is not implemented.** No mail code ships in this tree and no mail secret
  name is allow-listed. Any future send must go through a preview-and-confirm mechanism;
  that mechanism is a documented pending boundary (`docs/security/mail-integration-policy.md`),
  not present here.
- **Setup is documented, not turnkey.** The shipped systemd units read a per-service
  environment file and run the V2 entrypoints (`bot.telegram_bot`, `bot.claude_worker`,
  `bot.integrations_worker`), but they are still **templates**: server accounts, env files,
  secret-manager machine tokens, and host permissions are provisioned out-of-band, and the
  placeholders (`${AIOS_HOME}` and friends) must be substituted per deployment. Mail-related
  secret names remain documented-only (no mail code ships). Treat the tree as a reference
  architecture that requires a server and manual setup, not verified one-click configuration.

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
| 3 | Per-tool Allow/Deny gate on dangerous tool calls, enabled by default, fail-closed; classifier is a regex speed bump, not a hermetic barrier | Wired and on; boundary stated |
| 4.1 | Task/dialogue bodies plaintext in Git; only names/commits neutralized | Deferred (Block -1B) |
| 4.2 | Laptop-task handoff body plaintext in inbox repo | Body encryption not implemented |
| 4.3 | Reply payload not encrypted at rest | Current transport boundary |
| 4.4 | Framework conversation-state file encrypted at rest through the single-encryptor chokepoint; fail-closed without a key; same-path rollback and KEK-loss startup failure documented as limits | Closed, with stated limits |
| 4.5 | Personal views rendered and sent by the keyed worker only; no receiver-side fallback | Single route |
| 5.1 | Write double-apply if caller retries without a stable business key | Idempotency contract on callers |
| 5.2 | Due row with unregistered kind is dropped, not retried | Operational gap |
| 6 | Thin-transport isolation partial: import-time key resolution removed, but inbound task text still persists in cleartext | In progress |
| 7 | Assistant-side and git-level guards not present as runnable code (hooks dir empty) | Partial duplication |
| 8 | Mailbox send unimplemented (preview-confirm mechanism pending); setup is documented, not turnkey | Pending / manual setup |
