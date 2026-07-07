#!/usr/bin/env python3
"""Plain-python tests for the V2 entrypoints.

SAFE: a temporary SQLite database, a fake runner. No Telegram / Claude / Git host /
real secrets. Checks: importing telegram_bot/claude_worker with no side effects and
without TELEGRAM_BOT_TOKEN; claude_worker as a real queue consumer with a fake runner (no
real Claude). Importing claude_bridge (with Claude-login side effects) is checked
separately in the venv during the VPS check.
Run:  PYTHONPATH=<repo> python3 scripts/test_v2_entrypoints.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aios_v2ep_test_")
    os.environ["AIOS_TASK_QUEUE_DB"] = str(Path(tmp) / "q.sqlite3")
    # Ensure the entrypoints do not require a Telegram token to IMPORT.
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ.pop("TELEGRAM_OWNER_ID", None)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    # --- import telegram_bot: no env, no side effects, does not run ---
    from bot import telegram_bot as tb
    check("import telegram_bot ok, has main()", callable(getattr(tb, "main", None)))

    # --- import claude_worker WITHOUT TELEGRAM_BOT_TOKEN ---
    from bot import claude_worker as cw
    from bot import task_queue as q
    check("import claude_worker ok without TELEGRAM_BOT_TOKEN",
          "TELEGRAM_BOT_TOKEN" not in os.environ and callable(getattr(cw, "run_loop", None)))

    # --- import did not create the queue DB (no import-time side effects) ---
    check("imports: no queue DB side effect", not Path(os.environ["AIOS_TASK_QUEUE_DB"]).exists())

    calls = []

    async def fake_runner(task):
        calls.append(task["id"])
        return {"text": "fake worker result", "session_id": None}

    async def fast_sleep(_seconds):
        return None

    # --- empty queue -> noop, fake runner not called ---
    r = asyncio.run(cw.run_once(fake_runner))
    check("run_once empty queue -> noop", r.get("status") == "noop")
    check("fake runner NOT called on empty queue", len(calls) == 0)

    # --- one task -> run_loop(1 iteration) processes it, real Claude is NOT started ---
    tid = q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="do X")
    n = asyncio.run(cw.run_loop(fake_runner, max_iterations=1, poll_interval=0, sleep=fast_sleep))
    check("run_loop ran exactly 1 iteration", n == 1)
    check("fake runner processed the queued task", calls == [tid])
    t = q.get_task(tid)
    check("worker marked task done (fake runner, no real Claude)", t["status"] == "done")
    check("worker stored result", "fake worker result" in (t["result"] or ""))

    # --- B-02E: import bot.integrations_worker is side-effect-free ---
    # Strip any AIOS_INTEGRATIONS_* env that may have leaked into the
    # parent shell so the default-assertion holds (review-fix hygiene).
    for vname in ("AIOS_INTEGRATIONS_WORKER_ID",
                  "AIOS_INTEGRATIONS_POLL_S",
                  "AIOS_INTEGRATIONS_SWEEP_S",
                  "AIOS_INTEGRATIONS_LEASE_S"):
        os.environ.pop(vname, None)
    from bot import integrations_worker as iw
    check("import integrations_worker ok, has run_once + run_loop + main()",
          callable(getattr(iw, "run_once", None))
          and callable(getattr(iw, "run_loop", None))
          and callable(getattr(iw, "main", None)))
    check("integrations_worker.WORKER_ID has default 'integrations-worker-1'",
          iw.WORKER_ID == "integrations-worker-1")
    check("integrations_worker.WORKER_ID DISTINCT from claude-worker default",
          iw.WORKER_ID != cw.WORKER_ID)
    check("integrations_worker._noop_inert_applier is callable (default stub)",
          callable(getattr(iw, "_noop_inert_applier", None)))
    # Review-fix: extend smoke into meaningful exercise -- run_once on
    # an empty queue returns None synchronously (no asyncio).
    from bot import aios_storage as S
    iw_db = str(Path(tmp) / "iw_entry_q.sqlite3")
    S.init_db(db=iw_db)
    out_iw = iw.run_once(db=iw_db, now_fn=lambda: 1700000000.0)
    check("integrations_worker.run_once on empty queue -> None "
          "(sync; no asyncio)",
          out_iw is None)
    # And run_loop with max_iterations=1 + empty queue returns the
    # summary dict.
    summary_iw = iw.run_loop(
        applier=lambda r: {"ok": True},
        max_iterations=1,
        poll_interval=0.001, sweep_interval=9999.0,
        sleep=lambda s: None, now_fn=lambda: 1700000010.0,
        db=iw_db,
    )
    check("integrations_worker.run_loop returns summary dict "
          "with iterations/processed/sweeps keys",
          set(summary_iw.keys()) == {"iterations", "processed", "sweeps"})

    shutil.rmtree(tmp, ignore_errors=True)
    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
