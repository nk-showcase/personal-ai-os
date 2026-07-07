"""bot/aios_memory_store.py — encryption-aware SQLite store for MEMORY facts.

Fifth cutover domain: paragraphs of the Notion memory page become rows here. Every fact
is personal free text -> encrypted via the context_cipher chokepoint (cleartext column
kept for dual-read compat, mirrors the notes store). Keyed on the Notion block id
(source_block_id, the AAD identity); new facts get bot-minted `local-<uuid>` ids
(amendment 8). load joins active facts in position order — live parity with
claude_storage.load_memory (which joins block texts with newlines).
"""
from __future__ import annotations

import time

from . import aios_storage as S
from .context_cipher import decrypt_for_storage, encrypt_for_storage


def save_fact(rec, *, db=None) -> None:
    """Upsert one memory fact (keyed on source_block_id). Encrypted when flag+key on."""
    bid = rec.get("source_block_id")
    if not bid:
        raise ValueError("save_fact requires a non-empty source_block_id")
    content = rec.get("content")
    enc = encrypt_for_storage("memory", bid, "content", content) if content is not None else None
    now = time.time()
    with S.connect(db=db) as conn:
        if enc is None:
            conn.execute(
                "INSERT INTO memory_fact (source_block_id, content, content_encrypted, "
                "position, created_time, created_ts) VALUES (?,?,NULL,?,?,?) "
                "ON CONFLICT(source_block_id) DO UPDATE SET content=excluded.content, "
                "content_encrypted=NULL, position=excluded.position, "
                "created_time=excluded.created_time",
                (bid, content, rec.get("position"), rec.get("created_time"), now))
        else:
            conn.execute(
                "INSERT INTO memory_fact (source_block_id, content, content_encrypted, "
                "position, created_time, created_ts) VALUES (?,NULL,?,?,?,?) "
                "ON CONFLICT(source_block_id) DO UPDATE SET content=NULL, "
                "content_encrypted=excluded.content_encrypted, position=excluded.position, "
                "created_time=excluded.created_time",
                (bid, enc, rec.get("position"), rec.get("created_time"), now))


def get_fact(source_block_id, *, db=None):
    """Full fact row (decrypted) for the migration verify step, or None."""
    with S.connect(db=db) as conn:
        row = conn.execute("SELECT * FROM memory_fact WHERE source_block_id=?",
                           (source_block_id,)).fetchone()
    if row is None:
        return None
    content = (decrypt_for_storage(row["content_encrypted"], domain="memory",
                                   row_id=row["source_block_id"], field="content", db=db)
               if row["content_encrypted"] is not None else row["content"])
    return {"source_block_id": row["source_block_id"], "content": content,
            "position": row["position"], "created_time": row["created_time"]}


def load_memory_local(*, db=None) -> str:
    """Live parity with claude_storage.load_memory: active facts, position order,
    non-empty lines joined with newlines."""
    with S.connect(db=db) as conn:
        rows = conn.execute(
            "SELECT source_block_id, content, content_encrypted FROM memory_fact "
            "WHERE archived=0 ORDER BY position, id").fetchall()
    lines = []
    for r in rows:
        content = (decrypt_for_storage(r["content_encrypted"], domain="memory",
                                       row_id=r["source_block_id"], field="content", db=db)
                   if r["content_encrypted"] is not None else r["content"])
        if content:
            lines.append(content)
    return "\n".join(lines)


def append_fact_local(source_block_id, content, *, db=None) -> None:
    """Live append_memory equivalent: new fact at position = max+1."""
    with S.connect(db=db) as conn:
        row = conn.execute("SELECT COALESCE(MAX(position), -1) AS p FROM memory_fact").fetchone()
        pos = int(row["p"]) + 1
    save_fact({"source_block_id": source_block_id, "content": content, "position": pos,
               "created_time": None}, db=db)


def list_local_ids(*, db=None) -> list:
    with S.connect(db=db) as conn:
        return [r["source_block_id"] for r in conn.execute(
            "SELECT source_block_id FROM memory_fact WHERE archived=0 "
            "AND source_block_id IS NOT NULL").fetchall()]


def archive_fact(source_block_id, *, db=None) -> bool:
    with S.connect(db=db) as conn:
        cur = conn.execute("UPDATE memory_fact SET archived=1 WHERE source_block_id=?",
                           (source_block_id,))
        return cur.rowcount > 0


def restore_fact(source_block_id, *, db=None) -> bool:
    with S.connect(db=db) as conn:
        cur = conn.execute("UPDATE memory_fact SET archived=0 WHERE source_block_id=?",
                           (source_block_id,))
        return cur.rowcount > 0
