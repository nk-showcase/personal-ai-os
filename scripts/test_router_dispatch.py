"""Test the worker-side router-dispatch adapter wiring (behind the dispatch flag).

Offline + key-free: classifier.analyze is STUBBED so no LLM is needed; the test proves
the adapter-wiring contract independent of any live Anthropic-key dependency. The point
of this test is the ADAPTER, not any concrete domain handler:
  - flag OFF  = byte-identical echo, classifier.analyze is never called;
  - a multi-intent (list) result falls through to echo;
  - an intent the dispatcher does not handle falls through to echo;
  - the synthetic worker Update/Context surface fails LOUD on any un-modelled access
    (so a future intent's needs surface in a test, never silently);
  - run_intent on an un-merged envelope (no 'intent') fails LOUD (no silent False);
  - the worker message.date surface round-trips the parsed timestamp.

Platform: the project venv (py3.12). NOT bare /usr/bin/python3 — classifier.py uses
bare `X | None` at module scope without `from __future__ import annotations`.
Run:  PYTHONPATH=. .venv/bin/python scripts/test_router_dispatch.py
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.getcwd())

_d = tempfile.mkdtemp(prefix="aios_dispatch_")
DB = os.path.join(_d, "q.sqlite3")
os.environ["AIOS_TASK_QUEUE_DB"] = DB
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:dummy")
os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)   # cleartext conv_state for the test

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


from bot import task_queue, reply_queue, claude_worker_core, conv_state, router_dispatch  # noqa: E402
from bot import router_worker  # noqa: E402
import bot.classifier as classifier      # noqa: E402

# --- stub (key-free, offline) ---
_analyze_calls = []
_analyze_result = {"v": None}


async def _stub_analyze(text, project_names=None):
    _analyze_calls.append(text)
    r = _analyze_result["v"]
    if isinstance(r, Exception):
        raise r
    return r


classifier.analyze = _stub_analyze


def _run(envelope, chat_id):
    task_queue.enqueue_task(chat_id=chat_id, alias="router", mode="inbound",
                            task_text=json.dumps(envelope, ensure_ascii=False), source="telegram")
    # The generic claim EXCLUDES alias='router' rows (they belong to the keyed
    # worker's single-writer-per-chat claim). Claim exactly like the real consumer
    # (bot/integrations_worker.py) — otherwise process_one sees an empty queue.
    return asyncio.run(claude_worker_core.process_one(
        "test-dispatch", router_worker.build_route_runner(),
        claim_fn=task_queue.claim_next_route_task))


def _reply_rows(chat_id):
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT id, text, source FROM replies WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()]


def _conv_count(chat_id):
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT count(*) FROM conv_state WHERE chat_id=?", (chat_id,)).fetchone()[0]


def _env(chat_id, text):
    return {"kind": "text", "chat_id": chat_id, "message_id": 1, "text": text}


# === (a) multi-intent (list) falls through to echo ===
router_dispatch.ROUTER_DISPATCH = True
_analyze_result["v"] = [{"intent": "new", "content": "a"}, {"intent": "new", "content": "b"}]
_run(_env(102, "1. a\n2. b"), 102)
rows_a = _reply_rows(102)
check("(a) list result falls through: echo row present (text echoed)",
      len(rows_a) == 1 and "1. a" in rows_a[0]["text"] and rows_a[0]["source"] == "worker")

# === (b) an intent the dispatcher does not handle falls through ===
_analyze_result["v"] = {"intent": "delete", "num": 3}
_run(_env(103, "delete 3"), 103)
rows_b = _reply_rows(103)
check("(b) non-dispatch intent (delete) falls through to echo",
      len(rows_b) == 1 and "delete 3" in rows_b[0]["text"] and rows_b[0]["source"] == "worker")

# === (c) flag OFF = byte-identical echo, analyze NOT called ===
router_dispatch.ROUTER_DISPATCH = False
_before_calls = len(_analyze_calls)
_analyze_result["v"] = {"intent": "new", "content": "x"}  # would be considered IF flag were ON
_run(_env(104, "some text"), 104)
rows_c = _reply_rows(104)
check("(c) flag OFF: echo row only, classifier.analyze NOT called (zero live change in shadow)",
      len(rows_c) == 1 and rows_c[0]["source"] == "worker" and len(_analyze_calls) == _before_calls)
router_dispatch.ROUTER_DISPATCH = True

# === (d) unsupported surface fails loud ===
upd = router_dispatch._WorkerUpdate(700, 700)
ctx = router_dispatch._WorkerContext({})
e_attr = raises(lambda: upd.callback_query, router_dispatch._UnsupportedSurface)
e_kw = raises(lambda: asyncio.run(upd.message.reply_text("x", reply_markup="kb")),
              router_dispatch._UnsupportedSurface)
e_set = raises(lambda: setattr(ctx, "chat_data", {}), router_dispatch._UnsupportedSurface)
e_ctxattr = raises(lambda: ctx.bot, router_dispatch._UnsupportedSurface)
check("(d) un-modelled attr / extra kwarg / chat_data reassignment all raise _UnsupportedSurface",
      all(isinstance(x, router_dispatch._UnsupportedSurface) for x in (e_attr, e_kw, e_set, e_ctxattr)))

# === (e) un-merged envelope (raw transport envelope, no 'intent') fails loud ===
e_e = raises(lambda: asyncio.run(router_dispatch.run_intent({"chat_id": 701, "text": "x"}, owner_id=701)),
             router_dispatch._UnsupportedSurface)
check("(e) run_intent on an un-merged envelope (no 'intent') raises _UnsupportedSurface (no silent False)",
      isinstance(e_e, router_dispatch._UnsupportedSurface))

# === (f) worker date surface: _WorkerUpdate carries the parsed message.date; absent -> None ===
from datetime import datetime as _dt  # noqa: E402
_date = _dt.fromisoformat("2026-06-24T08:00:00+00:00")
check("(f) _WorkerUpdate.message.date round-trips the parsed timestamp; absent -> None",
      router_dispatch._WorkerUpdate(900, 900, _date).message.date == _date
      and router_dispatch._WorkerUpdate(901, 901).message.date is None)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
