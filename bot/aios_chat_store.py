"""bot/aios_chat_store.py — encryption-aware SQLite store/read for CHAT history (block domain).

Fourth migration domain, and the only NESTED one: a chat is a Notion PAGE (row in `chat`) whose
messages are child callout BLOCKS (rows in `chat_message`). Both the chat title and every message
body are personal free text -> encrypted via the context_cipher chokepoint. chat_message has NO
cleartext content column (content_encrypted only), so encryption MUST be on; save refuses otherwise.

Page-atomic: save replaces ALL of a chat's messages inside ONE transaction. get_chat returns the
chat + a canonical messages digest (position|role|content over all messages, in order) so the verify
step catches any lost / reordered / altered message, not just the title. Keyed on the Notion page id
(chat.source_notion_page_id) and, per message, the Notion block id (chat_message.source_block_id).
"""
from __future__ import annotations

import hashlib
import time

from . import aios_storage as S
from .context_cipher import decrypt_for_storage, encrypt_for_storage

_SEP = "\x1f"


def _messages_key(messages) -> str:
    """Canonical digest of a message list: sha256 over position|role|content, in position order.
    Any change to count, order, role, or content changes the digest."""
    h = hashlib.sha256()
    for m in sorted(messages, key=lambda x: (x["position"], x.get("source_block_id") or "")):
        h.update(f"{m['position']}{_SEP}{m['role']}{_SEP}{m['content']}{_SEP}".encode("utf-8"))
    return h.hexdigest()


def save_chat(rec, *, db=None) -> None:
    """Upsert one chat page + replace all its messages, atomically. Title and every message body
    are encrypted (flag+key required — chat_message has no cleartext column). Keyed on the page id."""
    sid = rec.get("source_notion_id")
    if not sid:
        raise ValueError("save_chat requires a non-empty source_notion_id (page id)")

    # D3: encrypt everything BEFORE any write. A raise here leaves zero rows touched.
    title = rec.get("title")
    title_enc = encrypt_for_storage("chat", sid, "title", title) if title is not None else None
    enc_msgs = []
    for m in (rec.get("messages") or []):
        content = m.get("content")
        bid = m.get("source_block_id")
        if not bid:
            raise ValueError("chat message without source_block_id (AAD identity)")
        if not content:
            continue  # empty blocks are not messages (mirror the reader)
        ce = encrypt_for_storage("chat_message", bid, "content", content)
        if ce is None:
            # chat_message has no cleartext column — refuse rather than silently drop content.
            raise RuntimeError("chat migration requires encryption ON (no cleartext content column)")
        enc_msgs.append((bid, m.get("role") or "assistant", int(m.get("position") or 0), ce))

    now = time.time()
    with S.connect(db=db) as conn:
        conn.execute("BEGIN")
        try:
            if title_enc is None:
                conn.execute(
                    "INSERT INTO chat (source_notion_page_id, title, title_encrypted, status, "
                    "created_time, created_ts, updated_ts) VALUES (?,?,NULL,?,?,?,?) "
                    "ON CONFLICT(source_notion_page_id) DO UPDATE SET title=excluded.title, "
                    "title_encrypted=NULL, status=excluded.status, created_time=excluded.created_time, "
                    "updated_ts=excluded.updated_ts",
                    (sid, title, rec.get("status"), rec.get("created_time"), now, now),
                )
            else:
                conn.execute(
                    "INSERT INTO chat (source_notion_page_id, title, title_encrypted, status, "
                    "created_time, created_ts, updated_ts) VALUES (?,NULL,?,?,?,?,?) "
                    "ON CONFLICT(source_notion_page_id) DO UPDATE SET title=NULL, "
                    "title_encrypted=excluded.title_encrypted, status=excluded.status, "
                    "created_time=excluded.created_time, updated_ts=excluded.updated_ts",
                    (sid, title_enc, rec.get("status"), rec.get("created_time"), now, now),
                )
            chat_id = conn.execute(
                "SELECT id FROM chat WHERE source_notion_page_id=?", (sid,)).fetchone()["id"]
            conn.execute("DELETE FROM chat_message WHERE chat_id=?", (chat_id,))
            for bid, role, pos, ce in enc_msgs:
                conn.execute(
                    "INSERT INTO chat_message (chat_id, source_block_id, role, position, "
                    "content_encrypted, created_ts) VALUES (?,?,?,?,?,?)",
                    (chat_id, bid, role, pos, ce, now),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_latest_chat_local(*, db=None):
    """Live parity for get_latest_chat: newest ACTIVE chat by last activity (amendment 9 —
    updated_ts bumps on every append/title change), decrypted {id, title} or None."""
    with S.connect(db=db) as conn:
        row = conn.execute(
            "SELECT source_notion_page_id, title, title_encrypted FROM chat "
            "WHERE status='active' AND archived=0 "
            "ORDER BY updated_ts DESC, id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    title = (decrypt_for_storage(row["title_encrypted"], domain="chat",
                                 row_id=row["source_notion_page_id"], field="title", db=db)
             if row["title_encrypted"] is not None else row["title"])
    return {"id": row["source_notion_page_id"], "title": title or "Untitled"}


def count_messages_local(source_notion_id, *, db=None) -> int:
    """STRUCTURE-ONLY: number of message rows in a chat (COUNT, no content decrypted or read).
    0 when the chat does not exist or has no messages. Used by the current-chat pointer op so
    the keyless receiver learns whether the pointed-to chat is empty without seeing any text."""
    with S.connect(db=db) as conn:
        chat = conn.execute("SELECT id FROM chat WHERE source_notion_page_id=?",
                            (source_notion_id,)).fetchone()
        if chat is None:
            return 0
        row = conn.execute("SELECT COUNT(*) AS n FROM chat_message WHERE chat_id=?",
                           (chat["id"],)).fetchone()
    return int(row["n"]) if row else 0


