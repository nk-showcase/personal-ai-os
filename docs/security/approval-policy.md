# Approval policy

Principle: do not interrupt the operator during routine coding; ask for confirmation only where an
action can do irreversible harm.

## Without confirmation (routine coding flow)
Routine coding operations run without the operator's involvement — for example code edits, commits,
pushes to a working (non-main) branch, creating a branch, running tests, and ordinary fetch/pull of
the working state (without a destructive reset). The intent is deliberate: coding from a phone must
not turn into an endless stream of confirmations.

## Requires confirmation
Destructive and security-sensitive operations proceed only after an explicit Allow.

| Action | Mechanism |
|---|---|
| Sending a message | `CONFIRM_SEND` (after preview) |
| Push directly into `main` | confirmation; disabled by default |
| Force-push | confirmation; blocked by a guard by default |
| Deleting files / branches / data | confirmation |
| Destructive reset of the working tree | confirmation |
| Rewriting git history | confirmation |
| Changing secrets / auth / security / deploy config | confirmation (or review mode) |
| Disabling security guards / hooks | confirmation |
| Changing secret-manager rules | confirmation |

## Per-tool confirmations for operator Telegram tasks
- Routine coding tasks started by the operator through the Telegram chatbot run without per-tool
  confirmations. This is **deliberate** — coding from a phone should not be an endless stream of
  "Allow/Deny."
- It is safe because the path is constrained by a fail-closed gate
  (`bot/claude_policy.validate_task_execution_policy`): a durable queue + an operator-only Telegram
  route + an allowlisted project alias (`BRIDGE_PROJECTS`) + a known mode (unknown mode fails) +
  the claude-worker service (not the telegram-bot). An arbitrary repository path from the queue is
  not accepted — the repo is derived from the alias.
- It is controlled by an explicit setting `AIOS_CLAUDE_PERMISSION_MODE`. Per-tool gating is an
  **optional mode**, not the default.
- An approval queue (`task_queue` approvals / `make_queue_approver`) exists, but is not used for the
  routine coding path — by design. Dangerous operations from the table above are confirmed by their
  own mechanisms (CONFIRM_SEND, guards), not by per-tool gating.
