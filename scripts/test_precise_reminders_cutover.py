#!/usr/bin/env python3
"""Precise-reminders cutover tests: the one-shot PRECISE per-task reminder moves off the keyless
transport's in-memory JobQueue onto the shared schedule_queue + keyed integrations-worker executor.

GATE 0 contract under test: the task TITLE is read and sent ONLY worker-side at fire time
(schedule_jobs._precise_reminder via worker_telegram); the transport-side arm (reminder_lib.arm_precise_reminder)
writes a VALUE-FREE row ({task_id, chat_id}) and NEVER logs a title; the worker startup reconcile
(schedule_seed.seed_precise_reminders) logs COUNTS only. No title is ever logged in plaintext, and no
title ever transits a reply_queue row.

SAFE / OFFLINE: temp sqlite (AIOS_TASK_QUEUE_DB) only; bot.storage + bot.worker_telegram are STUBBED
via sys.modules so no Todoist read / Telegram send / secret touches the network. Because it imports the
real bot/reminder_lib.py + bot/schedule_jobs.py (3.10+ `X | None` annotations, no future-import), run on the
VPS venv (python 3.10+).

Run (VPS venv):  PYTHONPATH=. .venv/bin/python scripts/test_precise_reminders_cutover.py
"""
import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.getcwd())
_tmp = tempfile.mkdtemp(prefix="aios_precise_")
_DB = os.path.join(_tmp, "q.sqlite3")
os.environ["AIOS_TASK_QUEUE_DB"] = _DB
os.environ["AIOS_SECRETS_BACKEND"] = "env"    # get_secret reads env (no Bitwarden in tests)
os.environ["TELEGRAM_OWNER_ID"] = "424242"
os.environ.pop("BWS_ACCESS_TOKEN", None)

TZ = ZoneInfo(os.environ.get("AIOS_TZ", "UTC"))
OWNER = 424242
SECRET_TITLE = "SECRET_TASK_TITLE_XYZ"   # a distinctive title we assert never lands in a log

# ---- stub bot.storage (Todoist) + bot.worker_telegram (keyed send) BEFORE the modules' lazy imports ----
import bot  # noqa: E402

GET_TASK = {"v": None}          # what storage.get_task returns
GET_TASK_RAISES = {"v": False}  # make storage.get_task raise (network hiccup)
LIST_TASKS = {"v": []}          # what storage.list_tasks returns

_storage = types.ModuleType("bot.storage")
async def _get_task(task_id):
    if GET_TASK_RAISES["v"]:
        raise RuntimeError("todoist unreachable")
    return GET_TASK["v"]
async def _list_tasks():
    return LIST_TASKS["v"]
_storage.get_task = _get_task
_storage.list_tasks = _list_tasks
sys.modules["bot.storage"] = _storage
bot.storage = _storage

SENT = []                       # list[(chat_id, text)] captured worker-direct sends
_wt = types.ModuleType("bot.worker_telegram")
def _wt_send_message(chat_id, text, reply_markup=None, timeout=15.0):
    SENT.append((chat_id, text)); return {"ok": True, "parts": 1}
_wt.send_message = _wt_send_message
sys.modules["bot.worker_telegram"] = _wt
bot.worker_telegram = _wt

from bot import reminder_lib as rem      # noqa: E402
from bot import schedule_jobs as sj      # noqa: E402
from bot import schedule_queue as sq     # noqa: E402
from bot import schedule_executor as se  # noqa: E402
from bot import schedule_seed as ss      # noqa: E402

# ---- capture EVERY log record so we can prove no title is ever logged in plaintext ----
LOG_TEXT = []
class _Cap(logging.Handler):
    def emit(self, record):
        try:
            LOG_TEXT.append(record.getMessage())   # fully %-formatted with record.args
        except Exception:
            LOG_TEXT.append(str(record.msg))
_caph = _Cap()
logging.getLogger().addHandler(_caph)
logging.getLogger().setLevel(logging.DEBUG)

fails = 0
def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1

def _pending():
    with sqlite3.connect(_DB) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT kind, payload, due_ts, cancel_key, fired_ts FROM scheduled WHERE fired_ts IS NULL"
        ).fetchall()]

def _reset_db():
    with sqlite3.connect(_DB) as c:
        try:
            c.execute("DELETE FROM scheduled")
        except sqlite3.OperationalError:
            pass

