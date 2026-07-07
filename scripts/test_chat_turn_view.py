"""Gate 0 slice B4 test: the ENTIRE Claude-chat turn runs in the keyed worker.

anthropic_messages is mocked (no tokens spent, deterministic). Asserts: memory + history
loaded from the ENCRYPTED store, the reply is SENT directly (not returned through the
queue — the op result carries no reply text), the transcript + memory-save persist
locally and encrypted, first-turn titling, and the save_memory tool round-trips. Temp DB
+ throwaway KEK. Run:
    PYTHONPATH=. .venv/bin/python scripts/test_chat_turn_view.py
"""
import hashlib
import os
import shutil
import secrets
import sqlite3
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
os.environ.update({"TELEGRAM_BOT_TOKEN": "dummy:test", "TELEGRAM_OWNER_ID": "1"})
TMP = tempfile.mkdtemp(prefix="chatturn-")
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
from bot import chat_turn as CT               # noqa: E402
from bot import worker_telegram as WT         # noqa: E402
from bot import aios_chat_store as C          # noqa: E402
from bot import aios_memory_store as MM       # noqa: E402
from bot.aios_store_applier import store_view_applier  # noqa: E402


class _Rec:
    def __init__(self, kind, payload):
        self.kind = kind
        self.payload = payload


def _pk(domain, op, args):
    return {"op_b64": IP._b64({"domain": domain, "op": op, "args": args})}


# mock the sender + the Anthropic channel — capture what history/memory the model SEES
sent = []
WT.token_present = lambda: True
WT.send_message = lambda chat_id, text, reply_markup=None, timeout=15.0: (
    sent.append((chat_id, text)) or {"ok": True, "parts": 1})

seen_calls = []


async def _fake_anthropic(payload, *, timeout=120.0, max_wait_seconds=24.0):
    # snapshot messages at call time — run_claude_turn mutates the list across tool rounds
    seen_calls.append({"system": payload["system"], "messages": list(payload["messages"])})
    last_user = payload["messages"][-1]
    # first call: emit a save_memory tool_use; second call (after tool_result): final text
    if isinstance(last_user.get("content"), list) and last_user["content"][0].get("type") == "tool_result":
        return {"content": [{"type": "text", "text": "Saved. What else can I help with?"}],
                "stop_reason": "end_turn"}
    return {"content": [
        {"type": "text", "text": "Got it."},
        {"type": "tool_use", "id": "tu1", "name": "save_memory",
         "input": {"text": "Enjoys walking in the park in the mornings"}},
    ], "stop_reason": "tool_use"}


IP.anthropic_messages = _fake_anthropic

# seed a chat page with one prior turn + one memory fact (both encrypted)
PAGE = "local-chat-1"
C.save_chat({"source_notion_id": PAGE, "title": "Old title", "status": "active",
             "created_time": "2026-07-02T06:00:00.000Z",
             "messages": [{"source_block_id": "b1", "role": "user", "position": 0,
                           "content": "Hi"},
                          {"source_block_id": "b2", "role": "assistant", "position": 1,
                           "content": "Hello!"}]})
MM.save_fact({"source_block_id": "m1", "content": "Prefers the aisle seat", "position": 0,
              "created_time": None})

r = IP.decode_result(store_view_applier(_Rec("store_view", _pk(
    "chat", "chat_turn", {"chat_id": 42, "page_id": PAGE, "user_text": "help me plan"}))))
check("turn sent, structure only (no reply text in result)",
      r.get("ok") and r.get("sent") and not r.get("turn_failed")
      and "Saved" not in str(r) and "plan" not in str(r))
check("reply delivered directly to Telegram", len(sent) == 1 and sent[0][0] == 42
      and sent[0][1] == "Saved. What else can I help with?")

# the model saw the DECRYPTED prior history + memory (loaded worker-side)
first = seen_calls[0]
check("memory injected into system prompt (decrypted worker-side)",
      "Prefers the aisle seat" in first["system"])
check("prior history + new user msg passed to the model",
      [m["content"] for m in first["messages"]] == ["Hi", "Hello!", "help me plan"])

# transcript persisted locally + encrypted; new memory fact saved
msgs = C.load_messages_local(PAGE)
check("transcript appended locally (4 messages now)",
      [m["content"] for m in msgs] == ["Hi", "Hello!", "help me plan",
                                       "Saved. What else can I help with?"])
check("save_memory persisted locally",
      MM.load_memory_local() == "Prefers the aisle seat\nEnjoys walking in the park in the mornings")
db = sqlite3.connect(os.environ["AIOS_DATA_DB"])
nclear = db.execute("SELECT COUNT(*) FROM chat_message WHERE content_encrypted IS NULL").fetchone()[0]
raw = open(os.environ["AIOS_DATA_DB"], "rb").read()
check("new turn encrypted at rest (no plaintext in the DB file)",
      nclear == 0 and "help me plan".encode("utf-8") not in raw)

