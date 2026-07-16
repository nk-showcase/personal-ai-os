"""bot/claude_policy.py — explicit execution policy for coding tasks.

APPROVAL POLICY: routine coding tasks that the operator starts through the chat channel run
WITHOUT per-tool confirmations — coding from a phone must not turn into an endless stream of
"Allow/Deny". The gate below asks the operator ONLY about dangerous actions (see
approval_reason); routine work is auto-allowed by the callback instantly.

That is safe because the task path is constrained (fail-closed gate below):
  - the source is the durable queue;
  - the task was produced by the operator-only chat route (the transport checks the sender);
  - the alias comes from the BRIDGE_PROJECTS allowlist (or the special assistant alias);
  - the mode is known (ask/fix/continue/edit);
  - the service is claude-worker (NOT the transport).

HUMAN-IN-THE-LOOP: routine stays confirmation-free, but DANGEROUS / irreversible /
outbound-sending actions from docs/security/approval-policy.md pass a per-tool gate: the SDK's
can_use_tool callback asks the operator through the durable approval queue (Allow/Deny on the
phone); a timeout is a safe "no". The dangerous-vs-routine decision is approval_reason() below
(a pure function, testable without a network). Kill-switch: AIOS_TOOL_APPROVALS=0 restores the
old behaviour (bypassPermissions, no gate).

Telegram-free; contains no secrets; safe to import.
"""
from __future__ import annotations

import os
import re

ASSISTANT_ALIAS = "_assistant"            # must match bot/claude_bridge.py _ASSISTANT_ALIAS
READ_MODES = {"ask"}
WRITE_MODES = {"fix", "edit", "continue"}
KNOWN_MODES = READ_MODES | WRITE_MODES
DEFAULT_PERMISSION_MODE = "bypassPermissions"  # behaviour when the gate is DISABLED (see docstring)
GATED_PERMISSION_MODE = "default"              # gate enabled: permission requests go to can_use_tool


class TaskPolicyError(ValueError):
    """The task violates the execution policy (fail-closed)."""


def tool_approvals_enabled() -> bool:
    """Per-tool operator confirmation (Allow/Deny on the phone) for DANGEROUS actions.
    ENABLED by default. Disabled only by an explicit AIOS_TOOL_APPROVALS=0/off/false/no —
    an emergency kill-switch for when the gate gets in the way."""
    val = (os.getenv("AIOS_TOOL_APPROVALS") or "").strip().lower()
    return val not in {"0", "off", "false", "no"}


def resolve_claude_permission_mode() -> str:
    """Permission mode for the coding agent. An explicit AIOS_CLAUDE_PERMISSION_MODE always wins
    (CAUTION: an explicit 'bypassPermissions' also disables the can_use_tool gate — the SDK does
    not call the callback in bypass mode). Without an explicit value: gate enabled -> 'default'
    (permission requests arrive in can_use_tool), gate disabled -> 'bypassPermissions' (the old
    no-confirm policy)."""
    val = (os.getenv("AIOS_CLAUDE_PERMISSION_MODE") or "").strip()
    if val:
        return val
    return GATED_PERMISSION_MODE if tool_approvals_enabled() else DEFAULT_PERMISSION_MODE


# ---------------------------------------------------------------------------
# What counts as DANGEROUS (requires the operator's Allow/Deny) — per
# docs/security/approval-policy.md. Same principle as the policy itself: do not
# interrupt the operator during ordinary coding; confirm only where an action can
# do irreversible harm or send something out.
# ---------------------------------------------------------------------------

# Security / secrets / deploy / agent-policy files: editing them (and reading them through
# Bash — reading .env prints secrets into the task log, the same leak class the gate closes).
_SENSITIVE_PATH_RE = re.compile(
    r"(\.env\b|secrets-map\.yaml|\bsecrets?[-_./]|\.ssh/|authorized_keys|id_rsa|id_ed25519"
    r"|\.identity\b|kek\.age|/etc/systemd/|/etc/sudoers|sshd_config|/etc/ufw|/etc/nftables"
    r"|\.claude/settings|\.claude/hooks/|claude/hooks/|/\.ai-os/env/|/\.ai-os/keys/"
    r"|\bCLAUDE\.md\b|docs/security/|approval-policy)",
    re.IGNORECASE,
)

