"""Chat cutover test: claude_storage functions behind store_mode('chat')='sqlite'.

Real path claude_storage -> store_request -> applier -> aios_chat_store (encrypted),
store_request short-circuited to the applier (no worker in tests). Key assertions:
transcripts PERSIST locally with the raw gate OFF (destination-aware, amendment 4),
titles stay real locally, latest-chat follows last activity (amendment 9), everything
encrypted at rest. Temp DB + throwaway KEK. Run:
    PYTHONPATH=. .venv/bin/python scripts/test_chat_flagged.py
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
import time

sys.path.insert(0, os.getcwd())
fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


os.environ.pop("AIOS_RAW_NOTION_PERSIST", None)   # raw gate OFF — the hard case
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ.update({"TELEGRAM_BOT_TOKEN": "dummy:test", "TELEGRAM_OWNER_ID": "1"})
TMP = tempfile.mkdtemp(prefix="chatflag-")
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

CFG.set_store_mode("chat", "sqlite")
check("chat flag flipped", CS._chat_sqlite())
check("raw gate is OFF in this test", not __import__("bot.config", fromlist=["x"]).notion_raw_allowed())


async def main():
    c = sqlite3.connect(os.environ["AIOS_DATA_DB"])

    # create: REAL title persists locally (Notion path would neutralize it under raw OFF)
    cid = await CS.create_chat("Session: project kickoff")
    check("create_chat returns local id", bool(cid) and cid.startswith("local-"))
    latest = await CS.get_latest_chat()
    check("latest chat keeps the REAL title (destination-aware gate)",
          latest and latest["id"] == cid and latest["title"] == "Session: project kickoff")

    # transcripts persist locally DESPITE raw gate OFF — the central cutover property
    ok1 = await CS.save_messages(cid, "first question", "first answer")
    ok2 = await CS.save_messages(cid, "second question", "second answer")
    msgs = await CS.load_chat_messages(cid)
    check("transcripts persist locally with raw gate OFF (2 turns, order kept)",
          ok1 and ok2 and [m["content"] for m in msgs] ==
          ["first question", "first answer", "second question", "second answer"]
          and [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"])

    # at-rest: title + every message encrypted, no cleartext columns populated
    trow = c.execute("SELECT title, title_encrypted FROM chat WHERE source_notion_page_id=?",
                     (cid,)).fetchone()
    n_clear = c.execute("SELECT COUNT(*) FROM chat_message WHERE content_encrypted IS NULL").fetchone()[0]
    raw = open(os.environ["AIOS_DATA_DB"], "rb").read()
    check("title + messages encrypted at rest (and plaintext absent from the DB file)",
          trow[0] is None and trow[1] is not None and n_clear == 0
          and "question".encode("utf-8") not in raw)

    # latest follows last ACTIVITY: a second chat created later, then activity in the first
    time.sleep(0.05)
    cid2 = await CS.create_chat("Another chat")
    latest2 = await CS.get_latest_chat()
    check("newest-created chat becomes latest", latest2 and latest2["id"] == cid2)
    await CS.save_messages(cid, "back here again", "ok")
    latest3 = await CS.get_latest_chat()
    check("activity (append) moves the chat back to latest (amendment 9)",
          latest3 and latest3["id"] == cid)

    # title update keeps real text; archive removes from latest
    ok = await CS.update_chat_title(cid, "Session - summary")
    check("update_chat_title keeps real text locally",
          ok and (await CS.get_latest_chat())["title"] == "Session - summary")
    check("archive_chat -> other chat becomes latest",
          await CS.archive_chat(cid) and (await CS.get_latest_chat())["id"] == cid2)
    check("append to unknown chat -> False", not await CS.save_messages("local-nope", "a", "b"))


asyncio.run(main())
print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
