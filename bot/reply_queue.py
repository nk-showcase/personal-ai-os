"""bot/reply_queue.py — S6 0c: durable cross-process reply channel (worker -> transport -> Telegram).

When a feature is moved to the worker (which holds NO Telegram token), its user-facing reply is
enqueued here; the transport (telegram-bot, the only token holder) polls undelivered rows, sends
them, and stamps delivered_ts AFTER a successful send. Mirrors bot/task_queue.py conventions:
  - SQLite (stdlib); shares ~/.ai-os/queue/tasks.sqlite3 (override: env AIOS_TASK_QUEUE_DB).
  - WAL, busy_timeout, fresh-connection-per-op, atomic writes.
  - Import side-effect-free (DB created lazily); NO secrets / Telegram / Claude.

delivered_ts design (NOT an in-memory since_ts cursor): a hard restart never re-sends an
already-delivered reply, because delivery is recorded durably in the row itself.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

_BUSY_TIMEOUT_MS = 5000

KIND_SEND = "send"          # fresh user-facing message (0c behaviour; the only kind enqueue_reply writes)
KIND_EDIT = "edit"          # in-place edit of an existing message (edit_message_id is the target)
KIND_SEND_KB = "send_kb"    # fresh message carrying an inline keyboard (markup_json holds the buttons)
KIND_EDIT_KB = "edit_kb"    # in-place edit that also REPLACES the inline keyboard (edit_message_id + markup_json)


def _db_path() -> Path:
    raw = os.getenv("AIOS_TASK_QUEUE_DB")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".ai-os" / "queue" / "tasks.sqlite3"


def _now() -> float:
    return time.time()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # Fresh DB: create with the typed-row columns LAST, so a freshly-created table has the SAME
    # physical column order as a migrated prod DB (SQLite ADD COLUMN always appends). This keeps
    # the offline test's layout identical to prod (no fresh-vs-migrated divergence).
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'worker',
            created_ts REAL NOT NULL,
            delivered_ts REAL,
            reply_kind TEXT NOT NULL DEFAULT 'send',
            edit_message_id INTEGER,
            markup_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_replies_undelivered ON replies(delivered_ts, id);
        """
    )
    # migration-safe: an existing 0c DB has the flat schema (no reply_kind/edit_message_id).
    # Mirror task_queue.py:92-94 — POSITIONAL PRAGMA access (name is column index 1), robust to
    # row_factory. ADD COLUMN ... DEFAULT 'send' backfills every legacy row to a plain send;
    # edit_message_id has no NOT NULL/DEFAULT, so legacy rows backfill to NULL. Idempotent: the
    # `not in cols` guard makes a second open add nothing.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(replies)").fetchall()}
    if "reply_kind" not in cols:
        conn.execute("ALTER TABLE replies ADD COLUMN reply_kind TEXT NOT NULL DEFAULT 'send'")
    if "edit_message_id" not in cols:
        conn.execute("ALTER TABLE replies ADD COLUMN edit_message_id INTEGER")
    if "markup_json" not in cols:
        conn.execute("ALTER TABLE replies ADD COLUMN markup_json TEXT")


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


def _enqueue(chat_id: int, text: str, source: str, reply_kind: str,
             edit_message_id: "int | None", markup_json: "str | None" = None) -> int:
    ts = _now()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO replies (chat_id, text, source, created_ts, reply_kind, edit_message_id, markup_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (chat_id, text, source, ts, reply_kind, edit_message_id, markup_json),
        )
        return int(cur.lastrowid)


def enqueue_reply(chat_id: int, text: str, source: str = "worker") -> int:
    """Worker side: queue a fresh user-facing reply (KIND_SEND). UNCHANGED public signature.

    Always a plain send: reply_kind=KIND_SEND, edit_message_id=None. Byte-identical to 0c from
    the caller's view (the running bot calls this exactly as before)."""
    return _enqueue(chat_id, text, source, KIND_SEND, None)


def enqueue_edit(chat_id: int, text: str, edit_message_id: int, source: str = "worker") -> int:
    """Worker side: queue an in-place EDIT of an existing message (KIND_EDIT).

    INERT in this slice: nothing live calls this. edit_message_id is a plain int the (future)
    caller supplies — this function imports NO Telegram / CallbackQuery type. The transport
    delivers it via deliver_pending's edit_fn; with no edit_fn an edit row is left undelivered
    (never mis-sent as a plain send). Returns reply id."""
    return _enqueue(chat_id, text, source, KIND_EDIT, edit_message_id)


def enqueue_reply_kb(chat_id: int, text: str, inline_keyboard: list, source: str = "worker") -> int:
    """Worker side: queue a fresh user-facing reply that carries an inline keyboard (KIND_SEND_KB).

    inline_keyboard is a list of button ROWS, each row a list of {"text": ..., "callback_data": ...}
    dicts — plain JSON, so this module imports NO telegram type; the transport rebuilds the
    InlineKeyboardMarkup from markup_json at send time. INERT in this slice: nothing live calls this.
    The transport delivers it via deliver_pending's send_kb_fn; with no send_kb_fn a KB row is left
    UNDELIVERED (never mis-sent as a plain text send). Returns reply id."""
    return _enqueue(chat_id, text, source, KIND_SEND_KB, None, json.dumps(inline_keyboard, ensure_ascii=False))