# Destructive rm is allowed without a prompt ONLY in its simplest form on temporary paths —
# and never when the command contains '..' (path traversal `rm -rf /tmp/../home/...` would
# take rm out of /tmp): '..' is handled in approval_reason BEFORE this check.
_RM_TMP_OK_RE = re.compile(r"^\s*rm\s+(-[a-zA-Z]+\s+)*((/tmp/|/var/tmp/|/dev/shm/)\S+\s*)+$")

# `git ` + arbitrary global flags (`--no-pager`, partially `-c x=y`) before the subcommand,
# so that `git --no-pager reset --hard` does not slip past the gate.
_G = r"\bgit\s+(?:-\S+\s+)*"

_GATED_BASH: list[tuple[re.Pattern, str]] = [
    (re.compile(_G + r"push\b[^&|;]*(\s--force(-with-lease)?\b|\s-f\b|\s\+\S)"),
     "force-rewrite of shared history (force-push)"),
    (re.compile(_G + r"push\b[^&|;]*[\s:/+](main|master)\b"),
     "push directly into the main branch"),
    (re.compile(_G + r"push\b[^&|;]*(\s--delete\b|\s+\S+\s+:\S)"),
     "deleting a branch on the shared remote"),
    (re.compile(_G + r"reset\s+(\S+\s+)*--hard\b"), "hard state rollback (git reset --hard)"),
    (re.compile(_G + r"clean\b[^&|;]*\s-[a-zA-Z]*f"), "deleting untracked files (git clean -f)"),
    (re.compile(_G + r"branch\s+(\S+\s+)*-D\b"), "force-deleting a branch"),
    (re.compile(_G + r"(filter-branch|filter-repo)\b"), "rewriting git history"),
    (re.compile(r"\bfind\b[^&|;]*\s-delete\b"), "bulk file deletion (find -delete)"),
    (re.compile(r"\bsystemctl\s+(--user\s+)?(start|stop|restart|reload|enable|disable|mask|unmask|"
                r"edit|daemon-reload|daemon-reexec)\b"), "controlling server services (systemctl)"),
    (re.compile(r"\bservice\s+\S+\s+(start|stop|restart|reload)\b"), "controlling server services"),
    (re.compile(r"\b(reboot|shutdown|poweroff|halt)\b"), "rebooting/shutting down the server"),
    (re.compile(r"\b(pkill|killall)\b"), "force-killing processes"),
    (re.compile(r"\b(ufw|iptables|nft)\b"), "changing the firewall"),
    (re.compile(r"\bcrontab\b"), "changing the scheduler (crontab)"),
    (re.compile(r"\b(sendmail|msmtp|mutt|swaks)\b"), "sending mail"),
    (re.compile(r"\bbws\s+(secret|project)\s+(create|edit|delete|update)\b"),
     "changing secrets in the secret manager (bws)"),
    (re.compile(r"\bssh\b"), "running a command on another host (ssh)"),
    (re.compile(r"\bscp\b"), "copying to another host (scp)"),
    (re.compile(r"\brsync\b[^&|;]*(\s\S+@|::)"), "copying to another host (rsync)"),
    (re.compile(r"\b(nc|ncat|netcat|telnet)\b"), "outbound network connection (nc/telnet)"),
    # Fetch-and-RUN: remote code fetched and executed in one breath. The coding worker has no
    # file-level download guard on the server, so gate it here.
    (re.compile(r"\b(curl|wget|iwr|invoke-webrequest|fetch)\b[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b",
                re.IGNORECASE), "download-and-execute code from the internet (curl|sh)"),
    (re.compile(r"\bbase64\b[^|]*-d[^|]*\|\s*(ba|z)?sh\b", re.IGNORECASE),
     "download-and-execute (base64 decode into a shell)"),
    (re.compile(r"\beval\b[^;|]*\$\(\s*(curl|wget)\b", re.IGNORECASE),
     "executing the result of a download (eval $(curl))"),
    (re.compile(r"\bpython[0-9.]*\b[^;]*-c\b[^;]*(urlopen|urlretrieve|requests\.get)[^;]*"
                r"(exec|os\.system|subprocess|extractall)", re.IGNORECASE),
     "python downloads-and-executes code from the network"),
    # Package / plugin / repo installs — pull third-party code from the internet.
    (re.compile(r"\b(pip|pip3|pipx)\s+install\b", re.IGNORECASE), "package install (pip)"),
    (re.compile(r"\b(npm|pnpm|yarn|bun)\s+(install|add|i|ci)\b", re.IGNORECASE),
     "package install (npm/yarn)"),
    (re.compile(r"\bgem\s+install\b", re.IGNORECASE), "package install (gem)"),
    (re.compile(r"\b(cargo|go)\s+install\b", re.IGNORECASE), "package install (cargo/go)"),
    (re.compile(r"\b(brew|apt|apt-get|dnf|yum|pacman)\s+(install|add)\b", re.IGNORECASE),
     "system package install"),
    (re.compile(r"\bgit\s+(?:-\S+\s+)*clone\b", re.IGNORECASE), "downloading a repository (git clone)"),
    (re.compile(r"\bclaude\s+plugin\s+(install|marketplace\s+add)\b", re.IGNORECASE),
     "installing a plugin/skill"),
    (re.compile(r"\bcurl\b[^&|;]*(\s-d\b|\s--data\S*\b|\s-F\b|\s--form\b|\s-T\b|\s--upload-file\b"
                r"|\s-X\s*(POST|PUT|DELETE|PATCH)\b|\s--request\s*(POST|PUT|DELETE|PATCH)\b)",
                re.IGNORECASE),
     "sending data out (curl)"),
    (re.compile(r"\bwget\b[^&|;]*--(post-data|post-file|method=(POST|PUT|DELETE))", re.IGNORECASE),
     "sending data out (wget)"),
]

