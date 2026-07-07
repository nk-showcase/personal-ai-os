"""Encryption test across the reference domains through the same chokepoint.
Proves: (1) the ALTER-added *_encrypted column exists; (2) a round-trip
encrypt->write->read->decrypt for each field of each domain; (3) AAD isolation — a
ciphertext of one domain/field does NOT decrypt under another (no swapping).
Ephemeral KEK, temp DB, no real key/age/store.
Run:  PYTHONPATH=. <python-with-cryptography> scripts/test_context_encryption_domains.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())

for _k in ("AIOS_CONTEXT_ENCRYPTION", "AIOS_CONTEXT_ALLOW_TEST_KEK",
           "AIOS_CONTEXT_TEST_KEK", "AIOS_CONTEXT_IDENTITY_FILE"):
    os.environ.pop(_k, None)

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


from bot import aios_storage as S  # noqa: E402
from bot.context_cipher import decrypt_for_storage, encrypt_for_storage, DecryptQuarantine  # noqa: E402

DB = os.path.join(tempfile.mkdtemp(prefix="aios_enc_dom_"), "t.sqlite3")
os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"
os.environ["AIOS_CONTEXT_ALLOW_TEST_KEK"] = "1"
os.environ["AIOS_CONTEXT_TEST_KEK"] = os.urandom(32).hex()

now = time.time()
# The reference at-rest-encrypted domains: chat titles, memory facts, demo notes.
DOMAINS = [
    {"domain": "chat", "table": "chat", "idcol": "source_notion_page_id",
     "fields": {"title": "title_encrypted"},
     "required": {"status": "active", "created_ts": now, "updated_ts": now}},
    {"domain": "memory", "table": "memory_fact", "idcol": "source_block_id",
     "fields": {"content": "content_encrypted"},
     "required": {"position": 0, "created_ts": now}},
    {"domain": "notes", "table": "note", "idcol": "note_id",
     "fields": {"content": "content_encrypted"},
     "required": {"created_ts": now}},
]

# 0. schema completeness — the ALTER-added *_encrypted column exists on `chat`
with S.connect(db=DB) as conn:
    have = {r[1] for r in conn.execute("PRAGMA table_info(chat)").fetchall()}
    want = {c for (t, c) in S._ENC_COLUMNS if t == "chat"}
    check("schema: all ALTER-added *_encrypted columns present in chat", want <= have)
    # the inline (in-_SCHEMA) encrypted columns are present too
    for tbl, col in (("memory_fact", "content_encrypted"), ("note", "content_encrypted"),
                     ("chat_message", "content_encrypted")):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
        check(f"schema: {col} present in {tbl}", col in cols)

# 1. per-domain, per-field round-trip
for d in DOMAINS:
    rid = f"{d['domain']}-r1"
    plain = {f: f"{d['domain']}:{f}:secret sample" for f in d["fields"]}
    enc_cols = {col: encrypt_for_storage(d["domain"], rid, f, plain[f])
                for f, col in d["fields"].items()}
    cols = [d["idcol"]] + list(d["required"].keys()) + list(enc_cols.keys())
    vals = [rid] + list(d["required"].values()) + list(enc_cols.values())
    ph = ",".join("?" * len(cols))
    with S.connect(db=DB) as conn:
        conn.execute(f"INSERT INTO {d['table']} ({','.join(cols)}) VALUES ({ph})", vals)
        readback = {col: conn.execute(
            f"SELECT {col} FROM {d['table']} WHERE {d['idcol']}=?", (rid,)).fetchone()[0]
            for col in enc_cols}
    ok = True
    for f, col in d["fields"].items():
        blob = readback[col]
        if not isinstance(blob, (bytes, bytearray)):
            ok = False; break
        if plain[f].encode("utf-8") in bytes(blob):
            ok = False; break
        if decrypt_for_storage(blob, domain=d["domain"], row_id=rid, field=f) != plain[f]:
            ok = False; break
    check(f"{d['domain']}: round-trip of all {len(d['fields'])} fields + ciphertext is opaque", ok)

# 2. AAD isolation: a memory/content ciphertext does NOT decrypt under another domain or field
rid = "memory-r1"
with S.connect(db=DB) as conn:
    blob = conn.execute("SELECT content_encrypted FROM memory_fact WHERE source_block_id=?",
                        (rid,)).fetchone()[0]
raised = False
try:
    decrypt_for_storage(blob, domain="notes", row_id=rid, field="content", db=DB)
except DecryptQuarantine:
    raised = True
check("AAD: a foreign domain does not decrypt (memory->notes)", raised)
raised = False
try:
    decrypt_for_storage(blob, domain="memory", row_id=rid, field="title", db=DB)
except DecryptQuarantine:
    raised = True
check("AAD: a foreign field does not decrypt (content->title)", raised)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print(f"RESULT: {fails} FAIL")
    sys.exit(1)
