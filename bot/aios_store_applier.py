"""bot/aios_store_applier.py — keyed-worker applier for local ENCRYPTED store ops.

Executes store_read / store_write queue rows. Runs ONLY in the keyed worker (the process that
holds the age identity): every op refuses fail-closed when encryption is OFF, so a mis-deployed
keyless process can never write cleartext into the store or serve unverified reads.

The op registry is (domain, op) -> callable. This reference build wires three domains against
their encryption-aware stores: `chat` (conversation transcripts), `memory` (long-term facts) and
`notes` (the published demo domain). Synthetic ids for NEW records are minted by the BOT at enqueue
time and travel in the payload — this applier never invents ids, so a crash-retry of the same row
rewrites the SAME row and is idempotent.

The VIEW registry (store_view rows) renders a reply and sends it to Telegram DIRECTLY in the keyed
worker: decrypted content never returns through the shared queue, only structure (ok/sent + a
value-free summary). `chat.chat_turn` is the reference end-to-end direct-send view; `notes.summary`
mirrors it for the demo domain.

BOUNDARY (privacy): store_write payloads carry text through the shared queue the same way the
integration write payloads do; sealing them to the worker recipient (store_seal machinery) is an
optional hardening — see docs/security/threat-model.md.
"""
from __future__ import annotations

import logging

from .context_key import encryption_enabled, get_kek

log = logging.getLogger("aios.store_applier")


class StoreUnavailable(RuntimeError):
    """Encryption/key not available in this process — op refused (fail-closed)."""


def _require_keyed() -> None:
    if not encryption_enabled():
        raise StoreUnavailable("store op refused: AIOS_CONTEXT_ENCRYPTION is OFF in this process")
    if get_kek() is None:
        raise StoreUnavailable("store op refused: KEK is not available in this process")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _new_local_id() -> str:
    import uuid
    return f"local-{uuid.uuid4()}"


# ---- chat domain --------------------------------------------------------------------

def _chat_create(args: dict) -> dict:
    from . import aios_chat_store as C
    from . import aios_config as CFG
    sid = args.get("id")
    if not sid:
        raise ValueError("chat.create_chat requires a bot-minted id")
    C.save_chat({"source_notion_id": sid, "title": args.get("title") or "New chat",
                 "status": "active", "created_time": _now_iso(), "messages": []})
    # Stamp the durable current-chat pointer to the NEWLY created (empty) page so that a
    # "Continue" right after "New chat" resumes THIS empty chat (msg_count 0 -> the receiver
    # falls through to get_latest, not resurrecting the previous conversation).
    CFG.set_current_chat_page_id(sid)
    return {"ok": True, "id": sid}


def _chat_latest(args: dict) -> dict:
    from . import aios_chat_store as C
    return {"ok": True, "chat": C.get_latest_chat_local()}


def _chat_current(args: dict) -> dict:
    """STRUCTURE-ONLY read of the durable current-chat pointer: {ok, page_id, msg_count}.
    page_id comes from the non-encrypted app_config pointer; msg_count counts message rows for
    that chat (COUNT only — NO decrypted content is read or returned). page_id None (pointer
    unset) -> msg_count 0. The receiver uses this to resume the real last chat by pointer even
    when it is archived (which get_latest_chat_local, filtering status='active', cannot see)."""
    from . import aios_chat_store as C
    from . import aios_config as CFG
    pid = CFG.get_current_chat_page_id()
    if not pid:
        return {"ok": True, "page_id": None, "msg_count": 0}
    return {"ok": True, "page_id": pid, "msg_count": C.count_messages_local(pid)}


def _chat_load(args: dict) -> dict:
    from . import aios_chat_store as C
    return {"ok": True, "messages": C.load_messages_local(args.get("sid"))}


def _chat_append(args: dict) -> dict:
    from . import aios_chat_store as C
    ok = C.append_messages_local(args.get("sid"), args.get("msgs") or [])
    return {"ok": ok} if ok else {"ok": False, "error": "not_found"}


def _chat_set_title(args: dict) -> dict:
    from . import aios_chat_store as C
    return {"ok": C.set_chat_title_local(args.get("sid"), args.get("title") or "")}


def _chat_set_status(args: dict) -> dict:
    from . import aios_chat_store as C
    return {"ok": C.set_chat_status_local(args.get("sid"), args.get("status") or "active")}


