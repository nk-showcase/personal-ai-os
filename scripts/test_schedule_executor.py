"""Test S6 stateful substrate-3: bot/schedule_executor.run_due_jobs — fire due jobs + re-arm
recurring ones with cancel_key dedup (CRIT-E / CRIT-E-2).

Pure stdlib (sqlite); runs on mac bare python3.  Run:  PYTHONPATH=. python3 scripts/test_schedule_executor.py
"""
import asyncio
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.getcwd())
_DB = os.path.join(tempfile.mkdtemp(prefix="aios_sched_"), "q.sqlite3")
os.environ["AIOS_TASK_QUEUE_DB"] = _DB

from bot import schedule_queue as sq, schedule_executor as se  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


def _pending(cancel_key):
    with sqlite3.connect(_DB) as c:
        return c.execute(
            "SELECT COUNT(*) FROM scheduled WHERE cancel_key=? AND fired_ts IS NULL", (cancel_key,)
        ).fetchone()[0]


def _due_ts(cancel_key):
    with sqlite3.connect(_DB) as c:
        row = c.execute(
            "SELECT due_ts FROM scheduled WHERE cancel_key=? AND fired_ts IS NULL", (cancel_key,)
        ).fetchone()
        return row[0] if row else None


log = []


async def _h(payload):
    log.append(payload)


HANDLERS = {"recurring": _h, "oneshot": _h}


def _nd(kind, payload):
    return 200.0 if kind == "recurring" else None   # recurring re-arms; one-shot does not


# --- 1. recurring fires once + re-arms exactly one pending row at the next due ---
sq.add_scheduled("recurring", 100.0, {"x": 1}, cancel_key="r1")
n = asyncio.run(se.run_due_jobs(HANDLERS, next_due_fn=_nd, now=150.0))   # 100<=150 due
check("#1 recurring fires once (handler called, returns 1)", n == 1 and log == [{"x": 1}])
check("#1 re-armed: exactly ONE pending row for cancel_key, at the next due_ts (200)",
      _pending("r1") == 1 and _due_ts("r1") == 200.0)

# --- 2. not re-fired before its new due_ts ---
log.clear()
n = asyncio.run(se.run_due_jobs(HANDLERS, next_due_fn=_nd, now=150.0))   # 200>150, not due
check("#2 re-armed row NOT fired before due (now<due)", n == 0 and log == [])

# --- 3. fires again when due, re-arms again (still exactly one pending) ---
n = asyncio.run(se.run_due_jobs(HANDLERS, next_due_fn=_nd, now=250.0))   # 200<=250 due
check("#3 recurring fires again at due and re-arms (still ONE pending)",
      n == 1 and _pending("r1") == 1)

# --- 4. DEDUP (CRIT-E-2): a restart double-rebuild leaves duplicate pending rows; re-arm collapses to one ---
sq.add_scheduled("recurring", 9999.0, {"y": 1}, cancel_key="r2")   # stray pending dup #1
sq.add_scheduled("recurring", 9999.0, {"y": 1}, cancel_key="r2")   # stray pending dup #2
sq.add_scheduled("recurring", 50.0, {"y": 1}, cancel_key="r2")     # the DUE one
check("#4 pre-state: 3 pending rows for cancel_key r2", _pending("r2") == 3)
n = asyncio.run(se.run_due_jobs(HANDLERS, next_due_fn=lambda k, p: 8000.0, now=100.0))
check("#4 after fire+re-arm: EXACTLY one pending row for r2 (duplicates cancelled)",
      _pending("r2") == 1 and _due_ts("r2") == 8000.0)

# --- 5. one-shot: fires, NO re-arm ---
log.clear()
sq.add_scheduled("oneshot", 10.0, {"z": 1}, cancel_key="o1")
n = asyncio.run(se.run_due_jobs(HANDLERS, next_due_fn=_nd, now=20.0))   # _nd -> None for oneshot
check("#5 one-shot fires once and is NOT re-armed (no pending)",
      n == 1 and log == [{"z": 1}] and _pending("o1") == 0)

# --- 6. handler exception does not block other due jobs; recurring still re-arms ---
log.clear()

async def _boom(payload):
    raise RuntimeError("handler exploded")

sq.add_scheduled("boomkind", 10.0, {}, cancel_key=None)
sq.add_scheduled("recurring", 10.0, {"ok": 1}, cancel_key="r4")
n = asyncio.run(se.run_due_jobs({"boomkind": _boom, "recurring": _h},
                                next_due_fn=lambda k, p: 6000.0 if k == "recurring" else None, now=20.0))
check("#6 a raising handler does not block others: both fired, recurring re-armed",
      n == 2 and log == [{"ok": 1}] and _pending("r4") == 1)

# --- 7. unhandled kind is consumed (claim_due batch) without crashing ---
sq.add_scheduled("unknownkind", 5.0, {}, cancel_key=None)
n = asyncio.run(se.run_due_jobs(HANDLERS, now=20.0))   # no handler for unknownkind, no next_due_fn
check("#7 unhandled due kind consumed without crash (KNOWN LIMITATION)", n == 1)

# --- 8. cancel removes a pending recurring row (flow 'stop' semantics) ---
sq.add_scheduled("recurring", 9000.0, {}, cancel_key="c1")
removed = sq.cancel("c1")
check("#8 cancel(cancel_key) removes the pending row", removed == 1 and _pending("c1") == 0)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