def enqueue_edit_kb(chat_id: int, text: str, edit_message_id: int, inline_keyboard: list,
                    source: str = "worker") -> int:
    """Worker side: queue an in-place EDIT that also replaces the message's inline keyboard
    (KIND_EDIT_KB) — e.g. a multi-step inline picker editing one message through its steps. Stores
    both edit_message_id and the buttons as plain JSON (no telegram type worker-side). INERT: no live
    caller yet. The transport delivers it via deliver_pending's edit_kb_fn; with no edit_kb_fn the row
    is left UNDELIVERED (never mis-sent). Returns reply id."""
    return _enqueue(chat_id, text, source, KIND_EDIT_KB, edit_message_id,
                    json.dumps(inline_keyboard, ensure_ascii=False))


def claim_undelivered(limit: int = 50) -> list[dict]:
    """Transport side: oldest undelivered replies (delivered_ts IS NULL), oldest first.

    Selects id/chat_id/text PLUS reply_kind/edit_message_id/markup_json — never task_text/result/
    result_json (no personal-data leak through a shared column). Read-only; delivery is recorded by
    mark_delivered AFTER a successful send/edit.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, chat_id, text, reply_kind, edit_message_id, markup_json "
            "FROM replies WHERE delivered_ts IS NULL ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_delivered(reply_id: int) -> bool:
    """Transport side: stamp delivered_ts AFTER a successful Telegram send.

    Idempotent: flips NULL -> ts only, so a hard restart (or a double poll) never re-sends an
    already-delivered row. Returns True iff this call performed the delivery (row was undelivered).
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE replies SET delivered_ts=? WHERE id=? AND delivered_ts IS NULL",
            (_now(), reply_id),
        )
        return cur.rowcount > 0


# Terminal (permanent) Telegram edit-400 reasons: re-issuing the same edit can never succeed,
# so the row is marked delivered (it leaves the backlog) instead of looping forever. Matched on
# the error STRING so this module imports no telegram type (stays stdlib-only / mac-testable).
_EDIT_TERMINAL_MARKERS = ("not modified", "message to edit not found", "message_id_invalid")


def _is_terminal_edit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _EDIT_TERMINAL_MARKERS)


async def deliver_pending(send_fn, edit_fn=None, limit: int = 50, *, send_kb_fn=None, edit_kb_fn=None) -> int:
    """Transport side: claim undelivered replies and dispatch each by reply_kind.

    BACK-COMPAT: deliver_pending(send_fn) — one positional callable — is unchanged from 0c
    (edit_fn / send_kb_fn / edit_kb_fn default None, limit default 50). With no edit_fn / send_kb_fn /
    edit_kb_fn and no edit / keyboard rows ever produced live, behaviour is byte-identical to 0c.

    Dispatch is an ALLOW-LIST on reply_kind (the SOLE discriminator):
      KIND_SEND                               -> await send_fn(chat_id, text)
      KIND_EDIT and edit_fn is not None       -> await edit_fn(chat_id, edit_message_id, text)
      KIND_SEND_KB and send_kb_fn is not None -> await send_kb_fn(chat_id, text, keyboard)
      KIND_EDIT_KB and edit_kb_fn is not None -> await edit_kb_fn(chat_id, edit_message_id, text, keyboard)
      KIND_* with no matching handler, or ANY unknown kind -> left UNDELIVERED (never mis-sent)

    Order: send/edit FIRST, mark_delivered ONLY after success — a failure leaves the row
    undelivered for the next poll (no lost reply); mark_delivered's idempotency prevents a
    double dispatch.

    EDIT failures split terminal vs transient: a permanent Telegram 400 ('not modified' /
    'message to edit not found' / invalid id) is treated as delivered-equivalent (marked +
    skipped) so a poison row never hot-loops forever; a transient edit error is left undelivered,
    exactly like a failed send. Returns the count actually dispatched this pass."""
    delivered = 0
    for r in claim_undelivered(limit):
        kind = r["reply_kind"]

        if kind == KIND_SEND:
            try:
                await send_fn(r["chat_id"], r["text"])
            except Exception:
                continue  # transient: leave undelivered, retried next poll
            if mark_delivered(r["id"]):
                delivered += 1

        elif kind == KIND_EDIT and edit_fn is not None:
            try:
                await edit_fn(r["chat_id"], r["edit_message_id"], r["text"])
            except Exception as exc:
                if _is_terminal_edit_error(exc):
                    # permanent 400 (already-applied / target gone): stamp it so the row leaves
                    # the backlog instead of re-firing the same doomed edit forever.
                    if mark_delivered(r["id"]):
                        delivered += 1
                continue  # transient edit error falls through here: left undelivered (retry)
            if mark_delivered(r["id"]):
                delivered += 1

        elif kind == KIND_SEND_KB and send_kb_fn is not None:
            try:
                keyboard = json.loads(r["markup_json"]) if r["markup_json"] else None
                await send_kb_fn(r["chat_id"], r["text"], keyboard)
            except Exception:
                continue  # transient send error (or bad markup): leave undelivered, retried next poll
            if mark_delivered(r["id"]):
                delivered += 1

        elif kind == KIND_EDIT_KB and edit_kb_fn is not None:
            try:
                keyboard = json.loads(r["markup_json"]) if r["markup_json"] else None
                await edit_kb_fn(r["chat_id"], r["edit_message_id"], r["text"], keyboard)
            except Exception as exc:
                # same terminal-vs-transient split as KIND_EDIT: a permanent 400 (already-applied /
                # target gone) is marked so the row leaves the backlog; a transient error retries.
                if _is_terminal_edit_error(exc):
                    if mark_delivered(r["id"]):
                        delivered += 1
                continue
            if mark_delivered(r["id"]):
                delivered += 1

        else:
            # KIND_EDIT with no edit_fn, OR an unknown kind written by a newer binary during a
            # rolling deploy. Intentionally PARKED: re-selected and skipped every poll, never
            # marked delivered, never mis-sent. Self-resolves once a poller that understands the
            # kind is deployed.
            continue

    return delivered
