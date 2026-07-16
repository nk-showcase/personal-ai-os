# Technical guards

These are technical barriers, not "agreements". Whatever is forbidden must be blocked by code.

## Implemented guards (present as runnable code in this tree)

| Guard | What it does | Where it lives |
|---|---|---|
| per-tool approval gate | Dangerous/irreversible/outbound tool calls of the coding agent wait for the operator's Allow/Deny on the phone (fail-closed, 5-min timeout = deny, kill-switch `AIOS_TOOL_APPROVALS=0`); routine coding is not interrupted by design | `bot/claude_policy.approval_reason` + `bot/claude_bridge_worker.make_gated_can_use_tool`, enabled by default |
| log redaction | Scrubs secret-shaped substrings from every log record before it is written | `bot/log_redaction.py` (`install_log_redaction()` in each service) |
| per-service secret allow-list | A service that requests a secret outside its allow-list is denied (activated via `AIOS_SERVICE_NAME`) | `bot/secrets_loader.py` |

## Design targets (NOT present as runnable code in this tree)

The guards below are part of the target design but do **not** ship here. Do not read them as active protections.

| Guard | What it would do | Status |
|---|---|---|
| email-sensitive-filter | Mark OTP / banking / recovery / reset messages as sensitive and hide their body | No mail code ships (see SECURITY.md §6) |
| confirm-before-email-send | Block sending without preview + explicit confirmation (`CONFIRM_SEND`) | Documented target only; no such check in the code |
| cache-fallback-warning | Warn the operator when running on the last-known-good cache and enter degraded mode if it is stale | Not built — the loader only logs a cache-read fallback line; there is no operator warning, staleness check, or degraded mode |

## Design principle: intentional duplication

Critical prohibitions (pushing to the main branch, force-push, deleting data, printing secret values) are meant to be enforced in two independent places, so that bypassing one layer does not open the action. One runnable layer ships: the per-tool approval gate intercepts those actions at the coding agent's tool boundary. The second, independent layer — the git and assistant-side hooks below — is provisioned per deployment.

> Boundary: the enforcement hooks named above — the assistant-level hooks (secret-scan-before-output, block-secrets-in-prompts, block-destructive-delete, block-direct-push-to-main, block-print-secrets) and the git `pre-push` hooks (block-force-push, block-direct-push-to-main) — are part of the target design but are not included as runnable code in this reference tree: the project hooks directory (`claude/hooks/`) ships empty. Note also the two documented boundaries of the shipped gate (it does not gate edits to its own policy module, nor the coding-agent credential file). See `docs/security/threat-model.md`.

> One hook file *does* ship: `.claude/hooks/security_guard.py` — an **advisory, fail-open** PreToolUse *context-injector* (it pushes SECURITY.md into the model's context before an edit; it never blocks a tool call and never enforces anything). It is **not wired** in this published tree — the `.claude/settings.json` that would register it is provisioned per deployment and is not included — so as published it is inert. It is not one of the enforcement guards above.
