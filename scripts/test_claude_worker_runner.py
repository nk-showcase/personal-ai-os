#!/usr/bin/env python3
"""Plain-python tests for bot.claude_worker_runner (Block 2/7 execution layer).

SAFE: a temporary SQLite DB, a fake executor, approvals through the REAL queue (temp DB)
resolved by the test. No network / Telegram / Claude / GitHub / real secrets. The real
claude_bridge is NOT imported (default_executor is only checked for refusing to run).

Coverage: mode ask->read_only, fix/edit->edit, continue; success; failure without a secret;
approval allow / deny / timeout (safe deny); import with no side effects.
Run:  PYTHONPATH=<repo> python3 scripts/test_claude_worker_runner.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aios_cwr_test_")
    os.environ["AIOS_TASK_QUEUE_DB"] = str(Path(tmp) / "q.sqlite3")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

    from bot import task_queue as q
    from bot import claude_worker_core as core
    from bot import claude_worker_runner as r

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    # --- imports have no side effects (DB not created until first call) ---
    check("import: no DB side effect", not Path(os.environ["AIOS_TASK_QUEUE_DB"]).exists())

    # --- explicit mode mapping ---
    check("mode ask -> read_only", r.resolve_mode("ask").read_only is True)
    check("mode fix -> edit", r.resolve_mode("fix").read_only is False)
    check("mode edit -> edit", r.resolve_mode("edit").read_only is False)
    _cont = r.resolve_mode("continue")
    check("mode continue -> edit+continue", _cont.read_only is False and _cont.is_continue is True)
    check("mode unknown -> read_only (safe)", r.resolve_mode("weird").read_only is True)

    seen = {}

    async def fake_exec(*, task, read_only, is_continue, resume_session_id, design_mode, approve):
        seen["read_only"] = read_only
        seen["is_continue"] = is_continue
        seen["resume"] = resume_session_id
        seen["design_mode"] = design_mode
        seen["task_text"] = task["task_text"]
        return {"text": "done editing", "edited_files": ["a.py"]}

    runner = r.build_runner(fake_exec)

    # --- 1: ask -> read_only behavior through process_one ---
    seen.clear()
    q.enqueue_task(chat_id=1, alias="proj", mode="ask", task_text="read it")
    res = asyncio.run(core.process_one("w1", runner))
    check("ask routed read_only=True", seen.get("read_only") is True and res["status"] == "done")
    check("executor received task_text", seen.get("task_text") == "read it")

    # --- 2: fix -> edit behavior + resume passthrough ---
    seen.clear()
    tid2 = q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="fix it", resume_session_id="sess-9")
    res = asyncio.run(core.process_one("w1", runner))
    check("fix routed read_only=False", seen.get("read_only") is False)
    check("resume_session_id passed through", seen.get("resume") == "sess-9")

    # --- 3: success stores result ---
    t2 = q.get_task(tid2)
    check("success -> task done", t2["status"] == "done")
    check("success -> result stored", "done editing" in (t2["result"] or ""))

    # --- 3b: design_mode flows from queue task to executor (Block 3 patch) ---
    seen.clear()
    q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="design task", design_mode=True)
    asyncio.run(core.process_one("w1", runner))
    check("design_mode True routed to executor", seen.get("design_mode") is True)
    seen.clear()
    q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="plain", design_mode=False)
    asyncio.run(core.process_one("w1", runner))
    check("design_mode False routed to executor", seen.get("design_mode") is False)

    # --- 4: failure stores failed/error WITHOUT secret ---
    fake_token = "sk-ant-" + ("b" * 30)  # assembled at runtime, no literal in the file
    async def boom_exec(*, task, read_only, is_continue, resume_session_id, design_mode, approve):
        raise RuntimeError("kaboom secret=" + fake_token)

    tid3 = q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="oops")
    res = asyncio.run(core.process_one("w1", r.build_runner(boom_exec)))
    t3 = q.get_task(tid3)
    check("failure -> failed", res["status"] == "failed" and t3["status"] == "failed")
    check("failure -> exception type kept", "RuntimeError" in (t3["error"] or ""))
    check("failure -> secret redacted",
          fake_token not in (t3["error"] or "") and "<redacted>" in (t3["error"] or ""))

    # --- 5/6: approval allow / deny via queue-backed approver ---
    tid_a = q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="approve target")

    async def approve_with_verdict(set_to):
        approver = r.make_queue_approver(tid_a, timeout=2.0, poll_interval=0.01)
        call = asyncio.create_task(approver("Bash", {"command": "ls"}))
        for _ in range(400):
            ap = q.poll_pending_approval(task_id=tid_a)
            if ap:
                q.resolve_approval(ap["id"], set_to)
                break
            await asyncio.sleep(0.005)
        return await call

    check("approval allow -> True", asyncio.run(approve_with_verdict(True)) is True)
    check("approval deny -> False", asyncio.run(approve_with_verdict(False)) is False)

    # --- 7: approval timeout -> safe deny + closes pending approval (no stale button) ---
    # A separate task so it does not get confused with the allow/deny above.
    tid_to = q.enqueue_task(chat_id=1, alias="proj", mode="fix", task_text="timeout target")

    async def approve_timeout():
        approver = r.make_queue_approver(tid_to, timeout=0.15, poll_interval=0.02)
        start = time.monotonic()
        verdict = await approver("Bash", {"command": "rm -rf /"})
        return verdict, (time.monotonic() - start)

    v_to, elapsed = asyncio.run(approve_timeout())
    check("approval timeout -> safe deny (False)", v_to is False)
    check("approval timeout actually waited ~timeout", elapsed >= 0.13)
    # Block 2 patch regression: after a timeout there must be NO pending approval left,
    # otherwise the transport would show a stale Allow/Deny button.
    check("no stale pending approval after timeout", q.poll_pending_approval(task_id=tid_to) is None)

    # --- 8: default_executor refuses to run real Claude in Block 2 ---
    async def call_default():
        try:
            await r.default_executor(task={"task_text": "x", "chat_id": 1}, read_only=True,
                                     is_continue=False, resume_session_id=None, approve=None)
            return "no-raise"
        except NotImplementedError:
            return "raised"

    check("default_executor raises (no real Claude in Block 2)", asyncio.run(call_default()) == "raised")

    shutil.rmtree(tmp, ignore_errors=True)
    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
