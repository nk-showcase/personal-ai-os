"""Synthetic end-to-end test of bot/aios_migrate.py with the REAL encryption chokepoint.

Safe: builds a THROWAWAY KEK (its own age identity + sealed kek.age) and a temp DB — never
touches the real recovery key, real kek.age, or the production data DB. Exercises the actual
context_cipher -> context_store (ChaCha20-Poly1305) path via the notes store.

Note shape (canonical): a note is a single free-text body keyed on a bot-minted note_id, stored
encrypted-at-rest in content_encrypted (cleartext `content` NULL when encrypted). The migration
engine keys each source record on `source_notion_id` and verifies the `content` field on a fresh
decrypt.

Run on a host with `age` + `cryptography` (the VPS venv):
    PYTHONPATH=. .venv/bin/python scripts/test_migrate_engine.py
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
TMP = tempfile.mkdtemp(prefix="migtest-")
IDF = os.path.join(TMP, "id.txt")
KEKF = os.path.join(TMP, "kek.age")
DBF = os.path.join(TMP, "aios.sqlite3")

# --- OFF guard FIRST (before enabling encryption): the engine must refuse to run cleartext ---
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ["AIOS_DATA_DB"] = DBF
os.environ["AIOS_AGE_BIN"] = AGE
os.environ["AIOS_CONTEXT_IDENTITY_FILE"] = IDF
os.environ["AIOS_CONTEXT_KEK_FILE"] = KEKF

from bot import aios_migrate as M  # noqa: E402

_noop_save = lambda rec, *, db=None: None
_noop_verify = lambda sid, *, db=None: {}
try:
    M.migrate_records("notes", [{"source_notion_id": "x"}], _noop_save, _noop_verify,
                      verify_fields=["content"])
    check("engine REFUSES to run with encryption OFF", False)
except RuntimeError:
    check("engine REFUSES to run with encryption OFF", True)

# --- provision a throwaway KEK: own identity, own sealed kek.age ---
subprocess.run([KG, "-o", IDF], check=True, capture_output=True)
pub = subprocess.run([KG, "-y", IDF], check=True, capture_output=True, text=True).stdout.strip()
KEK = secrets.token_bytes(32)
subprocess.run([AGE, "-r", pub, "-o", KEKF], input=KEK, check=True)
os.environ["AIOS_CONTEXT_ENCRYPTION"] = "1"

from bot import context_key  # noqa: E402
import hashlib  # noqa: E402
context_key.KEK_SHA256_PIN = hashlib.sha256(KEK).hexdigest()  # pin the throwaway KEK (prod pin is git-tracked)
check("throwaway KEK unwraps via the engine's key custody", context_key.get_kek() == KEK)

from bot import aios_storage as S            # noqa: E402
from bot import aios_notes_store as N        # noqa: E402
S.init_db()

# Records keyed on source_notion_id (what the engine reads); the note body is `content`.
RECORDS = [
    {"source_notion_id": "note-1", "content": "A body with unicode ☕ and a newline\nline 2"},
    {"source_notion_id": "note-2", "content": "plain english body"},
    {"source_notion_id": "note-3", "content": None},
]


def save_one(rec, *, db=None):
    # A None body means "no note" -> skip the write (leaves both columns NULL / row absent),
    # mirroring how the engine treats a source record with no free text.
    if rec.get("content") is None:
        return
    N.save_note(rec["source_notion_id"], rec["content"], db=db)


VF = ["content"]


def verify_one(sid, *, db=None):
    row = N.get_note(sid, db=db)
    return row if row is not None else {}


res = M.migrate_records("notes", RECORDS, save_one, verify_one, verify_fields=VF)
# note-3 has no body: it is saved as a no-op and verifies as an (absent) empty match on `content`
# (both None) — so all three rows verify.
check("all rows read+saved+verified (content field)", res.all_verified and res.verified == 3)
check("no mismatch / no errors", res.mismatch == 0 and res.errors == 0)

# at-rest proof: the bodies with content are stored ENCRYPTED (content_encrypted set, cleartext NULL)
with S.connect() as c:
    r1 = c.execute("SELECT content, content_encrypted FROM note WHERE note_id='note-1'").fetchone()
    check("note-1 body stored ENCRYPTED at rest (cleartext column NULL)",
          r1["content"] is None and r1["content_encrypted"] is not None)
    # the ciphertext blob must NOT contain the plaintext bytes
    check("ciphertext does not leak the plaintext substring",
          "A body with unicode".encode("utf-8") not in bytes(r1["content_encrypted"]))
    r3 = c.execute("SELECT content, content_encrypted FROM note WHERE note_id='note-3'").fetchone()
    check("note-3 (no body) never wrote a row", r3 is None)

check("decrypt round-trips the unicode/newline body exactly",
      N.get_note_body("note-1") == "A body with unicode ☕ and a newline\nline 2")

# idempotent re-run: same verified, still exactly 2 rows (upsert, no duplicates; note-3 skipped)
res2 = M.migrate_records("notes", RECORDS, save_one, verify_one, verify_fields=VF)
with S.connect() as c:
    n = c.execute("SELECT COUNT(*) FROM note").fetchone()[0]
check("re-run is idempotent (all verified, still 2 rows, no dupes)",
      res2.all_verified and n == 2)

# tamper: flip a byte in an encrypted blob -> fresh verify must FAIL closed (quarantine), not pass
with S.connect() as c:
    blob = bytearray(c.execute("SELECT content_encrypted FROM note WHERE note_id='note-2'").fetchone()[0])
    blob[-1] ^= 0xFF
    c.execute("UPDATE note SET content_encrypted=? WHERE note_id='note-2'", (bytes(blob),))
tampered_raises = False
try:
    verify_one("note-2")
except Exception:
    tampered_raises = True
check("tampered ciphertext fails closed on verify (quarantine, never silent)", tampered_raises)
with S.connect() as c:
    q = c.execute("SELECT COUNT(*) FROM decrypt_quarantine WHERE domain='notes'").fetchone()[0]
check("tamper recorded a value-free decrypt_quarantine row", q >= 1)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
print(f"RESULT: {fails} FAIL")
sys.exit(1)
