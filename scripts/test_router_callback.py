"""Test S6: callback (button-tap) dispatch — router_dispatch.run_callback + the router_worker
kind=='callback' branch. Mirrors test_router_dispatch.py. Offline + key-free (no classifier on the
callback path); a STUB handler is registered into CALLBACK_HANDLERS to prove the pipe end-to-end.

Platform: the project venv (py3.12) — router_dispatch/conv_state use bare `X | None` at module scope
without future-annotations (TypeError on bare 3.9).
Run:  cd ~/apps/ChatBot && PYTHONPATH=. .venv/bin/python scripts/test_router_callback.py
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.getcwd())
_d = tempfile.mkdtemp(prefix="aios_cb_")
DB = os.path.join(_d, "q.sqlite3")
os.environ["AIOS_TASK_QUEUE_DB"] = DB
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:dummy")
os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


def raises(fn, exc):
    try:
        fn()
        return None
    except exc as e:  # noqa: BLE001
        return e


from bot import task_queue, reply_queue, claude_worker_core, conv_state, router_dispatch  # noqa: E402,F401
from bot import router_worker  # noqa: E402

# --- stub callback handlers registered for test prefixes ---
_calls = []


async def _stub_cb(update, context):
    q = update.callback_query
    _calls.append(q.data)
    context.chat_data["last_cb"] = q.data          # writes conv_state
    await q.answer()                               # no-op (transport already answered)
    await q.message.reply_text("cb:" + q.data)     # -> reply_queue, source='router'


async def _boom_cb(update, context):
    _ = update.callback_query.data                 # touch the surface
    raise RuntimeError("cb boom")


router_dispatch.CALLBACK_HANDLERS["tcb_"] = _stub_cb
router_dispatch.CALLBACK_HANDLERS["tboom_"] = _boom_cb


def _run(envelope, chat_id):
    task_queue.enqueue_task(chat_id=chat_id, alias="router", mode="inbound",
                            task_text=json.dumps(envelope, ensure_ascii=False), source="telegram")
    # alias='router' rows are claimed ONLY by claim_next_route_task (the generic claim_next_task
    # excludes them, single-writer-per-chat §3.6) — drive the router worker with the route claim.
    return asyncio.run(claude_worker_core.process_one(
        "test-cb", router_worker.build_route_runner(),
        claim_fn=task_queue.claim_next_route_task))


def _reply_rows(chat_id):
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT id, text, source FROM replies WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()]


def _conv_count(chat_id):
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT count(*) FROM conv_state WHERE chat_id=?", (chat_id,)).fetchone()[0]


def _cb(chat_id, data):
    return {"kind": "callback", "chat_id": chat_id, "message_id": 7, "callback_data": data}


router_dispatch.ROUTER_DISPATCH = True

# (a) a registered tap routes: handler runs, reply via reply_queue (source='router'), conv_state persisted
_calls.clear()
res_a = _run(_cb(201, "tcb_yes_2026"), 201)
rows_a = _reply_rows(201)
check("(a) callback dispatched (status done)", res_a.get("status") == "done")
check("(a) handler ran exactly once with the tapped data", _calls == ["tcb_yes_2026"])
check("(a) one reply row, source='router', carries the handler's text",
      len(rows_a) == 1 and rows_a[0]["source"] == "router" and rows_a[0]["text"] == "cb:tcb_yes_2026")
check("(a) conv_state persisted the handler's chat_data write (last_cb round-trips)",
      _conv_count(201) == 1 and conv_state.load_state(201).get("last_cb") == "tcb_yes_2026")

# (b) an UNREGISTERED prefix: run_callback returns False -> falls through to the echo path
_calls.clear()
_run(_cb(202, "unknown_tap_x"), 202)
rows_b = _reply_rows(202)
check("(b) unregistered callback falls through to echo (source='worker'), handler NOT run",
      _calls == [] and len(rows_b) == 1 and rows_b[0]["source"] == "worker")

# (c) flag OFF: no dispatch, echo only, handler not run
router_dispatch.ROUTER_DISPATCH = False
_calls.clear()
_run(_cb(203, "tcb_yes_off"), 203)
rows_c = _reply_rows(203)
check("(c) flag OFF: echo only (source='worker'), callback handler NOT run",
      _calls == [] and len(rows_c) == 1 and rows_c[0]["source"] == "worker")
router_dispatch.ROUTER_DISPATCH = True

# (d) handler exception -> task failed AND no conv_state row written (atomic-on-success)
res_d = _run(_cb(204, "tboom_x"), 204)
check("(d) raising callback handler -> task failed AND no conv_state persisted",
      res_d.get("status") == "failed" and _conv_count(204) == 0)

# (e) fail-loud surface: un-modelled attrs / kwargs raise _UnsupportedSurface (edits + reply_markup
# ARE modelled now; an un-modelled attr and an un-modelled reply_text kwarg must still fail loud)
upd = router_dispatch._WorkerCallbackUpdate("tcb_x", 700, 700, message_id=9, message_text="t", has_markup=True)
e_upd = raises(lambda: upd.foobar, router_dispatch._UnsupportedSurface)
e_q = raises(lambda: upd.callback_query.no_such_attr, router_dispatch._UnsupportedSurface)
e_kw = raises(lambda: asyncio.run(upd.callback_query.message.reply_text("x", disable_web_page_preview=True)),
              router_dispatch._UnsupportedSurface)
check("(e) un-modelled update/query attr + un-modelled reply_text kwarg all raise _UnsupportedSurface",
      all(isinstance(x, router_dispatch._UnsupportedSurface) for x in (e_upd, e_q, e_kw)))

# (f) run_callback guards: a non-callback envelope / a non-str callback_data fail loud
e_kind = raises(lambda: asyncio.run(router_dispatch.run_callback({"kind": "text", "chat_id": 1}, owner_id=1)),
                router_dispatch._UnsupportedSurface)
e_data = raises(lambda: asyncio.run(
    router_dispatch.run_callback({"kind": "callback", "chat_id": 1, "callback_data": None}, owner_id=1)),
    router_dispatch._UnsupportedSurface)
check("(f) run_callback rejects a non-callback envelope and a non-string callback_data (fail loud)",
      isinstance(e_kind, router_dispatch._UnsupportedSurface)
      and isinstance(e_data, router_dispatch._UnsupportedSurface))

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
