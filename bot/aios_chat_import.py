"""bot/aios_chat_import.py — Phase 3 importer: Notion chat pages + message blocks -> local encrypted.

NON-DESTRUCTIVE, block domain. For every chat page in NOTION_CLAUDE_CHATS_DB (full pagination):
read its title/status/created_time, then read ALL its child callout blocks (full pagination) as
messages — role from the icon (👤 user / 🤖 assistant), content = all rich_text joined (a segment
missing plain_text raises rather than truncating), position = order among message blocks. Feeds the
aios_migrate engine, verifying title/status/created_time/message_count AND a canonical messages
digest (so a lost/reordered/altered message is caught). Notion is only READ.
"""
from __future__ import annotations

import asyncio

from . import aios_chat_store as C
from . import aios_migrate as M
from .config import NOTION_CLAUDE_CHATS_DB
from .integrations_proxy import notion_request

VERIFY_FIELDS = ["title", "status", "created_time", "message_count", "messages_key"]

_USER_ICON = "\U0001f464"  # 👤


def _join_rt(rich_text) -> str:
    parts = []
    for seg in rich_text or []:
        if "plain_text" not in seg:
            raise ValueError("Notion rich_text segment without plain_text — refusing a lossy read")
        parts.append(seg["plain_text"])
    return "".join(parts)


def _join_title(prop) -> str | None:
    arr = (prop or {}).get("title") or []
    text = _join_rt(arr)
    return text if text != "" else None


async def _read_messages(page_id: str) -> list[dict]:
    """All callout message blocks of a chat page, fully paginated, in order."""
    messages: list[dict] = []
    params: dict = {"page_size": 100}
    pos = 0
    while True:
        r = await notion_request("GET", f"/blocks/{page_id}/children", params=params, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Notion load messages failed: HTTP {r.status_code}")
        data = r.json()
        for block in data.get("results", []):
            if block.get("type") != "callout":
                continue
            callout = block["callout"]
            icon = callout.get("icon", {}).get("emoji", "")
            role = "user" if icon == _USER_ICON else "assistant"
            content = _join_rt(callout.get("rich_text", []))
            if not content:
                continue  # mirror load_chat_messages: empty blocks are not messages
            messages.append({"source_block_id": block["id"], "role": role,
                             "position": pos, "content": content})
            pos += 1
        nxt = data.get("next_cursor")
        if not data.get("has_more") or not nxt:
            break
        params = {"page_size": 100, "start_cursor": nxt}
    return messages


async def _read_all() -> list[dict]:
    if not NOTION_CLAUDE_CHATS_DB:
        raise RuntimeError("NOTION_CLAUDE_CHATS_DB is not configured")
    out: list[dict] = []
    cursor = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = await notion_request(
            "POST", f"/databases/{NOTION_CLAUDE_CHATS_DB}/query", json=body, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"Notion chats query failed: HTTP {resp.status_code}")
        data = resp.json()
        for page in data.get("results", []):
            props = page.get("properties", {})
            status_obj = props.get("Status", {}).get("select")
            messages = await _read_messages(page["id"])
            out.append({
                "source_notion_id": page["id"],
                "title": _join_title(props.get("Name")),
                "status": status_obj.get("name") if status_obj else None,
                "created_time": page.get("created_time"),
                "messages": messages,
                "message_count": len(messages),
                "messages_key": C._messages_key(messages),
            })
        nxt = data.get("next_cursor")
        if not data.get("has_more") or not nxt:
            break
        cursor = nxt
    return out


def _save_one(rec, *, db=None) -> None:
    C.save_chat(rec, db=db)


def _verify_one(sid, *, db=None) -> dict:
    row = C.get_chat(sid, db=db)
    return row if row is not None else {}


def _list_local_ids(*, db=None) -> list:
    from . import aios_storage as S
    with S.connect(db=db) as conn:
        return [r["source_notion_page_id"] for r in conn.execute(
            "SELECT source_notion_page_id FROM chat WHERE archived=0 "
            "AND source_notion_page_id IS NOT NULL").fetchall()]


def _archive_local(sid, *, db=None) -> None:
    C.archive_chat_row(sid, db=db)


def import_chats(*, db=None, reconcile: bool = False):
    """Refuses when flipped; with reconcile=True also tombstones local chats deleted at
    source and reports count parity. Returns (MigrateResult, dict|None)."""
    M.refuse_if_flipped("chat")
    records = asyncio.run(_read_all())
    res = M.migrate_records("chat", records, _save_one, _verify_one,
                            verify_fields=VERIFY_FIELDS, db=db)
    rec_info = None
    if reconcile:
        rec_info = M.reconcile_deletions(
            "chat", [r["source_notion_id"] for r in records], _list_local_ids,
            _archive_local, db=db,
        )
    return res, rec_info