def _chat_archive(args: dict) -> dict:
    from . import aios_chat_store as C
    return {"ok": C.archive_chat_row(args.get("sid"))}


def _chat_restore(args: dict) -> dict:
    from . import aios_chat_store as C
    return {"ok": C.restore_chat_row(args.get("sid"))}


# ---- memory domain ------------------------------------------------------------------

def _memory_load(args: dict) -> dict:
    from . import aios_memory_store as MM
    return {"ok": True, "text": MM.load_memory_local()}


def _memory_append(args: dict) -> dict:
    from . import aios_memory_store as MM
    bid = args.get("bid")
    if not bid:
        raise ValueError("memory.append requires a bot-minted bid")
    MM.append_fact_local(bid, args.get("text") or "")
    return {"ok": True, "id": bid}


def _memory_archive(args: dict) -> dict:
    from . import aios_memory_store as MM
    return {"ok": MM.archive_fact(args.get("sid"))}


def _memory_restore(args: dict) -> dict:
    from . import aios_memory_store as MM
    return {"ok": MM.restore_fact(args.get("sid"))}


# ---- notes domain (reference demo) --------------------------------------------------
# The published end-to-end demo: a single free-text note, encrypted at rest. save_note WRITES one
# note (bot-minted id -> idempotent retry); get_summary READS a value-free structural summary.

def _notes_save(args: dict) -> dict:
    from . import aios_notes_store as N
    note_id = args.get("note_id")
    if not note_id:
        raise ValueError("notes.save_note requires a bot-minted note_id")
    N.save_note(note_id, args.get("content") or "")
    return {"ok": True, "id": note_id}


def _notes_summary(args: dict) -> dict:
    from . import aios_notes_store as N
    return {"ok": True, "summary": N.get_summary()}


def _notes_archive(args: dict) -> dict:
    from . import aios_notes_store as N
    return {"ok": N.archive_note(args.get("note_id"))}


def _notes_restore(args: dict) -> dict:
    from . import aios_notes_store as N
    return {"ok": N.restore_note(args.get("note_id"))}


# (domain, op) -> callable(args) -> result dict. Extended per domain; unknown ops fail loudly.
STORE_OPS = {
    ("chat", "create_chat"): _chat_create,
    ("chat", "get_latest_chat"): _chat_latest,
    ("chat", "current"): _chat_current,
    ("chat", "load_messages"): _chat_load,
    ("chat", "append_messages"): _chat_append,
    ("chat", "set_title"): _chat_set_title,
    ("chat", "set_status"): _chat_set_status,
    ("chat", "archive_record"): _chat_archive,
    ("chat", "restore_record"): _chat_restore,
    ("memory", "load"): _memory_load,
    ("memory", "append"): _memory_append,
    ("memory", "archive_record"): _memory_archive,
    ("memory", "restore_record"): _memory_restore,
    ("notes", "save_note"): _notes_save,
    ("notes", "get_summary"): _notes_summary,
    ("notes", "archive_record"): _notes_archive,
    ("notes", "restore_record"): _notes_restore,
}


# ---- views: render + DIRECT Telegram send in the keyed worker ------------------------
# Decrypted text never returns through the queue; the result carries structure only
# (ok/sent/summary). Without the Telegram token every view reports
# {"ok": False, "send_unavailable": True} and the bot falls back to its non-direct path.

