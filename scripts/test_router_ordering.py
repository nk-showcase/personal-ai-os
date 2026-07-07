"""Test single-writer-per-chat ordering for the unified-router consumer (hard-stage prereq, §3.6).

task_queue.claim_next_route_task: alias='router' only, skips a chat that already has a running
router row -> two router rows for the SAME chat never run concurrently / out of order, while
different chats flow freely. The generic claim_next_task (coding tasks) is UNCHANGED.

Pure stdlib (sqlite) -> runs on mac bare python3.  Run:  PYTHONPATH=. python3 scripts/test_router_ordering.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["AIOS_TASK_QUEUE_DB"] = os.path.join(tempfile.mkdtemp(prefix="aios_ord_"), "q.sqlite3")

from bot import task_queue, claude_worker_core  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


def _route(chat_id, text):
    return task_queue.enqueue_task(chat_id=chat_id, alias="router", mode="inbound",
                                   task_text=text, source="telegram")


# --- 1. per-chat single-writer ordering ---
a1 = _route(10, "a1")
a2 = _route(10, "a2")
b1 = _route(20, "b1")

c1 = task_queue.claim_next_route_task("w")
check("#1 first claim = A1 (oldest router row)", c1 and c1["id"] == a1)
c2 = task_queue.claim_next_route_task("w")
check("#1 second claim = B1 (different chat) — A2 is SKIPPED while A1 runs (per-chat order)",
      c2 and c2["id"] == b1)
c3 = task_queue.claim_next_route_task("w")
check("#1 third claim = None (A2 blocked by running A1; chat B exhausted)", c3 is None)
task_queue.update_task(a1, "done")
c4 = task_queue.claim_next_route_task("w")
check("#1 after A1 done, A2 is now claimable (chat A released)", c4 and c4["id"] == a2)

# --- 2. route claim ignores non-router rows ---
cod = task_queue.enqueue_task(chat_id=31, alias="coding", mode="ask", task_text="c", source="telegram")
r = _route(32, "r")
cr = task_queue.claim_next_route_task("w")
check("#2 route claim takes the router row, NOT the queued coding row", cr and cr["id"] == r)
cr2 = task_queue.claim_next_route_task("w")
check("#2 route claim returns None (the coding row is invisible to it)", cr2 is None)
check("#2 the coding row is still queued (untouched by the route claim)",
      task_queue.get_task(cod)["status"] == "queued")

# --- 3. generic claim_next_task is UNCHANGED (claims the coding row regardless of alias/chat) ---
g = task_queue.claim_next_task("w")
check("#3 generic claim still claims the queued coding row (live claude-worker unaffected)",
      g and g["id"] == cod)

# --- 4. process_one(claim_fn=...) plumbing ---
async def _runner(task):
    return {"id": task["id"]}

rp = _route(40, "rp")
res = asyncio.run(claude_worker_core.process_one("w", _runner, claim_fn=task_queue.claim_next_route_task))
check("#4 process_one(claim_fn=route) processes a router row -> done",
      res.get("status") == "done" and task_queue.get_task(rp)["status"] == "done")
cod2 = task_queue.enqueue_task(chat_id=41, alias="coding", mode="ask", task_text="c2", source="telegram")
res2 = asyncio.run(claude_worker_core.process_one("w", _runner, claim_fn=task_queue.claim_next_route_task))
check("#4 process_one(claim_fn=route) is noop when only a coding row is queued (route-scoped)",
      res2.get("status") == "noop" and task_queue.get_task(cod2)["status"] == "queued")
res3 = asyncio.run(claude_worker_core.process_one("w", _runner))  # default generic claim
check("#4 process_one default claim processes the coding row (back-compat preserved)",
      res3.get("status") == "done" and task_queue.get_task(cod2)["status"] == "done")

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
