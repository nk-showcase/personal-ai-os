"""Safe test for the secrets layer. No pytest, dummy values only.
Does NOT read real secrets, does not start the bot, prints no values.
Run (from the repo root):  PYTHONPATH=. .venv/bin/python scripts/test_secrets_loader.py
"""
import importlib
import os
import sys

sys.path.insert(0, os.getcwd())

# Clear the control variables so the test is deterministic.
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "AIOS_SECRETS_BACKEND", "BWS_ACCESS_TOKEN"):
    os.environ.pop(_k, None)

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


from bot import secrets_loader as sl  # noqa: E402
from bot.secrets_loader import get_secret, MissingSecretError  # noqa: E402

# 1. env as the source
os.environ["SOME_DUMMY_SECRET"] = "dummyval"
check("env source returns the value", get_secret("SOME_DUMMY_SECRET") == "dummyval")

# 2. required missing -> MissingSecretError with the NAME (strict format), no value
try:
    get_secret("TELEGRAM_BOT_TOKEN", required=True)
    check("required-missing raises", False)
except MissingSecretError as e:
    msg = str(e)
    check("message == 'Missing required secret: TELEGRAM_BOT_TOKEN'",
          msg == "Missing required secret: TELEGRAM_BOT_TOKEN")
    check("message contains nothing value-like (name only)", "dummy" not in msg.lower())

# 3. non-required missing -> default, no crash
check("non-required -> default", get_secret("NOPE_SECRET", default="d") == "d")

# 4. bitwarden backend without bws/token does NOT crash -> default
os.environ["AIOS_SECRETS_BACKEND"] = "bitwarden"
check("bitwarden backend without bws/token does not crash", get_secret("NOPE2", default="d2") == "d2")
os.environ["AIOS_SECRETS_BACKEND"] = "env"

# 5. cache directory outside the repository (won't be committed)
check("cache directory outside the repository", sl.cache_dir_is_outside_repo())

# 6. critical secrets are not written to disk
check("PREVIOUS_HOSTING_MASTER_TOKEN in NO_DISK_CACHE", "PREVIOUS_HOSTING_MASTER_TOKEN" in sl.NO_DISK_CACHE)
check("CLAUDE_OAUTH in NO_DISK_CACHE", "CLAUDE_OAUTH" in sl.NO_DISK_CACHE)

# 7. config imports with dummy env and reads values
os.environ["TELEGRAM_BOT_TOKEN"] = "000:dummy"
os.environ["TELEGRAM_OWNER_ID"] = "1"
import bot.config as cfg  # noqa: E402
importlib.reload(cfg)
check("config imports with dummy env",
      cfg.TELEGRAM_BOT_TOKEN == "000:dummy" and cfg.TELEGRAM_OWNER_ID == 1)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print(f"RESULT: {fails} FAIL")
    sys.exit(1)