_FS_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_FS_READ_TOOLS = {"Read", "NotebookRead"}

# WebFetch (GET an arbitrary URL) narrow exfil gate: ask ONLY when the URL looks like it CARRIES
# data out — a query value that looks like a secret/token, a long high-entropy value, or a shell
# variable interpolation ($VAR) that could smuggle a secret. A plain docs URL (no such query)
# stays routine, so reading documentation never prompts.
_WEBFETCH_EXFIL_RE = re.compile(
    r"[?&][^=\s&]*(token|secret|key|api[_-]?key|auth|session|password|cred|env)[^=\s&]*="
    r"|=[A-Za-z0-9+/_-]{24,}"            # long high-entropy value in the query
    r"|\$\{?[A-Za-z_]",                    # $VAR / ${VAR} interpolation in the URL
    re.IGNORECASE,
)


def approval_reason(tool_name: str, tool_input: dict | None) -> str | None:
    """The reason (human-readable, for the operator's approval card) if the action requires
    Allow/Deny; otherwise None.

    A pure function without network/secrets: it looks only at the tool name and its input.
    Anything not on the list is routine and runs without a prompt (the "do not interrupt the
    operator during ordinary coding" policy)."""
    ti = tool_input or {}
    if tool_name == "Bash":
        cmd = str(ti.get("command") or "")
        if not cmd:
            return None
        if re.search(r"\brm\b", cmd) and (".." in cmd or not _RM_TMP_OK_RE.match(cmd)):
            return "deleting files (rm outside /tmp)"
        for pattern, reason in _GATED_BASH:
            if pattern.search(cmd):
                return reason
        if _SENSITIVE_PATH_RE.search(cmd):
            return "touching secrets/security/deploy files"
        return None
    if tool_name in _FS_EDIT_TOOLS:
        path = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
        if path and _SENSITIVE_PATH_RE.search(path):
            return "editing a secrets/security/deploy file"
        return None
    if tool_name in _FS_READ_TOOLS:
        # Reading code is routine; reading a secrets/keys/deploy file with the Read tool is the same
        # leak class the Bash branch gates (ask before reading a secret file).
        path = str(ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or "")
        if path and _SENSITIVE_PATH_RE.search(path):
            return "reading a secrets/security/deploy file"
        return None
    if tool_name == "WebFetch":
        url = str(ti.get("url") or "")
        if url and _WEBFETCH_EXFIL_RE.search(url):
            return "outbound request carrying secret-looking data (possible leak)"
        return None
    return None


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
