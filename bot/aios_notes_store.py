"""bot/aios_notes_store.py — encryption-aware SQLite store for the reference "notes" demo domain.

Third reference domain (after chat + memory): a note is a single free-text string. Every note is
encrypted at rest via the context_cipher chokepoint (a cleartext column is kept only for the
flag-OFF dual-read path, mirroring aios_memory_store). Notes are keyed on a bot-minted note_id
(the AAD identity); this module never invents ids for a save, so a crash-retry of the same queue
row rewrites the SAME row and is idempotent.

There are NO personal fields: a note carries only opaque text + a bot-minted id + a timestamp.
get_summary returns a value-free structural summary (a count plus short previews), never the full
note bodies, so a read summary can travel back through the shared queue without leaking content.
"""
from __future__ import annotations

import time

from . import aios_storage as S
from .context_cipher import decrypt_for_storage, encrypt_for_storage


def save_note(note_id: str, content: str, *, db=None) -> str:
    """Upsert one note (keyed on note_id). Encrypted at rest when the flag + key are on.

    note_id MUST be a non-empty bot-minted id (the AAD binding). Returns the note_id.
    """
    if not note_id:
        raise ValueError("save_note requires a non-empty note_id (bot-minted, AAD binding)")
    if content is None:
        raise ValueError("save_note requires note content")
    enc = encrypt_for_storage("notes", note_id, "content", content)
    now = time.time()
    with S.connect(db=db) as conn:
        if enc is None:
            conn.execute(
                "INSERT INTO note (note_id, content, content_encrypted, created_ts) "
                "VALUES (?,?,NULL,?) "
                "ON CONFLICT(note_id) DO UPDATE SET content=excluded.content, "
                "content_encrypted=NULL",
                (note_id, content, now))
        else:
            conn.execute(
                "INSERT INTO note (note_id, content, content_encrypted, created_ts) "
                "VALUES (?,NULL,?,?) "
                "ON CONFLICT(note_id) DO UPDATE SET content=NULL, "
                "content_encrypted=excluded.content_encrypted",
                (note_id, enc, now))
    return note_id


def get_note(note_id: str, *, db=None):
    """Full note row (decrypted), or None. Used by tests/verify; the decrypted body stays in the
    keyed worker and is never returned through the shared queue by get_summary."""
    with S.connect(db=db) as conn:
        row = conn.execute("SELECT * FROM note WHERE note_id=?", (note_id,)).fetchone()
    if row is None:
        return None
    content = (decrypt_for_storage(row["content_encrypted"], domain="notes",
                                   row_id=row["note_id"], field="content", db=db)
               if row["content_encrypted"] is not None else row["content"])
    return {"note_id": row["note_id"], "content": content, "created_ts": row["created_ts"]}


def get_note_body(note_id: str, *, db=None):
    """Decrypted note body (the `content` field), or None if the note does not exist. Thin
    accessor over get_note used by the encryption-slice + migration tests: it returns ONLY the
    free-text body, never the structural row. A decrypt failure quarantines + raises inside the
    chokepoint (get_note), so this never returns tampered text as cleartext."""
    got = get_note(note_id, db=db)
    return None if got is None else got["content"]


def count_notes(*, db=None) -> int:
    with S.connect(db=db) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM note WHERE archived=0").fetchone()
    return int(row["n"])


def get_summary(*, preview_limit: int = 3, preview_chars: int = 40, db=None) -> dict:
    """Value-free structural summary of the saved notes: a total count plus up to `preview_limit`
    short previews of the most recent notes (each truncated to `preview_chars`). This is the ONLY
    read op exposed as a demo view — it never returns full note bodies, so the summary is safe to
    carry back through the shared queue.
    """
    with S.connect(db=db) as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) AS n FROM note WHERE archived=0").fetchone()["n"])
        rows = conn.execute(
            "SELECT note_id, content, content_encrypted FROM note "
            "WHERE archived=0 ORDER BY created_ts DESC, id DESC LIMIT ?",
            (preview_limit,)).fetchall()
    previews = []
    for r in rows:
        content = (decrypt_for_storage(r["content_encrypted"], domain="notes",
                                       row_id=r["note_id"], field="content", db=db)
                   if r["content_encrypted"] is not None else r["content"])
        text = (content or "").strip().replace("\n", " ")
        if len(text) > preview_chars:
            text = text[:preview_chars].rstrip() + "..."
        previews.append(text)
    return {"count": total, "previews": previews}


def list_note_ids(*, db=None) -> list:
    with S.connect(db=db) as conn:
        return [r["note_id"] for r in conn.execute(
            "SELECT note_id FROM note WHERE archived=0 AND note_id IS NOT NULL").fetchall()]


def archive_note(note_id: str, *, db=None) -> bool:
    with S.connect(db=db) as conn:
        cur = conn.execute("UPDATE note SET archived=1 WHERE note_id=?", (note_id,))
        return cur.rowcount > 0


def restore_note(note_id: str, *, db=None) -> bool:
    with S.connect(db=db) as conn:
        cur = conn.execute("UPDATE note SET archived=0 WHERE note_id=?", (note_id,))
        return cur.rowcount > 0