# NOT first turn (page had prior messages) -> title unchanged
check("title unchanged on a continuing turn",
      C.get_latest_chat_local()["title"] == "Old title")

# first-turn titling: a brand-new empty page gets titled from the first user text
# (long text -> truncated to 50 chars + "...", exercising the ellipsis branch)
C.save_chat({"source_notion_id": "local-chat-2", "title": "New chat", "status": "active",
             "created_time": "2026-07-02T07:00:00.000Z", "messages": []})
LONG = "Tell me in detail about planning a productive week and staying organized"
IP.decode_result(store_view_applier(_Rec("store_view", _pk(
    "chat", "chat_turn", {"chat_id": 42, "page_id": "local-chat-2", "user_text": LONG}))))
check("first turn titles the chat from the user text (truncated + ellipsis)",
      C.get_latest_chat_local()["title"] == LONG[:50] + "..." and len(LONG) > 50)

# Telegram send failure (non-200 -> RuntimeError): the turn now SENDS BEFORE persisting, so the
# error propagates (the queue retries the whole row) and NOTHING was written -> a clean retry with
# no duplicate transcript turn and no repeated memory-save (F1: was persist-then-send, a dup-turn bug).
_msgs_before = len(C.load_messages_local(PAGE))
_mem_before = MM.load_memory_local()
_good_send = WT.send_message


def _boom_send(chat_id, text, reply_markup=None, timeout=15.0):
    raise RuntimeError("telegram send failed: HTTP 429")


WT.send_message = _boom_send
_raised = False
try:
    store_view_applier(_Rec("store_view", _pk(
        "chat", "chat_turn", {"chat_id": 42, "page_id": PAGE, "user_text": "retry me"})))
except RuntimeError:
    _raised = True
WT.send_message = _good_send
check("chat_turn send failure propagates (row retried, not silently ignored)", _raised)
check("chat_turn send failure wrote NOTHING (no dup transcript, no repeated memory-save)",
      len(C.load_messages_local(PAGE)) == _msgs_before and MM.load_memory_local() == _mem_before)

# ---- free-chat turn-timeout fix: on an empty channel response, retry ONCE reusing the SAME
# history (owner re-types nothing); only if the retry ALSO yields nothing show ONE calm line.

# Test A: empty {} on the 1st call, a real text reply on the 2nd -> exactly one retry, the real
# reply is sent (NOT the calm msg), transcript grows by exactly 2 (no duplicate turn), not failed.
_msgs_A = len(C.load_messages_local(PAGE))
_sent_A = len(sent)
_calls_A = len(seen_calls)


async def _fake_empty_then_reply(payload, *, timeout=120.0, max_wait_seconds=24.0):
    seen_calls.append({"system": payload["system"], "messages": list(payload["messages"])})
    if len(seen_calls) - _calls_A == 1:
        return {}  # 1st call: empty channel -> triggers the silent auto-retry
    return {"content": [{"type": "text", "text": "Here is the reply after the retry."}],
            "stop_reason": "end_turn"}


IP.anthropic_messages = _fake_empty_then_reply
rA = IP.decode_result(store_view_applier(_Rec("store_view", _pk(
    "chat", "chat_turn", {"chat_id": 42, "page_id": PAGE, "user_text": "think"}))))
check("retry-then-success: exactly 2 anthropic calls (one retry, not 1, not 3)",
      len(seen_calls) - _calls_A == 2)
check("retry-then-success: exactly ONE new sent entry = the real reply (not the calm msg)",
      len(sent) - _sent_A == 1 and sent[-1] == (42, "Here is the reply after the retry.")
      and sent[-1][1] != CT.CHAT_RETRY_SOFT_MSG)
check("retry-then-success: transcript grew by exactly 2 (no duplicate turn)",
      len(C.load_messages_local(PAGE)) - _msgs_A == 2)
check("retry-then-success: turn_failed False (recovered on the retry)",
      rA.get("ok") and rA.get("sent") and not rA.get("turn_failed"))

# Test B: empty {} on EVERY call -> 2 calls total, exactly one calm-msg send, no scary strings,
# nothing persisted (transcript + memory unchanged), result {ok,sent,turn_failed:True}.
_msgs_B = len(C.load_messages_local(PAGE))
_mem_B = MM.load_memory_local()
_sent_B = len(sent)
_calls_B = len(seen_calls)


async def _fake_always_empty(payload, *, timeout=120.0, max_wait_seconds=24.0):
    seen_calls.append({"system": payload["system"], "messages": list(payload["messages"])})
    return {}


IP.anthropic_messages = _fake_always_empty
rB = IP.decode_result(store_view_applier(_Rec("store_view", _pk(
    "chat", "chat_turn", {"chat_id": 42, "page_id": PAGE, "user_text": "silence"}))))
