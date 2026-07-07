"""bot/claude_worker_runner.py — claude-worker execution layer.

Adapts a claimed task from the queue to the existing execution logic in
`claude_bridge._run_claude` through an INJECTABLE boundary (the executor), so
that unit tests do NOT launch a real Claude process. Tool approvals travel
through the durable queue (`task_queue`), NOT through an in-process
`asyncio.Future`.

Boundaries of this layer:
  - The live `/bridge` and Telegram transport are NOT switched over here; that
    is a separate wiring step.
  - This module does NOT modify `bot/main.py`, `bot/handlers.py`, or
    `bot/claude_bridge.py`.
  - `default_executor` is the real boundary to `claude_bridge._run_claude`, but
    it is NOT wired in yet (no repo, no bot, no caller). Calling it without full
    wiring deliberately raises NotImplementedError, so that nothing real runs by
    accident and no queue approval is silently ignored.

Side-effect-free import: only imports and constants. `claude_bridge` is imported
LAZILY inside `default_executor`, so unit tests need no Telegram/Claude
dependencies. Result serialization and secret scrubbing are done by
`claude_worker_core.process_one` (this module returns the value as-is).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from . import task_queue

log = logging.getLogger("aios.claude_worker.runner")

DEFAULT_APPROVAL_TIMEOUT_S = 300.0
DEFAULT_APPROVAL_POLL_S = 2.0


@dataclass(frozen=True)
class ModeSpec:
    read_only: bool
    is_continue: bool


def resolve_mode(mode) -> ModeSpec:
    """Explicit mapping mode -> executor behavior.

    ask          -> read-only (read tools only: Read/Grep/Glob)
    fix / edit   -> edits allowed
    continue     -> edits allowed, session resumed
    unknown      -> read-only (safe fallback)
    """
    m = (mode or "").strip().lower()
    if m == "ask":
        return ModeSpec(read_only=True, is_continue=False)
    if m in ("fix", "edit"):
        return ModeSpec(read_only=False, is_continue=False)
    if m == "continue":
        return ModeSpec(read_only=False, is_continue=True)
    log.warning("runner: unknown mode %r -> read_only (safe default)", mode)
    return ModeSpec(read_only=True, is_continue=False)


def make_queue_approver(
    task_id,
    *,
    timeout=DEFAULT_APPROVAL_TIMEOUT_S,
    poll_interval=DEFAULT_APPROVAL_POLL_S,
    queue=task_queue,
    sleep=asyncio.sleep,
):
    """Return an async approve(tool_name, tool_input) -> bool backed by the durable queue.

    Puts an approval request into the queue (`request_approval`) and polls for a
    verdict (`get_verdict`) until it arrives or the timeout elapses. Timeout ->
    safe deny (False). NO asyncio.Future across the process boundary — only rows
    in SQLite.
    """
    async def approve(tool_name, tool_input) -> bool:
        approval_id = queue.request_approval(task_id, tool_name, tool_input)
        deadline = time.monotonic() + float(timeout)
        while True:
            verdict = queue.get_verdict(approval_id)
            if verdict is not None:
                return bool(verdict)
            if time.monotonic() >= deadline:
                # Close the approval as a safe-deny, so the transport does not show
                # a stale Allow/Deny button after the worker's local timeout.
                # resolve_approval is a no-op if the row is no longer pending (a
                # verdict arrived at the last moment), so the race leaves no stale
                # pending row.
                log.warning("runner: approval %s timed out -> deny + close", approval_id)
                queue.resolve_approval(approval_id, False)
                return False
            await sleep(poll_interval)

    return approve


async def run_queue_task(
    task,
    *,
    executor,
    approval_timeout=DEFAULT_APPROVAL_TIMEOUT_S,
    approval_poll=DEFAULT_APPROVAL_POLL_S,
    queue=task_queue,
    approver_factory=make_queue_approver,
):
    """Adapter: claimed task -> executor call with explicit flags + queue approval.

    executor is an async callable(*, task, read_only, is_continue, resume_session_id,
    design_mode, approve) -> any serializable result. The return value is passed
    through as-is; serialization and secret scrubbing are done by
    claude_worker_core.process_one.
    """
    spec = resolve_mode(task.get("mode"))
    approve = approver_factory(
        task["id"], timeout=approval_timeout, poll_interval=approval_poll, queue=queue
    )
    return await executor(
        task=task,
        read_only=spec.read_only,
        is_continue=spec.is_continue,
        resume_session_id=task.get("resume_session_id"),
        design_mode=bool(task.get("design_mode")),
        approve=approve,
    )


def build_runner(
    executor,
    *,
    approval_timeout=DEFAULT_APPROVAL_TIMEOUT_S,
    approval_poll=DEFAULT_APPROVAL_POLL_S,
    queue=task_queue,
):
    """Build a runner(task) for claude_worker_core.process_one with the given executor."""
    async def runner(task):
        return await run_queue_task(
            task,
            executor=executor,
            approval_timeout=approval_timeout,
            approval_poll=approval_poll,
            queue=queue,
        )

    return runner


async def default_executor(
    *, task, read_only, is_continue, resume_session_id, approve, repo=None, bot=None,
    design_mode=False
):
    """Real boundary to claude_bridge._run_claude. NOT wired in yet.

    When the transport/bridge are connected, a prepared `repo` and an approval
    delivery channel arrive here; only then does `claude_bridge._run_claude` run.
    Until then, a deliberate NotImplementedError prevents accidentally launching a
    real Claude process and ignoring a queue approval.
    """
    if repo is None or bot is None:
        raise NotImplementedError(
            "default_executor: real execution wiring (repo prep + routing queue "
            "approvals into claude_bridge._run_claude) lands in the /bridge-switch "
            "step. Unit tests must inject a fake executor."
        )
    # --- Bridge-switch wiring point (NOT executed until wired in) ---
    # NOTE: routing `approve` (queue approval) into _run_claude requires editing
    # claude_bridge.py (which currently approves in-process through the bot). That
    # is the separate bridge-switch step. The lazy import keeps unit tests free of
    # Telegram/Claude dependencies.
    from . import claude_bridge_worker as claude_bridge  # noqa: PLC0415  (lazy by design)

    return await claude_bridge._run_claude(
        repo,
        task["task_text"],
        bot,
        task["chat_id"],
        read_only,
        design_mode=design_mode,
        resume_session_id=resume_session_id,
    )
