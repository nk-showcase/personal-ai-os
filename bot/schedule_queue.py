"""bot/schedule_queue.py — S6 0c: durable due_ts scheduler scanned by the worker.

The worker has no PTB JobQueue and no Telegram token. Instead of in-memory run_once/run_daily
timers (which vanish on restart), moved schedules become durable rows here; the worker scans
claim_due() each poll tick and fires due jobs ONCE. Recurring jobs re-schedule themselves (the
executor computes the next due_ts and calls add_scheduled again) — so the next occurrence is a
persisted row that survives a process kill. Conversational timers use cancel_key to be cancelled.

Mirrors bot/task_queue.py: SQLite (stdlib), shares ~/.ai-os/queue/tasks.sqlite3 (override
AIOS_TASK_QUEUE_DB), WAL, busy_timeout, fresh-connection-per-op, atomic claim (BEGIN IMMEDIATE).
Import side-effect-free; NO secrets / Telegram / Claude.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

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
        CREATE TABLE IF NOT EXISTS scheduled (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            due_ts REAL NOT NULL,
            cancel_key TEXT,
            created_ts REAL NOT NULL,
            fired_ts REAL
        );
        CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled(fired_ts, due_ts, id);
        CREATE INDEX IF NOT EXISTS idx_sched_cancel ON scheduled(cancel_key);
        """
    )


@contextmanager
def _conn():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Group-aware perms so the bot + worker can SHARE this queue DB (see queue_perms).
    from . import queue_perms
    _gid = queue_perms.apply_dir_perms(path.parent)
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")  # consistency with the other stores (data-layer-decision §6)
        _ensure_schema(conn)
        queue_perms.apply_file_perms(path, _gid)  # every connect (incl. -wal/-shm), not only on create
        yield conn
    finally:
        conn.close()


def add_scheduled(kind: str, due_ts: float, payload: dict | None = None,
                  cancel_key: str | None = None) -> int:
    """Schedule a one-shot job due at due_ts (epoch). payload is value-free job context (JSON).
    cancel_key lets a later cancel() remove it while still pending. Returns the row id."""
    ts = _now()
    pj = json.dumps(payload or {}, ensure_ascii=False)
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO scheduled (kind, payload, due_ts, cancel_key, created_ts) VALUES (?,?,?,?,?)",
            (kind, pj, due_ts, cancel_key, ts),
        )
        return int(cur.lastrowid)


def upsert_scheduled(kind: str, due_ts: float, payload: dict | None = None,
                     cancel_key: str | None = None) -> int:
    """ATOMIC cancel-then-add for a keyed one-shot: inside ONE `BEGIN IMMEDIATE` transaction, delete
    every not-yet-fired row sharing cancel_key and insert the fresh row. Returns the new row id.

    Unlike cancel()+add_scheduled() (two SEPARATE transactions, so two writers can both cancel-find-
    nothing then both insert -> two rows), concurrent upserts of the SAME cancel_key serialize on the
    write lock, so EXACTLY ONE pending row survives. Use when two independent writers may arm the same
    key at once (e.g. the receiver arming a precise reminder at task creation while the worker
    reconciles the same task at startup). cancel_key is REQUIRED (a keyless upsert cannot dedup)."""
    if not cancel_key:
        raise ValueError("upsert_scheduled requires a cancel_key")
    ts = _now()
    pj = json.dumps(payload or {}, ensure_ascii=False)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")  # take the write lock so a concurrent upsert of this key waits
        try:
            conn.execute(
                "DELETE FROM scheduled WHERE cancel_key=? AND fired_ts IS NULL", (cancel_key,)
            )
            cur = conn.execute(
                "INSERT INTO scheduled (kind, payload, due_ts, cancel_key, created_ts) VALUES (?,?,?,?,?)",
                (kind, pj, due_ts, cancel_key, ts),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return int(cur.lastrowid)


def claim_due(now: float | None = None) -> list[dict]:
    """Worker side: atomically claim ALL due (due_ts<=now, not yet fired) jobs, stamp fired_ts,
    and return them (payload parsed to dict), oldest-due first. One-shot: a fired job is never
    returned again. BEGIN IMMEDIATE prevents two workers from double-firing the same job."""
    n = _now() if now is None else now
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT * FROM scheduled WHERE fired_ts IS NULL AND due_ts<=? ORDER BY due_ts, id",
                (n,),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.execute(
                    f"UPDATE scheduled SET fired_ts=? WHERE id IN ({','.join('?' * len(ids))})",
                    (n, *ids),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except (ValueError, TypeError):
                d["payload"] = {}
            out.append(d)
        return out


def cancel(cancel_key: str) -> int:
    """Cancel (delete) not-yet-fired jobs with this cancel_key. Returns count cancelled.
    Used for conversational-timer cancellation semantics (e.g. a pending timer 'stop')."""
    with _conn() as conn:
        cur = conn.execute(
            "DELETE FROM scheduled WHERE cancel_key=? AND fired_ts IS NULL", (cancel_key,)
        )
        return cur.rowcount


def pending_count() -> int:
    """Count of not-yet-fired scheduled jobs (helper / health)."""
    with _conn() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM scheduled WHERE fired_ts IS NULL").fetchone()[0])
