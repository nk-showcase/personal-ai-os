"""store_view plumbing + the surviving worker views (chat resume + notes demo).

Asserts: the store_view kind is registered everywhere; the view applier is fail-closed
without keys; without the Telegram token a view reports send_unavailable (the bot falls
back — no lost replies); with a mocked sender the worker-rendered text is BYTE-IDENTICAL to
the shared renderer the bot fallback uses (parity by construction); decrypted content never
appears in the op result (only structure). Temp DB + throwaway KEK. Run:
    PYTHONPATH=. .venv/bin/python scripts/test_store_view.py
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


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ.update({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_OWNER_ID": "1"})
TMP = tempfile.mkdtemp(prefix="storeview-")
os.environ["AIOS_DATA_DB"] = os.path.join(TMP, "aios.sqlite3")
os.environ["TELEGRAM_BOT_TOKEN"] = "dummy:test"  # config import needs it; sender is mocked

from bot import aios_request_queue as RQ   # noqa: E402
from bot import integrations_proxy as IP            # noqa: E402
from bot.aios_store_applier import store_view_applier, StoreUnavailable  # noqa: E402

check("store_view in ALLOWED_KINDS", "store_view" in RQ.ALLOWED_KINDS)
check("store_view in WRITE_KINDS (send side effect -> dead-letter, no silent drop)",
      "store_view" in RQ.WRITE_KINDS)
check("KIND_STORE_VIEW in proxy INTEGRATION_KINDS", IP.KIND_STORE_VIEW in IP.INTEGRATION_KINDS)


class _Rec:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


def _pk(domain, op, args):
    return {"op_b64": IP._b64({"domain": domain, "op": op, "args": args})}


# fail-closed without keys
try:
    store_view_applier(_Rec("store_view", _pk("notes", "get_summary", {"chat_id": 1})))
    check("view REFUSED with encryption OFF", False)
except StoreUnavailable:
    check("view REFUSED with encryption OFF", True)

# provision throwaway KEK
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

from bot import aios_chat_store as C     # noqa: E402
from bot import aios_notes_store as N    # noqa: E402
from bot import view_render as VR        # noqa: E402
from bot import worker_telegram as WT    # noqa: E402

# keys ON but NO telegram token -> send_unavailable (fallback contract)
WT.token_present = lambda: False
r = IP.decode_result(store_view_applier(_Rec("store_view", _pk("notes", "get_summary",
                                                              {"chat_id": 1}))))
check("no token -> ok False + send_unavailable (bot falls back)",
      r.get("ok") is False and r.get("send_unavailable") is True)

# mock the sender: capture (chat_id, text) instead of touching the network
sent = []
WT.token_present = lambda: True
WT.send_message = lambda chat_id, text, reply_markup=None, timeout=15.0: (
    sent.append((chat_id, text)) or {"ok": True, "parts": 1})

# ---- notes demo view: empty summary, then with two notes ----------------------------
r = IP.decode_result(store_view_applier(_Rec("store_view", _pk("notes", "get_summary",
                                                              {"chat_id": 42}))))
check("empty notes summary sent (count 0, structure only)",
      r.get("ok") is True and r.get("sent") is True and r.get("count") == 0
      and sent[-1] == (42, "You have no saved notes yet."))

SECRET_A = "call the dentist about the crown"
SECRET_B = "renew the passport before autumn"
N.save_note("local-note-a", SECRET_A)
N.save_note("local-note-b", SECRET_B)
r = IP.decode_result(store_view_applier(_Rec("store_view", _pk("notes", "get_summary",
                                                              {"chat_id": 42}))))
summary = N.get_summary()
expected = VR.render_notes_summary(summary["count"], summary["previews"])
check("notes summary view text BYTE-IDENTICAL to the shared renderer",
      r.get("ok") is True and r.get("sent") is True and sent[-1][1] == expected)
check("notes summary sent to the right chat, count returned as structure",
      sent[-1][0] == 42 and r.get("count") == 2)
check("no full note body in the op result (structure only)",
      SECRET_A not in str(r) and SECRET_B not in str(r))

# ---- notes demo WRITE view: save a note, structure-only result ----------------------
r = IP.decode_result(store_view_applier(_Rec("store_view", _pk("notes", "save_note",
                                             {"chat_id": 7, "text": "water the plants"}))))
check("notes save_note view: sent confirmation + id, no body in result",
      r.get("ok") is True and r.get("sent") is True and r.get("id")
      and sent[-1] == (7, "Noted.") and "water the plants" not in str(r))

# ---- chat resume view: title is decrypted worker-side, only the rendered line is sent ----
CANARY_TITLE = "secret chat title canary"
C.save_chat({"source_notion_id": "local-chat-1", "title": CANARY_TITLE, "status": "active",
             "created_time": "2026-07-02T05:00:00.000Z",
             "messages": [
                 {"source_block_id": "b1", "role": "user", "content": "hello", "position": 0},
                 {"source_block_id": "b2", "role": "assistant", "content": "hi", "position": 1},
                 {"source_block_id": "b3", "role": "user", "content": "again", "position": 2},
             ]})
r = IP.decode_result(store_view_applier(_Rec("store_view", _pk("chat", "resume",
                                                              {"chat_id": 99}))))
n_user = 2  # two user messages above
expected_line = VR.render_chat_resume(CANARY_TITLE, n_user)
check("chat resume view text BYTE-IDENTICAL to the shared renderer",
      r.get("ok") is True and r.get("sent") is True and sent[-1] == (99, expected_line))
check("chat resume returns structure only (page_id + msg_count, NO title)",
      r.get("page_id") == "local-chat-1" and r.get("msg_count") == n_user
      and CANARY_TITLE not in str(r))

# unknown view fails loudly
try:
    store_view_applier(_Rec("store_view", _pk("notes", "nope", {})))
    check("unknown view fails loudly", False)
except ValueError:
    check("unknown view fails loudly", True)

# ---- bot-side transport: view_request maps the poll outcome (one fix for the whole
# worker-sends mechanism). The keyed worker sends personal views to Telegram ITSELF; when
# its turn runs longer than the ~12s bot poll window the poll returns WRITE_ACCEPTED — the
# row is durably queued and WILL be delivered. That is NOT unavailability, so the receiver
# must not tell the user to retry (a retry duplicates the turn). Only a dead/expired row
# (WRITE_DEAD) or a corrupt result blob is genuinely unavailable.
import asyncio  # noqa: E402
from bot.aios_worker_contract import PollOutcome, PollResultState as _S  # noqa: E402


def _outcome(state, result=None):
    return PollOutcome(state=state, correlation_id="cid-x", result=result,
                       note=None, source_status="x")


def _drive_view(outcome):
    IP._blocking_round_trip = lambda *a, **k: outcome
    return asyncio.run(IP.view_request("notes", "get_summary", {"chat_id": 1}))


_orig_brt = IP._blocking_round_trip
try:
    acc = _drive_view(_outcome(_S.WRITE_ACCEPTED))
    check("view_request WRITE_ACCEPTED -> accepted (worker will send; NOT unavailable)",
          acc.get("ok") is False and acc.get("accepted") is True and not acc.get("unavailable"))
    dead = _drive_view(_outcome(_S.WRITE_DEAD))
    check("view_request WRITE_DEAD -> unavailable (real failure; NOT accepted)",
          dead.get("ok") is False and dead.get("unavailable") is True and not dead.get("accepted"))
    saved = _drive_view(_outcome(_S.WRITE_SAVED, IP.encode_result({"ok": True, "sent": True})))
    check("view_request WRITE_SAVED -> decoded worker result (ok & sent pass through)",
          saved.get("ok") is True and saved.get("sent") is True)
    corrupt = _drive_view(_outcome(_S.WRITE_SAVED, {"result_b64": "!!not-base64!!"}))
    check("view_request corrupt result blob -> unavailable (no crash, no fake success)",
          corrupt.get("ok") is False and corrupt.get("unavailable") is True)
finally:
    IP._blocking_round_trip = _orig_brt

# ---- receiver reply policy: view_render.view_reply_for maps the cases so every view
# handler stays consistent (chat + notes share this one helper).
check("view_reply_for: sent -> None (silence; worker already delivered)",
      VR.view_reply_for({"ok": True, "sent": True}) is None)
check("view_reply_for: accepted -> accepted msg (NOT the busy 'retry' message)",
      VR.view_reply_for({"ok": False, "accepted": True}) == VR.VIEW_ACCEPTED_MESSAGE
      and VR.VIEW_ACCEPTED_MESSAGE != VR.VIEW_BUSY_MESSAGE)
check("view_reply_for: accepted message does NOT ask to repeat",
      "repeat" not in VR.VIEW_ACCEPTED_MESSAGE.lower()
      and "try again" not in VR.VIEW_ACCEPTED_MESSAGE.lower())
check("view_reply_for: unavailable -> busy message (retry is appropriate)",
      VR.view_reply_for({"ok": False, "unavailable": True}) == VR.VIEW_BUSY_MESSAGE)
check("view_reply_for: non-dict -> busy message (defensive)",
      VR.view_reply_for(None) == VR.VIEW_BUSY_MESSAGE)

print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
