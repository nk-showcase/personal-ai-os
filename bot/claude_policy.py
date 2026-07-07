"""bot/claude_policy.py — explicit execution policy for coding tasks.

The permission mode is configurable via the AIOS_CLAUDE_PERMISSION_MODE environment variable.
Every task passes the fail-closed check below (validate_task_execution_policy) on its alias and
mode before it runs. The rationale and the full boundary — including which operations always
require confirmation regardless of mode — live in the public policy document
docs/security/approval-policy.md.

Telegram-free; contains no secrets; safe to import.
"""
from __future__ import annotations

import os

ASSISTANT_ALIAS = "_assistant"            # must match bot/claude_bridge.py _ASSISTANT_ALIAS
READ_MODES = {"ask"}
WRITE_MODES = {"fix", "edit", "continue"}
KNOWN_MODES = READ_MODES | WRITE_MODES
DEFAULT_PERMISSION_MODE = "bypassPermissions"  # configurable via AIOS_CLAUDE_PERMISSION_MODE (see module docstring)


class TaskPolicyError(ValueError):
    """The task violates the execution policy (fail-closed)."""


def resolve_claude_permission_mode() -> str:
    """Return the permission mode for Claude Code.

    Configurable via the AIOS_CLAUDE_PERMISSION_MODE environment variable; falls back to
    DEFAULT_PERMISSION_MODE when unset. See docs/security/approval-policy.md.
    """
    val = (os.getenv("AIOS_CLAUDE_PERMISSION_MODE") or "").strip()
    return val or DEFAULT_PERMISSION_MODE


def validate_task_execution_policy(task, *, known_aliases=None) -> dict:
    """Fail-closed check run BEFORE executing a task pulled from the queue.

    Requires: a known mode (otherwise fail); ask => read-only; write modes = fix/edit/continue;
    alias is either ASSISTANT_ALIAS or a member of known_aliases (the project allowlist).
    Returns {alias, mode, read_only}. Does NOT trust an arbitrary repository path from the
    queue — the worker derives the repo from the alias, never from a queue-supplied path.
    """
    if not isinstance(task, dict):
        raise TaskPolicyError("task must be a dict")
    alias = (task.get("alias") or "").strip()
    mode = (task.get("mode") or "").strip().lower()
    if mode not in KNOWN_MODES:
        raise TaskPolicyError(f"unknown mode (fail-closed): {mode!r}")
    if alias == ASSISTANT_ALIAS:
        pass
    elif known_aliases is None:
        raise TaskPolicyError("alias allowlist (BRIDGE_PROJECTS) not provided; refuse non-assistant alias")
    elif alias not in set(known_aliases):
        raise TaskPolicyError(f"alias not in allowlisted BRIDGE_PROJECTS: {alias!r}")
    return {"alias": alias, "mode": mode, "read_only": mode in READ_MODES}
