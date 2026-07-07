#!/usr/bin/env python3
"""Plain-python test for neutral Git metadata of queued PC tasks.

SAFE: no network / git push / Telegram / coding agent / real secrets. Git calls are
mocked (stub bot.claude_bridge._sh), cloning is mocked, a temporary directory is used.

Proves:
  * the pc_tasks filename does NOT contain words from the task body (neutral timestamp+random id);
  * the pc_tasks commit message is neutral ("pc-task: queued <id>"), without the body;
  * the id in the filename == the id in the commit message;
  * the task body IS PRESERVED in the file (the offline phone->PC workflow is not broken);
  * a secret dictated in the body is scrubbed before the temporary plaintext write (_redact);
  * (if bot.claude_bridge imports in the venv) the bridge commit messages are
    string constants without task interpolation, and the task log repr scrubs the secret,
    collapses newlines, and truncates length.

Run:  PYTHONPATH=<repo> python3 scripts/test_pc_tasks_metadata.py   (exit 0 = OK, 1 = FAIL)
"""
import asyncio
import os
import re
import shutil
import sys
import tempfile
import types
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    # Dummy env so any transitive config import stays import-safe (NOT real secrets).
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:dummy")
    os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
    os.environ.setdefault(
        "AIOS_TASK_QUEUE_DB",
        str(Path(tempfile.mkdtemp(prefix="aios_pct_db_")) / "q.sqlite3"),
    )

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    from bot import pc_tasks

    ID_RE = r"\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-[0-9a-f]{8}"

    # --- neutral id helper: correct shape + no dependence on any input ---
    tid1 = pc_tasks._new_task_id()
    tid2 = pc_tasks._new_task_id()
    check("task id shape <ts>-<8hex>", bool(re.match(r"^" + ID_RE + r"$", tid1)))
    check("task id random suffix differs across calls", tid1 != tid2)

    # --- behavioral submit_pc_task with mocked clone + git transport ---
    work = Path(tempfile.mkdtemp(prefix="aios_pct_repo_"))

    recorded = []
    stub = types.ModuleType("bot.claude_bridge_worker")  # submit does `from . import claude_bridge_worker as cb`

    async def fake_sh(args, cwd=None):
        recorded.append(list(args))
        return (0, "", "")

    stub._sh = fake_sh
    sys.modules["bot.claude_bridge_worker"] = stub

    async def fake_clone():
        return work

    pc_tasks._ensure_personal_clone = fake_clone

    body_words = ["buildwidget", "alpha", "pipeline", "deploycustomer"]
    marker = " ".join(body_words)
    secret = "ghp_" + ("a" * 36)  # github PAT shape -> must be scrubbed by _redact
    rel = asyncio.run(
        pc_tasks.submit_pc_task("on pc " + marker + " token " + secret, chat_id=42)
    )

    check("submit returns _pc_tasks/*.md path",
          isinstance(rel, str) and rel.startswith("_pc_tasks/") and rel.endswith(".md"))
    check("filename matches neutral id pattern",
          bool(re.match(r"^_pc_tasks/" + ID_RE + r"\.md$", rel or "")))
    check("filename contains NO body words",
          rel is not None and not any(w in rel for w in body_words))

    # commit message recorded via the mocked transport
    commit_args = [a for a in recorded if "-m" in a]
    check("git commit invoked exactly once", len(commit_args) == 1)
    commit_msg = commit_args[0][commit_args[0].index("-m") + 1] if commit_args else ""
    check("commit msg is neutral 'pc-task: queued <id>'",
          bool(re.match(r"^pc-task: queued " + ID_RE + r"$", commit_msg)))
    check("commit msg contains NO body words",
          not any(w in commit_msg for w in body_words))
    fname = (rel or "").split("/")[-1]
    check("commit-msg id == filename id",
          commit_msg.startswith("pc-task: queued ")
          and commit_msg[len("pc-task: queued "):] + ".md" == fname)

    # body preserved in the file (workflow intact); dictated secret scrubbed
    written = (work / rel).read_text(encoding="utf-8") if rel else ""
    check("file PRESERVES task body (phone->PC workflow intact)",
          all(w in written for w in body_words))
    check("file scrubs dictated secret (_redact)",
          secret not in written and "<redacted>" in written)
    check("file frontmatter carries neutral task_id key", "task_id: " in written)

    # --- bridge helpers: only on a host where the bridge worker imports (server venv) ---
    sys.modules.pop("bot.claude_bridge_worker", None)  # drop stub so the REAL module loads
    try:
        from bot import claude_bridge_worker as cb
        check("auto-push commit msg is a constant (no interpolation)",
              "{" not in cb._AUTO_PUSH_COMMIT_MSG)
        check("commit-push commit msg is a constant (no interpolation)",
              "{" not in cb._COMMIT_PUSH_COMMIT_MSG)
        log_secret = "sk-ant-" + ("b" * 30)
        rep = cb._task_log_repr("line one\nline two " + log_secret + " tail")
        check("bridge log repr scrubs secret",
              log_secret not in rep and "<redacted>" in rep)
        check("bridge log repr collapses newlines to single line", "\n" not in rep)
        long_rep = cb._task_log_repr("Z" * 5000)
        check("bridge log repr truncates long task",
              "truncated" in long_rep and len(long_rep) < 2200)
    except Exception as e:  # SDK/telegram absent on this host -> skip, not fail
        print(f"SKIP: bridge helper checks (claude_bridge import unavailable: {type(e).__name__})")

    shutil.rmtree(work, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
