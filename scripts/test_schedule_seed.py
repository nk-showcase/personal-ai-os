#!/usr/bin/env python3
"""Tests for bot/schedule_seed.seed_schedules — the startup-seeder that arms the daily schedule rows.

SAFE / OFFLINE: temp sqlite (AIOS_TASK_QUEUE_DB) only; no network, no Todoist/Telegram send, no real
owner id. The executor round-trip uses a STUB handler, so the real handler (Todoist / worker_telegram)
is never invoked.

Covers:
  T1  after seed_schedules(): exactly ONE pending row — event_reminders at its configured wall-clock
      time, cancel_key 'schedule:<kind>', payload {'chat_id': owner}.
  T2  idempotent restart: a second seed_schedules() -> STILL exactly one pending row (cancel-then-add).
  T3  the due_ts is strictly future.
  T4  missing TELEGRAM_OWNER_ID (fresh subprocess) -> returns 0, seeds NOTHING (never chat_id=0).
  T5  executor round-trip: seed -> run_due_jobs(now>=due) fires the (stub) handler once and
      re-arms EXACTLY ONE pending row for the next day (same cancel_key), no duplicates.

Run (VPS venv 3.12):  PYTHONPATH=. .venv/bin/python scripts/test_schedule_seed.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OWNER = "424242"
TZ = ZoneInfo(os.environ.get("AIOS_TZ", "UTC"))
_tmp = tempfile.mkdtemp(prefix="aios_seed_")
_DB = str(Path(_tmp) / "q.sqlite3")
os.environ["AIOS_TASK_QUEUE_DB"] = _DB
os.environ["AIOS_SECRETS_BACKEND"] = "env"      # get_secret reads env (no Bitwarden in tests)
os.environ["TELEGRAM_OWNER_ID"] = OWNER
os.environ.pop("BWS_ACCESS_TOKEN", None)

sys.path.insert(0, os.getcwd())

from bot import schedule_seed          # noqa: E402
from bot import schedule_jobs          # noqa: E402
from bot import schedule_executor      # noqa: E402
from bot import schedule_queue         # noqa: E402

_failed: list = []


def check(name: str, cond: bool) -> None:
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        _failed.append(name)


def _pending_by_kind():
    with sqlite3.connect(_DB) as c:
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            "SELECT kind, payload, due_ts, cancel_key, fired_ts FROM scheduled WHERE fired_ts IS NULL"
        ).fetchall()]
    return {r["kind"]: r for r in rows}, len(rows)


EVT = schedule_jobs.KIND_EVENT_REMINDERS

# -----------------------------------------------------------------------------
# T1: one pending row for the daily kind, correct fields
# -----------------------------------------------------------------------------
n1 = schedule_seed.seed_schedules()
by, total = _pending_by_kind()
check("T1a: seed_schedules() reported 1 kind seeded", n1 == 1)
check("T1b: exactly ONE pending row", total == 1)
check("T1c: kind is event_reminders", set(by) == {EVT})
if EVT in by:
    check("T1d: cancel_key + payload",
          by[EVT]["cancel_key"] == "schedule:event_reminders"
          and json.loads(by[EVT]["payload"]) == {"chat_id": int(OWNER)})

# -----------------------------------------------------------------------------
# T2: idempotent across a restart (second seed -> still one row)
# -----------------------------------------------------------------------------
schedule_seed.seed_schedules()
schedule_seed.seed_schedules()
_, total2 = _pending_by_kind()
check("T2: extra seed calls -> STILL exactly one pending row (cancel-then-add)", total2 == 1)

# -----------------------------------------------------------------------------
# T3: due strictly future
# -----------------------------------------------------------------------------
by, _ = _pending_by_kind()
now = datetime.now(ZoneInfo("UTC")).timestamp()
if EVT in by:
    check("T3a: event_reminders due is strictly future",
          float(by[EVT]["due_ts"]) > now)

# -----------------------------------------------------------------------------
# T4: missing owner id (fresh subprocess, clean env) -> seeds nothing
# -----------------------------------------------------------------------------
_db4 = str(Path(_tmp) / "q4.sqlite3")
_env4 = {k: v for k, v in os.environ.items() if k != "TELEGRAM_OWNER_ID"}
_env4["AIOS_TASK_QUEUE_DB"] = _db4
_env4["AIOS_SECRETS_BACKEND"] = "env"
_env4.pop("BWS_ACCESS_TOKEN", None)
_code = (
    "import sys; sys.path.insert(0, " + repr(os.getcwd()) + "); "
    "from bot import schedule_seed, schedule_queue; "
    "n = schedule_seed.seed_schedules(); "
    "print('SEEDED', n, 'PENDING', schedule_queue.pending_count())"
)
_p = subprocess.run([sys.executable, "-c", _code], capture_output=True, text=True, env=_env4, timeout=30)
check("T4a: missing owner -> subprocess ran cleanly", _p.returncode == 0)
check("T4b: missing owner -> seeded 0, pending 0 (never seeds chat_id=0)",
      "SEEDED 0 PENDING 0" in (_p.stdout or "").strip())

# -----------------------------------------------------------------------------
# T5: executor round-trip with a STUB handler (no real handler)
# -----------------------------------------------------------------------------
with sqlite3.connect(_DB) as _con:
    _con.execute("DELETE FROM scheduled")
schedule_seed.seed_schedules()
by5, _ = _pending_by_kind()
_due = float(by5[EVT]["due_ts"])

fired = {EVT: 0}

def _mk_stub(kind):
    async def _h(payload):
        fired[kind] += 1
    return _h

_stub_handlers = {EVT: _mk_stub(EVT)}
# now = due + 1s so the seeded row is due; next_due_fn re-arms it for its next day.
n_fired = asyncio.run(schedule_executor.run_due_jobs(
    _stub_handlers, next_due_fn=schedule_jobs.next_due_fn, now=_due + 1.0))
check("T5a: run_due_jobs fired exactly one job", n_fired == 1)
check("T5b: the stub handler ran once", fired[EVT] == 1)
by5b, total5 = _pending_by_kind()
check("T5c: exactly ONE re-armed pending row remains (no duplicates)", total5 == 1)
if total5 == 1:
    check("T5d: the re-armed row keeps its cancel_key",
          by5b.get(EVT, {}).get("cancel_key") == "schedule:event_reminders")
    _now = datetime.now(ZoneInfo("UTC")).timestamp()
    check("T5e: the re-armed row is FUTURE (no same-tick re-fire)",
          float(by5b[EVT]["due_ts"]) > _now)

shutil.rmtree(_tmp, ignore_errors=True)

if _failed:
    print("\nRESULT: FAIL (" + ", ".join(_failed) + ")")
    sys.exit(1)
print("\nRESULT: ALL PASS")
sys.exit(0)
