"""Memory cutover test: load_memory/append_memory behind store_mode('memory')='sqlite'.

Key assertions: facts persist locally with the raw gate OFF (destination-aware),
encrypted at rest (plaintext absent from the DB file), live-parity join order,
tombstone ops. Temp DB + throwaway KEK. Run:
    PYTHONPATH=. .venv/bin/python scripts/test_memory_flagged.py
"""
import asyncio
import hashlib
import os
import shutil
import secrets
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.getcwd())
fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


os.environ.pop("AIOS_RAW_NOTION_PERSIST", None)
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ.update({"TELEGRAM_BOT_TOKEN": "dummy:test", "TELEGRAM_OWNER_ID": "1"})
TMP = tempfile.mkdtemp(prefix="memflag-")
os.environ["AIOS_DATA_DB"] = os.path.join(TMP, "aios.sqlite3")

AGE = os.environ.get("AIOS_AGE_BIN") or shutil.which("age") or os.path.expanduser("~/.local/bin/age")
KG = os.path.join(os.path.dirname(AGE), "age-keygen")
IDF, KEKF = os.path.join(TMP, "id.txt"), os.path.join(TMP, "kek.age")
subprocess.run([KG, "-o", IDF], check=True, capture_output=True)
pub = subprocess.run([KG, "-y", IDF], check=True, capture_output=True, text=True).stdout.strip()
KEK = secrets.token_bytes(32)
subprocess.run([AGE, "-r", pub, "-o", KEKF], input=KEK, check=True)
os.environ.update({"AIOS_AGE_BIN": AGE, "AIOS_CONTEXT_IDENTITY_FILE": IDF,
                   "AIOS_CONTEXT_KEK_FILE": KEKF, "AIOS_CONTEXT_ENCRYPTION": "1"})

from bot import context_key  # noqa: E402
context_key.KEK_SHA256_PIN = hashlib.sha256(KEK).hexdigest()

from bot import aios_config as CFG            # noqa: E402
from bot import integrations_proxy as IP      # noqa: E402
from bot import claude_storage as CS          # noqa: E402
from bot.aios_store_applier import StoreUnavailable  # noqa: E402


class _Rec:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


async def _direct_store_request(domain, op, args=None, *, is_write, business_key=None):
    kind = IP.KIND_STORE_WRITE if is_write else IP.KIND_STORE_READ
    payload = {"op_b64": IP._b64({"domain": domain, "op": op, "args": args or {}})}
    try:
        return IP.decode_result(IP.integration_applier(_Rec(kind, payload)))
    except StoreUnavailable:
        return {"ok": False, "unavailable": True}
    except ValueError as e:
        return {"ok": False, "error": str(e)}


IP.store_request = _direct_store_request
CS.store_request = _direct_store_request

CFG.set_store_mode("memory", "sqlite")
check("memory flag flipped", CS._memory_sqlite())
check("raw gate OFF in this test", not __import__("bot.config", fromlist=["x"]).notion_raw_allowed())


async def main():
    check("empty memory loads as empty string", await CS.load_memory() == "")
    ok1 = await CS.append_memory("Prefers the aisle seat on flights")
    ok2 = await CS.append_memory("Works best in the early morning")
    check("facts persist locally with raw gate OFF (destination-aware)",
          ok1 and ok2 and await CS.load_memory() == "Prefers the aisle seat on flights\nWorks best in the early morning")

    c = sqlite3.connect(os.environ["AIOS_DATA_DB"])
    n_clear = c.execute("SELECT COUNT(*) FROM memory_fact WHERE content_encrypted IS NULL").fetchone()[0]
    raw = open(os.environ["AIOS_DATA_DB"], "rb").read()
    check("facts encrypted at rest (plaintext absent from DB file)",
          n_clear == 0 and "aisle".encode("utf-8") not in raw)

    # tombstone routing compat (archive_record/restore_record via the store applier)
    from bot import aios_storage as ST
    with ST.connect(os.environ["AIOS_DATA_DB"]) as sc:
        bid = sc.execute(
            "SELECT source_block_id FROM memory_fact ORDER BY position LIMIT 1"
        ).fetchone()[0]
    res = await _direct_store_request("memory", "archive_record", {"sid": bid}, is_write=True)
    check("memory fact archive via store routing",
          res.get("ok") and await CS.load_memory() == "Works best in the early morning")
    restored = await _direct_store_request("memory", "restore_record", {"sid": bid}, is_write=True)
    check("memory fact restore", restored.get("ok")
          and (await CS.load_memory()).startswith("Prefers the aisle"))


asyncio.run(main())
print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
