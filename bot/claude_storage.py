"""Notion storage for Claude chat history and memory."""

import logging
from datetime import date

from .config import (
    NOTION_CLAUDE_CHATS_DB,
    NOTION_CLAUDE_MEMORY_PAGE,
    notion_raw_allowed,
)
# Path B: route Notion through the integrations-worker when keyless (transport).
# In a keyed process this calls Notion directly, so behavior is unchanged there.
from .integrations_proxy import notion_request, store_request, write_business_key

logger = logging.getLogger(__name__)

RICH_TEXT_LIMIT = 2000


def _chat_sqlite() -> bool:
    """Cutover flag for the chat domain (one combined read+write flag, amendment 4).
    Fail-safe 'notion' = today's behavior."""
    try:
        from . import aios_config as CFG
        return CFG.get_store_mode("chat") == "sqlite"
    except Exception:
        return False


def _memory_sqlite() -> bool:
    """Cutover flag for the memory domain. Fail-safe 'notion'."""
    try:
        from . import aios_config as CFG
        return CFG.get_store_mode("memory") == "sqlite"
    except Exception:
        return False


def _new_local_id() -> str:
    """Bot-minted synthetic id (page or message block, amendment 8)."""
    import uuid
    return f"local-{uuid.uuid4()}"


def _neutral_chat_title() -> str:
    """Block -1C: a neutral chat title that contains NO free user text."""
    return f"Chat {date.today().isoformat()}"


def _rich_text(text: str) -> list:
    """Split text into Notion rich_text array (2000 char chunks)."""
    if not text:
        return [{"type": "text", "text": {"content": ""}}]
    parts = []
    while text:
        parts.append({"type": "text", "text": {"content": text[:RICH_TEXT_LIMIT]}})
        text = text[RICH_TEXT_LIMIT:]
    return parts


def _message_block(role: str, content: str) -> dict:
    """Create a Notion callout block for a chat message."""
    icon = "\U0001f464" if role == "user" else "\U0001f916"
    return {
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": icon},
            "rich_text": _rich_text(content),
        },
    }


# ── Chat CRUD ──────────────────────────────────────────────


async def create_chat(title: str = "New chat") -> str | None:
    """Create a new chat. Returns page_id (local: bot-minted 'local-<uuid>') or None."""
    if _chat_sqlite():
        # Destination-aware gate (amendment 4): the local store is encrypted at rest,
        # so the REAL title persists here — the neutral-title rule is Notion-only.
        sid = _new_local_id()
        res = await store_request(
            "chat", "create_chat", {"id": sid, "title": title}, is_write=True,
            business_key=write_business_key("chat", title, date.today().isoformat()),
        )
        return (res.get("id") or sid) if res.get("ok") else None

    if not NOTION_CLAUDE_CHATS_DB:
        return None
    # When raw Notion persist is OFF, never store a free-text title
    # (e.g. one derived from the task text). The page still exists (structured); only the title is neutral.
    if not notion_raw_allowed():
        title = _neutral_chat_title()
    body = {
        "parent": {"database_id": NOTION_CLAUDE_CHATS_DB},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Status": {"select": {"name": "active"}},
        },
    }
    # Idempotent: a stable key (chat|title|date) so a transport retry of the same
    # create reuses the row instead of spawning a duplicate chat page.
    r = await notion_request(
        "POST", "/pages", json=body,
        business_key=write_business_key("chat", title, date.today().isoformat()),
    )
    if r.status_code == 200:
        return r.json()["id"]
    logger.warning(f"create_chat failed: {r.status_code} {r.text}")
    return None


async def get_latest_chat() -> dict | None:
    """Get the most recent active chat. Returns {id, title} or None."""
    if _chat_sqlite():
        res = await store_request("chat", "get_latest_chat", {}, is_write=False)
        return res.get("chat") if res.get("ok") else None

    if not NOTION_CLAUDE_CHATS_DB:
        return None
    body = {
        "filter": {"property": "Status", "select": {"equals": "active"}},
        "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        "page_size": 1,
    }
    r = await notion_request(
        "POST", f"/databases/{NOTION_CLAUDE_CHATS_DB}/query", json=body,
    )
    if r.status_code != 200:
        logger.warning(f"get_latest_chat failed: {r.status_code}")
        return None
    results = r.json().get("results", [])
    if not results:
        return None
    page = results[0]
    title_prop = page["properties"].get("Name", {}).get("title", [])
    title = title_prop[0]["plain_text"] if title_prop else "Untitled"
    return {"id": page["id"], "title": title}


