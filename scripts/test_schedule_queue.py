#!/usr/bin/env python3
"""Plain-python tests for bot/schedule_queue.py (S6 0c worker scheduler). Offline, pure SQLite.

Covers: due jobs claimed once (one-shot, no double-fire after restart); future jobs not claimed
until due; payload round-trips; cancel() removes pending; oldest-due ordering.
  python3 scripts/test_schedule_queue.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["AIOS_TASK_QUEUE_DB"] = str(Path(tempfile.mkdtemp(prefix="aios_schedq_")) / "q.sqlite3")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import schedule_queue as sq  # noqa: E402

_FAILS = []


def check(cond, label):
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        _FAILS.append(label)


def main() -> int:
    T = 1_000_000.0  # fixed reference epoch

    j_past = sq.add_scheduled("reminder", due_ts=T - 10, payload={"chat_id": 7, "text": "ping"})
    j_future = sq.add_scheduled("reminder", due_ts=T + 3600, payload={"k": "v"})
    check(isinstance(j_past, int) and sq.pending_count() == 2, "two pending after add")

    due = sq.claim_due(now=T)
    check(len(due) == 1 and due[0]["id"] == j_past, "claim_due returns only the past-due job")
    check(due[0]["payload"] == {"chat_id": 7, "text": "ping"}, "payload round-trips as dict")
    check(due[0]["kind"] == "reminder", "kind preserved")

    # one-shot: a fired job is never returned again (restart-safe)
    check(sq.claim_due(now=T) == [], "fired job not returned again (one-shot)")
    check(sq.pending_count() == 1, "only the future job remains pending")

    # future job becomes due when the clock passes due_ts
    check(sq.claim_due(now=T + 4000)[0]["id"] == j_future, "future job claimed once due")
    check(sq.pending_count() == 0, "nothing pending after both fired")

    # ordering: oldest-due first
    a = sq.add_scheduled("k", due_ts=T + 5)
    b = sq.add_scheduled("k", due_ts=T + 1)
    ordered = [r["id"] for r in sq.claim_due(now=T + 100)]
    check(ordered == [b, a], "claim_due orders by due_ts (oldest first)")

    # cancel removes a pending job before it fires
    c = sq.add_scheduled("reminder", due_ts=T + 9999, cancel_key="reminder:7")
    check(sq.pending_count() == 1, "one pending before cancel")
    check(sq.cancel("reminder:7") == 1, "cancel returns count cancelled")
    check(sq.pending_count() == 0 and sq.claim_due(now=T + 100000) == [],
          "cancelled job never fires")

    print()
    if _FAILS:
        print(f"RESULT: {len(_FAILS)} FAIL")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
