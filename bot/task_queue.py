"""bot/task_queue.py — durable task queue (telegram-bot -> claude-worker).

Purpose: a durable (restart-surviving) cross-process queue of coding tasks plus an approval
channel between the transport (telegram-bot) and the executor (claude-worker). It replaces the
in-process asyncio.Future handoff, which does NOT cross a process boundary.

Properties:
  - SQLite (stdlib), no external dependencies and no network calls.
  - Database lives OUTSIDE the repository: ~/.ai-os/queue/tasks.sqlite3 (override: env AIOS_TASK_QUEUE_DB).
  - Directory 700, database file 600 (where the OS permits).
  - WAL, busy_timeout, short transactions, atomic claim (BEGIN IMMEDIATE).
  - Side-effect-free import — the database is created lazily on first call.
  - No secrets, Telegram, Claude, or GitHub.

The live /bridge command enqueues onto this durable queue via bridge_queue.enqueue_bridge_task.

Task statuses:     queued, running, awaiting_approval, done, failed, cancelled.
Approval statuses: pending, allowed, denied, expired.

Returned Task/Approval values are dicts (keys = table columns; on an Approval the tool_input
field is already parsed from JSON into a dict).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

TASK_STATUSES = {"queued", "running", "awaiting_approval", "done", "failed", "cancelled"}
APPROVAL_STATUSES = {"pending", "allowed", "denied", "expired"}

# A pending approval older than this threshold is treated as expired (verdict = None).
APPROVAL_TIMEOUT_S = int(os.getenv("AIOS_APPROVAL_TIMEOUT", "300"))

_BUSY_TIMEOUT_MS = 5000


def _db_path() -> Path:
    raw = os.getenv("AIOS_TASK_QUEUE_DB")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".ai-os" / "queue" / "tasks.sqlite3"


def _now() -> float:
    return time.time()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            mode TEXT NOT NULL,
            task_text TEXT NOT NULL,
            design_mode INTEGER NOT NULL DEFAULT 0,
            resume_session_id TEXT,
            source TEXT NOT NULL DEFAULT 'telegram',
            status TEXT NOT NULL DEFAULT 'queued',
            worker_id TEXT,
            result TEXT,
            error TEXT,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, created_ts);
        CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_ts);
        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            tool_input TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            verdict INTEGER,
            created_ts REAL NOT NULL,
            resolved_ts REAL,
            FOREIGN KEY(task_id) REFERENCES tasks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_appr_status ON approvals(status, created_ts);
        CREATE INDEX IF NOT EXISTS idx_appr_task ON approvals(task_id, status);
        """
    )
    # migration-safe: add design_mode to an existing tasks table that lacks the column.
    # ALTER TABLE ADD COLUMN ... DEFAULT 0 is compatible with an older DB (old rows -> design_mode=0).
    cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "design_mode" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN design_mode INTEGER NOT NULL DEFAULT 0")


@contextmanager
def _conn():
    """A fresh connection per operation (autocommit). Directory/database are created lazily."""
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Group-aware perms so the bot user + worker user can SHARE this queue DB (see queue_perms).
    # Replaces the old chmod(parent, 0o700)-every-connect that re-locked the shared dir owner-only.
    from . import queue_perms
    _gid = queue_perms.apply_dir_perms(path.parent)
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        _ensure_schema(conn)
        queue_perms.apply_file_perms(path, _gid)  # every connect (incl. -wal/-shm), not only on create
        yield conn
    finally:
        conn.close()


def _expire_stale(conn: sqlite3.Connection) -> None:
    cutoff = _now() - APPROVAL_TIMEOUT_S
    conn.execute(
        "UPDATE approvals SET status='expired', resolved_ts=? WHERE status='pending' AND created_ts < ?",
        (_now(), cutoff),
    )


# --------------------------------------------------------------------------- tasks


def enqueue_task(chat_id, alias, mode, task_text, resume_session_id=None, source="telegram",
                 design_mode=False) -> int:
    """Enqueue a coding task (called by the transport). Returns task_id."""
    ts = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (chat_id, alias, mode, task_text, resume_session_id, source,"
            " design_mode, status, created_ts, updated_ts) VALUES (?,?,?,?,?,?, ?, 'queued', ?, ?)",
            (chat_id, alias, mode, task_text, resume_session_id, source,
             1 if design_mode else 0, ts, ts),
        )
        return int(cur.lastrowid)


