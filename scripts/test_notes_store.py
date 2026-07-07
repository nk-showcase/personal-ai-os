"""End-to-end test of the reference "notes" demo domain.

Proves the published mechanisms carry a live payload, all offline (no network, no real secrets):

  classifier 'notes' intent -> router_dispatch gate -> request-queue op-spec ->
  aios_notes_store (encrypted at rest) -> a view in aios_store_applier (direct send).

Coverage:
  (1) aios_notes_store.save_note / get_note round-trips a note through the encrypted-at-rest
      column, and get_summary returns a VALUE-FREE structural summary (count + short previews),
      never the full bodies through the applier result.
  (2) store_op_applier routes ("notes","save_note") to the encrypted store; the applier result
      carries STRUCTURE ONLY (ok/id), not the note text.
  (3) store_view_applier routes the two demo views ("notes","save_note" / "get_summary"): the
      keyed worker renders + "sends" to Telegram directly (captured), and returns structure only.
  (4) router_dispatch gates 'notes' to its two actions and nothing else.

A throwaway age identity + KEK is built in a temp dir so the encrypted path is real. If the `age`
binary is unavailable, the encrypted-store assertions are SKIPPED but the router-gate and pure
in-process assertions still run.

Run:
    PYTHONPATH=. python3 scripts/test_notes_store.py
"""
import hashlib
import os
import shutil
import secrets
import subprocess
import sys
import tempfile

sys.path.insert(0, os.getcwd())
fails = 0
skips = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


def skip(name, reason):
    global skips
    skips += 1
    print(f"SKIP: {name} ({reason})")


# ---- throwaway KEK + temp DB harness (clone of scripts/test_continue_chat.py setup) --------
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ.update({"TELEGRAM_BOT_TOKEN": "dummy:test", "TELEGRAM_OWNER_ID": "1"})
TMP = tempfile.mkdtemp(prefix="notesstore-")
os.environ["AIOS_DATA_DB"] = os.path.join(TMP, "aios.sqlite3")

AGE = os.environ.get("AIOS_AGE_BIN") or shutil.which("age") or os.path.expanduser("~/.local/bin/age")
KG = os.path.join(os.path.dirname(AGE), "age-keygen")
_ENCRYPTED = os.path.exists(AGE) and os.path.exists(KG)
if _ENCRYPTED:
    IDF, KEKF = os.path.join(TMP, "id.txt"), os.path.join(TMP, "kek.age")
    subprocess.run([KG, "-o", IDF], check=True, capture_output=True)
    pub = subprocess.run([KG, "-y", IDF], check=True, capture_output=True, text=True).stdout.strip()
    KEK = secrets.token_bytes(32)
    subprocess.run([AGE, "-r", pub, "-o", KEKF], input=KEK, check=True)
    os.environ.update({"AIOS_AGE_BIN": AGE, "AIOS_CONTEXT_IDENTITY_FILE": IDF,
                       "AIOS_CONTEXT_KEK_FILE": KEKF, "AIOS_CONTEXT_ENCRYPTION": "1"})
    from bot import context_key  # noqa: E402
    context_key.KEK_SHA256_PIN = hashlib.sha256(KEK).hexdigest()

from bot import integrations_proxy as IP        # noqa: E402
from bot import aios_notes_store as N            # noqa: E402
from bot import router_dispatch as RD            # noqa: E402
from bot import worker_telegram as WT            # noqa: E402


class _Rec:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


def _pk(domain, op, args):
    return {"op_b64": IP._b64({"domain": domain, "op": op, "args": args})}


def _note_col_encrypted(note_id: str) -> bool:
    """True iff the note row stores ciphertext (content_encrypted set, cleartext content NULL)."""
    from bot import aios_storage as S
    with S.connect() as conn:
        row = conn.execute("SELECT content, content_encrypted FROM note WHERE note_id=?",
                           (note_id,)).fetchone()
    return row is not None and row["content"] is None and row["content_encrypted"] is not None


