#!/usr/bin/env python3
"""Plain-python test (no pytest) for bot.claude_worker_core.

SAFE: temporary DB, a fake async runner; no real secrets / network /
Claude / Telegram / GitHub.
Covers: empty queue -> no-op; task claim; runner receives payload;
success -> done + result; failing runner -> failed + error WITHOUT the secret;
a cancelled task is not executed.
Run:  PYTHONPATH=<repo> python3 scripts/test_claude_worker_core.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aios_cwc_test_")
    os.environ["AIOS_TASK_QUEUE_DB"] = str(Path(tmp) / "q.sqlite3")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

    from bot import task_queue as q
    from bot import claude_worker_core as core

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    calls = []
    # A secret-shaped string used to verify redaction; assembled at runtime so the
    # literal does NOT land in the file and trip the repository secret-scan.
    fake_token = "sk-ant-" + ("a" * 28)

    async def ok_runner(task):
        calls.append(task)
        return {"reply": "done editing", "files": ["a.py"]}

    async def boom_runner(task):
        calls.append(task)
        raise RuntimeError("boom leaking token=" + fake_token + " end")

    async def never_runner(task):
        calls.append(task)
        return "should not run"

    try:
        # 1. empty queue -> no-op, runner NOT called
        calls.clear()
        r = asyncio.run(core.process_one("w1", never_runner))
        check("empty queue -> noop", r["status"] == "noop" and r["task_id"] is None)
        check("empty queue -> runner not called", len(calls) == 0)

        # 2-4. enqueue -> claim -> success runner -> done + result saved
        calls.clear()
        tid = q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="do X")
        r = asyncio.run(core.process_one("w1", ok_runner))
        check("claims enqueued task -> done", r["status"] == "done" and r["task_id"] == tid)
        check("runner received task payload", len(calls) == 1 and calls[0]["task_text"] == "do X")
        t = q.get_task(tid)
        check("task status done", t["status"] == "done")
        check("result saved", "done editing" in (t["result"] or ""))

        # 5. failing runner -> failed + error saved WITHOUT secret
        calls.clear()
        tid2 = q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="break it")
        r = asyncio.run(core.process_one("w1", boom_runner))
        check("failing runner -> failed", r["status"] == "failed" and r["task_id"] == tid2)
        t2 = q.get_task(tid2)
        check("task status failed", t2["status"] == "failed")
        check("error keeps exception type", "RuntimeError" in (t2["error"] or ""))
        check(
            "error redacts secret value",
            fake_token not in (t2["error"] or "") and "<redacted>" in (t2["error"] or ""),
        )

        # 6. cancelled task is NOT executed
        calls.clear()
        tid3 = q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="cancel me")
        q.update_task(tid3, "cancelled")
        r = asyncio.run(core.process_one("w1", never_runner))
        check("cancelled task not claimed -> noop", r["status"] == "noop")
        check("cancelled runner not called", len(calls) == 0)
        check("cancelled task stays cancelled", q.get_task(tid3)["status"] == "cancelled")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
