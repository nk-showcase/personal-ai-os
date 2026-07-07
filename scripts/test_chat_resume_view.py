"""Gate 0 last leak: the "Continue" fallback (last-active-chat leg) runs in the keyed worker.

The receiver used to call load_chat_messages() on the FULL decrypted transcript and display the
personal chat title. This test proves the migrated worker view ("chat","resume") instead:
  - SENDS the resume line itself (title + user-message count) to Telegram, worker-side;
  - returns STRUCTURE ONLY {ok, sent, page_id, msg_count} — no title, no message content;
  - never leaks the decrypted title/transcript through the shared queue.

Also covers the no-chat case (-> {ok, sent:False, empty:True}, nothing sent) and the no-token
case (-> {ok:False, send_unavailable:True}).

Run:
    PYTHONPATH=. .venv/bin/python scripts/test_chat_resume_view.py
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


# ---- throwaway KEK + temp DB harness (clone of scripts/test_continue_chat.py setup) ---------
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ.update({"TELEGRAM_BOT_TOKEN": "dummy:test", "TELEGRAM_OWNER_ID": "1"})
TMP = tempfile.mkdtemp(prefix="chatresume-")
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
from bot import view_render as VR             # noqa: E402
from bot import worker_telegram as WT         # noqa: E402
from bot.aios_store_applier import store_view_applier  # noqa: E402


class _Rec:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


def _pk(domain, op, args):
    return {"op_b64": IP._b64({"domain": domain, "op": op, "args": args})}


def _run(domain, op, args=None):
    """Run a store_view op through the applier, decoded (exactly as the receiver sees it)."""
    return IP.decode_result(store_view_applier(_Rec("store_view", _pk(domain, op, args or {}))))


# ---- capture worker-side Telegram sends (no real network) -----------------------------------
sent = []
WT.token_present = lambda: True
WT.send_message = lambda chat_id, text, reply_markup=None, timeout=15.0: (
    sent.append((chat_id, text)) or {"ok": True, "parts": 1})

# leak canaries: a distinctive title + message content that MUST NOT appear in any result dict
CANARY_TITLE = "CANARY-TITLE-alpha"
CANARY_MSG = "CANARY-BODY-private-conversation"
N = 3  # user messages
MSGS = [
    {"source_block_id": "m1", "role": "user", "position": 0, "content": CANARY_MSG + " 1"},
    {"source_block_id": "m2", "role": "assistant", "position": 1, "content": CANARY_MSG + " a"},
    {"source_block_id": "m3", "role": "user", "position": 2, "content": CANARY_MSG + " 2"},
    {"source_block_id": "m4", "role": "assistant", "position": 3, "content": CANARY_MSG + " b"},
    {"source_block_id": "m5", "role": "user", "position": 4, "content": CANARY_MSG + " 3"},
]
PAGE = "local-resume-1"

# ============================================================================================
# (0) No token -> {ok:False, send_unavailable:True}, nothing sent (checked before seeding).
# ============================================================================================
WT.token_present = lambda: False
res_notoken = _run("chat", "resume", {"chat_id": 42})
check("no token -> {ok:False, send_unavailable:True}",
      res_notoken.get("ok") is False and res_notoken.get("send_unavailable") is True)
check("no token -> nothing sent", len(sent) == 0)
WT.token_present = lambda: True

# ============================================================================================
# (1) No chat yet -> {ok:True, sent:False, empty:True}, nothing sent.
# ============================================================================================
res_empty = _run("chat", "resume", {"chat_id": 42})
check("no chat -> {ok:True, sent:False, empty:True}",
      res_empty.get("ok") is True and res_empty.get("sent") is False
      and res_empty.get("empty") is True)
check("no chat -> nothing sent", len(sent) == 0)

# ============================================================================================
# (2) Seed a chat with N user messages (+ assistant replies) and a canary title. The worker
#     view SENDS exactly one message == render_chat_resume(title, N), returns structure only.
# ============================================================================================
C.save_chat({"source_notion_id": PAGE, "title": CANARY_TITLE, "status": "active",
             "created_time": "2026-07-04T06:00:00.000Z", "messages": MSGS})

res = _run("chat", "resume", {"chat_id": 42})

expected_line = VR.render_chat_resume(CANARY_TITLE, N)
check("SENDS exactly one message", len(sent) == 1)
check("send targets chat 42", len(sent) == 1 and sent[0][0] == 42)
check("sent text == render_chat_resume(title, N)",
      len(sent) == 1 and sent[0][1] == expected_line)
check("resume line embeds the title and user-message count",
      expected_line.startswith("\U0001f47e ") and CANARY_TITLE in expected_line
      and str(N) in expected_line)

# result is STRUCTURE ONLY: exactly {ok, sent, page_id, msg_count}
check("result keys are exactly {ok, sent, page_id, msg_count}",
      set(res.keys()) == {"ok", "sent", "page_id", "msg_count"})
check("result: ok & sent", res.get("ok") is True and res.get("sent") is True)
check("result: page_id is the seeded page", res.get("page_id") == PAGE)
check("result: msg_count == N (user messages only)", res.get("msg_count") == N)

# PRIVACY: neither the title nor any message content is present in the result dict.
blob = str(res)
check("PRIVACY: canary TITLE absent from result", CANARY_TITLE not in blob)
check("PRIVACY: canary MESSAGE content absent from result", CANARY_MSG not in blob)
check("PRIVACY: no 'title' / no message text key in result",
      "title" not in res and "content" not in res and "messages" not in res)

print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
