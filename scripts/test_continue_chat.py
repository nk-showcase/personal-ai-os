"""Gate 0 continue-chat fix: the durable "current chat" pointer that makes "Continue" resume
the owner's genuinely-last conversation instead of an empty new chat.

Reproduces the two diagnosed defects and proves the pointer fixes both, end to end through the
LOCAL store layer (store_op_applier / store_view_applier), with a throwaway KEK + temp DB:

  BUG: a real chat with N messages set to status='archived' + a newer EMPTY active chat ->
       get_latest_chat_local returns the empty one (the archived real chat is invisible).
  FIX: after the pointer is stamped to the archived real chat id, the new ("chat","current")
       op returns {page_id=real, msg_count=N} -> the archived chat is resumable by pointer.
  NEW CHAT: stamping the pointer to a fresh EMPTY id makes ("chat","current") return that id
       with msg_count 0 -> continue_chat falls through to get_latest (does NOT resurrect the old
       chat).
  PRIVACY: the ("chat","current") result contains NO decrypted message text (structure only).

Also verifies the two WRITE sites that stamp the pointer in the keyed worker: _chat_create
(new-chat create op) and _view_chat_turn (after a persisted turn).

Run:
    PYTHONPATH=. .venv/bin/python scripts/test_continue_chat.py
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


# ---- throwaway KEK + temp DB harness (clone of scripts/test_chat_turn_view.py setup) --------
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ.update({"TELEGRAM_BOT_TOKEN": "dummy:test", "TELEGRAM_OWNER_ID": "1"})
TMP = tempfile.mkdtemp(prefix="continuechat-")
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

from bot import integrations_proxy as IP     # noqa: E402
from bot import aios_chat_store as C          # noqa: E402
from bot import aios_config as CFG            # noqa: E402
from bot import worker_telegram as WT         # noqa: E402
from bot.aios_store_applier import store_op_applier, store_view_applier  # noqa: E402


class _Rec:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


def _pk(domain, op, args):
    return {"op_b64": IP._b64({"domain": domain, "op": op, "args": args})}


def store_op(domain, op, args=None):
    """Run a store_read/store_write op through the applier, decoded (as the receiver sees it)."""
    return IP.decode_result(store_op_applier(_Rec("store_read", _pk(domain, op, args or {}))))


def view_op(domain, op, args=None):
    return IP.decode_result(store_view_applier(_Rec("store_view", _pk(domain, op, args or {}))))


# ============================================================================================
# (1) Reproduce the bug: an ARCHIVED real chat (N messages) + a newer EMPTY ACTIVE chat.
#     get_latest_chat_local (status='active' AND archived=0) returns the EMPTY one; the archived
#     real chat is invisible -> "Continue" would open an empty chat. This is the diagnosed bug.
# ============================================================================================
REAL = "local-real-1"     # the owner's genuinely-last conversation (later archived)
EMPTY = "local-empty-2"   # a newer, empty active chat (e.g. a "New chat" nobody wrote in)
REAL_MSGS = [
    {"source_block_id": "r1", "role": "user", "position": 0, "content": "first user message"},
    {"source_block_id": "r2", "role": "assistant", "position": 1, "content": "assistant reply"},
    {"source_block_id": "r3", "role": "user", "position": 2, "content": "second user message"},
]
N = 3
C.save_chat({"source_notion_id": REAL, "title": "Sample conversation", "status": "active",
             "created_time": "2026-07-01T06:00:00.000Z", "messages": REAL_MSGS})
# newer empty active chat (created AFTER the real one -> wins get_latest ordering)
C.save_chat({"source_notion_id": EMPTY, "title": "New chat", "status": "active",
             "created_time": "2026-07-03T08:00:00.000Z", "messages": []})
# archive the real chat (as start_new_chat does when the owner opens a new one)
C.set_chat_status_local(REAL, "archived")

latest = C.get_latest_chat_local()
check("BUG reproduced: get_latest_chat_local returns the EMPTY active chat, not the real one",
      latest is not None and latest["id"] == EMPTY)
check("BUG reproduced: the archived real chat is INVISIBLE to get_latest_chat_local",
      latest["id"] != REAL)

# ============================================================================================
# (2) The FIX: stamp the pointer to the real (archived) chat id -> ("chat","current") returns
#     {page_id=real, msg_count=N}. The archived chat is resumable by pointer.
# ============================================================================================
CFG.set_current_chat_page_id(REAL)
cur = store_op("chat", "current")
check("FIX: chat.current returns the pointed-to (archived) real chat page_id",
      cur.get("ok") and cur.get("page_id") == REAL)
check("FIX: chat.current returns the real message count (N=3), so Continue resumes it",
      cur.get("msg_count") == N)

# PRIVACY: the current-chat result carries NO decrypted message text (structure only).
blob = str(cur)
leaked = [s for s in ("first user", "assistant reply", "second user", "Sample conversation") if s in blob]
check("PRIVACY: chat.current result contains NO decrypted message text (structure only)",
      not leaked)
check("PRIVACY: chat.current keys are structure-only {ok, page_id, msg_count}",
      set(cur.keys()) == {"ok", "page_id", "msg_count"})

# ============================================================================================
# (3) NEW CHAT: stamping the pointer to a fresh EMPTY id makes chat.current return that id with
#     msg_count 0 -> continue_chat would fall through to get_latest (NOT resurrect the old chat).
#     Exercised via the REAL worker create op (_chat_create), which stamps the pointer itself.
# ============================================================================================
FRESH = "local-fresh-3"
res_create = store_op("chat", "create_chat", {"id": FRESH, "title": "New chat"})
check("new-chat create op succeeds", res_create.get("ok") and res_create.get("id") == FRESH)
check("new-chat create op STAMPS the pointer to the new empty page id",
      CFG.get_current_chat_page_id() == FRESH)
cur_fresh = store_op("chat", "current")
check("NEW CHAT: chat.current points at the fresh empty chat with msg_count 0",
      cur_fresh.get("page_id") == FRESH and cur_fresh.get("msg_count") == 0)
check("NEW CHAT: msg_count 0 -> Continue falls through (does NOT resurrect the old chat)",
      not (cur_fresh.get("page_id") and cur_fresh.get("msg_count", 0) > 0))

# ============================================================================================
# (4) The worker stamps the pointer AFTER a persisted turn (_view_chat_turn). Point at the fresh
#     empty chat, run one turn through the view applier, and confirm the pointer now resolves to
#     that chat with a non-zero count -> Continue would resume it.
# ============================================================================================
sent = []
WT.token_present = lambda: True
WT.send_message = lambda chat_id, text, reply_markup=None, timeout=15.0: (
    sent.append((chat_id, text)) or {"ok": True, "parts": 1})


async def _fake_anthropic(payload, *, timeout=120.0, max_wait_seconds=24.0):
    return {"content": [{"type": "text", "text": "Done, replying."}], "stop_reason": "end_turn"}


IP.anthropic_messages = _fake_anthropic

# turn on the EMPTY real chat (REAL is archived but still a valid page id) — resume-by-pointer
# does not require the row to be active, so a turn on it re-stamps the pointer to it.
view_op("chat", "chat_turn", {"chat_id": 42, "page_id": REAL, "user_text": "continue"})
check("worker stamps the pointer to the chat it just persisted a turn for",
      CFG.get_current_chat_page_id() == REAL)
cur_after_turn = store_op("chat", "current")
check("after a persisted turn, chat.current resolves to that chat with a NON-zero count",
      cur_after_turn.get("page_id") == REAL and cur_after_turn.get("msg_count") == N + 2)

# ============================================================================================
# (5) Pointer unset -> chat.current is a clean {page_id: None, msg_count: 0} (no crash, no read).
# ============================================================================================
CFG.set_current_chat_page_id("")
cur_none = store_op("chat", "current")
check("pointer unset -> chat.current returns {page_id: None, msg_count: 0}",
      cur_none.get("ok") and cur_none.get("page_id") is None and cur_none.get("msg_count") == 0)

print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
