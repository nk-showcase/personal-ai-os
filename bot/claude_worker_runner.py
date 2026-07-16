"""bot/claude_worker_runner.py — claude-worker execution layer.

Adapts a claimed task from the queue to the existing execution logic in
`claude_bridge._run_claude` through an INJECTABLE boundary (the executor), so
that unit tests do NOT launch a real Claude process. Tool approvals travel
through the durable queue (`task_queue`), NOT through an in-process
`asyncio.Future`.

Boundaries of this layer:
  - This module does NOT modify `bot/main.py`, `bot/handlers.py`, or
    `bot/claude_bridge.py`.
  - The real production executor is `bot/claude_worker._claude_executor`: it
    receives the queue-backed `approve` built here (make_queue_approver +
    make_approval_notifier) and passes it into `claude_bridge._run_claude`,
    where it becomes the can_use_tool approval gate.
  - `default_executor` below is a deliberate test boundary, not the production
    path. Calling it without full wiring raises NotImplementedError, so that
    nothing real runs by accident and no queue approval is silently ignored.

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


def make_approval_notifier(chat_id, task_id):
    """on_request hook for make_queue_approver: proactively deliver the Allow/Deny card to
    the operator through reply_queue (the transport sends it to the chat within ~2s; the tap
    is handled by the existing bridge:approve handler). The danger reason comes from
    tool_input['_reason'] — the gate puts it there for the card; a delivery failure does not
    break approve(): without the card the operator can still open the request via
    /bridge approvals."""
    def notify(approval_id, tool_name, tool_input):
        if not chat_id:
            return
        try:
            from . import bridge_queue, reply_queue
            ti = dict(tool_input or {})
            reason = str(ti.pop("_reason", "") or "")
            card = bridge_queue.approval_card(approval_id, task_id, tool_name, ti, reason=reason)
            reply_queue.enqueue_reply_kb(int(chat_id), card["text"], card["keyboard"],
                                         source="claude-worker")
        except Exception as e:  # noqa: BLE001 — the card must not break the approve loop itself
            log.warning("runner: approval card enqueue failed (%s) — operator can use /bridge approvals",
                        type(e).__name__)
    return notify


def make_queue_approver(
    task_id,
    *,
    timeout=DEFAULT_APPROVAL_TIMEOUT_S,
    poll_interval=DEFAULT_APPROVAL_POLL_S,
    queue=task_queue,
    sleep=asyncio.sleep,
    on_request=None,
):
    """Return an async approve(tool_name, tool_input) -> bool backed by the durable queue.

    Puts an approval request into the queue (`request_approval`) and polls for a
    verdict (`get_verdict`) until it arrives or the timeout elapses. Timeout ->
    safe deny (False). NO asyncio.Future across the process boundary — only rows
    in SQLite.
    on_request(approval_id, tool_name, tool_input) is an optional "request created"
    hook (the proactive operator card); its failure does not affect the approval loop.
    """
    async def approve(tool_name, tool_input) -> bool:
        approval_id = queue.request_approval(task_id, tool_name, tool_input)
        if on_request is not None:
            try:
                on_request(approval_id, tool_name, tool_input)
            except Exception as e:  # noqa: BLE001
                log.warning("runner: on_request hook failed (%s)", type(e).__name__)
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
        task["id"], timeout=approval_timeout, poll_interval=approval_poll, queue=queue,
        on_request=make_approval_notifier(task.get("chat_id"), task["id"]),
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
    """Test boundary to claude_bridge._run_claude — NOT the production path.

    The production executor is `bot/claude_worker._claude_executor`, which prepares
    the repo and passes `approve` into `_run_claude` (the can_use_tool gate). This
    fallback exists so unit tests have an explicit boundary: calling it without a
    prepared repo raises NotImplementedError instead of accidentally launching a
    real coding-agent process and ignoring a queue approval.
    """
    if repo is None or bot is None:
        raise NotImplementedError(
            "default_executor: this is a test boundary; the production wiring "
            "(repo prep + queue approvals into claude_bridge._run_claude) lives in "
            "bot/claude_worker._claude_executor. Unit tests must inject a fake executor."
        )
    from . import claude_bridge_worker as claude_bridge  # noqa: PLC0415  (lazy by design)

    return await claude_bridge._run_claude(
        repo,
        task["task_text"],
        bot,
        task["chat_id"],
        read_only,
        design_mode=design_mode,
        resume_session_id=resume_session_id,
        approve=approve,
    )
