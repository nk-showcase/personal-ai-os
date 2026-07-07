"""Test S6 step b: the TRANSPORT callback producer (router_transport.route_callback) +
its registration (main.router_callback_handlers). Mirrors test_router_callback.py (worker side) and
test_router_worker.py (registration), but for the PRODUCER half: owner-auth, answer-the-spinner,
build the exact kind='callback' envelope, and enqueue an alias='router' row.

Platform: the project venv (py3.12) — route_callback imports bot.config (router_encrypt_enabled) and
bot.handlers.is_authorized (-> telegram). Run:
  cd ~/apps/ChatBot && PYTHONPATH=. .venv/bin/python scripts/test_router_callback_producer.py

Offline + key-free: temp queue DB, dummy non-secret token env, encryption OFF (the ON branch is proven
with a STUBBED seal — a real age round-trip stays in test_router_seal.py on the mac).
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.getcwd())
_d = tempfile.mkdtemp(prefix="aios_cbprod_")
DB = os.path.join(_d, "q.sqlite3")
os.environ["AIOS_TASK_QUEUE_DB"] = DB
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:dummy")
os.environ["TELEGRAM_OWNER_ID"] = "4242"          # set BEFORE importing config/handlers
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ.pop("AIOS_ROUTER_ENCRYPT", None)
os.environ.pop("AIOS_ROUTER_DISPATCH", None)

OWNER = 4242

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler  # noqa: E402
from bot import config, task_queue, router_transport  # noqa: E402
import bot.router_seal as router_seal  # noqa: E402

config.ROUTER_ENCRYPT = False  # explicit: OFF unless a case flips it
task_queue.list_updates()      # materialize the schema (tasks table) before the first direct DB read


# --- fake telegram surface (only what is_authorized + route_callback read) ---
class FakeChat:
    def __init__(self, chat_id, chat_type="private"):
        self.id = chat_id
        self.type = chat_type


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeMessage:
    def __init__(self, message_id=7, text=None, reply_markup=None):
        self.message_id = message_id
        self.text = text
        self.reply_markup = reply_markup


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers = 0          # COUNTER — the only way to observe answer() firing

    async def answer(self, *a, **k):
        self.answers += 1


class FakeUpdate:
    # route_callback reads query.message, never update.message -> keep None so an accidental
    # update.message access would fail loud.
    message = None

    def __init__(self, query, chat_id, user_id, chat_type="private"):
        self.callback_query = query
        self.effective_chat = FakeChat(chat_id, chat_type)
        self.effective_user = FakeUser(user_id)


def _router_rows(chat_id):
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT id, alias, mode, task_text FROM tasks WHERE chat_id=? AND alias='router' ORDER BY id",
            (chat_id,)).fetchall()]


def drive(update):
    """Run route_callback; report whether it reached the enqueue path (raises ApplicationHandlerStop)
    or early-returned."""
    try:
        asyncio.run(router_transport.route_callback(update, None))
        return "returned"
    except ApplicationHandlerStop:
        return "stopped"


# (a) NON-OWNER tap: auth-drop BEFORE answer -> 0 answers, 0 rows, early return
q_a = FakeQuery("note_add_2026-06-24", FakeMessage(message_id=11, text="t", reply_markup=object()))
res_a = drive(FakeUpdate(q_a, chat_id=9999, user_id=9999))
check("(a) non-owner: early return, answer() NOT called, no router row",
      res_a == "returned" and q_a.answers == 0 and _router_rows(9999) == [])

# (b) OWNER happy path WITH keyboard: 1 answer, exactly 1 row, envelope decoded field-by-field
msg_b = FakeMessage(message_id=55, text="Add this note?", reply_markup=object())
q_b = FakeQuery("note_add_2026-06-24", msg_b)
res_b = drive(FakeUpdate(q_b, chat_id=OWNER, user_id=OWNER))
rows_b = _router_rows(OWNER)
env_b = json.loads(rows_b[0]["task_text"]) if rows_b else {}
check("(b) owner tap reached enqueue (ApplicationHandlerStop) + answer() fired once",
      res_b == "stopped" and q_b.answers == 1)
check("(b) exactly one alias='router' row, mode='inbound'",
      len(rows_b) == 1 and rows_b[0]["alias"] == "router" and rows_b[0]["mode"] == "inbound")
check("(b) envelope fields exact (kind/chat_id/message_id/callback_data/msg_text/msg_has_markup)",
      env_b.get("kind") == "callback"
      and env_b.get("chat_id") == OWNER
      and env_b.get("message_id") == 55
      and env_b.get("callback_data") == "note_add_2026-06-24"
      and env_b.get("msg_text") == "Add this note?"
      and env_b.get("msg_has_markup") is True)

# (c) OWNER tap, query.message is None (no tapped message): message_id/msg_text None, has_markup False
q_c = FakeQuery("note_tag_work", None)
res_c = drive(FakeUpdate(q_c, chat_id=4243, user_id=OWNER))   # owner in another private chat id
rows_c = _router_rows(4243)
env_c = json.loads(rows_c[0]["task_text"]) if rows_c else {}
check("(c) owner tap with no message: one row, message_id/msg_text None, msg_has_markup False",
      res_c == "stopped" and q_c.answers == 1 and len(rows_c) == 1
      and env_c.get("message_id") is None and env_c.get("msg_text") is None
      and env_c.get("msg_has_markup") is False
      and env_c.get("callback_data") == "note_tag_work")

# (d) OWNER tap, EMPTY callback_data: answered, then dropped (nothing to route) -> 1 answer, 0 rows
q_d = FakeQuery("", FakeMessage(message_id=8, text="x", reply_markup=object()))
res_d = drive(FakeUpdate(q_d, chat_id=4244, user_id=OWNER))
check("(d) empty callback_data: answered once, early return, no router row",
      res_d == "returned" and q_d.answers == 1 and _router_rows(4244) == [])

# (e) OWNER tap, no keyboard on the tapped message: msg_has_markup False (reply_markup None)
q_e = FakeQuery("note_tag_personal", FakeMessage(message_id=70, text="hi", reply_markup=None))
res_e = drive(FakeUpdate(q_e, chat_id=4245, user_id=OWNER))
rows_e = _router_rows(4245)
env_e = json.loads(rows_e[0]["task_text"]) if rows_e else {}
check("(e) tapped message without keyboard -> msg_has_markup False",
      res_e == "stopped" and len(rows_e) == 1 and env_e.get("msg_has_markup") is False
      and env_e.get("msg_text") == "hi")

# (h) OWNER taps carrying neutral note prefixes: each is a non-empty owner tap, so route_callback
#     answers the spinner and enqueues exactly one router row (the worker routes it by prefix).
for _i, _pfx in enumerate(("note_add_2026-06-24",
                           "note_tag_home",
                           "note_add_quick")):
    _cid = 4250 + _i
    q_h = FakeQuery(_pfx, FakeMessage(message_id=71, text="pick", reply_markup=object()))
    res_h = drive(FakeUpdate(q_h, chat_id=_cid, user_id=OWNER))
    check(f"(h) note tap '{_pfx}' -> stopped, answered, exactly one router row",
          res_h == "stopped" and q_h.answers == 1 and len(_router_rows(_cid)) == 1)

# (f) ENCRYPT-ON via STUBBED seal: mode='inbound-enc', task_text == sealed sentinel (no real age round-trip)
SENTINEL = "SEALED_STUB_BLOB_b64=="
_captured = {}
_orig_seal = router_seal.seal_inbound


def _stub_seal(payload, *a, **k):
    _captured["payload"] = payload
    return SENTINEL


router_seal.seal_inbound = _stub_seal
config.ROUTER_ENCRYPT = True
try:
    q_f = FakeQuery("note_add_2026-06-24", FakeMessage(message_id=99, text="enc", reply_markup=object()))
    res_f = drive(FakeUpdate(q_f, chat_id=4246, user_id=OWNER))
    rows_f = _router_rows(4246)
    payload_env = json.loads(_captured.get("payload", "{}"))
    check("(f) encrypt ON: one row, mode='inbound-enc', task_text is the sealed blob, payload was the envelope",
          res_f == "stopped" and len(rows_f) == 1 and rows_f[0]["mode"] == "inbound-enc"
          and rows_f[0]["task_text"] == SENTINEL
          and payload_env.get("kind") == "callback" and payload_env.get("callback_data") == "note_add_2026-06-24")
finally:
    router_seal.seal_inbound = _orig_seal
    config.ROUTER_ENCRYPT = False

# (g) REGISTRATION via bot.main: gated on AIOS_ROUTER_DISPATCH, group=1, callback is route_callback.
_main = None
try:
    from bot import main as _main
except Exception as e:  # pragma: no cover — only on an env without telegram/dotenv
    print("NOTE: bot.main not importable here (%s) — skipping registration checks" % type(e).__name__)
if _main is not None:
    from bot import router_dispatch
    _saved = router_dispatch.ROUTER_DISPATCH
    config.UNIFIED_ROUTER = False     # keep the text catch-all out so it cannot confuse the count
    try:
        router_dispatch.ROUTER_DISPATCH = False
        check("(g) router_callback_handlers() == [] at AIOS_ROUTER_DISPATCH OFF",
              _main.router_callback_handlers() == [])
        check("(g) text router_handlers() still [] (independent of the dispatch flag)",
              _main.router_handlers() == [])
        router_dispatch.ROUTER_DISPATCH = True
        ch = _main.router_callback_handlers()
        ok_one = len(ch) == 1
        h, g = ch[0] if ok_one else (None, None)
        check("(g) exactly one callback handler at ON, group=1, CallbackQueryHandler, callback is route_callback",
              ok_one and g == 1 and isinstance(h, CallbackQueryHandler)
              and getattr(h, "callback", None) is router_transport.route_callback)
    finally:
        router_dispatch.ROUTER_DISPATCH = _saved

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
