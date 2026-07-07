"""Test S-1 router skeleton: flag OFF inert, flag ON registers exactly the catch-all at group=1,
and the worker echo round-trip on the durable queues (enqueue -> worker -> reply -> deliver).
Self-contained: dummy non-secret env + temp DB; no live Telegram/Claude/VPS/token.
Needs the project venv (telegram + python-dotenv). Run:  PYTHONPATH=. .venv/bin/python scripts/test_router_worker.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
_d = tempfile.mkdtemp(prefix="aios_router_")
os.environ["AIOS_TASK_QUEUE_DB"] = os.path.join(_d, "q.sqlite3")  # isolate queues
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:dummy")          # non-secret dummy for config import
os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
os.environ.pop("AIOS_UNIFIED_ROUTER", None)

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


from bot import config, task_queue, reply_queue, router_worker, claude_worker  # noqa: E402
from bot import claude_worker_core  # noqa: E402


async def _route_once():
    """Process exactly one alias='router' row. The generic claude_worker claim (claim_next_task)
    EXCLUDES alias='router' (single-writer-per-chat §3.6), so the router worker must be driven by
    claim_next_route_task. Returns the process_one result dict."""
    return await claude_worker_core.process_one(
        "router-w", router_worker.build_route_runner(),
        claim_fn=task_queue.claim_next_route_task)

# --- 1. flag default/OFF ---
config.UNIFIED_ROUTER = False
check("unified_router_enabled() False at OFF", config.unified_router_enabled() is False)
config.UNIFIED_ROUTER = True
check("unified_router_enabled() True at ON", config.unified_router_enabled() is True)
config.UNIFIED_ROUTER = False

# 1d (CRIT-F evidence): at OFF nothing has enqueued a router row
check("OFF: zero alias='router' rows persisted",
      [t for t in task_queue.list_updates() if t["alias"] == "router"] == [])

# --- 1b. router_handlers() OFF/ON via bot.main (imports telegram; skip if env lacks deps) ---
_main = None
try:
    from bot import main as _main
except Exception as e:  # pragma: no cover — only on an env without telegram/dotenv
    print("NOTE: bot.main not importable here (%s) — skipping handler-registration checks" % type(e).__name__)
if _main is not None:
    config.UNIFIED_ROUTER = False
    check("router_handlers() == [] at OFF", _main.router_handlers() == [])
    config.UNIFIED_ROUTER = True
    rh = _main.router_handlers()
    check("router_handlers() returns exactly one handler at ON", len(rh) == 1)
    if len(rh) == 1:
        h, g = rh[0]
        from bot import router_transport
        check("catch-all registered at group=1 (shadowed by group-0 handle_text)", g == 1)
        check("catch-all callback is route_catch_all",
              getattr(h, "callback", None) is router_transport.route_catch_all)
    config.UNIFIED_ROUTER = False

# --- 2. flag ON skeleton round-trip on the durable queues ---
config.UNIFIED_ROUTER = True
env = {"kind": "text", "chat_id": 7, "message_id": 456, "text": "café ☕"}  # non-ASCII to prove UTF-8 round-trip
tid = task_queue.enqueue_task(chat_id=7, alias="router", mode="inbound",
                              task_text=json.dumps(env, ensure_ascii=False), source="telegram")
t = task_queue.get_task(tid)
check("route row enqueued (alias/mode/status/text)",
      bool(t) and t["alias"] == "router" and t["mode"] == "inbound"
      and t["status"] == "queued" and json.loads(t["task_text"]) == env)
res1 = asyncio.run(_route_once())
check("worker processed exactly 1 route row", res1.get("status") == "done")
check("task flipped to done", task_queue.get_task(tid)["status"] == "done")
und = reply_queue.claim_undelivered()
check("exactly one reply enqueued with echo text + chat_id",
      len(und) == 1 and und[0]["text"] == "routed: " + env["text"] and und[0]["chat_id"] == 7)
captured = []


async def _fake_send(cid, text):
    captured.append((cid, text))


asyncio.run(reply_queue.deliver_pending(_fake_send))
check("reply delivered via fake_send (chat_id, text)", captured == [(7, "routed: " + env["text"])])
check("no undelivered replies remain after delivery", reply_queue.claim_undelivered() == [])

# --- 3. envelope JSON round-trip integrity (special chars / newlines / quotes) ---
env2 = {"kind": "text", "chat_id": 9, "message_id": 1, "text": "line1\nline2 \"q\" ☕"}
task_queue.enqueue_task(chat_id=9, alias="router", mode="inbound",
                        task_text=json.dumps(env2, ensure_ascii=False), source="telegram")
asyncio.run(_route_once())
und2 = [r for r in reply_queue.claim_undelivered() if r["chat_id"] == 9]
check("envelope text survived json round-trip byte-exact",
      len(und2) == 1 and und2[0]["text"] == "routed: " + env2["text"])

# --- 4. non-route alias is a guarded no-op error (task -> failed, no reply) ---
before = len(reply_queue.claim_undelivered())
tidc = task_queue.enqueue_task(chat_id=5, alias="coding", mode="ask",
                               task_text="x", source="telegram")
asyncio.run(claude_worker.run_once(router_worker.build_route_runner()))
check("non-route alias -> task failed (NotImplementedError guard)",
      task_queue.get_task(tidc)["status"] == "failed")
check("non-route alias produced no reply",
      len(reply_queue.claim_undelivered()) == before)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print(f"RESULT: {fails} FAIL")
    sys.exit(1)
