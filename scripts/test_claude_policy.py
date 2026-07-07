#!/usr/bin/env python3
"""Plain-python tests for bot.claude_policy (Block 4.1/8 execution policy).

SAFE: pure policy logic; no Telegram / Claude / network / secrets / process launch.
Coverage: a valid operator fix task passes; ask => read-only; unknown alias fails;
unknown mode fails (fail-closed); _assistant allowed; resolve_claude_permission_mode default.
Run:  PYTHONPATH=<repo> python3 scripts/test_claude_policy.py
"""
import os
import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bot import claude_policy as p

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    def raises(fn):
        try:
            fn()
            return False
        except p.TaskPolicyError:
            return True

    known = {"myproj", "personal"}

    # valid operator fix task passes; write mode -> not read-only
    res = p.validate_task_execution_policy({"alias": "myproj", "mode": "fix"}, known_aliases=known)
    check("valid fix task passes", res["alias"] == "myproj" and res["mode"] == "fix" and res["read_only"] is False)

    # ask maps to read-only
    res_ask = p.validate_task_execution_policy({"alias": "myproj", "mode": "ask"}, known_aliases=known)
    check("ask -> read_only", res_ask["read_only"] is True)

    # continue / edit are write modes
    check("continue -> write", p.validate_task_execution_policy({"alias": "myproj", "mode": "continue"}, known_aliases=known)["read_only"] is False)
    check("edit -> write", p.validate_task_execution_policy({"alias": "myproj", "mode": "edit"}, known_aliases=known)["read_only"] is False)

    # _assistant allowed even without known_aliases
    check("_assistant allowed", p.validate_task_execution_policy({"alias": "_assistant", "mode": "fix"})["alias"] == "_assistant")

    # unknown alias fails
    check("unknown alias fails", raises(lambda: p.validate_task_execution_policy({"alias": "evilproj", "mode": "fix"}, known_aliases=known)))

    # non-assistant alias with no allowlist -> fail-closed
    check("no allowlist + non-assistant -> fail", raises(lambda: p.validate_task_execution_policy({"alias": "myproj", "mode": "fix"})))

    # unknown mode fails (fail-closed)
    check("unknown mode fails", raises(lambda: p.validate_task_execution_policy({"alias": "myproj", "mode": "delete"}, known_aliases=known)))
    check("empty mode fails", raises(lambda: p.validate_task_execution_policy({"alias": "myproj", "mode": ""}, known_aliases=known)))

    # resolve permission mode: default bypassPermissions (no-confirm policy)
    os.environ.pop("AIOS_CLAUDE_PERMISSION_MODE", None)
    check("default permission mode = bypassPermissions", p.resolve_claude_permission_mode() == "bypassPermissions")
    os.environ["AIOS_CLAUDE_PERMISSION_MODE"] = "default"
    check("env override respected (optional gating)", p.resolve_claude_permission_mode() == "default")
    os.environ.pop("AIOS_CLAUDE_PERMISSION_MODE", None)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
