"""Synthetic e2e test of the CHAT block domain. Throwaway KEK + temp DB.
    PYTHONPATH=. .venv/bin/python scripts/test_chat_migrate.py
"""
import os
import shutil
import secrets
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


AGE = os.environ.get("AIOS_AGE_BIN") or shutil.which("age") or os.path.expanduser("~/.local/bin/age")
KG = os.path.join(os.path.dirname(AGE), "age-keygen")
TMP = tempfile.mkdtemp(prefix="chatmig-")
IDF, KEKF, DBF = (os.path.join(TMP, x) for x in ("id.txt", "kek.age", "aios.sqlite3"))
subprocess.run([KG, "-o", IDF], check=True, capture_output=True)
pub = subprocess.run([KG, "-y", IDF], check=True, capture_output=True, text=True).stdout.strip()
KEK = secrets.token_bytes(32)
subprocess.run([AGE, "-r", pub, "-o", KEKF], input=KEK, check=True)
os.environ.update({
    "AIOS_AGE_BIN": AGE, "AIOS_CONTEXT_IDENTITY_FILE": IDF, "AIOS_CONTEXT_KEK_FILE": KEKF,
    "AIOS_CONTEXT_ENCRYPTION": "1", "AIOS_DATA_DB": DBF,
    "TELEGRAM_BOT_TOKEN": "dummy:test", "TELEGRAM_OWNER_ID": "1",
})

from bot import context_key  # noqa: E402
import hashlib  # noqa: E402
context_key.KEK_SHA256_PIN = hashlib.sha256(KEK).hexdigest()  # pin the throwaway KEK (prod pin is git-tracked)
check("throwaway KEK unwraps", context_key.get_kek() == KEK)
from bot import aios_storage as S        # noqa: E402
from bot import aios_chat_store as C     # noqa: E402
from bot import aios_chat_import as CI    # noqa: E402
from bot import aios_migrate as M        # noqa: E402
S.init_db()


def mk(sid, title, status, ct, msgs):
    return {"source_notion_id": sid, "title": title, "status": status, "created_time": ct,
            "messages": msgs, "message_count": len(msgs), "messages_key": C._messages_key(msgs)}


M1 = [{"source_block_id": "b1", "role": "user", "position": 0, "content": "hello ☕"},
      {"source_block_id": "b2", "role": "assistant", "position": 1, "content": "hi there\nhow are you"}]
R = [
    mk("c1", "Planning conversation", "active", "2026-06-01T10:00:00.000Z", M1),
    mk("c2", None, "archived", "2026-06-02T10:00:00.000Z", []),
]


def save_one(rec, *, db=None):
    C.save_chat(rec, db=db)


def verify_one(sid, *, db=None):
    row = C.get_chat(sid, db=db)
    return row if row is not None else {}


res = M.migrate_records("chat", R, save_one, verify_one, verify_fields=CI.VERIFY_FIELDS)
check("all chats read+saved+verified (incl messages digest)", res.all_verified and res.verified == 2)

with S.connect() as c:
    ch = c.execute("SELECT id, title, title_encrypted FROM chat WHERE source_notion_page_id='c1'").fetchone()
    check("c1: title stored ENCRYPTED (cleartext NULL)", ch["title"] is None and ch["title_encrypted"] is not None)
    mrow = c.execute("SELECT content_encrypted FROM chat_message WHERE chat_id=? AND source_block_id='b1'",
                     (ch["id"],)).fetchone()
    check("message content stored ENCRYPTED + no plaintext leak",
          mrow["content_encrypted"] is not None and "hello".encode("utf-8") not in bytes(mrow["content_encrypted"]))

g = C.get_chat("c1")
check("decrypt round-trips title + messages in order",
      g["title"] == "Planning conversation" and g["message_count"] == 2
      and g["messages_key"] == C._messages_key(M1))

res2 = M.migrate_records("chat", R, save_one, verify_one, verify_fields=CI.VERIFY_FIELDS)
with S.connect() as c:
    nchat = c.execute("SELECT COUNT(*) FROM chat").fetchone()[0]
    nmsg = c.execute("SELECT COUNT(*) FROM chat_message cm JOIN chat ch ON cm.chat_id=ch.id "
                     "WHERE ch.source_notion_page_id='c1'").fetchone()[0]
check("idempotent re-run: 2 chats, c1 still exactly 2 messages (no dupes)",
      res2.all_verified and nchat == 2 and nmsg == 2)

# lose a message -> page-atomic verify (messages_key) must catch it
with S.connect() as c:
    cid = c.execute("SELECT id FROM chat WHERE source_notion_page_id='c1'").fetchone()["id"]
    c.execute("DELETE FROM chat_message WHERE chat_id=? AND source_block_id='b2'", (cid,))
check("lost message caught by page-atomic verify (messages_key mismatch)",
      not M._fields_match(R[0], verify_one("c1"), CI.VERIFY_FIELDS))

# tamper a message ciphertext -> fail closed on verify
with S.connect() as c:
    cid = c.execute("SELECT id FROM chat WHERE source_notion_page_id='c1'").fetchone()["id"]
    blob = bytearray(c.execute("SELECT content_encrypted FROM chat_message WHERE chat_id=? AND source_block_id='b1'",
                               (cid,)).fetchone()[0])
    blob[-1] ^= 0xFF
    c.execute("UPDATE chat_message SET content_encrypted=? WHERE chat_id=? AND source_block_id='b1'",
              (bytes(blob), cid))
tampered = False
try:
    verify_one("c1")
except Exception:
    tampered = True
check("tampered message ciphertext fails closed (quarantine)", tampered)

print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