import json  # noqa: E402
_tomorrow = (datetime.now(TZ) + timedelta(days=1)).replace(microsecond=0)
_tom_iso = _tomorrow.strftime("%Y-%m-%dT%H:%M:%S")           # floating -> configured wall clock
_yesterday_iso = (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")

# =============================================================================
# A. arm_precise_reminder — receiver-side, value-free schedule_queue row
# =============================================================================
_reset_db()
r = rem.arm_precise_reminder("TASK1", _tom_iso, OWNER)
rows = _pending()
check("A1a: arm returned True for a future time-specific due", r is True)
check("A1b: exactly one pending row", len(rows) == 1)
if rows:
    row = rows[0]
    pl = json.loads(row["payload"])
    check("A1c: kind is precise_reminder", row["kind"] == sj.KIND_PRECISE_REMINDER)
    check("A1d: cancel_key is precise:TASK1", row["cancel_key"] == "precise:TASK1")
    check("A1e: payload is value-free {task_id, chat_id} — NO title/content key",
          pl == {"task_id": "TASK1", "chat_id": OWNER})
    check("A1f: due_ts matches the configured wall-clock instant, in the future",
          abs(float(row["due_ts"]) - _tomorrow.timestamp()) < 1.5 and float(row["due_ts"]) > time.time())

# A2: dedup — arming the SAME task twice keeps exactly one pending row (atomic cancel-then-add)
rem.arm_precise_reminder("TASK1", _tom_iso, OWNER)
check("A2a: re-arming same task_id -> STILL one pending row (atomic upsert)", len(_pending()) == 1)
# A2b: re-arm with a LATER due -> still one row, and the due is REPLACED (edit-safe, not stacked)
_later = (_tomorrow + timedelta(hours=2))
rem.arm_precise_reminder("TASK1", _later.strftime("%Y-%m-%dT%H:%M:%S"), OWNER)
_rows2 = _pending()
check("A2b: re-arm with new due -> one row with the UPDATED due (upsert replaces)",
      len(_rows2) == 1 and abs(float(_rows2[0]["due_ts"]) - _later.timestamp()) < 1.5)
# A2c: upsert_scheduled REQUIRES a cancel_key (a keyless upsert cannot dedup -> guard raises)
_raised = False
try:
    sq.upsert_scheduled(sj.KIND_PRECISE_REMINDER, time.time() + 100, {"x": 1})
except ValueError:
    _raised = True
check("A2c: upsert_scheduled without cancel_key raises ValueError", _raised)

# A3: rejects non-precise / past / bad due
_reset_db()
check("A3a: date-only due (no 'T') -> False, no row",
      rem.arm_precise_reminder("T", "2026-07-10", OWNER) is False and len(_pending()) == 0)
check("A3b: past due -> False, no row",
      rem.arm_precise_reminder("T", _yesterday_iso, OWNER) is False and len(_pending()) == 0)
check("A3c: empty due -> False, no row",
      rem.arm_precise_reminder("T", "", OWNER) is False and len(_pending()) == 0)
check("A3d: unparseable due -> False, no row",
      rem.arm_precise_reminder("T", "not-a-dateThh", OWNER) is False and len(_pending()) == 0)

# =============================================================================
# B. _precise_reminder — worker-side fire: re-fetch, send, skip closed, generic fallback
# =============================================================================
GET_TASK_RAISES["v"] = False
GET_TASK["v"] = {"id": "TASK1", "content": SECRET_TITLE, "checked": False, "is_deleted": False}
SENT.clear()
asyncio.run(sj._precise_reminder({"task_id": "TASK1", "chat_id": OWNER}))
check("B1a: fires exactly one worker-direct send", len(SENT) == 1)
check("B1b: send carries the LIVE-refetched title", bool(SENT) and SENT[0] == (OWNER, f"Reminder: {SECRET_TITLE}"))

for _flag in ("checked", "is_deleted"):
    GET_TASK["v"] = {"id": "TASK1", "content": SECRET_TITLE, "checked": False, "is_deleted": False}
    GET_TASK["v"][_flag] = True
    SENT.clear()
    asyncio.run(sj._precise_reminder({"task_id": "TASK1", "chat_id": OWNER}))
    check(f"B2: task {_flag}=True -> skipped, no send", len(SENT) == 0)

GET_TASK["v"] = None
SENT.clear()
asyncio.run(sj._precise_reminder({"task_id": "TASK1", "chat_id": OWNER}))
check("B2c: get_task None (deleted) -> skipped, no send", len(SENT) == 0)

GET_TASK_RAISES["v"] = True
SENT.clear()
asyncio.run(sj._precise_reminder({"task_id": "TASK1", "chat_id": OWNER}))
check("B3: get_task raises -> generic fallback send (best-effort, no crash)",
      len(SENT) == 1 and SENT[0] == (OWNER, "Reminder: task"))
GET_TASK_RAISES["v"] = False

# =============================================================================
# C. registry + one-shot recurrence policy + executor round-trip
# =============================================================================
check("C1: HANDLERS registers precise_reminder (else claim_due silently drops it)",
      sj.HANDLERS.get(sj.KIND_PRECISE_REMINDER) is sj._precise_reminder)
check("C2: next_due_fn(precise_reminder) is None -> ONE-SHOT (no re-arm)",
      sj.next_due_fn(sj.KIND_PRECISE_REMINDER, {"task_id": "X", "chat_id": OWNER}) is None)

# C3: executor round-trip — a due precise row fires ONCE and leaves NO re-armed row (one-shot)
_reset_db()
sq.add_scheduled(sj.KIND_PRECISE_REMINDER, time.time() - 5,
                 {"task_id": "TASK9", "chat_id": OWNER}, cancel_key="precise:TASK9")
GET_TASK["v"] = {"id": "TASK9", "content": SECRET_TITLE, "checked": False, "is_deleted": False}
SENT.clear()
n_fired = asyncio.run(se.run_due_jobs(sj.HANDLERS, next_due_fn=sj.next_due_fn, now=time.time()))
check("C3a: run_due_jobs fired exactly one", n_fired == 1)
check("C3b: handler sent exactly one worker-direct reminder", len(SENT) == 1)
check("C3c: ONE-SHOT -> zero pending rows remain after fire (no re-arm)", len(_pending()) == 0)

# =============================================================================
# D. seed_precise_reminders — worker-side startup reconcile
# =============================================================================
_reset_db()
LIST_TASKS["v"] = [
    {"id": "P1", "content": SECRET_TITLE, "due_date": _tom_iso, "parent_id": None},           # arm
    {"id": "P2", "content": SECRET_TITLE, "due_date": _tom_iso[:10], "parent_id": None},       # date-only -> skip
    {"id": "P3", "content": SECRET_TITLE, "due_date": _tom_iso, "parent_id": "P1"},            # subtask -> skip
    {"id": "P4", "content": SECRET_TITLE, "due_date": _yesterday_iso, "parent_id": None},      # past -> skip
    {"id": "P5", "content": SECRET_TITLE, "due_date": None, "parent_id": None},                # no due -> skip
    {"id": "P6", "content": SECRET_TITLE, "due_date": _tom_iso, "parent_id": None},            # arm
]
armed = asyncio.run(ss.seed_precise_reminders())
rows = _pending()
cks = {r["cancel_key"] for r in rows}
check("D1a: seeder armed exactly the 2 future time-specific top-level tasks", armed == 2)
check("D1b: exactly 2 pending rows, for P1 and P6 only",
      len(rows) == 2 and cks == {"precise:P1", "precise:P6"})
check("D1c: every seeded row is value-free (no title in payload)",
      all("content" not in json.loads(r["payload"]) and SECRET_TITLE not in r["payload"] for r in rows))

# D2: idempotent across a restart
armed2 = asyncio.run(ss.seed_precise_reminders())
check("D2: second seed -> still exactly 2 pending rows (cancel-then-add per task_id)",
      armed2 == 2 and len(_pending()) == 2)

# D3: missing owner -> seeds nothing (fresh subprocess, clean env, never chat_id=0)
_db3 = os.path.join(_tmp, "q3.sqlite3")
_env3 = {k: v for k, v in os.environ.items() if k != "TELEGRAM_OWNER_ID"}
_env3["AIOS_TASK_QUEUE_DB"] = _db3
_env3["AIOS_SECRETS_BACKEND"] = "env"
_env3.pop("BWS_ACCESS_TOKEN", None)
# No storage stub needed: with the owner id unset, seed_precise_reminders() returns EARLY (owner
# guard) BEFORE it lazily imports storage / reads Todoist — so this never touches the network.
_code = (
    "import sys,asyncio; sys.path.insert(0, " + repr(os.getcwd()) + "); "
    "from bot import schedule_seed, schedule_queue; "
    "n=asyncio.run(schedule_seed.seed_precise_reminders()); "
    "print('ARMED', n, 'PENDING', schedule_queue.pending_count())"
)
_p = subprocess.run([sys.executable, "-c", _code], capture_output=True, text=True, env=_env3, timeout=30)
check("D3a: missing owner -> subprocess ran cleanly", _p.returncode == 0)
check("D3b: missing owner -> armed 0, pending 0 (never chat_id=0)",
      "ARMED 0 PENDING 0" in (_p.stdout or "").strip())

# =============================================================================
# E. GATE-0 leak assertion: NO task title ever appears in ANY captured log line
# =============================================================================
check("E1: the secret title NEVER appears in any log record (arm/fire/seed all title-silent)",
      all(SECRET_TITLE not in line for line in LOG_TEXT))

# =============================================================================
# F. cutover invariants — the receiver-side leak path is gone
# =============================================================================
def _grep(pattern, *files):
    return subprocess.run(["grep", "-rnE", pattern, *files], capture_output=True, text=True).stdout.strip()

check("F1: main.py no longer calls reschedule_precise_reminders (receiver rescan removed)",
      _grep("reschedule_precise_reminders", "bot/main.py") == "")
check("F2: handlers.py no longer calls schedule_precise_reminder (JobQueue arm removed)",
      _grep("schedule_precise_reminder", "bot/handlers.py") == "")
check("F3: handlers.py uses arm_precise_reminder at task creation",
      _grep("arm_precise_reminder", "bot/handlers.py") != "")
check("F4: dead leak funcs removed from reminder.py (schedule_precise_reminder/_precise_reminder_callback/reschedule_precise_reminders)",
      _grep(r"def (schedule_precise_reminder|_precise_reminder_callback|reschedule_precise_reminders)", "bot/reminder.py") == "")

import shutil  # noqa: E402
shutil.rmtree(_tmp, ignore_errors=True)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
print("RESULT: %d FAIL" % fails)
sys.exit(1)