check("always-empty: exactly 2 anthropic calls total (initial + one retry)",
      len(seen_calls) - _calls_B == 2)
_new_sent_B = [t for _, t in sent[_sent_B:]]
check("always-empty: exactly one sent entry = the calm CHAT_RETRY_SOFT_MSG",
      _new_sent_B == [CT.CHAT_RETRY_SOFT_MSG])
check("always-empty: no scary strings in the sent text",
      not any(s in t for t in _new_sent_B
              for s in ("Error", "too long", "start a new chat", "try again")))
check("always-empty: nothing persisted (transcript + memory unchanged)",
      len(C.load_messages_local(PAGE)) == _msgs_B and MM.load_memory_local() == _mem_B)
check("always-empty: result ok/sent/turn_failed=True",
      rB.get("ok") is True and rB.get("sent") is True and rB.get("turn_failed") is True)

# Test C: content with only a blank text block twice -> run_claude_turn returns the empty-answer
# placeholder both times -> _empty() treats it as empty -> same calm outcome as Test B.
_msgs_C = len(C.load_messages_local(PAGE))
_mem_C = MM.load_memory_local()
_sent_C = len(sent)
_calls_C = len(seen_calls)


async def _fake_blank_text(payload, *, timeout=120.0, max_wait_seconds=24.0):
    seen_calls.append({"system": payload["system"], "messages": list(payload["messages"])})
    # non-empty content, but the only text block is blank -> run_claude_turn -> empty-answer placeholder
    return {"content": [{"type": "text", "text": "   "}], "stop_reason": "end_turn"}


IP.anthropic_messages = _fake_blank_text
rC = IP.decode_result(store_view_applier(_Rec("store_view", _pk(
    "chat", "chat_turn", {"chat_id": 42, "page_id": PAGE, "user_text": "blank"}))))
check("blank-text placeholder: exactly 2 anthropic calls (retry on the empty-answer placeholder)",
      len(seen_calls) - _calls_C == 2)
check("blank-text placeholder: one calm send, nothing persisted, turn_failed=True",
      [t for _, t in sent[_sent_C:]] == [CT.CHAT_RETRY_SOFT_MSG]
      and len(C.load_messages_local(PAGE)) == _msgs_C and MM.load_memory_local() == _mem_C
      and rC.get("turn_failed") is True)

# restore the good Anthropic fake for the remaining blocks
IP.anthropic_messages = _fake_anthropic

# no token -> fall back (bot handles it), never a lost reply
WT.token_present = lambda: False
r = IP.decode_result(store_view_applier(_Rec("store_view", _pk(
    "chat", "chat_turn", {"chat_id": 42, "page_id": PAGE, "user_text": "more"}))))
check("no token -> send_unavailable (bot falls back)",
      r.get("ok") is False and r.get("send_unavailable") is True)

# ---- receiver-side: handle_claude_text reacts to the view_request outcome correctly.
# WRITE_ACCEPTED (worker took the turn, runs >12s, sends the reply itself in 20-40s) must
# show the calm "replying" line — NOT a "busy, try again" line (a retry spawns a SECOND turn:
# second reply + second transcript entry). view_request is mocked to isolate the wiring.
import asyncio  # noqa: E402
from bot import claude_handlers as CH  # noqa: E402
from bot import view_render as VR      # noqa: E402


class _FakeChat:
    def __init__(self, cid):
        self.id = cid

    async def send_action(self, *a, **k):
        pass


class _FakeMsg:
    def __init__(self, text, cid):
        self.text = text
        self.chat = _FakeChat(cid)
        self.replies = []

    async def reply_text(self, text, **k):
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, text, cid):
        self.message = _FakeMsg(text, cid)


class _Ctx:
    def __init__(self):
        self.chat_data = {}


def _fake_view(result):
    async def _f(*a, **k):
        return result
    return _f


def _run_chat(view_result):
    IP.view_request = _fake_view(view_result)
    upd = _FakeUpdate("help me plan", 7)
    ctx = _Ctx()
    ctx.chat_data["claude_state"] = {"active": True, "chat_page_id": "p1", "history": []}
    consumed = asyncio.run(CH.handle_claude_text(upd, ctx))
    return consumed, upd.message.replies


c_acc, r_acc = _run_chat({"ok": False, "accepted": True})
check("chat handler: accepted -> replying line (worker will send; not the busy 'retry' line)",
      c_acc is True and r_acc == [VR.VIEW_ACCEPTED_MESSAGE])
c_sent, r_sent = _run_chat({"ok": True, "sent": True})
check("chat handler: sent -> silence (worker already delivered)", c_sent is True and r_sent == [])
c_un, r_un = _run_chat({"ok": False, "unavailable": True})
check("chat handler: unavailable -> busy message (retry appropriate)",
      c_un is True and r_un == [VR.VIEW_BUSY_MESSAGE])

print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