def _view_chat_turn(args: dict) -> dict:
    """The reference end-to-end direct-send view: an ENTIRE Claude-chat turn runs in the keyed
    worker. Memory + history are loaded from the local ENCRYPTED store, Claude is called with the
    key IN this process (direct — no queue), memory-saves and the transcript are persisted locally,
    and the reply is sent to Telegram DIRECTLY. The assistant reply, the loaded memory and the
    history never return through the shared queue. The bot receives only structure.

    args: {chat_id, page_id, user_text, first_turn}. Runs the async turn on a private loop
    (the applier is sync, called from the worker's executor)."""
    import asyncio as _asyncio
    from . import aios_chat_store as C
    from . import aios_memory_store as MM
    from . import chat_turn as CT
    from . import worker_telegram as WT

    if not WT.token_present():
        return {"ok": False, "send_unavailable": True}
    chat_id = args.get("chat_id")
    page_id = args.get("page_id")
    user_text = (args.get("user_text") or "").strip()
    if not chat_id or not page_id or not user_text:
        raise ValueError("chat.chat_turn view requires chat_id, page_id, user_text")

    prior = C.load_messages_local(page_id)
    first_turn = len(prior) == 0  # store is the source of truth
    history = prior[-CT.MAX_HISTORY:]
    history.append({"role": "user", "content": user_text})
    memory = MM.load_memory_local()

    async def _go():
        return await CT.run_claude_turn(history, memory)

    def _empty(res) -> bool:
        return not res or res.get("reply") == CT.EMPTY_REPLY

    loop = _asyncio.new_event_loop()
    try:
        result = None
        for _attempt in range(2):  # one silent auto-retry, SAME history/memory (no re-type)
            try:
                result = loop.run_until_complete(_go())
            except CT.TurnTimeout:
                result = None
            if not _empty(result):
                break
        if _empty(result):
            WT.send_message(chat_id, CT.CHAT_RETRY_SOFT_MSG)
            return {"ok": True, "sent": True, "turn_failed": True}
    finally:
        loop.close()

    reply = result["reply"]
    # SEND FIRST, then persist. If the Telegram send raises (non-200 -> RuntimeError, e.g. 429 on a
    # long reply — exactly the slow turns that also blow the receiver's poll window), it propagates
    # and the queue retries the WHOLE row: because NOTHING was written yet, the retry is clean — no
    # duplicate transcript turn, no repeated memory-save, no second landed reply, no wasted second
    # LLM turn persisted; first_turn stays correct. WorkerSendUnavailable (no token) is caught by
    # the token check above.
    WT.send_message(chat_id, reply)
    # Persist memory-saves locally (deduped against current memory).
    seen = set(memory.split("\n"))
    for fact in result.get("memory_saves") or []:
        if fact and fact not in seen:
            MM.append_fact_local(_new_local_id(), fact)
            seen.add(fact)
    # Persist the transcript turn locally (bot-minted block ids).
    C.append_messages_local(page_id, [
        {"source_block_id": _new_local_id(), "role": "user", "content": user_text},
        {"source_block_id": _new_local_id(), "role": "assistant", "content": reply},
    ])
    if first_turn:
        C.set_chat_title_local(page_id, user_text[:50] + ("..." if len(user_text) > 50 else ""))
    # Stamp the durable current-chat pointer to this page AFTER the turn is persisted, so
    # "Continue" resumes the owner's genuinely-last conversation — even if it was later
    # archived (get_latest_chat_local, filtering status='active', cannot see an archived chat).
    from . import aios_config as CFG
    CFG.set_current_chat_page_id(page_id)
    return {"ok": True, "sent": True, "turn_failed": False}


def _view_chat_resume(args: dict) -> dict:
    """"Continue" fallback (last-active-chat leg) — the keyed worker resolves the newest active
    chat, renders the resume line (decrypted title + user-message count) and sends it to Telegram
    DIRECTLY. The title and the transcript never return through the shared queue: the receiver
    gets STRUCTURE ONLY {ok, sent, page_id, msg_count}. This closes the last read-back leak,
    where the keyless receiver used to load the chat messages and display the personal title.

    args: {chat_id}. No active chat -> {ok, sent:False, empty:True} (receiver starts a new one)."""
    from . import aios_chat_store as C
    from . import view_render as VR
    from . import worker_telegram as WT

    if not WT.token_present():
        return {"ok": False, "send_unavailable": True}
    latest = C.get_latest_chat_local()
    if latest is None:
        return {"ok": True, "sent": False, "empty": True}
    msgs = C.load_messages_local(latest["id"])
    n = sum(1 for m in msgs if m["role"] == "user")
    WT.send_message(args.get("chat_id"), VR.render_chat_resume(latest["title"], n))
    return {"ok": True, "sent": True, "page_id": latest["id"], "msg_count": n}


def _view_notes_save(args: dict) -> dict:
    """Reference direct-send WRITE view for the demo domain: mint a bot id, write the note to the
    local ENCRYPTED store, and send a confirmation to Telegram DIRECTLY. The note text never
    returns through the shared queue — the receiver gets STRUCTURE ONLY {ok, sent, id}.

    args: {chat_id, text}. A bot-minted note_id may be supplied in args (idempotent retry); if
    absent, this view mints one. Mirrors the Gate-0 direct-send pattern: token-gated, structure-only."""
    from . import aios_notes_store as N
    from . import worker_telegram as WT

    if not WT.token_present():
        return {"ok": False, "send_unavailable": True}
    chat_id = args.get("chat_id")
    text = (args.get("text") or args.get("content") or "").strip()
    if not chat_id or not text:
        raise ValueError("notes.save_note view requires chat_id and text")
    # Idempotent id: reuse a bot-minted note_id from args if present, else mint one here.
    note_id = args.get("note_id") or _new_local_id()
    N.save_note(note_id, text)
    WT.send_message(chat_id, "Noted.")
    return {"ok": True, "sent": True, "id": note_id}