def claim_next_task(worker_id=None):
    """Atomically claim the oldest queued task and move it to running (called by the worker).

    Returns a Task (dict) or None if the queue is empty. BEGIN IMMEDIATE guarantees that two
    processes cannot claim the same task.
    """
    ts = _now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Defense-in-depth: EXCLUDE alias='router' rows. Those belong to the unified-router
            # queue and are consumed ONLY by the keyed worker's claim_next_route_task (single-
            # writer-per-chat). The claude-worker SHARES this DB (AIOS_TASK_QUEUE_DB=
            # ${AIOS_HOME}/.ai-os/queue/tasks.sqlite3), and its generic FIFO claim here would
            # otherwise STEAL a router row and run it through the coding runner (a mis-run, with the
            # routed message silently lost) once AIOS_ROUTER_DISPATCH is armed. Coding tasks always
            # carry a project alias (never 'router'; the column is NOT NULL), so this filter never
            # affects them.
            row = conn.execute(
                "SELECT id FROM tasks WHERE status='queued' AND alias!='router'"
                " ORDER BY created_ts, id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            tid = row["id"]
            conn.execute(
                "UPDATE tasks SET status='running', worker_id=?, updated_ts=? WHERE id=?",
                (worker_id, ts, tid),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        claimed = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(claimed) if claimed else None


def claim_next_route_task(worker_id=None):
    """SINGLE-WRITER-PER-CHAT claim for the unified-router consumer (hard-stage prerequisite,
    redesign §3.6). Like claim_next_task BUT scoped to alias='router' AND skips any chat that
    already has an in-flight (running) router row — so two router rows for the SAME chat are
    NEVER processed concurrently / out of order. This is what lets a stateful flow's text turn
    and a timer callback (both conv_state read-modify-write the same per-chat blob) not clobber
    each other across processes; PTB's in-process per-chat lock has no cross-process equivalent.

    Scope: ONLY alias='router' rows. The generic claim_next_task (coding tasks, other aliases)
    is UNCHANGED — this never alters the live claude-worker's claim behavior.

    Returns the claimed Task (dict) or None when there is no eligible router row (queue empty OR
    every queued router row's chat already has one running). Atomic via BEGIN IMMEDIATE.

    NOTE (head-of-line, by design): a router row stuck in 'running' blocks ONLY its own chat_id;
    other chats keep flowing. Recovering a wedged 'running' row (timeout/requeue) is a separate
    concern, not this guard's job.
    """
    ts = _now()
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id FROM tasks WHERE status='queued' AND alias='router'"
                " AND chat_id NOT IN ("
                "   SELECT chat_id FROM tasks WHERE status='running' AND alias='router'"
                " ) ORDER BY created_ts, id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            tid = row["id"]
            conn.execute(
                "UPDATE tasks SET status='running', worker_id=?, updated_ts=? WHERE id=?",
                (worker_id, ts, tid),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        claimed = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(claimed) if claimed else None


def update_task(task_id, status, result=None, error=None) -> None:
    """Update a task's status (called by the worker). result/error=None do not overwrite prior values."""
    if status not in TASK_STATUSES:
        raise ValueError(f"unknown task status: {status}")
    with _conn() as conn:
        conn.execute(
            "UPDATE tasks SET status=?, result=COALESCE(?, result),"
            " error=COALESCE(?, error), updated_ts=? WHERE id=?",
            (status, result, error, _now(), task_id),
        )


def get_task(task_id):
    """Read a task by id (helper). Returns a dict or None."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_updates(since_ts=None):
    """List tasks updated after since_ts (the transport polls this and relays to Telegram).

    since_ts=None -> all tasks. Returns list[dict], sorted by updated_ts.
    """
    with _conn() as conn:
        if since_ts is None:
            rows = conn.execute("SELECT * FROM tasks ORDER BY updated_ts, id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE updated_ts > ? ORDER BY updated_ts, id", (since_ts,)
            ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------------- approvals


def request_approval(task_id, tool_name, tool_input) -> int:
    """Request a tool approval (called by the worker). tool_input is a dict (-> JSON).

    Also moves the task to awaiting_approval. Returns approval_id.
    """
    ts = _now()
    payload = json.dumps(tool_input, ensure_ascii=False)
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO approvals (task_id, tool_name, tool_input, status, created_ts)"
            " VALUES (?,?,?, 'pending', ?)",
            (task_id, tool_name, payload, ts),
        )
        conn.execute(
            "UPDATE tasks SET status='awaiting_approval', updated_ts=? WHERE id=?", (ts, task_id)
        )
        return int(cur.lastrowid)


def poll_pending_approval(task_id=None):
    """The oldest pending approval (the transport renders an Allow/Deny button).

    task_id=None -> any. Returns an Approval (dict, tool_input already parsed) or None.
    Stale pending approvals are marked expired before selection.
    """
    with _conn() as conn:
        _expire_stale(conn)
        if task_id is None:
            row = conn.execute(
                "SELECT * FROM approvals WHERE status='pending' ORDER BY created_ts, id LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM approvals WHERE status='pending' AND task_id=?"
                " ORDER BY created_ts, id LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["tool_input"] = json.loads(d["tool_input"])
        except (ValueError, TypeError):
            d["tool_input"] = {}
        return d


def resolve_approval(approval_id, verdict: bool) -> None:
    """Record the user's verdict (called by the transport from cb_approve). Only while still pending."""
    status = "allowed" if verdict else "denied"
    with _conn() as conn:
        conn.execute(
            "UPDATE approvals SET status=?, verdict=?, resolved_ts=? WHERE id=? AND status='pending'",
            (status, 1 if verdict else 0, _now(), approval_id),
        )


def get_verdict(approval_id):
    """The verdict for approval_id (the worker polls this). True=allowed, False=denied,

    None=pending/expired/no such row. Stale pending approvals are marked expired.
    """
    with _conn() as conn:
        _expire_stale(conn)
        row = conn.execute(
            "SELECT status FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "allowed":
            return True
        if row["status"] == "denied":
            return False
        return None
