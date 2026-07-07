"""bot/aios_memory_import.py — Notion memory page -> local ENCRYPTED memory_fact store.

Fifth cutover domain. Reads the memory page's child blocks (same walk as
claude_storage.load_memory, but keeping each block's id/position), then runs the
shared migration engine: encrypt-on-save, fresh-connection verify of every field,
idempotent upsert by block id, refuse-if-flipped, reconcile for the final catch-up.
Notion is strictly READ-ONLY here.
"""
from __future__ import annotations

import asyncio

from . import aios_memory_store as MM
from . import aios_migrate as M
from .config import NOTION_CLAUDE_MEMORY_PAGE
from .integrations_proxy import notion_request

VERIFY_FIELDS = ["content", "position"]


async def _read_all() -> list[dict]:
    if not NOTION_CLAUDE_MEMORY_PAGE:
        return []
    out: list[dict] = []
    path = f"/blocks/{NOTION_CLAUDE_MEMORY_PAGE}/children"
    params: dict = {"page_size": 100}
    pos = 0
    while True:
        r = await notion_request("GET", path, params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"memory read failed: HTTP {r.status_code}")
        data = r.json()
        for block in data.get("results", []):
            btype = block.get("type", "")
            rich_text = block.get(btype, {}).get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if not text:
                continue  # empty blocks are not facts (mirror the live reader)
            out.append({
                "source_notion_id": block["id"],  # engine identity key
                "source_block_id": block["id"],
                "content": text,
                "position": pos,
                "created_time": block.get("created_time"),
            })
            pos += 1
        if data.get("has_more"):
            params = {"page_size": 100, "start_cursor": data["next_cursor"]}
        else:
            break
    return out


def _save_one(rec, *, db=None) -> None:
    MM.save_fact(rec, db=db)


def _verify_one(sid, *, db=None) -> dict:
    row = MM.get_fact(sid, db=db)
    return row if row is not None else {}


def _archive_local(sid, *, db=None) -> None:
    MM.archive_fact(sid, db=db)


def import_memory(*, db=None, reconcile: bool = False):
    """Refuses when flipped; with reconcile=True also tombstones local facts deleted at
    source and reports count parity. Returns (MigrateResult, dict|None)."""
    M.refuse_if_flipped("memory")
    records = asyncio.run(_read_all())
    res = M.migrate_records("memory", records, _save_one, _verify_one,
                            verify_fields=VERIFY_FIELDS, db=db)
    rec_info = None
    if reconcile:
        rec_info = M.reconcile_deletions(
            "memory", [r["source_notion_id"] for r in records], MM.list_local_ids,
            _archive_local, db=db,
        )
    return res, rec_info