def _view_notes_summary(args: dict) -> dict:
    """Reference direct-send READ view for the demo domain: read a value-free notes summary from
    the local ENCRYPTED store and send it to Telegram DIRECTLY. The note bodies never return
    through the shared queue — the receiver gets STRUCTURE ONLY {ok, sent, count}.

    args: {chat_id}. Mirrors _view_chat_resume: token-gated, structure-only result."""
    from . import aios_notes_store as N
    from . import worker_telegram as WT

    if not WT.token_present():
        return {"ok": False, "send_unavailable": True}
    chat_id = args.get("chat_id")
    if not chat_id:
        raise ValueError("notes.get_summary view requires chat_id")
    summary = N.get_summary()
    count = summary["count"]
    if count == 0:
        text = "You have no saved notes yet."
    else:
        lines = [f"You have {count} saved note(s). Most recent:"]
        lines.extend(f"- {p}" for p in summary["previews"])
        text = "\n".join(lines)
    WT.send_message(chat_id, text)
    return {"ok": True, "sent": True, "count": count}


VIEW_OPS = {
    ("chat", "chat_turn"): _view_chat_turn,
    ("chat", "resume"): _view_chat_resume,
    ("notes", "save_note"): _view_notes_save,
    ("notes", "get_summary"): _view_notes_summary,
}


def store_view_applier(record) -> dict:
    """Queue applier for store_view rows: decrypt locally, render, send to Telegram
    DIRECTLY. Same b64 packing as store ops; fail-closed without keys."""
    _require_keyed()
    from .integrations_proxy import decode_op, encode_result, _unb64
    from .worker_telegram import WorkerSendUnavailable
    payload = record.payload or {}
    if "op_b64_sealed" in payload:
        # Sealed inbound (AIOS_STORE_SEAL ON): open with the worker's 0600 context identity.
        # Fail-closed — a bad/missing identity RAISES -> the row goes failed->dead (owner-visible),
        # never a cleartext leak.
        from .store_seal import open_store_inbound
        op_spec = _unb64(open_store_inbound(payload["op_b64_sealed"]))
    elif "op_b64" in payload:
        op_spec = decode_op(payload)      # legacy base64 fallback (transition + OFF)
    else:
        op_spec = payload
    fn = VIEW_OPS.get((op_spec.get("domain"), op_spec.get("op")))
    if fn is None:
        raise ValueError(f"store_view_applier: unknown view {op_spec.get('domain')!r}/{op_spec.get('op')!r}")
    try:
        return encode_result(fn(op_spec.get("args") or {}))
    except WorkerSendUnavailable:
        return encode_result({"ok": False, "send_unavailable": True})


def store_op_applier(record) -> dict:
    """Queue applier for store_read/store_write rows.

    The payload is b64-packed ({"op_b64": ...} with {domain, op, args} inside) — like the
    integration ops (so a secret scan and the queue journal see only the packing, not personal
    text). The result is returned packed the same way ({"result_b64": ...})."""
    _require_keyed()
    from .integrations_proxy import decode_op, encode_result, _unb64  # lazy: avoid import cycles
    payload = record.payload or {}
    if "op_b64_sealed" in payload:
        # Sealed inbound (AIOS_STORE_SEAL ON): open with the worker's 0600 context identity.
        # Fail-closed — a bad/missing identity RAISES -> the row goes failed->dead (owner-visible),
        # never a cleartext leak.
        from .store_seal import open_store_inbound
        op_spec = _unb64(open_store_inbound(payload["op_b64_sealed"]))
    elif "op_b64" in payload:
        op_spec = decode_op(payload)      # legacy base64 fallback (transition + OFF)
    else:
        op_spec = payload
    domain = op_spec.get("domain")
    op = op_spec.get("op")
    fn = STORE_OPS.get((domain, op))
    if fn is None:
        raise ValueError(f"store_op_applier: unknown op {domain!r}/{op!r}")
    return encode_result(fn(op_spec.get("args") or {}))
