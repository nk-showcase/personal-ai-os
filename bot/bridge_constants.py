"""bot/bridge_constants.py — S6 Stage 0b: shared bridge constants (NO secrets, NO telegram).

Extracted from bot/claude_bridge.py so the transport-bridge (claude_bridge.py) and the
worker-bridge (claude_bridge_worker.py, added in a later 0b commit) can both import these
without dragging each other's surface across the secret/decryption boundary. Pure config
read from env; resolves NO secret. Breaks the worker<->transport import cycle.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_WORK_DIR = Path(os.environ.get("BRIDGE_WORK_DIR", "/tmp/claude-bridge"))
_GIT_USER_NAME = os.environ.get("BRIDGE_GIT_NAME", "<YOUR_ORG>")
_GIT_USER_EMAIL = os.environ.get("BRIDGE_GIT_EMAIL", "bot@users.noreply.github.com")
_ASSISTANT_ALIAS = "_assistant"
_SKILLS_DIR = Path(os.environ.get("BRIDGE_SKILLS_DIR", "/home/app/.claude/skills"))
_SKILLS_REPO = os.environ.get(
    "BRIDGE_SKILLS_REPO", "https://github.com/<YOUR_ORG>/claude-skills.git"
)


def _load_projects() -> dict:
    raw = os.environ.get("BRIDGE_PROJECTS", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        log.warning("BRIDGE_PROJECTS is not valid JSON")
        return {}


# Session-history recording lives here so BOTH the transport (picker UI reads it) AND the
# claude-worker process (records a new sid after each run) can share it without either side
# pulling the other's surface (telegram / secrets) across the seam.
# Shared location used when the transport (bot user) and the coding worker (ai-os user)
# run as different Linux users. Both are members of the aios-queue group; this directory
# is provisioned once with group=aios-queue + mode 2770 so either service can read and
# write the recent-sessions index. Without a shared path the two processes would each fall
# back to their own $HOME and the picker would show an empty list even though sessions
# had been recorded — that was the "previous session missing from the picker" bug.
_SHARED_BRIDGE_STATE_DIR = Path(
    os.environ.get("AIOS_HOME", "/home/user") + "/.ai-os/claude-bridge"
)


def _default_recent_sessions_path() -> Path:
    try:
        if _SHARED_BRIDGE_STATE_DIR.is_dir() and os.access(str(_SHARED_BRIDGE_STATE_DIR), os.R_OK | os.W_OK):
            return _SHARED_BRIDGE_STATE_DIR / "_recent_sessions.json"
    except PermissionError:
        pass
    home = Path.home()
    volume_dir = home / ".claude" / "projects"
    try:
        if volume_dir.exists():
            return volume_dir / "_recent_sessions.json"
    except PermissionError:
        pass
    return _WORK_DIR / "_recent_sessions.json"


_RECENT_SESSIONS_CAP = 7


def _resolve_recent_sessions_file() -> Path:
    """Resolve the recent-sessions index path lazily, on every call.

    Regression fix ("previous sessions missing after code+chat-bot"): the shared
    bridge-state dir is provisioned by systemd tmpfiles during boot but may not
    yet exist at the moment the transport (bot user) process imports this
    module. Capturing the path once at import time then locked the bot on the
    /tmp/claude-bridge fallback, while the worker (running slightly later) wrote
    to the real shared file — so the picker showed an empty list. Recomputing
    each time lets both processes converge on the shared path as soon as it is
    ready, without a service restart.
    """
    override = os.environ.get("BRIDGE_RECENT_SESSIONS_FILE", "").strip()
    if override:
        return Path(override)
    return _default_recent_sessions_path()


_RECENT_SESSIONS_FILE = _resolve_recent_sessions_file()


def _load_recent_sessions() -> dict:
    """Format: {chat_id: {alias: [{sid, ts, preview}]}}"""
    path = _resolve_recent_sessions_file()
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("recent-sessions: load failed: %r", e)
    return {}


def _save_recent_sessions(data: dict) -> None:
    path = _resolve_recent_sessions_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("recent-sessions: save failed: %r", e)


def _record_session(chat_id: int, alias: str, session_id: str, task: str) -> None:
    """Append or bump a session in history. Newest first, capped at _RECENT_SESSIONS_CAP."""
    if not session_id:
        return
    data = _load_recent_sessions()
    cid = str(chat_id)
    chat_map = data.setdefault(cid, {})
    lst = chat_map.setdefault(alias, [])
    lst = [e for e in lst if e.get("sid") != session_id]
    from datetime import datetime, timezone
    preview = (task or "").strip().replace("\n", " ")[:60]
    lst.insert(0, {
        "sid": session_id,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preview": preview,
    })
    chat_map[alias] = lst[:_RECENT_SESSIONS_CAP]
    _save_recent_sessions(data)