def load_messages_local(source_notion_id, *, db=None) -> list:
    """Live parity for load_chat_messages: [{role, content}] in position order, decrypted."""
    with S.connect(db=db) as conn:
        chat = conn.execute("SELECT id FROM chat WHERE source_notion_page_id=?",
                            (source_notion_id,)).fetchone()
        if chat is None:
            return []
        rows = conn.execute(
            "SELECT source_block_id, role, position, content_encrypted FROM chat_message "
            "WHERE chat_id=? ORDER BY position, id", (chat["id"],)).fetchall()
    out = []
    for r in rows:
        content = decrypt_for_storage(r["content_encrypted"], domain="chat_message",
                                      row_id=r["source_block_id"], field="content", db=db)
        if content:
            out.append({"role": r["role"], "content": content})
    return out


def append_messages_local(source_notion_id, msgs, *, db=None) -> bool:
    """Append message blocks to an existing chat (live save_messages equivalent).
    Each msg = {source_block_id (bot-minted), role, content}; encrypted, positions continue
    from the current max, chat.updated_ts bumps (drives get_latest ordering). One transaction."""
    enc = []
    for m in msgs:
        bid = m.get("source_block_id")
        if not bid:
            raise ValueError("append_messages_local: message without source_block_id")
        content = m.get("content") or ""
        ce = encrypt_for_storage("chat_message", bid, "content", content)
        if ce is None:
            raise RuntimeError("chat store requires encryption ON (no cleartext content column)")
        enc.append((bid, m.get("role") or "assistant", ce))
    now = time.time()
    with S.connect(db=db) as conn:
        chat = conn.execute("SELECT id FROM chat WHERE source_notion_page_id=?",
                            (source_notion_id,)).fetchone()
        if chat is None:
            return False
        conn.execute("BEGIN")
        try:
            pos_row = conn.execute("SELECT COALESCE(MAX(position), -1) AS p FROM chat_message "
                                   "WHERE chat_id=?", (chat["id"],)).fetchone()
            pos = int(pos_row["p"]) + 1
            for bid, role, ce in enc:
                conn.execute(
                    "INSERT INTO chat_message (chat_id, source_block_id, role, position, "
                    "content_encrypted, created_ts) VALUES (?,?,?,?,?,?)",
                    (chat["id"], bid, role, pos, ce, now))
                pos += 1
            conn.execute("UPDATE chat SET updated_ts=? WHERE id=?", (now, chat["id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return True


def set_chat_title_local(source_notion_id, title, *, db=None) -> bool:
    """Re-encrypt and update the title; bumps updated_ts (mirrors Notion last_edited)."""
    enc = encrypt_for_storage("chat", source_notion_id, "title", title)
    now = time.time()
    with S.connect(db=db) as conn:
        if enc is None:
            cur = conn.execute("UPDATE chat SET title=?, title_encrypted=NULL, updated_ts=? "
                               "WHERE source_notion_page_id=?", (title, now, source_notion_id))
        else:
            cur = conn.execute("UPDATE chat SET title=NULL, title_encrypted=?, updated_ts=? "
                               "WHERE source_notion_page_id=?", (enc, now, source_notion_id))
        return cur.rowcount > 0


def set_chat_status_local(source_notion_id, status, *, db=None) -> bool:
    """Live archive_chat equivalent: status select ('active'/'archived')."""
    with S.connect(db=db) as conn:
        cur = conn.execute("UPDATE chat SET status=?, updated_ts=? WHERE source_notion_page_id=?",
                           (status, time.time(), source_notion_id))
        return cur.rowcount > 0


def archive_chat_row(source_notion_id, *, db=None) -> bool:
    """Amendment-6 tombstone (delete_page routing compat) — distinct from status='archived'."""
    with S.connect(db=db) as conn:
        cur = conn.execute("UPDATE chat SET archived=1 WHERE source_notion_page_id=?",
                           (source_notion_id,))
        return cur.rowcount > 0


def restore_chat_row(source_notion_id, *, db=None) -> bool:
    with S.connect(db=db) as conn:
        cur = conn.execute("UPDATE chat SET archived=0 WHERE source_notion_page_id=?",
                           (source_notion_id,))
        return cur.rowcount > 0


def get_chat(source_notion_id, *, db=None):
    """Return the chat + its decrypted messages and a canonical messages digest, or None."""
    with S.connect(db=db) as conn:
        row = conn.execute(
            "SELECT * FROM chat WHERE source_notion_page_id=?", (source_notion_id,)).fetchone()
        if row is None:
            return None
        chat_id = row["id"]
        msg_rows = conn.execute(
            "SELECT source_block_id, role, position, content_encrypted FROM chat_message "
            "WHERE chat_id=? ORDER BY position, source_block_id", (chat_id,)).fetchall()

    title = (decrypt_for_storage(row["title_encrypted"], domain="chat",
                                 row_id=row["source_notion_page_id"], field="title", db=db)
             if row["title_encrypted"] is not None else row["title"])
    messages = []
    for mr in msg_rows:
        content = decrypt_for_storage(mr["content_encrypted"], domain="chat_message",
                                      row_id=mr["source_block_id"], field="content", db=db)
        messages.append({"source_block_id": mr["source_block_id"], "role": mr["role"],
                         "position": mr["position"], "content": content})
    return {
        "source_notion_id": row["source_notion_page_id"],
        "title": title, "status": row["status"], "created_time": row["created_time"],
        "message_count": len(messages), "messages_key": _messages_key(messages),
    }
