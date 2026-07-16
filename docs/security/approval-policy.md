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

| Action | Mechanism | Shipped? |
|---|---|---|
| Push directly into `main` | per-tool gate (live Allow/Deny) | yes — `approval_reason` |
| Force-push | per-tool gate (live Allow/Deny) | yes — `approval_reason` |
| Deleting files / branches / data | per-tool gate (live Allow/Deny) | yes — `approval_reason` |
| Destructive reset of the working tree | per-tool gate (live Allow/Deny) | yes — `approval_reason` |
| Rewriting git history | per-tool gate (live Allow/Deny) | yes — `approval_reason` |
| Download-and-execute / package install / outbound data send | per-tool gate (live Allow/Deny) | yes — `approval_reason` |
| Editing secrets / auth / security / deploy files | per-tool gate (live Allow/Deny) | yes — `approval_reason` |
| Sending mail via a CLI tool (sendmail/msmtp/…) | per-tool gate (live Allow/Deny) | yes — `approval_reason` |
| Sending a message via the integrations worker (Notion/Todoist/Telegram write-back) | `CONFIRM_SEND` after preview | **no — documented target**, not wired (writes execute directly); see SECURITY.md §6 |

The per-tool gate covers the **coding agent's own tool calls**. The integrations worker's outbound
write-backs are **not** behind the gate today (they execute directly once enqueued); a preview +
`CONFIRM_SEND` step in front of them is a documented target, not shipped.

## Per-tool gate: human-in-the-loop for DANGEROUS actions (enabled by default)
- The dangerous coding-agent actions in the table above pass through a live Allow/Deny on the
  operator's phone. Mechanism: the SDK's `can_use_tool` callback → `bot/claude_policy.approval_reason` decides
  "dangerous vs routine" → a dangerous action is placed on the durable approval queue
  (`make_queue_approver`), the operator proactively receives a card with Allow/Deny buttons
  (`make_approval_notifier` → reply_queue), and the tap is handled by the existing `bridge:approve`
  handler; **no answer within 5 minutes → a safe "no"** (the action is not executed; the coding
  agent continues without it and reports honestly).
- Routine work (code edits, commits, pushes to a working branch, tests, ordinary git commands) still
  runs **without** a prompt — the callback allows it instantly. The rule "coding from a phone ≠
  endless Allow/Deny" is preserved.
- What is gated (see `approval_reason`): download-and-execute code from the internet
  (`curl|wget … | sh`, `base64 -d | sh`, `eval $(curl)`, python download-and-exec); installing
  third-party code (`pip/pipx install`, `npm/pnpm/yarn/bun install|add|ci`, `gem/cargo/go install`,
  `brew/apt/dnf/yum/pacman install`, `git clone`, `claude plugin install`); force-push (including a
  `+refspec`) and push to main; deleting branches/files (`rm` outside /tmp and any `rm` containing
  `..`, `git clean -f`, `branch -D`, `find -delete`); `git reset --hard`; history rewriting;
  `systemctl` service control; server reboot; the firewall; crontab; sending mail; changing secrets
  in the secret manager (`bws`); ssh/scp/rsync/nc/telnet to other hosts; naive outbound data sends
  via `curl`/`wget` (POST/PUT/upload); reading/editing secret files, keys, `.claude` settings and
  hooks, systemd units, CLAUDE.md, and this policy itself. Robust to global git flags
  (`git --no-pager reset --hard`).
- BOUNDARY (honestly): this is a regex filter over the command string — a speed bump, NOT a
  hermetic anti-exfiltration barrier. Deliberate or injected exfiltration through an arbitrary
  network client (a GET with a secret in the query, a python socket, base64 plus a nonstandard
  channel) is not caught and not claimed. The real first line is the operator-only task route
  (`validate_task_execution_policy`) and the fact that only the operator sources tasks. The gate
  lowers the risk of accidental self-harm by the coding agent and catches the obviously dangerous;
  it does not replace isolation.
- Fail-closed: an error inside the policy itself → deny; an SDK without `can_use_tool` → a loud
  task failure, not a silent pass.
- Kill-switch: `AIOS_TOOL_APPROVALS=0` restores the old no-confirm policy (`bypassPermissions`, no
  gate). An explicit `AIOS_CLAUDE_PERMISSION_MODE` always wins; note that an explicit
  `bypassPermissions` also disables the gate (the SDK does not call the callback in bypass mode).
- The first line of defence is unchanged: the fail-closed gate
  `bot/claude_policy.validate_task_execution_policy` (durable queue + operator-only route +
  allowlisted alias + known mode + the claude-worker service, not the transport).
