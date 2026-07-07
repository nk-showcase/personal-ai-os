"""bot/claude_worker_core.py — claude-worker core (task-queue consumer).

A pure consumer core built on top of bot/task_queue.py: it claims the next task and executes
it through an INJECTED runner. The core itself does NOT launch Claude Code, does NOT touch the
network, and does NOT read secrets. Flow: claim queued -> runner -> done/failed.

Boundaries:
  - This core takes any async runner. The real Claude Code runner is wired in
    bot/claude_worker.py (build_default_runner), which stays outside this import-light core.
  - No dependency on Telegram / Claude / GitHub here.

Side-effect-free import: the module level contains only imports and constants.

runner — an async callable(task: dict) -> result. It receives the claimed task (a dict from
task_queue) and returns an arbitrary result (coerced to a string, with secrets scrubbed).
"""
from __future__ import annotations

import json
import logging
import re

from . import task_queue

log = logging.getLogger("aios.claude_worker")

# Targeted secret patterns scrubbed from result/error. They are anchored to characteristic
# prefixes/structure, so they do NOT touch legitimate output (e.g. a 40-hex git SHA).
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),            # telegram bot token
    re.compile(r"0\.[0-9a-f\-]{30,}\.[A-Za-z0-9+/=]{20,}"),    # bws access token
]
_MAX_RESULT = 8000
_MAX_ERROR = 2000


def _redact(text: str) -> str:
    if not text:
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub("<redacted>", text)
    return text


def _coerce_result(result) -> str:
    if result is None:
        return ""
    if isinstance(result, (dict, list)):
        try:
            s = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(result)
    else:
        s = str(result)
    return _redact(s)[:_MAX_RESULT]


def _safe_error(exc: BaseException) -> str:
    """Secret-free error string: exception type + scrubbed message."""
    return (f"{type(exc).__name__}: " + _redact(str(exc)))[:_MAX_ERROR]


async def process_one(worker_id: str, runner, *, claim_fn=None):
    """Claim one task from the queue and execute it through runner.

    Returns a dict with status:
      - empty queue       -> {"status":"noop","task_id":None,"detail":"queue empty"}  (runner NOT called)
      - success           -> {"status":"done","task_id":id,"result":...}   (task done, result stored)
      - runner exception  -> {"status":"failed","task_id":id,"error":...}  (task failed, secret-free error)

    Cancelled tasks are not executed: claim_next_task selects only queued rows, so a cancelled
    task never reaches the worker. The worker does not crash on a runner error.
    """
    # claim_fn defaults to the generic FIFO claim; the router passes claim_next_route_task
    # (single-writer-per-chat, §3.6). Signature: claim_fn(worker_id=...) -> task dict | None.
    task = (claim_fn or task_queue.claim_next_task)(worker_id=worker_id)
    if task is None:
        return {"status": "noop", "task_id": None, "detail": "queue empty"}

    task_id = task["id"]
    try:
        result = await runner(task)
    except Exception as exc:  # noqa: BLE001 — any runner failure => task failed, worker keeps going
        err = _safe_error(exc)
        task_queue.update_task(task_id, "failed", error=err)
        log.warning("claude-worker %s: task %s failed (%s)", worker_id, task_id, type(exc).__name__)
        return {"status": "failed", "task_id": task_id, "error": err}

    result_str = _coerce_result(result)
    task_queue.update_task(task_id, "done", result=result_str)
    log.info("claude-worker %s: task %s done", worker_id, task_id)
    return {"status": "done", "task_id": task_id, "result": result_str}
