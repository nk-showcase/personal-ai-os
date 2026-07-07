"""Test S6 CRIT-A: the pc_tasks/laptop preflight lifted into handlers.preflight_pc_task is
behavior-preserving. Proves the contract handle_text relies on: returns True (handled ->
caller short-circuits) on a regex/laptop trigger, False (fall-through) otherwise; pops the
one-shot laptop_mode state BEFORE the submit await; forwards content UNSTRIPPED to submit_pc_task;
sends the exact three replies keyed on TOO_LONG / truthy rel_path / None; and emits the
unconditional preflight log line on every call (even fall-through).

Platform: the project venv (py3.12) — importing bot.handlers pulls telegram + config.
Run:  cd ${AIOS_HOME}/apps/ChatBot && PYTHONPATH=. .venv/bin/python scripts/test_s6_crit_a_preflight.py

Offline + key-free: submit_pc_task + is_pc_task are stubbed; no git/network/GitHub token.
"""
import asyncio
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["AIOS_SECRETS_BACKEND"] = "env"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:dummy")
os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
os.environ["AIOS_TASK_QUEUE_DB"] = os.path.join(tempfile.mkdtemp(prefix="aios_crita_"), "q.sqlite3")

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


from bot import handlers, pc_tasks, claude_bridge  # noqa: E402

# --- capture the bot.handlers logger so we can assert the unconditional preflight log line ---
_records = []


class _ListHandler(logging.Handler):
    def emit(self, record):
        _records.append(record.getMessage() if record.args is None else (record.msg % record.args))


handlers.logger.addHandler(_ListHandler())
handlers.logger.setLevel(logging.INFO)

# --- stub the pc_tasks surface preflight_pc_task uses (same module object via lazy import) ---
_submit_calls = []
_submit_ret = {"v": None}


async def _fake_submit(content, chat_id):
    _submit_calls.append((content, chat_id))
    return _submit_ret["v"]


pc_tasks.submit_pc_task = _fake_submit
_regex = {"v": False}
pc_tasks.is_pc_task = lambda content: _regex["v"]


class _Msg:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _Upd:
    def __init__(self, chat_id):
        self.message = _Msg(chat_id)


def reset(chat_id):
    _submit_calls.clear()
    _records.clear()
    claude_bridge._pending_task_flow.pop(chat_id, None)


def run(chat_id, content):
    upd = _Upd(chat_id)
    handled = asyncio.run(handlers.preflight_pc_task(upd, content))
    return handled, upd.message.replies


# (1) REGEX MATCH -> rel_path: True, queued-OK reply with rel in backticks, submit called w/ FULL content, no pop
reset(101)
_regex["v"] = True
_submit_ret["v"] = "_pc_tasks/abc.md"
h, rep = run(101, "on pc do X")
check("(1) regex match: returns True, one queued-OK reply naming the rel_path in backticks",
      h is True and len(rep) == 1 and "queued" in rep[0] and "`_pc_tasks/abc.md`" in rep[0])
check("(1) submit called once with the FULL unstripped content; no laptop_mode pop happened",
      _submit_calls == [("on pc do X", 101)] and 101 not in claude_bridge._pending_task_flow)

# (2) LAPTOP_MODE, no regex: True, state POPPED, submit called, queued-OK reply
reset(102)
_regex["v"] = False
_submit_ret["v"] = "_pc_tasks/x.md"
claude_bridge._pending_task_flow[102] = {"laptop_mode": True}
h, rep = run(102, "just some text that is not a command")
check("(2) laptop_mode (no regex): returns True, laptop_mode state consumed (popped), one reply",
      h is True and 102 not in claude_bridge._pending_task_flow
      and _submit_calls == [("just some text that is not a command", 102)] and len(rep) == 1)

# (3) TOO_LONG sentinel -> long-command warning
reset(103)
_regex["v"] = True
_submit_ret["v"] = "TOO_LONG"
h, rep = run(103, "on pc a very long command")
check("(3) TOO_LONG: returns True, the 'too long' warning reply",
      h is True and len(rep) == 1 and "too long" in rep[0])

# (4) failure (None) -> exact failure reply
reset(104)
_regex["v"] = True
_submit_ret["v"] = None
h, rep = run(104, "on pc X")
check("(4) submit None: returns True, exact failure reply",
      h is True and rep == ["Could not write the command to the queue. Check the platform logs."])

# (5) ORDER-ON-FAILURE: laptop_mode + submit None -> state popped DESPITE failure (pop-before-await)
reset(105)
_regex["v"] = False
_submit_ret["v"] = None
claude_bridge._pending_task_flow[105] = {"laptop_mode": True}
h, rep = run(105, "something")
check("(5) pop-before-await preserved: laptop_mode popped even though submit failed",
      h is True and 105 not in claude_bridge._pending_task_flow and _submit_calls == [("something", 105)])

# (6) FALL-THROUGH: no laptop_mode, no regex -> False, submit NOT called, no replies, BUT preflight log fired
reset(106)
_regex["v"] = False
_submit_ret["v"] = "should-not-be-used"
h, rep = run(106, "an ordinary message")
check("(6) fall-through: returns False, submit NOT called, zero replies",
      h is False and _submit_calls == [] and rep == [])
check("(6) the unconditional preflight log line fired even on fall-through",
      any("handle_text preflight: chat=106" in m for m in _records))

# (7) DOUBLE-TRIGGER: laptop_mode AND regex -> popped, submit called with full content, True
reset(107)
_regex["v"] = True
_submit_ret["v"] = "_pc_tasks/d.md"
claude_bridge._pending_task_flow[107] = {"laptop_mode": True}
h, rep = run(107, "on pc double trigger")
check("(7) double-trigger: popped, submit called with full content, returns True",
      h is True and 107 not in claude_bridge._pending_task_flow
      and _submit_calls == [("on pc double trigger", 107)] and len(rep) == 1)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
