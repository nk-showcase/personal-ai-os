"""Test S6: bot/schedule_jobs — live daily jobs (event reminders) migrated onto the schedule
executor. INERT: the live run_daily registrations in main.py are untouched.

DELIVERY CONTRACT (Gate 0 / worker-sends): the personal text goes out via the keyed worker's OWN
Telegram send, NEVER through a reply_queue row the keyless receiver would poll+send:
  _event_reminders -> worker_telegram.send_message(chat_id, text)
So worker_telegram.send_message is STUBBED/CAPTURED here, and the test asserts NO reply_queue row
ever carries the personal text.

storage (task source) is STUBBED via sys.modules so no network/secret is touched. The real
reminder._parse_schedule/_parse_sent + _next_daily run unstubbed; because the event handler imports
the real bot/reminder.py (which uses 3.10+ `X | None` syntax with no future-annotations import),
this test runs on the project venv (python 3.10+), not bare python3 (3.9).
Run (venv):  PYTHONPATH=. .venv/bin/python scripts/test_schedule_jobs.py
"""
import asyncio
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
_DB = os.path.join(tempfile.mkdtemp(prefix="aios_sjobs_"), "q.sqlite3")
os.environ["AIOS_TASK_QUEUE_DB"] = _DB

# A configurable timezone for the test clock (an arbitrary non-zero offset). The SAME zone drives the schedule
# default (AIOS_SCHEDULE_TZ, set BEFORE importing schedule_jobs) and the wall-clock readbacks, so the
# boundary + re-arm assertions stay internally consistent (no UTC-vs-local mismatch). Etc/GMT+7 is the
# IANA spelling of UTC-7 (POSIX sign convention).
SCHED_TZ_NAME = "Etc/GMT+7"
os.environ["AIOS_SCHEDULE_TZ"] = SCHED_TZ_NAME
LOCAL_TZ = ZoneInfo(SCHED_TZ_NAME)
_today = datetime.now(LOCAL_TZ).date()
_tomorrow_str = (_today + timedelta(days=1)).isoformat()

# --- stub bot.storage (task source) BEFORE schedule_jobs' lazy imports ---
import bot  # noqa: E402

TASKS = {"v": []}
COMMENTS = {"v": {}}     # task_id -> list[{"content": ...}]
ADDED = []               # (task_id, text) write-backs

_storage = types.ModuleType("bot.storage")
async def _list_tasks():
    return TASKS["v"]
async def _get_comments(task_id):
    return COMMENTS["v"].get(task_id, [])
async def _add_comment(task_id, text):
    ADDED.append((task_id, text)); return True
_storage.list_tasks = _list_tasks
_storage.get_comments = _get_comments
_storage.add_comment = _add_comment
sys.modules["bot.storage"] = _storage
bot.storage = _storage

# --- worker-sends capture: the KEYED worker's own Telegram send (reminders deliver here, NOT via
#     reply_queue). Capture (chat_id, text) so we can assert the personal text left this way. ---
SENT = []                # list[(chat_id, text)]
_wt = types.ModuleType("bot.worker_telegram")
def _wt_send_message(chat_id, text, reply_markup=None, timeout=15.0):
    SENT.append((chat_id, text)); return {"ok": True, "parts": 1}
_wt.send_message = _wt_send_message
sys.modules["bot.worker_telegram"] = _wt
bot.worker_telegram = _wt

from bot import schedule_jobs as sj, schedule_queue as sq, schedule_executor as se, reply_queue as rq  # noqa: E402,F401

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


TZ = ZoneInfo("UTC")


def _epoch(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=LOCAL_TZ).timestamp()


def _replies(chat_id):
    # Personal text now goes out WORKER-DIRECT, so the reply_queue 'replies' table may never be
    # created (nothing enqueues). A missing table == zero reply rows for the leak assertions.
    with sqlite3.connect(_DB) as c:
        try:
            rows = c.execute("SELECT chat_id, text FROM replies WHERE chat_id=?", (chat_id,)).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(zip(("chat_id", "text"), r)) for r in rows]


def _sent(chat_id):
    """Texts delivered via the keyed worker's OWN Telegram send (worker_telegram.send_message)."""
    return [t for (cid, t) in SENT if cid == chat_id]


