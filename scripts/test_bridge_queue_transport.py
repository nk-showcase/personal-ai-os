#!/usr/bin/env python3
"""Plain-python tests for bot.bridge_queue (transport producer + queue approvals).

SAFE: a temporary SQLite DB; no network / Telegram / Claude / GitHub / real secrets. Tests the
telegram-free logic that claude_bridge.py calls (claude_bridge itself is checked via py_compile
+ a safe-import check on the venv).

Coverage: enqueue fields correct; status formatting queued/running/done/failed/not-found
(+ redaction + truncation); approval pending payload; allow/deny resolve the queue; a stale
callback does not crash; parse callback valid/invalid; side-effect-free import.
Run:  PYTHONPATH=<repo> python3 scripts/test_bridge_queue_transport.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aios_bq_test_")
    os.environ["AIOS_TASK_QUEUE_DB"] = str(Path(tmp) / "q.sqlite3")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root

    from bot import task_queue as q
    from bot import bridge_queue as bq

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    # --- import has no side effects ---
    check("import: no DB side effect", not Path(os.environ["AIOS_TASK_QUEUE_DB"]).exists())

    # --- enqueue producer: fields correct ---
    tid = bq.enqueue_bridge_task(chat_id=42, alias="myproj", mode="fix",
                                 task_text="add feature X", resume_session_id="sess-7")
    t = q.get_task(tid)
    check("enqueue returns id + db created", isinstance(tid, int) and tid > 0 and Path(os.environ["AIOS_TASK_QUEUE_DB"]).exists())
    check("enqueue fields: chat_id", t["chat_id"] == 42)
    check("enqueue fields: alias", t["alias"] == "myproj")
    check("enqueue fields: mode", t["mode"] == "fix")
    check("enqueue fields: task_text", t["task_text"] == "add feature X")
    check("enqueue fields: resume_session_id", t["resume_session_id"] == "sess-7")
    check("enqueue fields: source telegram", t["source"] == "telegram")
    check("enqueue fields: status queued", t["status"] == "queued")

    # --- enqueue validation ---
    def raises(fn):
        try:
            fn(); return False
        except ValueError:
            return True
    check("enqueue rejects empty alias", raises(lambda: bq.enqueue_bridge_task(chat_id=1, alias="", mode="fix", task_text="x")))
    check("enqueue rejects empty task_text", raises(lambda: bq.enqueue_bridge_task(chat_id=1, alias="a", mode="fix", task_text="  ")))
    check("enqueue rejects bad mode", raises(lambda: bq.enqueue_bridge_task(chat_id=1, alias="a", mode="delete", task_text="x")))

    # --- status formatting ---
    check("status not found", bq.format_task_status(None) == "Task not found.")
    check("status queued", "queued" in bq.format_task_status(q.get_task(tid)) and f"#{tid}" in bq.format_task_status(q.get_task(tid)))
    q.claim_next_task(worker_id="w1")
    check("status running", "running" in bq.format_task_status(q.get_task(tid)))
    # done + redaction + truncation
    fake_token = "sk-ant-" + ("c" * 30)
    long_tail = "Z" * 4000
    q.update_task(tid, "done", result="pushed commit abc123; token=" + fake_token + " " + long_tail)
    done_msg = bq.format_task_status(q.get_task(tid))
    check("status done shows status", "done" in done_msg)
    check("status done redacts secret", fake_token not in done_msg and "<redacted>" in done_msg)
    check("status done truncates", "…(truncated)" in done_msg and len(done_msg) < 2200)
    # failed + redaction
    tid_f = bq.enqueue_bridge_task(chat_id=1, alias="p", mode="ask", task_text="q")
    q.claim_next_task(worker_id="w1")
    q.update_task(tid_f, "failed", error="RuntimeError: boom " + fake_token)
    fail_msg = bq.format_task_status(q.get_task(tid_f))
    check("status failed shows error without secret", "failed" in fail_msg and fake_token not in fail_msg and "<redacted>" in fail_msg)

    # --- approval parse ---
    check("parse approve allow", bq.parse_approval_callback("bridge:approve:5:allow") == (5, True))
    check("parse approve deny", bq.parse_approval_callback("bridge:approve:5:deny") == (5, False))
    check("parse invalid prefix", bq.parse_approval_callback("bridge:pick:fix:proj") is None)
    check("parse invalid verdict", bq.parse_approval_callback("bridge:approve:5:maybe") is None)
    check("parse non-int id", bq.parse_approval_callback("bridge:approve:x:allow") is None)

    # --- approval pending view + allow/deny resolve via queue ---
    tid_a = bq.enqueue_bridge_task(chat_id=1, alias="p", mode="fix", task_text="approve target")
    aid_allow = q.request_approval(tid_a, "Bash", {"command": "ls -la"})
    view = bq.pending_approval_view(task_id=tid_a)
    check("pending view returns payload", view is not None and view["approval_id"] == aid_allow)
    check("pending view callbacks", view["allow_cb"] == f"bridge:approve:{aid_allow}:allow" and view["deny_cb"] == f"bridge:approve:{aid_allow}:deny")
    res_allow = bq.resolve_approval_callback(view["allow_cb"])
    check("approval allow -> ok + queue True", res_allow["ok"] and q.get_verdict(aid_allow) is True)

    aid_deny = q.request_approval(tid_a, "Write", {"path": "x.py"})
    res_deny = bq.resolve_approval_callback(bq.build_approval_callbacks(aid_deny)["deny"])
    check("approval deny -> ok + queue False", res_deny["ok"] and q.get_verdict(aid_deny) is False)

    # --- stale / nonexistent approval callback: graceful, no crash ---
    res_stale = bq.resolve_approval_callback("bridge:approve:999999:allow")
    check("stale approval -> graceful not-ok", res_stale["ok"] is False and "no longer current" in res_stale["message"])
    res_bad = bq.resolve_approval_callback("bridge:nonsense")
    check("invalid callback -> graceful not-ok", res_bad["ok"] is False)

    # --- no pending approval -> view None ---
    check("no pending approval -> None", bq.pending_approval_view(task_id=tid_f) is None)

    # --- design_mode detection + trigger strip ---
    tid_d = bq.enqueue_bridge_task(chat_id=1, alias="p", mode="fix", task_text="design  add dark theme")
    td = q.get_task(tid_d)
    check("design trigger -> design_mode stored", td["design_mode"] == 1)
    check("design trigger stripped from task_text", td["task_text"] == "add dark theme")
    tid_d2 = bq.enqueue_bridge_task(chat_id=1, alias="p", mode="fix", task_text="design: home page")
    td2 = q.get_task(tid_d2)
    check("design trigger with colon -> design_mode + strip", td2["design_mode"] == 1 and td2["task_text"] == "home page")
    tid_n = bq.enqueue_bridge_task(chat_id=1, alias="p", mode="fix", task_text="just fix the bug")
    tn = q.get_task(tid_n)
    check("normal text -> design_mode 0 + unchanged", tn["design_mode"] == 0 and tn["task_text"] == "just fix the bug")

    # --- Block 3 patch: resolve_resume_sid (picker session preserved) ---
    check("resume: picker sid preserved (fix)", bq.resolve_resume_sid({"resume_sid": "S1"}, "fix") == "S1")
    check("resume: picker sid preserved (ask)", bq.resolve_resume_sid({"resume_sid": "S2"}, "ask") == "S2")
    check("resume: 'new' -> None", bq.resolve_resume_sid({"resume_sid": "new"}, "fix") is None)
    check("resume: continue fallback to last", bq.resolve_resume_sid({}, "continue", last_session_id="L9") == "L9")
    check("resume: ask no sid -> None", bq.resolve_resume_sid({}, "ask", last_session_id="L9") is None)
    check("resume: continue no sid/no last -> None", bq.resolve_resume_sid({}, "continue") is None)

    shutil.rmtree(tmp, ignore_errors=True)
    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
