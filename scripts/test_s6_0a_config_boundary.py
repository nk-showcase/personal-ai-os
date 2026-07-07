#!/usr/bin/env python3
"""S6 Stage 0a invariant: importing the transport surface must NOT resolve the
forbidden integration/LLM/GitHub secrets into secrets_loader._mem.

Before 0a, config.py module body resolved all of them at import (any module that
imported config — i.e. the whole handler tree — pulled them into _mem). After 0a,
only the transport's own TELEGRAM_BOT_TOKEN (+ the non-secret TELEGRAM_OWNER_ID)
may be resolved at import; the rest are fetched lazily at call time by the
worker-side module that uses them.

Runs on the venv (imports telegram + the full handler tree).
  python3 scripts/test_s6_0a_config_boundary.py
"""
import os
import sys
from pathlib import Path

os.environ["AIOS_SECRETS_BACKEND"] = "env"
os.environ["TELEGRAM_BOT_TOKEN"] = "dummy-not-real"
os.environ["TELEGRAM_OWNER_ID"] = "1"
os.environ.setdefault("AIOS_TASK_QUEUE_DB", "/tmp/aios_s6_0a_q.sqlite3")
# Forbidden keys absent from env: any appearance in _mem would prove an import-time leak.
FORBIDDEN = {
    "TODOIST_API_KEY", "ANTHROPIC_API_KEY",
    "NOTION_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO_WRITE_TOKEN",
}
for _k in FORBIDDEN:
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot.secrets_loader as sl  # noqa: E402

# The transport's real import surface (what bot.main.main() pulls in):
import bot.handlers  # noqa: E402,F401  — statically imports the whole feature tree
import bot.claude_bridge  # noqa: E402,F401  — bridge interceptors
import bot.main  # noqa: E402,F401

ALLOWED = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"}


def main() -> int:
    resolved = set(sl._mem.keys())
    leaked = resolved & FORBIDDEN
    unexpected = resolved - ALLOWED
    print("resolved-at-import:", sorted(resolved))

    ok = True
    if leaked:
        print("FAIL: forbidden secrets resolved at transport import:", sorted(leaked))
        ok = False
    else:
        print("PASS: no forbidden secret resolved at import")
    if unexpected:
        print("FAIL: unexpected secret resolved at import:", sorted(unexpected))
        ok = False
    else:
        print("PASS: only allowed transport secrets resolved")

    print()
    print("RESULT: ALL PASS" if ok else "RESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