# --- 1. _next_daily boundary (generic recurrence, unchanged) ---
check("#1 just BEFORE the configured time -> today at that time",
      sj._next_daily(_epoch(2026, 6, 23, 6, 59), 7, 0, SCHED_TZ_NAME) == _epoch(2026, 6, 23, 7, 0))
check("#1 EXACTLY at the configured time -> tomorrow (strictly future, no same-instant re-fire)",
      sj._next_daily(_epoch(2026, 6, 23, 8, 30), 8, 30, SCHED_TZ_NAME) == _epoch(2026, 6, 24, 8, 30))

# --- 2. event reminders: due event -> consolidated WORKER-DIRECT send + task-source write-back;
#        already-sent silent ---
TASKS["v"] = [
    {"id": "A", "content": "Event A", "labels": ["event"], "due_date": _tomorrow_str + "T20:00:00"},
    {"id": "B", "content": "Event B", "labels": ["event"], "due_date": _tomorrow_str + "T20:00:00"},
]
COMMENTS["v"] = {"A": [], "B": [{"content": f"reminded_1d ({_today.isoformat()})"}]}
ADDED.clear()
SENT.clear()
sq.add_scheduled(sj.KIND_EVENT_REMINDERS, time.time() - 10, payload={"chat_id": 42}, cancel_key=sj.KIND_EVENT_REMINDERS)
n = asyncio.run(se.run_due_jobs(sj.HANDLERS, next_due_fn=sj.next_due_fn, now=time.time()))
sent42 = _sent(42)
check("#2 fired (run_due_jobs == 1)", n == 1)
check("#2 exactly one consolidated worker-direct send", len(sent42) == 1)
check("#2 due event A IS in the worker send", bool(sent42) and "Event A" in sent42[0])
check("#2 already-sent event B is NOT in the worker send", bool(sent42) and "Event B" not in sent42[0])
check("#2 NO reply_queue row carries the personal event text (no keyless-receiver transit)",
      len(_replies(42)) == 0)
check("#2 write-back marked A sent (reminded_1d), exactly once",
      ADDED == [("A", f"reminded_1d ({_today.isoformat()})")])
with sqlite3.connect(_DB) as c:
    pend = c.execute("SELECT due_ts FROM scheduled WHERE cancel_key=? AND fired_ts IS NULL",
                     (sj.KIND_EVENT_REMINDERS,)).fetchall()
check("#2 re-armed exactly one future daily row at the configured time",
      len(pend) == 1 and pend[0][0] > time.time()
      and datetime.fromtimestamp(pend[0][0], LOCAL_TZ).hour == 7
      and datetime.fromtimestamp(pend[0][0], LOCAL_TZ).minute == 0)

# --- 3. event reminders: nothing due -> silent (no worker send, no reply row, no write-back) ---
TASKS["v"] = []
ADDED.clear()
SENT.clear()
sq.add_scheduled(sj.KIND_EVENT_REMINDERS, time.time() - 10, payload={"chat_id": 43}, cancel_key="evrem-empty")
asyncio.run(se.run_due_jobs(sj.HANDLERS, next_due_fn=sj.next_due_fn, now=time.time()))
check("#3 no event tasks -> no worker send, no reply row, no write-back",
      len(_sent(43)) == 0 and len(_replies(43)) == 0 and ADDED == [])

# --- 4. cutover: event-reminders are cut over to the worker's schedule executor. (a) main.py no
#        longer registers run_daily 'daily_reminders'. (b) the keyless receiver
#        (main.py/handlers.py) never IMPORTS schedule_jobs — it is driven only by the keyed worker
#        (bot/schedule_seed -> schedule_executor). Comments allowed. ---
_rd = subprocess.run(["grep", "-rn", 'name="daily_reminders"', "bot/main.py"],
                     capture_output=True, text=True).stdout.strip()
check("#4a cutover: main.py no longer registers run_daily 'daily_reminders'", _rd == "")
_imp = subprocess.run(
    ["grep", "-rnE", r"(from \.|from bot\.|import )schedule_jobs", "bot/main.py", "bot/handlers.py"],
    capture_output=True, text=True).stdout.strip()
check("#4b: no live IMPORT of schedule_jobs in main.py/handlers.py (comments allowed)", _imp == "")

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