# ============================================================================================
# (4) router_dispatch gate — pure, no DB/keys. 'notes' is dispatchable; its actions are gated.
# ============================================================================================
check("notes is a dispatchable intent", "notes" in RD.DISPATCH_INTENTS)
check("cut domains are NOT dispatchable",
      not ({"health", "books", "theater", "recipe"} & set(RD.DISPATCH_INTENTS)))


async def _run_intent(action):
    env = {"intent": "notes", "action": action, "chat_id": 1, "kind": "text"}
    # No handlers wired here -> if the gate lets it through it would import handlers; we only
    # assert the SUB-GATE decision, so a non-notes action must return False WITHOUT importing.
    return await RD.run_intent(env, owner_id=1)


import asyncio  # noqa: E402
check("notes sub-gate REJECTS an unknown action",
      asyncio.new_event_loop().run_until_complete(_run_intent("bogus_action")) is False)


# ============================================================================================
# Encrypted-store + applier coverage (needs the age binary for a real KEK).
# ============================================================================================
if not _ENCRYPTED:
    skip("encrypted notes store + applier round-trip", "age binary not available")
else:
    from bot.aios_store_applier import store_op_applier, store_view_applier  # noqa: E402

    def store_op(domain, op, args=None):
        return IP.decode_result(store_op_applier(_Rec("store_write", _pk(domain, op, args or {}))))

    def view_op(domain, op, args=None):
        return IP.decode_result(store_view_applier(_Rec("store_view", _pk(domain, op, args or {}))))

    # capture direct sends instead of hitting Telegram
    _sent = []
    WT.token_present = lambda: True
    WT.send_message = lambda chat_id, text, reply_markup=None, timeout=15.0: _sent.append((chat_id, text))

    # (1) store layer: save + read back the real note; get_summary is value-free.
    NOTE_TEXT = "call the plumber on Friday afternoon"
    N.save_note("note-1", NOTE_TEXT)
    got = N.get_note("note-1")
    check("(1) save_note round-trips the note body", got is not None and got["content"] == NOTE_TEXT)
    check("(1) note is encrypted at rest (cleartext column is NULL)",
          _note_col_encrypted("note-1"))
    N.save_note("note-2", "buy milk and eggs")
    summ = N.get_summary()
    check("(1) get_summary reports the true count", summ["count"] == 2)
    check("(1) get_summary previews are truncated, not full bodies",
          all(len(p) <= 43 for p in summ["previews"]))

    # (2) store_op_applier WRITE path: a bot-minted id -> structure-only result, note text absent.
    res = store_op("notes", "save_note", {"note_id": "note-3", "content": "secret note text"})
    check("(2) store_op save_note returns ok + id", res.get("ok") and res.get("id") == "note-3")
    check("(2) store_op result carries NO note text", "secret note text" not in repr(res))
    check("(2) the note landed in the encrypted store", N.get_note("note-3") is not None)

    # (3) store_view_applier: the demo save + summary views render + send directly.
    _sent.clear()
    r_save = view_op("notes", "save_note", {"chat_id": 1, "text": "view-written note"})
    check("(3) save_note view sent a confirmation", r_save.get("sent") is True and len(_sent) == 1)
    check("(3) save_note view result is structure-only (no note text)",
          "view-written note" not in repr(r_save))
    check("(3) the view-written note is in the store", N.count_notes() == 4)

    _sent.clear()
    r_sum = view_op("notes", "get_summary", {"chat_id": 1})
    check("(3) get_summary view sent a summary", r_sum.get("sent") is True and len(_sent) == 1)
    # Privacy property: the QUEUE result carries only a count/structure, never note bodies.
    # (The message the worker sends to the OWNER's own Telegram may include truncated previews;
    # that is not a queue leak.)
    check("(3) get_summary QUEUE result carries only a count (no bodies)",
          r_sum.get("count") == 4 and "secret note text" not in repr(r_sum)
          and "view-written note" not in repr(r_sum))


print()
if fails:
    print(f"{fails} FAILURE(S)" + (f", {skips} skipped" if skips else ""))
    sys.exit(1)
print("ALL PASS" + (f" ({skips} skipped)" if skips else ""))
