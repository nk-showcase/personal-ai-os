#!/usr/bin/env python3
"""Plain-python tests for bot/reply_queue.py typed control-rows (S6 stateful-2: send/edit).

Offline, pure SQLite, no Telegram. Verifies: additive schema + guarded migration of a legacy
flat DB; enqueue_reply unchanged (KIND_SEND); enqueue_edit (KIND_EDIT, target id); allow-list
dispatch; the fail-safe that an edit with no edit_fn / an unknown kind is NEVER mis-sent;
terminal-vs-transient edit-failure handling (no poison loop); back-compat one-arg deliver_pending.
  python3 scripts/test_reply_control_rows.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_DB = str(Path(tempfile.mkdtemp(prefix="aios_replyctl_")) / "q.sqlite3")
os.environ["AIOS_TASK_QUEUE_DB"] = _DB
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import reply_queue as rq  # noqa: E402

_FAILS = []


def check(cond, label):
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        _FAILS.append(label)


class FakeBadRequest(Exception):
    """Stand-in for telegram.error.BadRequest — classified by its message string."""


def main() -> int:
    # --- 1. send row: KIND_SEND, edit_message_id inert (None) ---
    rq.enqueue_reply(111, "hi")
    u = rq.claim_undelivered()
    check(u[0]["reply_kind"] == rq.KIND_SEND and u[0]["edit_message_id"] is None,
          "enqueue_reply -> KIND_SEND, edit_message_id is None")
    keys = set(u[0].keys())
    check({"id", "chat_id", "text"} <= keys
          and not (keys & {"task_text", "result", "result_json"}),
          "claim exposes id/chat_id/text (+typed fields), never task_text/result (no leak)")

    # --- 2. BACK-COMPAT: deliver_pending(send_fn) — ONE positional callable, like the live bot ---
    sent = []

    async def send_fn(chat_id, text):
        sent.append((chat_id, text))

    n = asyncio.run(rq.deliver_pending(send_fn))  # no edit_fn
    check(n == 1 and sent == [(111, "hi")] and rq.claim_undelivered() == [],
          "back-compat: deliver_pending(send_fn) delivers send rows, byte-identical to 0c")

    # --- 3. edit dispatch via edit_fn(chat_id, message_id, text) ---
    rq.enqueue_edit(222, "edited", edit_message_id=900)
    edits = []

    async def edit_fn(chat_id, message_id, text):
        edits.append((chat_id, message_id, text))

    n = asyncio.run(rq.deliver_pending(send_fn, edit_fn))
    check(n == 1 and edits == [(222, 900, "edited")] and rq.claim_undelivered() == [],
          "enqueue_edit -> KIND_EDIT dispatched to edit_fn(chat_id, message_id, text)")

    # --- 4. FAIL-SAFE: an edit row with NO edit_fn stays undelivered (never mis-sent as a send) ---
    rq.enqueue_edit(333, "no-handler", edit_message_id=901)
    sent.clear()
    n = asyncio.run(rq.deliver_pending(send_fn))  # edit_fn omitted on purpose
    check(n == 0 and sent == []
          and [r["text"] for r in rq.claim_undelivered()] == ["no-handler"],
          "fail-safe: edit row with no edit_fn left UNDELIVERED, never routed to send_fn")
    # and it delivers once an edit_fn appears (proves it was only parked, not corrupted)
    edits.clear()
    n = asyncio.run(rq.deliver_pending(send_fn, edit_fn))
    check(n == 1 and edits == [(333, 901, "no-handler")],
          "parked edit delivers once edit_fn is supplied")

    # --- 5. HOSTILE send row: non-null edit_message_id must STILL route to send_fn (kind is king) ---
    with sqlite3.connect(_DB) as raw:
        raw.execute(
            "INSERT INTO replies (chat_id, text, source, created_ts, reply_kind, edit_message_id) "
            "VALUES (?,?,?,?,?,?)",
            (444, "kind-wins", "worker", 0.0, rq.KIND_SEND, 999),
        )
        raw.commit()
    sent.clear()
    edits.clear()
    n = asyncio.run(rq.deliver_pending(send_fn, edit_fn))
    check(n == 1 and sent == [(444, "kind-wins")] and edits == [],
          "hostile send row w/ non-null edit_message_id routes to send_fn, NEVER edit_fn")

    # --- 6. unknown kind: parked (undelivered, never sent, never edited) ---
    with sqlite3.connect(_DB) as raw:
        raw.execute(
            "INSERT INTO replies (chat_id, text, source, created_ts, reply_kind, edit_message_id) "
            "VALUES (?,?,?,?,?,?)",
            (555, "future-kind", "worker", 0.0, "answer", None),
        )
        raw.commit()
    sent.clear()
    edits.clear()
    n = asyncio.run(rq.deliver_pending(send_fn, edit_fn))
    check(n == 0 and sent == [] and edits == []
          and [r["text"] for r in rq.claim_undelivered()] == ["future-kind"],
          "unknown kind parked: not sent, not edited, stays undelivered")
    # drain it so it doesn't pollute later asserts
    with sqlite3.connect(_DB) as raw:
        raw.execute("UPDATE replies SET delivered_ts=0.1 WHERE text='future-kind'")
        raw.commit()

    # --- 7. TERMINAL edit-400 converges: marked delivered, NOT re-fired forever ---
    rq.enqueue_edit(666, "dup", edit_message_id=902)

    async def edit_not_modified(chat_id, message_id, text):
        raise FakeBadRequest("Bad Request: message is not modified")

    n = asyncio.run(rq.deliver_pending(send_fn, edit_not_modified))
    check(n == 1 and rq.claim_undelivered() == [],
          "terminal edit-400 ('not modified') marked delivered -> row leaves backlog (no poison loop)")
    # second poll: nothing left to fire -> proves convergence, not an infinite retry
    fired = []

    async def edit_track(chat_id, message_id, text):
        fired.append(message_id)

    n = asyncio.run(rq.deliver_pending(send_fn, edit_track))
    check(n == 0 and fired == [], "terminal edit-400 NOT re-fired on the next poll (converged)")

    # --- 8. TRANSIENT edit error: left undelivered, retried (0c semantics) ---
    rq.enqueue_edit(777, "retry-edit", edit_message_id=903)

    async def edit_transient(chat_id, message_id, text):
        raise asyncio.TimeoutError("network blip")

    n = asyncio.run(rq.deliver_pending(send_fn, edit_transient))
    check(n == 0 and [r["text"] for r in rq.claim_undelivered()] == ["retry-edit"],
          "transient edit error leaves row UNDELIVERED (retry), unlike a terminal 400")
    n = asyncio.run(rq.deliver_pending(send_fn, edit_fn))
    check(n == 1 and rq.claim_undelivered() == [], "retried edit delivers on the next good poll")

    # --- 9. legacy-flat-DB migration backfill (covers the PROD path, distinct from fresh DB) ---
    legacy = str(Path(tempfile.mkdtemp(prefix="aios_legacy_")) / "q.sqlite3")
    with sqlite3.connect(legacy) as raw:
        raw.executescript(
            """CREATE TABLE replies (
                 id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
                 source TEXT NOT NULL DEFAULT 'worker', created_ts REAL NOT NULL, delivered_ts REAL);"""
        )
        raw.execute("INSERT INTO replies (chat_id, text, source, created_ts) VALUES (1,'old',?,?)",
                    ("worker", 0.0))
        raw.commit()
    os.environ["AIOS_TASK_QUEUE_DB"] = legacy
    # first open under this slice migrates; legacy row backfills to send/None
    u = rq.claim_undelivered()
    check(len(u) == 1 and u[0]["reply_kind"] == "send" and u[0]["edit_message_id"] is None,
          "legacy flat-DB row backfills to reply_kind='send', edit_message_id=NULL")
    with sqlite3.connect(legacy) as raw:
        cols = {r[1] for r in raw.execute("PRAGMA table_info(replies)").fetchall()}
    check({"reply_kind", "edit_message_id"} <= cols, "migration added both typed columns to the legacy DB")
    rq.enqueue_edit(2, "now-edit", edit_message_id=5)
    got = []

    async def ed(chat_id, message_id, text):
        got.append((chat_id, message_id, text))

    asyncio.run(rq.deliver_pending(send_fn, ed))
    check(got == [(2, 5, "now-edit")], "migrated legacy DB then dispatches a KIND_EDIT row correctly")
    os.environ["AIOS_TASK_QUEUE_DB"] = _DB  # restore

    # --- 10. KB rows (KIND_SEND_KB): inline-keyboard support, same inert/park fail-safe as edit ---
    kb = [[{"text": "✅ Yes", "callback_data": "note_add_2026-06-24"},
           {"text": "❌ No", "callback_data": "note_skip_2026-06-24"}]]
    rq.enqueue_reply_kb(888, "confirm?", kb)
    u = [r for r in rq.claim_undelivered() if r["chat_id"] == 888]
    check(len(u) == 1 and u[0]["reply_kind"] == rq.KIND_SEND_KB and bool(u[0]["markup_json"]),
          "enqueue_reply_kb -> KIND_SEND_KB row with markup_json populated")

    # FAIL-SAFE: a KB row with NO send_kb_fn is PARKED (never mis-sent as a plain text send)
    sent.clear()
    n = asyncio.run(rq.deliver_pending(send_fn))  # no send_kb_fn
    check(n == 0 and sent == []
          and [r["text"] for r in rq.claim_undelivered() if r["chat_id"] == 888] == ["confirm?"],
          "fail-safe: KB row with no send_kb_fn left UNDELIVERED, never routed to send_fn")

    # delivers once send_kb_fn appears; it receives (chat_id, text, PARSED keyboard list)
    kb_calls = []

    async def send_kb_fn(chat_id, text, keyboard):
        kb_calls.append((chat_id, text, keyboard))

    n = asyncio.run(rq.deliver_pending(send_fn, send_kb_fn=send_kb_fn))
    check(n == 1 and kb_calls == [(888, "confirm?", kb)]
          and [r for r in rq.claim_undelivered() if r["chat_id"] == 888] == [],
          "KB row delivers via send_kb_fn(chat_id, text, keyboard), keyboard parsed back to a list")

    # --- 11. edit_kb rows (KIND_EDIT_KB): edit-with-keyboard, same inert/park + terminal-400 handling ---
    kb2 = [[{"text": "Work", "callback_data": "note_tag_work_2026-06-23"}]]
    rq.enqueue_edit_kb(889, "pick a tag?", edit_message_id=42, inline_keyboard=kb2)
    u11 = [r for r in rq.claim_undelivered() if r["chat_id"] == 889]
    check(len(u11) == 1 and u11[0]["reply_kind"] == rq.KIND_EDIT_KB
          and u11[0]["edit_message_id"] == 42 and bool(u11[0]["markup_json"]),
          "enqueue_edit_kb -> KIND_EDIT_KB row with edit_message_id + markup_json")

    # FAIL-SAFE: no edit_kb_fn -> parked (not mis-sent, not routed to send/edit)
    sent.clear()
    n = asyncio.run(rq.deliver_pending(send_fn))  # no edit_kb_fn
    check(n == 0 and sent == []
          and [r["text"] for r in rq.claim_undelivered() if r["chat_id"] == 889] == ["pick a tag?"],
          "fail-safe: edit_kb row with no edit_kb_fn left UNDELIVERED")

    # delivers via edit_kb_fn(chat_id, message_id, text, keyboard)
    ekb_calls = []

    async def edit_kb_fn(chat_id, message_id, text, keyboard):
        ekb_calls.append((chat_id, message_id, text, keyboard))

    n = asyncio.run(rq.deliver_pending(send_fn, edit_kb_fn=edit_kb_fn))
    check(n == 1 and ekb_calls == [(889, 42, "pick a tag?", kb2)]
          and [r for r in rq.claim_undelivered() if r["chat_id"] == 889] == [],
          "edit_kb row delivers via edit_kb_fn(chat_id, message_id, text, keyboard)")

    # TERMINAL edit-400 on an edit_kb row converges (marked delivered, not re-fired forever)
    rq.enqueue_edit_kb(890, "dup", edit_message_id=43, inline_keyboard=kb2)

    async def edit_kb_not_modified(chat_id, message_id, text, keyboard):
        raise FakeBadRequest("Bad Request: message is not modified")

    n = asyncio.run(rq.deliver_pending(send_fn, edit_kb_fn=edit_kb_not_modified))
    check(n == 1 and [r for r in rq.claim_undelivered() if r["chat_id"] == 890] == [],
          "terminal edit-400 on an edit_kb row marked delivered (no poison loop)")

    print()
    if _FAILS:
        print(f"RESULT: {len(_FAILS)} FAIL")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