async def get_current_chat() -> dict | None:
    """Read the durable current-chat pointer (structure only): {page_id, msg_count} or None.
    This is the real "Continue" source of truth — the keyed worker stamps the pointer on every
    persisted chat turn and on new-chat create, so it points at the owner's genuinely-last
    conversation even when that chat is archived (which get_latest_chat, filtering active, misses).
    Keyless by design: the receiver never touches the DB, it goes through the store proxy; the
    result carries NO decrypted transcript. None when the pointer is unset or the worker is
    unavailable (the caller then falls back to get_latest_chat)."""
    res = await store_request("chat", "current", {}, is_write=False)
    if not res.get("ok"):
        return None
    pid = res.get("page_id")
    if not pid:
        return None
    return {"page_id": pid, "msg_count": int(res.get("msg_count") or 0)}


async def load_chat_messages(page_id: str) -> list[dict]:
    """Load messages from a chat page. Returns [{role, content}, ...]."""
    if _chat_sqlite():
        res = await store_request("chat", "load_messages", {"sid": page_id}, is_write=False)
        return (res.get("messages") or []) if res.get("ok") else []

    messages = []
    path = f"/blocks/{page_id}/children"
    params: dict = {"page_size": 100}
    while True:
        r = await notion_request("GET", path, params=params, timeout=30)
        if r.status_code != 200:
            logger.warning(f"load_chat_messages failed: {r.status_code}")
            break
        data = r.json()
        for block in data.get("results", []):
            if block["type"] != "callout":
                continue
            callout = block["callout"]
            icon = callout.get("icon", {}).get("emoji", "")
            role = "user" if icon == "\U0001f464" else "assistant"
            text = "".join(
                rt.get("plain_text", "") for rt in callout.get("rich_text", [])
            )
            if text:
                messages.append({"role": role, "content": text})
        if data.get("has_more"):
            params = {"page_size": 100, "start_cursor": data["next_cursor"]}
        else:
            break
    return messages


async def save_messages(page_id: str, user_msg: str, assistant_msg: str) -> bool:
    """Append user + assistant message blocks to a chat page."""
    if _chat_sqlite():
        # Destination-aware gate: transcripts DO persist locally — encrypted at rest,
        # with no plaintext in the file. The skip-write rule is Notion-only
        # (Notion stores plaintext). Block ids are bot-minted.
        import hashlib
        turn_hash = hashlib.sha1(
            f"{user_msg}\x00{assistant_msg}".encode("utf-8")).hexdigest()[:16]
        res = await store_request(
            "chat", "append_messages",
            {"sid": page_id, "msgs": [
                {"source_block_id": _new_local_id(), "role": "user", "content": user_msg},
                {"source_block_id": _new_local_id(), "role": "assistant", "content": assistant_msg},
            ]},
            is_write=True,
            business_key=write_business_key("save_msg", page_id, turn_hash),
        )
        if not res.get("ok"):
            logger.warning(f"save_messages(local) failed: {res.get('error')}")
        return bool(res.get("ok"))

    # Block -1C: raw transcript persistence to Notion is gated. When OFF, skip the
    # write entirely — no Notion API call, no raw text leaves; history stays in RAM.
    if not notion_raw_allowed():
        logger.info("save_messages: raw Notion persist OFF — transcript not written")
        return True
    blocks = [
        _message_block("user", user_msg),
        _message_block("assistant", assistant_msg),
    ]
    try:
        # Idempotent: key on page + a hash of this exact turn so a transport retry of
        # the SAME append reuses the row instead of duplicating the message blocks,
        # while a genuinely new turn (different text) appends normally.
        import hashlib
        turn_hash = hashlib.sha1(
            f"{user_msg}\x00{assistant_msg}".encode("utf-8")
        ).hexdigest()[:16]
        r = await notion_request(
            "PATCH", f"/blocks/{page_id}/children",
            json={"children": blocks}, timeout=15,
            business_key=write_business_key("save_msg", page_id, turn_hash),
        )
        if r.status_code != 200:
            logger.warning(f"save_messages failed: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        logger.warning(f"save_messages error: {e}")
        return False


async def update_chat_title(page_id: str, title: str) -> bool:
    """Update chat page title."""
    if _chat_sqlite():
        # Local: the REAL title persists (encrypted at rest) — neutral-title rule is Notion-only.
        res = await store_request("chat", "set_title",
                                  {"sid": page_id, "title": title[:100]}, is_write=True)
        return bool(res.get("ok"))

    # Block -1C: when raw OFF, never derive the title from free user text — use neutral.
    if not notion_raw_allowed():
        title = _neutral_chat_title()
    body = {
        "properties": {
            "Name": {"title": [{"text": {"content": title[:100]}}]},
        }
    }
    try:
        r = await notion_request(
            "PATCH", f"/pages/{page_id}", json=body, timeout=15,
            business_key=write_business_key("update_chat_title", page_id),
        )
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"update_chat_title error: {e}")
        return False


