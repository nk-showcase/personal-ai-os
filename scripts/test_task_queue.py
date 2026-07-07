#!/usr/bin/env python3
"""Plain-python test (no pytest) for bot.task_queue.

SAFE: temp DB, no real secrets / network / Telegram / Claude / GitHub.
Full cycle: enqueue -> claim -> update running -> request approval -> poll approval
-> resolve approval -> get verdict -> update done -> list updates (+ deny path, since filter).
Run:  PYTHONPATH=<repo> python3 scripts/test_task_queue.py   (exit 0 = OK, 1 = FAIL)
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aios_tq_test_")
    os.environ["AIOS_TASK_QUEUE_DB"] = str(Path(tmp) / "q.sqlite3")
    os.environ["AIOS_APPROVAL_TIMEOUT"] = "300"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

    from bot import task_queue as q

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    try:
        check("import has no DB side effect (file created lazily)",
              not Path(os.environ["AIOS_TASK_QUEUE_DB"]).exists())

        tid = q.enqueue_task(chat_id=123, alias="myproj", mode="fix", task_text="add feature X")
        check("enqueue returns id", isinstance(tid, int) and tid > 0)
        check("db file created on first call", Path(os.environ["AIOS_TASK_QUEUE_DB"]).exists())

        t = q.claim_next_task(worker_id="w1")
        check("claim returns the queued task", t is not None and t["id"] == tid)
        check("claimed status == running", bool(t) and t["status"] == "running")
        check("worker_id recorded", bool(t) and t["worker_id"] == "w1")
        check("empty queue -> claim None", q.claim_next_task(worker_id="w1") is None)

        aid = q.request_approval(tid, "Bash", {"command": "ls -la"})
        check("request_approval returns id", isinstance(aid, int) and aid > 0)
        check("task flips to awaiting_approval", q.get_task(tid)["status"] == "awaiting_approval")

        ap = q.poll_pending_approval()
        check("poll returns the pending approval", ap is not None and ap["id"] == aid)
        check("tool_input parsed to dict",
              bool(ap) and isinstance(ap["tool_input"], dict) and ap["tool_input"]["command"] == "ls -la")

        check("verdict None while pending", q.get_verdict(aid) is None)
        q.resolve_approval(aid, True)
        check("verdict True after allow", q.get_verdict(aid) is True)
        check("no pending approval left", q.poll_pending_approval() is None)

        q.update_task(tid, "done", result="patched 2 files; pushed")
        done = q.get_task(tid)
        check("task done", done["status"] == "done")
        check("result stored", done["result"] == "patched 2 files; pushed")

        # since_ts filter + deny path on a second task
        marker = time.time()
        time.sleep(0.01)
        tid2 = q.enqueue_task(chat_id=123, alias="p2", mode="ask", task_text="read code")
        ids = [r["id"] for r in q.list_updates(since_ts=marker)]
        check("since_ts filter returns the new task", tid2 in ids and tid not in ids)

        aid2 = q.request_approval(tid2, "Write", {"path": "x.py"})
        q.resolve_approval(aid2, False)
        check("verdict False after deny", q.get_verdict(aid2) is False)

        check("unknown approval id -> None", q.get_verdict(999999) is None)
        try:
            q.update_task(tid, "bogus_status")
            check("invalid status rejected", False)
        except ValueError:
            check("invalid status rejected", True)

        # --- design_mode field (Block 3 patch) ---
        tid_d = q.enqueue_task(chat_id=1, alias="p", mode="fix", task_text="x", design_mode=True)
        check("design_mode=True stored", q.get_task(tid_d)["design_mode"] == 1)
        tid_nd = q.enqueue_task(chat_id=1, alias="p", mode="fix", task_text="y")
        check("design_mode default 0", q.get_task(tid_nd)["design_mode"] == 0)

        # --- migration-safe: old DB without design_mode column gets it via ALTER ---
        import sqlite3 as _sq
        old_db = str(Path(tmp) / "old.sqlite3")
        _c = _sq.connect(old_db)
        _c.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,"
            " alias TEXT NOT NULL, mode TEXT NOT NULL, task_text TEXT NOT NULL, resume_session_id TEXT,"
            " source TEXT NOT NULL DEFAULT 'telegram', status TEXT NOT NULL DEFAULT 'queued',"
            " worker_id TEXT, result TEXT, error TEXT, created_ts REAL NOT NULL, updated_ts REAL NOT NULL)"
        )
        _c.execute("INSERT INTO tasks (chat_id,alias,mode,task_text,created_ts,updated_ts)"
                   " VALUES (1,'a','fix','old row',0,0)")
        _c.commit(); _c.close()
        os.environ["AIOS_TASK_QUEUE_DB"] = old_db  # repoint loader at the legacy DB
        new_id = q.enqueue_task(chat_id=2, alias="b", mode="fix", task_text="new", design_mode=True)
        check("migration: new row design_mode=1 on migrated old DB", q.get_task(new_id)["design_mode"] == 1)
        check("migration: pre-existing old row gets design_mode default 0", q.get_task(1)["design_mode"] == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
