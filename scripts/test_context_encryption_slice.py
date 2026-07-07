"""Test of the first at-rest encryption slice (NOTES) — chokepoint + key-seam + gating.
Safe: ephemeral KEK (os.urandom), a temp DB; no real key/age/server/Notion needed.
The crypto cases (1, 2, 5) require the `cryptography` package installed; cases 3, 4, 6 do not.
Run (from the repo root):  PYTHONPATH=. <python-with-cryptography> scripts/test_context_encryption_slice.py

Note shape (canonical): a note is a single free-text body keyed on a bot-minted note_id. The row
columns are note_id + content (cleartext, flag-OFF path) + content_encrypted (BLOB, flag-ON path).
save_note(note_id, content); get_note_body(note_id) returns the decrypted body or None.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())

_CTX_ENV = ("AIOS_CONTEXT_ENCRYPTION", "AIOS_CONTEXT_ALLOW_TEST_KEK",
            "AIOS_CONTEXT_TEST_KEK", "AIOS_CONTEXT_IDENTITY_FILE")


def clear_env():
    for k in _CTX_ENV:
        os.environ.pop(k, None)


clear_env()
fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


from bot import aios_storage as S  # noqa: E402
from bot.aios_notes_store import save_note, get_note_body  # noqa: E402
from bot.context_key import KekUnavailable  # noqa: E402
from bot.context_cipher import DecryptQuarantine  # noqa: E402

_tmp = tempfile.mkdtemp(prefix="aios_enc_slice_")
DB = os.path.join(_tmp, "aios_test.sqlite3")
HEXKEK = os.urandom(32).hex()


def row(note_id):
    with S.connect(db=DB) as conn:
        return conn.execute(
            "SELECT content, content_encrypted FROM note WHERE note_id=?", (note_id,)
        ).fetchone()


# --- 1. Flag OFF -> cleartext path (no crypto required) ---
clear_env()
save_note("n-clear", "hello body", db=DB)
r = row("n-clear")
check("OFF: body stored as cleartext", r["content"] == "hello body")
check("OFF: content_encrypted = NULL", r["content_encrypted"] is None)
check("OFF: get_note_body returns the text", get_note_body("n-clear", db=DB) == "hello body")

# --- 2. Flag ON + injected test KEK -> encrypted, round-trip ---
clear_env()
os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"
os.environ["AIOS_CONTEXT_ALLOW_TEST_KEK"] = "1"
os.environ["AIOS_CONTEXT_TEST_KEK"] = HEXKEK
save_note("n-enc", "secret body", db=DB)
r = row("n-enc")
check("ON: cleartext column content = NULL", r["content"] is None)
check("ON: content_encrypted populated (BLOB)", isinstance(r["content_encrypted"], (bytes, bytearray)))
check("ON: plaintext not present in the ciphertext", b"secret body" not in bytes(r["content_encrypted"]))
check("ON: get_note_body decrypts back", get_note_body("n-enc", db=DB) == "secret body")

# --- 3. Flag ON, no key -> KekUnavailable, NOTHING written (no silent cleartext) ---
clear_env()
os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"
raised = False
try:
    save_note("n-nokey", "x", db=DB)
except KekUnavailable:
    raised = True
check("ON+no key: save_note raises KekUnavailable", raised)
check("ON+no key: row NOT created (no cleartext leak)", get_note_body("n-nokey", db=DB) is None)

# --- 4. Empty note_id -> ValueError, nothing written (AAD binding) ---
clear_env()
raised = False
try:
    save_note("", "x", db=DB)
except ValueError:
    raised = True
check("empty note_id -> ValueError", raised)

# --- 5. Ciphertext tamper -> DecryptQuarantine + value-free quarantine row, no text returned ---
clear_env()
os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"
os.environ["AIOS_CONTEXT_ALLOW_TEST_KEK"] = "1"
os.environ["AIOS_CONTEXT_TEST_KEK"] = HEXKEK
save_note("n-tamper", "topsecret", db=DB)
with S.connect(db=DB) as conn:
    blob = bytearray(conn.execute(
        "SELECT content_encrypted FROM note WHERE note_id=?", ("n-tamper",)).fetchone()[0])
    blob[-1] ^= 0x01
    conn.execute("UPDATE note SET content_encrypted=? WHERE note_id=?", (bytes(blob), "n-tamper"))
raised = False
try:
    get_note_body("n-tamper", db=DB)
except DecryptQuarantine:
    raised = True
check("tamper: get_note_body raises DecryptQuarantine (no text returned)", raised)
with S.connect(db=DB) as conn:
    q = conn.execute(
        "SELECT domain, field, error_class FROM decrypt_quarantine WHERE row_id=?", ("n-tamper",)
    ).fetchone()
check("tamper: value-free quarantine row created (domain/field/error class)",
      q is not None and q["domain"] == "notes" and q["field"] == "content" and bool(q["error_class"]))

# --- 6. Test hook is inert without the explicit sentinel: flag ON + TEST_KEK, but ALLOW unset ---
clear_env()
os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"
os.environ["AIOS_CONTEXT_TEST_KEK"] = HEXKEK  # without AIOS_CONTEXT_ALLOW_TEST_KEK
raised = False
try:
    save_note("n-d4", "y", db=DB)
except KekUnavailable:
    raised = True
check("no sentinel: test hook does not activate -> KekUnavailable", raised)

clear_env()
print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print(f"RESULT: {fails} FAIL")
    sys.exit(1)
