"""bot/bridge_queue.py — telegram-free transport<->queue glue for /bridge.

Pure producer / status / approval logic on top of bot/task_queue.py, with NO telegram
dependency — so it can be unit-tested with plain python, without Telegram/Claude.
Telegram-facing code (bot/claude_bridge.py) imports and calls these functions.

Import has no side effects. No network, secrets, Telegram, or Claude.
"""
from __future__ import annotations

import json
import os

from . import task_queue
from .claude_worker_core import _redact  # strips secret-shaped strings (telegram-free)

_VALID_MODES = {"ask", "fix", "continue"}
_MAX_SHOWN = 1500
APPROVAL_CB_PREFIX = "bridge:approve:"
# Leading keywords that switch a task into "design" mode. Configurable via the
# AIOS_DESIGN_TRIGGERS env var (comma-separated); defaults to just "design".
_DESIGN_TRIGGERS = tuple(
    t.strip().lower()
    for t in (os.getenv("AIOS_DESIGN_TRIGGERS", "design")).split(",")
    if t.strip()
)


def extract_design_mode(text):
    """A trigger word at the start of the text switches on design mode.

    Rule: lstrip; case-insensitive startswith; strip the trigger then
    lstrip(" :,-\\n\\t"). Returns (design_mode: bool, text_without_trigger). Telegram-free.
    """
    raw = text or ""
    stripped = raw.lstrip()
    lower = stripped.lower()
    for trig in _DESIGN_TRIGGERS:
        if lower.startswith(trig):
            return True, stripped[len(trig):].lstrip(" :,-\n\t")
    return False, raw


def resolve_resume_sid(state, mode, last_session_id=None):
    """Determine the resume_session_id for a task (telegram-free).

    `state` is _pending_task_flow[chat_id] (or {}). Priority: the session picked in the
    picker (resume_sid); 'new' -> None; otherwise for mode=='continue' fall back to the last
    local session (last_session_id). Otherwise None.
    """
    state = state or {}
    sid = state.get("resume_sid")
    if sid == "new":
        return None
    if sid:
        return sid
    if mode == "continue":
        return last_session_id or None
    return None


def enqueue_bridge_task(*, chat_id, alias, mode, task_text, resume_session_id=None, queue=task_queue) -> int:
    """Put a /bridge task into the durable queue (producer). Returns task_id.

    Validates required fields, detects design mode from the start of the text and
    strips the trigger from task_text. Does NOT run the real Claude.
    """
    alias = (alias or "").strip()
    mode = (mode or "").strip().lower()
    design_mode, task_text = extract_design_mode((task_text or "").strip())
    task_text = task_text.strip()
    if not alias:
        raise ValueError("alias is required")
    if not task_text:
        raise ValueError("task_text is required")
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown mode: {mode}")
    return queue.enqueue_task(
        chat_id=chat_id, alias=alias, mode=mode, task_text=task_text,
        resume_session_id=resume_session_id, source="telegram", design_mode=design_mode,
    )


def _truncate(text: str) -> str:
    if text and len(text) > _MAX_SHOWN:
        return text[:_MAX_SHOWN] + "\n…(truncated)"
    return text


def format_task_status(task) -> str:
    """Task status message for the operator. No secret values, truncated."""
    if not task:
        return "Task not found."
    head = f"Task #{task.get('id')} [{task.get('alias')} · {task.get('mode')}] → {task.get('status')}"
    status = task.get("status")
    if status == "done":
        body = _truncate(_redact(task.get("result") or ""))
        return head + (("\n\n" + body) if body else "")
    if status == "failed":
        err = _truncate(_redact(task.get("error") or ""))
        return head + (("\n\nError: " + err) if err else "")
    return head  # queued / running / awaiting_approval / cancelled


def build_approval_callbacks(approval_id) -> dict:
    """callback_data strings for the Allow/Deny buttons (telegram-free)."""
    return {
        "allow": f"{APPROVAL_CB_PREFIX}{approval_id}:allow",
        "deny": f"{APPROVAL_CB_PREFIX}{approval_id}:deny",
    }


def pending_approval_view(*, task_id=None, queue=task_queue):
    """The oldest pending approval as a telegram-free payload, or None."""
    ap = queue.poll_pending_approval(task_id=task_id)
    if ap is None:
        return None
    try:
        rendered = json.dumps(ap.get("tool_input"), ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        rendered = str(ap.get("tool_input"))
    rendered = _truncate(_redact(rendered))
    cbs = build_approval_callbacks(ap["id"])
    return {
        "approval_id": ap["id"],
        "task_id": ap.get("task_id"),
        "tool_name": ap.get("tool_name"),
        "text": f"Approval for task #{ap.get('task_id')} — tool {ap.get('tool_name')}:\n\n{rendered}",
        "allow_cb": cbs["allow"],
        "deny_cb": cbs["deny"],
    }


def parse_approval_callback(data):
    """Parse 'bridge:approve:<id>:<allow|deny>' -> (approval_id:int, verdict:bool) | None."""
    if not data or not data.startswith(APPROVAL_CB_PREFIX):
        return None
    parts = data.split(":")  # ['bridge','approve','<id>','<allow|deny>']
    if len(parts) != 4 or parts[3] not in ("allow", "deny"):
        return None
    try:
        approval_id = int(parts[2])
    except (ValueError, TypeError):
        return None
    return approval_id, (parts[3] == "allow")


def resolve_approval_callback(data, *, queue=task_queue) -> dict:
    """Parse a callback and record the verdict in the queue. Graceful on stale/expired/nonexistent.

    Returns {ok: bool, message: str, toast: str}. resolve_approval is a no-op if the row is
    no longer pending, so repeated/stale taps neither crash nor overwrite the verdict.
    """
    parsed = parse_approval_callback(data)
    if parsed is None:
        return {"ok": False, "message": "Invalid approval action.", "toast": "invalid"}
    approval_id, verdict = parsed
    queue.resolve_approval(approval_id, verdict)  # no-op if no longer pending
    final = queue.get_verdict(approval_id)
    if final is None:
        return {
            "ok": False,
            "message": f"Approval #{approval_id} is no longer current (expired or closed).",
            "toast": "not current",
        }
    word = "Allowed" if final else "Denied"
    return {"ok": True, "message": f"{word} — approval #{approval_id}.", "toast": word.lower()}
