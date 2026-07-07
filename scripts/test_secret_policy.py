"""Test for the secret-access policy (per-service least privilege).
Safe: dummy values only, does not read real secrets, does not print values.
Run (from the repo root):  PYTHONPATH=. .venv/bin/python scripts/test_secret_policy.py

Checks:
  1. SERVICE_ALLOWED / FORBIDDEN_EVERYWHERE in code match config/secrets-map.yaml
     (drift between code and the access map is not allowed).
  2. get_secret() is fail-closed when AIOS_SERVICE_NAME is set: a foreign secret -> denied,
     even if the value is present in env. An allowed secret is returned. forbidden -> always denied.
  3. When AIOS_SERVICE_NAME is NOT set -> enforcement off (prior behavior).
  4. The denial message contains no secret value (name + service only).
"""
import os
import sys

sys.path.insert(0, os.getcwd())

for _k in ("AIOS_SERVICE_NAME", "AIOS_SECRETS_BACKEND", "BWS_ACCESS_TOKEN"):
    os.environ.pop(_k, None)

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


from bot import secrets_loader as sl  # noqa: E402
from bot.secrets_loader import get_secret, SecretAccessDenied  # noqa: E402

MAP_PATH = os.path.join(os.getcwd(), "config", "secrets-map.yaml")


def _parse_map(path):
    """Minimal line-by-line parser for THIS specific file (no pyyaml dependency).
    Extracts services.<svc>.allowed_secrets and forbidden_everywhere."""
    services, forbidden = {}, set()
    section = cur = None
    in_allowed = False
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0 and stripped.endswith(":"):
            section, cur, in_allowed = stripped[:-1], None, False
            continue
        if section == "services":
            if indent == 2 and stripped.endswith(":"):
                cur = stripped[:-1]
                services.setdefault(cur, set())
                in_allowed = False
            elif indent == 4 and stripped.rstrip().endswith("allowed_secrets:"):
                in_allowed = True
            elif indent == 4 and stripped.endswith(":"):
                in_allowed = False
            elif indent >= 6 and stripped.startswith("- ") and in_allowed and cur:
                item = stripped[2:].split("#", 1)[0].strip()
                if item:
                    services[cur].add(item)
        elif section == "forbidden_everywhere":
            if indent == 2 and stripped.startswith("- "):
                item = stripped[2:].split("#", 1)[0].strip()
                if item:
                    forbidden.add(item)
    return services, forbidden


# 1. drift between code and the access map
map_services, map_forbidden = _parse_map(MAP_PATH)
check("SERVICE_ALLOWED matches secrets-map.yaml services.*.allowed_secrets",
      map_services == sl.SERVICE_ALLOWED)
check("FORBIDDEN_EVERYWHERE matches secrets-map.yaml forbidden_everywhere",
      map_forbidden == sl.FORBIDDEN_EVERYWHERE)

# 2. AIOS_SERVICE_NAME=telegram-bot -> strictly its own token, foreign/forbidden -> denied
sl._mem.clear()
os.environ["AIOS_SERVICE_NAME"] = "telegram-bot"
os.environ["TELEGRAM_BOT_TOKEN"] = "000:dummy"
os.environ["NOTION_API_KEY"] = "dummy_notion"
os.environ["SOME_DUMMY_SECRET"] = "dummyval"

check("telegram-bot gets its own TELEGRAM_BOT_TOKEN",
      get_secret("TELEGRAM_BOT_TOKEN") == "000:dummy")

denied_msg = ""
try:
    get_secret("NOTION_API_KEY")
    check("telegram-bot does NOT get the foreign NOTION_API_KEY (denied)", False)
except SecretAccessDenied as e:
    denied_msg = str(e)
    check("telegram-bot does NOT get the foreign NOTION_API_KEY (denied)", True)

check("the denial message contains NO value (name/service only)",
      "dummy_notion" not in denied_msg and "NOTION_API_KEY" in denied_msg)

# Two-user split: the bot user must be DENIED the AI/encryption key in-process (defense-in-depth
# beyond the OS file perms). telegram-bot is allowed only TELEGRAM_BOT_TOKEN.
os.environ["ANTHROPIC_API_KEY"] = "dummy_anth"
try:
    get_secret("ANTHROPIC_API_KEY")
    check("telegram-bot does NOT get ANTHROPIC_API_KEY (denied)", False)
except SecretAccessDenied:
    check("telegram-bot does NOT get ANTHROPIC_API_KEY (denied)", True)

try:
    get_secret("PREVIOUS_HOSTING_MASTER_TOKEN")
    check("forbidden secret PREVIOUS_HOSTING_MASTER_TOKEN always denied", False)
except SecretAccessDenied:
    check("forbidden secret PREVIOUS_HOSTING_MASTER_TOKEN always denied", True)

check("an unmanaged name (not a secret in the map) resolves as before",
      get_secret("SOME_DUMMY_SECRET") == "dummyval")

# 3. claude-worker -> ANTHROPIC allowed, TODOIST not allowed
sl._mem.clear()
os.environ["AIOS_SERVICE_NAME"] = "claude-worker"
os.environ["ANTHROPIC_API_KEY"] = "dummy_anth"
os.environ["TODOIST_API_KEY"] = "dummy_todo"
check("claude-worker gets ANTHROPIC_API_KEY", get_secret("ANTHROPIC_API_KEY") == "dummy_anth")
try:
    get_secret("TODOIST_API_KEY")
    check("claude-worker does NOT get TODOIST_API_KEY (denied)", False)
except SecretAccessDenied:
    check("claude-worker does NOT get TODOIST_API_KEY (denied)", True)

# 4. AIOS_SERVICE_NAME not set -> enforcement off (as before)
sl._mem.clear()
os.environ.pop("AIOS_SERVICE_NAME", None)
check("without AIOS_SERVICE_NAME a foreign secret resolves (enforcement OFF)",
      get_secret("NOTION_API_KEY") == "dummy_notion")

# 5. unknown service -> any managed secret denied (fail-closed)
sl._mem.clear()
os.environ["AIOS_SERVICE_NAME"] = "typo-service"
try:
    get_secret("TELEGRAM_BOT_TOKEN")
    check("unknown service -> denied for a managed secret", False)
except SecretAccessDenied:
    check("unknown service -> denied for a managed secret", True)
os.environ.pop("AIOS_SERVICE_NAME", None)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print(f"RESULT: {fails} FAIL")
    sys.exit(1)
