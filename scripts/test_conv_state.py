"""Test S-3 conv_state durable store + dual blob codec.

Offline, no live Telegram / token. Two platforms:
  - CODEC + encryption-OFF subset (#1-#8d, #11): runs on Mac bare python3 (3.9.6, NO cryptography)
    — proves the int-key/set corruption is fixed, collision raises, unregistered/nested set
    raises (loud), {"__set__"} dict stays a dict, set order is cross-process stable, cleartext at
    rest, barrier #14 clean.
  - encryption-ON subset (#9, #10, #12, #13): runs on the VPS temp venv (py3.12 + cryptography)
    with the GATED test-sentinel KEK (AIOS_CONTEXT_ALLOW_TEST_KEK=1 + AIOS_CONTEXT_TEST_KEK) —
    NO init_kek, no age, no Bitwarden, zero owner step. Auto-skipped where cryptography is absent.

Run (Mac):  PYTHONPATH=. python3 scripts/test_conv_state.py
Run (VPS):  cd ~/apps/app && PYTHONPATH=. .venv/bin/python scripts/test_conv_state.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.getcwd())

_d = tempfile.mkdtemp(prefix="aios_convstate_")
os.environ["AIOS_TASK_QUEUE_DB"] = os.path.join(_d, "q.sqlite3")        # conv_state rows (throwaway)
os.environ["AIOS_DATA_DB"] = os.path.join(_d, "data.sqlite3")          # quarantine rows (throwaway)
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)                        # OFF for the codec block
os.environ.pop("AIOS_CONTEXT_ALLOW_TEST_KEK", None)
os.environ.pop("AIOS_CONTEXT_TEST_KEK", None)

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


def raises(fn, exc=Exception):
    try:
        fn()
        return None
    except exc as e:  # noqa: BLE001
        return e


import bot.conv_state as cs  # noqa: E402

DB = os.environ["AIOS_TASK_QUEUE_DB"]


def _raw_row(chat_id):
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT blob, blob_encrypted FROM conv_state WHERE chat_id=?", (chat_id,)
        ).fetchone()


# ============================ CODEC + OFF (Mac bare python3) ============================

# --- 1. INT-KEY resolves after real save->load (the headline corruption the slice fixes) ---
cs.save_state(1001, {"task_map": {1: "T-aaa", 2: "T-bbb"}})
s = cs.load_state(1001)
check("#1 task_map[1]=='T-aaa' & [2]=='T-bbb' with INT keys (.get(1) works, not 'row exists')",
      s["task_map"][1] == "T-aaa" and s["task_map"][2] == "T-bbb"
      and all(isinstance(k, int) for k in s["task_map"]))

# --- 1b. task_map with several int keys resolves ---
cs.save_state(1002, {"task_map": {3: "T-ccc", 5: "T-ddd"}})
s = cs.load_state(1002)
check("#1b task_map[3] & task_map[5] resolve with int keys",
      s["task_map"][3] == "T-ccc" and s["task_map"][5] == "T-ddd")

# --- 1c. task_map (int key -> dict value) ---
cs.save_state(1003, {"task_map": {3: {"id": "T-x", "content": "a"}}})
s = cs.load_state(1003)
check("#1c task_map[3]['id']=='T-x' (int key + nested dict value intact)",
      s["task_map"][3]["id"] == "T-x" and isinstance(list(s["task_map"])[0], int))

# --- 2. Every SET field round-trips as a set (registered SET_FIELDS: top-level + parent.child) ---
set_cases = {
    1010: ({"note_tags_selected": {"a", "b"}}, ["note_tags_selected"]),
    1011: ({"note_draft": {"attachments": {"f1", "f2"}}}, ["note_draft", "attachments"]),
}
ok2 = True
for cid, (state, _) in set_cases.items():
    cs.save_state(cid, state)
    back = cs.load_state(cid)
    # walk to each set leaf and compare
    if cid == 1010:
        ok2 &= isinstance(back["note_tags_selected"], set) and back["note_tags_selected"] == {"a", "b"}
    if cid == 1011:
        ok2 &= isinstance(back["note_draft"]["attachments"], set) and back["note_draft"]["attachments"] == {"f1", "f2"}
check("#2 every SET field loads back as a set with members intact", ok2)

# --- 3. Nested + mixed in one blob ---
cs.save_state(1020, {
    "task_map": {1: "T-1"},
    "note_draft": {"body": "reminder text", "attachments": {"f1"}},
    "last_undo": {"prev_notes": None, "kind": "edit"},
    "note_list": ["r0", "r1"],
})
s = cs.load_state(1020)
check("#3 int keys int + nested set is set + None preserved + list preserved together",
      isinstance(list(s["task_map"])[0], int)
      and isinstance(s["note_draft"]["attachments"], set)
      and s["last_undo"]["prev_notes"] is None
      and isinstance(s["note_list"], list) and s["note_list"][0] == "r0")

# --- 4. Empty dict vs missing ---
cs.save_state(1030, {})
check("#4 save({}) then load == {} AND a row exists; load(absent) == {} with NO row",
      cs.load_state(1030) == {} and cs.load_state(99999) == {}
      and _raw_row(1030) is not None and _raw_row(99999) is None)

# --- 5. Idempotent re-serialize ---
x = {"task_map": {2: "b", 1: "a"}, "note_draft": {"attachments": {"z", "a"}}, "n": None, "k": [1, 2]}
b1 = cs.serialize(x)
b2 = cs.serialize(cs.deserialize(b1))
check("#5 serialize(deserialize(serialize(x))) == serialize(x) byte-exact", b1 == b2)

# --- 6. Int-key collision guard raises; non-canonical token kept distinct ---
e6 = raises(lambda: cs.serialize({"task_map": {1: "a", "1": "b"}}), ValueError)
back6 = cs.deserialize(cs.serialize({"task_map": {1: "a", "01": "b"}}))
check("#6 true str(k) collision raises ValueError; {1:'a','01':'b'} keeps int 1 AND str '01'",
      isinstance(e6, ValueError) and back6["task_map"][1] == "a" and back6["task_map"]["01"] == "b")

# --- 7. OFF stores cleartext ---
cs.save_state(1040, {"task_map": {1: "T-secret-substr"}})
row7 = _raw_row(1040)
blob7 = bytes(row7["blob"]) if row7 and row7["blob"] is not None else b""
check("#7 OFF: raw blob CONTAINS the cleartext substring; blob_encrypted IS NULL",
      b"T-secret-substr" in blob7 and row7["blob_encrypted"] is None)

# --- 8. Non-ASCII / emoji survive (ensure_ascii=False) ---
cs.save_state(1050, {"note_tags_selected": {"café ☕"}})
s = cs.load_state(1050)
row8 = _raw_row(1050)
blob8 = bytes(row8["blob"])
check("#8 non-ASCII+emoji set round-trips AND raw blob has literal UTF-8 (no \\uXXXX)",
      s["note_tags_selected"] == {"café ☕"}
      and "café".encode("utf-8") in blob8 and b"\\u00e9" not in blob8)

# --- 8b. Unregistered/nested set RAISES (loud); {"__set__"} dict stays dict ---
e8b = raises(lambda: cs.serialize({"unregistered_map": {1: {"labels": {"home", "work"}}}}), ValueError)
cs.save_state(1060, {"misc": {"__set__": ["x", "y"]}})
back8b = cs.load_state(1060)
check("#8b unregistered/nested set RAISES ValueError (loud, not silent set->dict)",
      isinstance(e8b, ValueError))
check("#8b real {'__set__':[...]} dict at non-registered path stays a DICT (no false coercion)",
      back8b["misc"] == {"__set__": ["x", "y"]} and not isinstance(back8b["misc"], set))

# --- 8c. Cross-process set ordering is byte-identical (PYTHONHASHSEED-independent) ---
code8c = ("import os,sys; sys.path.insert(0,os.getcwd()); import bot.conv_state as cs; "
          "print(cs.serialize({'note_tags_selected':{1,'1','a',2}}))")
def _ser_with_seed(seed):
    env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONPATH=os.getcwd())
    return subprocess.run([sys.executable, "-c", code8c], capture_output=True, text=True, env=env).stdout
o0, o1 = _ser_with_seed(0), _ser_with_seed(1)
check("#8c mixed set serializes byte-identically across two PYTHONHASHSEEDs",
      o0 == o1 and o0.strip() != "")

# --- 8d. tuple / datetime are loud ---
check("#8d tuple value raises (no silent degrade to list)",
      isinstance(raises(lambda: cs.serialize({"x": (1, 2)}), ValueError), ValueError))
check("#8d raw datetime value raises (no opaque json crash)",
      isinstance(raises(lambda: cs.serialize({"y": datetime(2026, 6, 23)}), Exception), Exception))

# --- 11. Barrier #14 stays clean (static import check) ---
src = open(os.path.join(os.getcwd(), "bot", "conv_state.py"), encoding="utf-8").read()
imports = "\n".join(ln for ln in src.splitlines() if ln.startswith(("import ", "from ")))
check("#11 conv_state imports context_cipher only; no cryptography/context_store/get_kek/aios_storage",
      "context_cipher" in imports
      and "cryptography" not in imports and "ChaCha20Poly1305" not in src
      and "context_store" not in imports and "get_kek" not in imports
      and "aios_storage" not in imports)

# ============================ encryption-ON (VPS temp venv only) ============================
try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except Exception:
    HAVE_CRYPTO = False

if not HAVE_CRYPTO:
    print("NOTE: cryptography absent (Mac bare python3) -> skipping encryption-ON tests #9/#10/#12/#13"
          " (run them on the VPS temp venv).")
else:
    from bot.context_key import KekUnavailable
    from bot.context_cipher import DecryptQuarantine

    def _set_kek_on():
        os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"
        os.environ["AIOS_CONTEXT_ALLOW_TEST_KEK"] = "1"
        os.environ["AIOS_CONTEXT_TEST_KEK"] = os.urandom(32).hex()

    def _set_kek_off_but_flag_on():
        os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"
        os.environ.pop("AIOS_CONTEXT_ALLOW_TEST_KEK", None)
        os.environ.pop("AIOS_CONTEXT_TEST_KEK", None)

    # --- 9. ON stores ciphertext, round-trips ---
    _set_kek_on()
    cs.save_state(2001, {"task_map": {1: "T-zzz"}})
    row9 = _raw_row(2001)
    enc9 = bytes(row9["blob_encrypted"]) if row9["blob_encrypted"] is not None else b""
    s9 = cs.load_state(2001)
    check("#9 ON: blob_encrypted set, blob NULL, ciphertext lacks plaintext, full int-key round-trip",
          row9["blob_encrypted"] is not None and row9["blob"] is None
          and b"T-zzz" not in enc9 and s9["task_map"][1] == "T-zzz")

    # --- 10. ON + no KEK = fail-closed (zero rows touched) ---
    _set_kek_off_but_flag_on()
    e10 = raises(lambda: cs.save_state(2002, {"task_map": {1: "x"}}), KekUnavailable)
    with sqlite3.connect(DB) as c:
        n10 = c.execute("SELECT count(*) FROM conv_state WHERE chat_id=?", (2002,)).fetchone()[0]
    check("#10 ON+no KEK raises KekUnavailable AND zero rows written",
          isinstance(e10, KekUnavailable) and n10 == 0)

    # --- 12. Undecryptable row raises (fail-closed end-to-end) ---
    _set_kek_on()
    cs.save_state(2003, {"task_map": {1: "T-corrupt"}})
    row12 = _raw_row(2003)
    bad = bytearray(bytes(row12["blob_encrypted"]))
    bad[-1] ^= 0xFF  # corrupt the AEAD tail
    with sqlite3.connect(DB) as c:
        c.execute("UPDATE conv_state SET blob_encrypted=? WHERE chat_id=?", (bytes(bad), 2003))
    e12 = raises(lambda: cs.load_state(2003), DecryptQuarantine)
    qn = 0
    try:
        with sqlite3.connect(os.environ["AIOS_DATA_DB"]) as c:
            qn = c.execute("SELECT count(*) FROM decrypt_quarantine").fetchone()[0]
    except Exception:
        qn = -1
    check("#12 corrupted ciphertext load RAISES DecryptQuarantine (NOT silent {}) + quarantine row",
          isinstance(e12, DecryptQuarantine) and qn >= 1)

    # --- 13. Cross-chat ciphertext relocation rejected (AAD binds row_id=chat_id) ---
    cs.save_state(2004, {"task_map": {1: "T-A"}})
    encA = bytes(_raw_row(2004)["blob_encrypted"])
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR REPLACE INTO conv_state (chat_id, blob, blob_encrypted, updated_ts) "
                  "VALUES (?,NULL,?,?)", (2005, encA, 0.0))
    e13 = raises(lambda: cs.load_state(2005), DecryptQuarantine)
    check("#13 chat A's ciphertext relocated to chat B raises DecryptQuarantine (AAD row_id binding)",
          isinstance(e13, DecryptQuarantine))

print("")
if fails == 0:
    print("RESULT: ALL PASS" + ("" if HAVE_CRYPTO else " (OFF/codec subset; ON subset skipped - no cryptography)"))
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