async def archive_chat(page_id: str) -> bool:
    """Set chat status to archived."""
    if _chat_sqlite():
        res = await store_request("chat", "set_status",
                                  {"sid": page_id, "status": "archived"}, is_write=True)
        return bool(res.get("ok"))

    body = {"properties": {"Status": {"select": {"name": "archived"}}}}
    try:
        r = await notion_request(
            "PATCH", f"/pages/{page_id}", json=body, timeout=15,
            business_key=write_business_key("archive_chat", page_id),
        )
        return r.status_code == 200
    except Exception as e:
        logger.warning(f"archive_chat error: {e}")
        return False


# ── Memory ─────────────────────────────────────────────────


async def load_memory() -> str:
    """Read memory content as text."""
    if _memory_sqlite():
        res = await store_request("memory", "load", {}, is_write=False)
        return (res.get("text") or "") if res.get("ok") else ""

    if not NOTION_CLAUDE_MEMORY_PAGE:
        return ""
    path = f"/blocks/{NOTION_CLAUDE_MEMORY_PAGE}/children"
    params: dict = {"page_size": 100}
    lines = []
    while True:
        r = await notion_request("GET", path, params=params, timeout=15)
        if r.status_code != 200:
            logger.warning(f"load_memory failed: {r.status_code}")
            break
        data = r.json()
        for block in data.get("results", []):
            btype = block.get("type", "")
            rich_text = block.get(btype, {}).get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text:
                lines.append(text)
        if data.get("has_more"):
            params = {"page_size": 100, "start_cursor": data["next_cursor"]}
        else:
            break
    return "\n".join(lines)


async def append_memory(text: str) -> bool:
    """Append a memory fact."""
    if _memory_sqlite():
        # Destination-aware gate (amendment 4): memory facts PERSIST locally (encrypted
        # at rest) — the skip-write rule below is Notion-only. Same fact-hash dedupe key.
        import hashlib
        fact_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        res = await store_request(
            "memory", "append", {"bid": _new_local_id(), "text": text}, is_write=True,
            business_key=write_business_key("append_memory", "local", fact_hash),
        )
        if not res.get("ok"):
            logger.warning(f"append_memory(local) failed: {res.get('error')}")
        return bool(res.get("ok"))

    # Raw free-text memory facts are gated. When OFF, no-op success —
    # no Notion API call, nothing raw persisted (the encrypted local store is the
    # persistence path; see the _memory_sqlite branch above).
    if not notion_raw_allowed():
        logger.info("append_memory: raw Notion persist OFF — memory fact not written")
        return True
    if not NOTION_CLAUDE_MEMORY_PAGE:
        return False
    block = {
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text)},
    }
    # Idempotent: key on the memory page + a hash of this fact so a transport retry
    # of the SAME append reuses the row instead of duplicating the paragraph, while
    # a genuinely new fact (different text) appends normally.
    import hashlib
    fact_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    r = await notion_request(
        "PATCH", f"/blocks/{NOTION_CLAUDE_MEMORY_PAGE}/children",
        json={"children": [block]},
        business_key=write_business_key("append_memory", NOTION_CLAUDE_MEMORY_PAGE, fact_hash),
    )
    if r.status_code != 200:
        logger.warning(f"append_memory failed: {r.status_code} {r.text}")
        return False
    return True
