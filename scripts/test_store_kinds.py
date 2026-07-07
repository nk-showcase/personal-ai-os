"""store_read/store_write plumbing test.

Asserts the kinds are registered consistently in ALL places (queue ALLOWED/WRITE/READ, proxy
INTEGRATION_KINDS + dispatch), that the store applier REFUSES fail-closed without encryption,
and that with a throwaway KEK a notes save/get round-trips encrypted end-to-end through the
applier. Safe: temp DB + throwaway key only.

    PYTHONPATH=. .venv/bin/python scripts/test_store_kinds.py
"""
import os
import shutil
import secrets
import subprocess
import sys
import tempfile
import hashlib

sys.path.insert(0, os.getcwd())
fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ.update({"TELEGRAM_BOT_TOKEN": "dummy:test", "TELEGRAM_OWNER_ID": "1"})
TMP = tempfile.mkdtemp(prefix="storekinds-")
os.environ["AIOS_DATA_DB"] = os.path.join(TMP, "aios.sqlite3")

from bot import aios_request_queue as RQ          # noqa: E402
from bot import integrations_proxy as IP                   # noqa: E402
from bot.aios_store_applier import store_op_applier, StoreUnavailable  # noqa: E402

# 1. Registration in every place (a kind missing anywhere strands rows silently).
check("store_write in ALLOWED_KINDS", "store_write" in RQ.ALLOWED_KINDS)
check("store_read in ALLOWED_KINDS", "store_read" in RQ.ALLOWED_KINDS)
check("store_write in WRITE_KINDS (dead-letter on TTL, never silent drop)", "store_write" in RQ.WRITE_KINDS)
check("store_read in READ_KINDS (may expire-and-drop)", "store_read" in RQ.READ_KINDS)
check("both kinds in proxy INTEGRATION_KINDS (worker filter uses this tuple)",
      IP.KIND_STORE_READ in IP.INTEGRATION_KINDS and IP.KIND_STORE_WRITE in IP.INTEGRATION_KINDS)


class _Rec:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


def _pk(domain, op, args):
    return {"op_b64": IP._b64({"domain": domain, "op": op, "args": args})}


# 2. Fail-closed: encryption OFF in this process -> the op is REFUSED (never cleartext).
try:
    store_op_applier(_Rec("store_write", _pk("notes", "save_note",
                                             {"note_id": "local-x", "content": "hello"})))
    check("store op REFUSED with encryption OFF", False)
except StoreUnavailable:
    check("store op REFUSED with encryption OFF", True)

# 3. Dispatch from integration_applier reaches the store applier (same refusal proves routing).
try:
    IP.integration_applier(_Rec("store_read", _pk("notes", "get_summary", {})))
    check("integration_applier dispatches store kinds", False)
except StoreUnavailable:
    check("integration_applier dispatches store kinds", True)

# 4. With a throwaway KEK: save/get round-trips encrypted through the applier.
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

# save_note (write) -> get_summary (read) round-trips through the store applier.
NID = "local-note-1"
BODY = "buy milk and eggs on the way home"
r1 = IP.decode_result(IP.integration_applier(_Rec("store_write",
     _pk("notes", "save_note", {"note_id": NID, "content": BODY}))))
check("store_write save_note ok (bot-minted synthetic id)", r1.get("ok") is True and r1.get("id") == NID)

r2 = IP.decode_result(IP.integration_applier(_Rec("store_read", _pk("notes", "get_summary", {}))))
summary = r2.get("summary") or {}
check("store_read returns a value-limited summary (count 1)", summary.get("count") == 1)
check("summary previews reflect the saved note", any(BODY[:10] in p for p in summary.get("previews", [])))

# archive round-trip: an archived note drops out of the summary count.
ra = IP.decode_result(IP.integration_applier(_Rec("store_write",
     _pk("notes", "archive_record", {"note_id": NID}))))
check("archive_record ok", ra.get("ok") is True)
r3 = IP.decode_result(IP.integration_applier(_Rec("store_read", _pk("notes", "get_summary", {}))))
check("archived note removed from summary (count 0)", (r3.get("summary") or {}).get("count") == 0)
# restore brings it back
IP.integration_applier(_Rec("store_write", _pk("notes", "restore_record", {"note_id": NID})))
r4 = IP.decode_result(IP.integration_applier(_Rec("store_read", _pk("notes", "get_summary", {}))))
check("restore_record brings the note back (count 1)", (r4.get("summary") or {}).get("count") == 1)

# at-rest proof: the note body is stored ENCRYPTED (cleartext column NULL).
import sqlite3  # noqa: E402
c = sqlite3.connect(os.environ["AIOS_DATA_DB"])
row = c.execute("SELECT content, content_encrypted FROM note WHERE note_id=?", (NID,)).fetchone()
check("note body stored ENCRYPTED at rest (cleartext NULL)", row[0] is None and row[1] is not None)
# The WRITE result is structure-only (id, no body). The READ summary intentionally carries
# short truncated previews (that is the demo view's contract), so the body is only checked
# against the write result here.
check("no note body appears in the write op result", BODY not in str(r1))

# retry idempotency: the same save payload re-applied -> same row, no duplicate.
IP.integration_applier(_Rec("store_write",
    {"domain": "notes", "op": "save_note", "args": {"note_id": NID, "content": BODY}}))
n = c.execute("SELECT COUNT(*) FROM note WHERE note_id=?", (NID,)).fetchone()[0]
check("retry of the same payload is idempotent (1 row)", n == 1)

# unknown op fails loudly (a typo'd op must not be silently dropped).
try:
    store_op_applier(_Rec("store_read", _pk("notes", "nope", {})))
    check("unknown store op fails loudly", False)
except ValueError:
    check("unknown store op fails loudly", True)

print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
