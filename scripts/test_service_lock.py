"""least-privilege lock simulation (item 1): with AIOS_SERVICE_NAME set per service,
assert every secret each service ACTUALLY requests resolves, forbidden ones are denied,
and importing bot.config never crashes under the lock. This is the pre-flight that makes
enabling the lock safe. No real secrets: values come from env stubs; we assert the
POLICY decision (allowed vs SecretAccessDenied), not the value.

    PYTHONPATH=. python3 scripts/test_service_lock.py
"""
import importlib
import os
import sys

sys.path.insert(0, os.getcwd())
fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


# stub values for every managed secret so "allowed" resolves to a value (not a miss)
STUBS = {
    "TELEGRAM_BOT_TOKEN": "dummy:tok", "TELEGRAM_OWNER_ID": "1",
    "NOTION_API_KEY": "n", "TODOIST_API_KEY": "t", "ANTHROPIC_API_KEY": "a",
    "GITHUB_REPO_WRITE_TOKEN": "g", "CLAUDE_CODE_OAUTH_TOKEN": "o",
    "GMAIL_READONLY_TOKEN": "gr", "GMAIL_SEND_TOKEN": "gs",
}
os.environ.update(STUBS)

import bot.secrets_loader as SL  # noqa: E402

# what each service FUNCTIONALLY resolves (must all be allowed) and a sample it must be DENIED
FUNCTIONAL = {
    "telegram-bot": ["TELEGRAM_BOT_TOKEN"],
    "integrations-worker": ["NOTION_API_KEY", "TODOIST_API_KEY", "ANTHROPIC_API_KEY",
                            "TELEGRAM_BOT_TOKEN"],
    "claude-worker": ["GITHUB_REPO_WRITE_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"],
}
MUST_DENY = {
    "telegram-bot": ["NOTION_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_REPO_WRITE_TOKEN"],
    "integrations-worker": ["GITHUB_REPO_WRITE_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"],
    "claude-worker": ["TELEGRAM_BOT_TOKEN", "NOTION_API_KEY", "TODOIST_API_KEY"],
}

for svc, needed in FUNCTIONAL.items():
    os.environ["AIOS_SERVICE_NAME"] = svc
    for name in needed:
        try:
            val = SL.get_secret(name, default="")
            check(f"[{svc}] allowed to resolve {name}", bool(val))
        except SL.SecretAccessDenied:
            check(f"[{svc}] allowed to resolve {name}", False)
    for name in MUST_DENY[svc]:
        try:
            SL.get_secret(name, default="")
            check(f"[{svc}] correctly DENIED {name}", False)
        except SL.SecretAccessDenied:
            check(f"[{svc}] correctly DENIED {name}", True)

# config import must survive the lock for EVERY service (config eagerly resolves the token)
for svc in FUNCTIONAL:
    os.environ["AIOS_SERVICE_NAME"] = svc
    sys.modules.pop("bot.config", None)
    try:
        cfg = importlib.import_module("bot.config")
        importlib.reload(cfg)
        # claude-worker is denied the token -> "" (it doesn't send); others get the stub
        if svc == "claude-worker":
            check(f"[{svc}] bot.config imports; token empty (denied, not needed)",
                  cfg.TELEGRAM_BOT_TOKEN == "")
        else:
            check(f"[{svc}] bot.config imports; token present",
                  cfg.TELEGRAM_BOT_TOKEN == "dummy:tok")
    except Exception as e:
        check(f"[{svc}] bot.config imports under the lock", False)
        print("   crash:", type(e).__name__, str(e)[:80])

# lock OFF (no AIOS_SERVICE_NAME) -> everything resolves (today's behavior, no regression)
os.environ.pop("AIOS_SERVICE_NAME", None)
check("lock OFF: any secret resolves (no service name set)",
      bool(SL.get_secret("NOTION_API_KEY", default="")))

# policy map must match secrets-map.yaml (drift guard already exists; assert ANTHROPIC landed)
check("integrations-worker now allowed ANTHROPIC_API_KEY",
      "ANTHROPIC_API_KEY" in SL.SERVICE_ALLOWED["integrations-worker"])

print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
