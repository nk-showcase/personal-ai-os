#!/usr/bin/env python3
"""S6 step-8 MUST-FIX B: bot.task_queue.claim_next_task must EXCLUDE alias='router' rows so the
claude-worker (which shares AIOS_TASK_QUEUE_DB) can never steal a router row from the keyed worker's
claim_next_route_task once AIOS_ROUTER_DISPATCH is armed.

OFFLINE: temp sqlite (AIOS_TASK_QUEUE_DB). Run (VPS venv):  PYTHONPATH=. .venv/bin/python scripts/test_claim_excludes_router.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="aios_claim_")
os.environ["AIOS_TASK_QUEUE_DB"] = str(Path(_tmp) / "q.sqlite3")
sys.path.insert(0, os.getcwd())

from bot import task_queue  # noqa: E402

_failed: list = []


def check(name, cond):
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        _failed.append(name)


CHAT = 555

# Enqueue a ROUTER row FIRST (older), then a CODING row. If the filter is missing, the generic
# claim would grab the older router row.
rid = task_queue.enqueue_task(chat_id=CHAT, alias="router", mode="inbound",
                              task_text='{"kind":"callback"}', source="telegram")
cid = task_queue.enqueue_task(chat_id=CHAT, alias="demo-project", mode="run",
                              task_text="do coding", source="telegram")

# T1: claim_next_task returns the CODING row, NOT the older router row.
t = task_queue.claim_next_task(worker_id="claude-w")
check("T1a: claim_next_task returned a task", t is not None)
check("T1b: claim_next_task returned the CODING row (alias != 'router')",
      t is not None and t["alias"] == "demo-project" and t["id"] == cid)
check("T1c: claim_next_task did NOT steal the router row", t is None or t["alias"] != "router")

# T2: the router row is STILL queued (untouched by the generic claim) and claim_next_route_task gets it.
r = task_queue.claim_next_route_task(worker_id="keyed-w")
check("T2a: claim_next_route_task claimed the router row", r is not None and r["id"] == rid)
check("T2b: it is the router alias", r is not None and r["alias"] == "router")

# T3: with ONLY a router row queued, claim_next_task returns None (never claims it).
_tmp2 = tempfile.mkdtemp(prefix="aios_claim2_")
os.environ["AIOS_TASK_QUEUE_DB"] = str(Path(_tmp2) / "q.sqlite3")
# force task_queue to re-resolve the db path (it reads env each _conn())
task_queue.enqueue_task(chat_id=CHAT, alias="router", mode="inbound",
                        task_text='{"kind":"callback"}', source="telegram")
check("T3: only-router queue -> claim_next_task returns None",
      task_queue.claim_next_task(worker_id="claude-w") is None)

shutil.rmtree(_tmp, ignore_errors=True)
shutil.rmtree(_tmp2, ignore_errors=True)

if _failed:
    print("\nRESULT: FAIL (" + ", ".join(_failed) + ")")
    sys.exit(1)
print("\nRESULT: ALL PASS")
sys.exit(0)
