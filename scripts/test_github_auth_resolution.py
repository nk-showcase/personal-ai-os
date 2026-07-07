#!/usr/bin/env python3
"""Tests for claude_bridge GitHub-token resolution + _auth_url (auto-push auth).

RUN: on the server venv — importing claude_bridge pulls in telegram + bot.config (needs the
deps and dummy TELEGRAM_* values). Does NOT touch the network or a real secret manager:
AIOS_SECRETS_BACKEND=env, and tokens are supplied via environment variables. No real secret
values are present.
  PYTHONPATH=<repo> python3 scripts/test_github_auth_resolution.py
"""
import os
import sys
from pathlib import Path

# Must be set BEFORE importing bot.config (config requires the bot token + owner id) and
# bot.claude_bridge (which pulls in task_queue — we point the DB at /tmp, no side effects).
os.environ.setdefault("AIOS_SECRETS_BACKEND", "env")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy-not-a-real-token")
os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
os.environ.setdefault("AIOS_TASK_QUEUE_DB", "/tmp/aios_ghtest_q.sqlite3")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot.secrets_loader as sl  # noqa: E402
from bot import claude_bridge_worker as cb  # noqa: E402

_FAILS = []


def check(cond, label):
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        _FAILS.append(label)


def _reset(**env):
    """Clear the in-memory secret cache and set only the given token env vars."""
    sl._mem.clear()
    for k in ("GITHUB_REPO_WRITE_TOKEN", "GITHUB_TOKEN"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v


def main() -> int:
    # --- _github_token: prefers the name from the secrets map ---
    _reset(GITHUB_REPO_WRITE_TOKEN="REPOWRITE", GITHUB_TOKEN="LEGACY")
    check(cb._github_token() == "REPOWRITE",
          "_github_token prefers GITHUB_REPO_WRITE_TOKEN")

    _reset(GITHUB_TOKEN="LEGACY")
    check(cb._github_token() == "LEGACY",
          "_github_token falls back to legacy GITHUB_TOKEN")

    _reset()
    check(cb._github_token() == "",
          "_github_token is empty when no name is set")

    # --- token auth flows via an inline credential helper, NOT via a URL on disk ---
    # With a token: git gets a credential.helper stub (username=x-access-token, password from
    # env AIOS_GIT_TOKEN). The remote URL (e.g. https://github.com/<YOUR_ORG>/example-repo.git)
    # is left clean — the token is never embedded in it.
    _reset(GITHUB_REPO_WRITE_TOKEN="TKN")
    cred_args = cb._git_cred_args()
    check(any("credential.helper=" in a and "x-access-token" in a for a in cred_args),
          "with a token: git gets a credential.helper stub for x-access-token")
    check(cb._git_env().get("AIOS_GIT_TOKEN") == "TKN",
          "the token is passed to git by env name (AIOS_GIT_TOKEN), not in argv/URL")
    check(all("TKN" not in a for a in cred_args),
          "the token VALUE never appears in the git argv (helper stub only)")

    # --- without a token: no credential helper, no token env ---
    _reset()
    check(cb._git_cred_args() == [],
          "without a token: no credential.helper args are added")
    check("AIOS_GIT_TOKEN" not in cb._git_env(),
          "without a token: no AIOS_GIT_TOKEN in the git env")

    print()
    if _FAILS:
        print(f"RESULT: {len(_FAILS)} FAIL")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
