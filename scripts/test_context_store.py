"""Test for the context_store encryption core (scheme 3, per-row DEK + ChaCha20-Poly1305).
Safe: keys are generated ephemerally (os.urandom), the real KEK is never used,
values are never printed. Requires `cryptography` in the environment.
Run (from the repo root):  PYTHONPATH=. <python-with-cryptography> scripts/test_context_store.py
"""
import os
import sys

sys.path.insert(0, os.getcwd())

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


from bot.context_store import encrypt_field, decrypt_field, DecryptError  # noqa: E402

KEK = os.urandom(32)
ID = dict(domain="notes", row_id="row-42", field="body")

# 1. round-trip (including unicode, empty string, long text)
for label, pt in [
    ("ascii", "hello"),
    ("unicode+emoji", "note with an accent: café ☕"),
    ("empty string", ""),
    ("long", "x" * 5000),
]:
    env = encrypt_field(pt, KEK, **ID)
    check(f"round-trip: {label}", decrypt_field(env, KEK, **ID) == pt)

# 2. encrypting the same text twice yields DIFFERENT envelopes (fresh DEK/nonce)
e1 = encrypt_field("same", KEK, **ID)
e2 = encrypt_field("same", KEK, **ID)
check("same text -> different envelopes (no deterministic leak)", e1 != e2)
check("both decrypt back to the original text",
      decrypt_field(e1, KEK, **ID) == "same" and decrypt_field(e2, KEK, **ID) == "same")

# 3. flipping a byte in the ciphertext -> DecryptError (tamper-evident)
env = bytearray(encrypt_field("secret-note", KEK, **ID))
env[-1] ^= 0x01
try:
    decrypt_field(bytes(env), KEK, **ID)
    check("flipped byte -> DecryptError", False)
except DecryptError:
    check("flipped byte -> DecryptError", True)

# 4. wrong KEK -> DecryptError (no data leaks)
env = encrypt_field("secret-note", KEK, **ID)
try:
    decrypt_field(env, os.urandom(32), **ID)
    check("wrong KEK -> DecryptError", False)
except DecryptError:
    check("wrong KEK -> DecryptError", True)

# 5. wrong AAD (different row/field) -> DecryptError: the ciphertext cannot be moved
env = encrypt_field("secret-note", KEK, **ID)
try:
    decrypt_field(env, KEK, domain="notes", row_id="row-99", field="body")
    check("different row_id -> DecryptError (row binding)", False)
except DecryptError:
    check("different row_id -> DecryptError (row binding)", True)
try:
    decrypt_field(env, KEK, domain="tasks", row_id="row-42", field="body")
    check("different domain -> DecryptError (domain binding)", False)
except DecryptError:
    check("different domain -> DecryptError (domain binding)", True)

# 6. the error message contains no value/key
try:
    decrypt_field(b"\x01\x00\x00garbagegarbagegarbage", KEK, **ID)
    check("corrupt envelope -> DecryptError", False)
except DecryptError as e:
    msg = str(e)
    check("corrupt envelope -> DecryptError", True)
    check("error text contains no 'secret'/'KEK value'", "secret-note" not in msg)

# 7. KEK of the wrong length -> ValueError (not DecryptError, this is a call error)
try:
    encrypt_field("x", os.urandom(16), **ID)
    check("short KEK -> ValueError", False)
except ValueError:
    check("short KEK -> ValueError", True)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print(f"RESULT: {fails} FAIL")
    sys.exit(1)
