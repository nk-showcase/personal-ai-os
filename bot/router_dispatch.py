"""Dispatch adapter: worker-side layer that runs a REAL intent handler against
fake update/context objects, then enqueues the handler's reply via reply_queue.

IMPORT-GRAPH INVARIANT (load-bearing):
  - This module imports `handlers` (-> telegram) and `classifier` (-> get_secret)
    LAZILY, inside functions only. Module-top imports are stdlib + bot.conv_state + reply_queue.
  - This module is imported ONLY worker-side (router_worker) and by scripts/test_router_dispatch.py.
    NOTHING in the transport graph (bot.main, bot.handlers, router_transport) may import it.
    Violating either rule pulls telegram / an Anthropic-key import into the transport process
    and breaks the config-boundary test.

SCOPE: exactly one handler call per dispatched intent. Dispatchable intents are the read-only
task views ('list', 'priority') plus the reference 'notes' demo domain (save_note / get_summary).
No timers, callbacks, photo, multi-intent, or stateful multi-turn flows.
"""
from __future__ import annotations

import copy
import os
from datetime import datetime

from . import conv_state, reply_queue

_TRUE = {"1", "true", "yes", "on"}
# Dispatch flag (default OFF). Lives HERE (worker-side, import-light) rather than in
# bot/config.py: config pulls python-dotenv + resolves the bot token at import, both absent on a
# bare python where the offline router/seal tests run. This module imports only
# conv_state + reply_queue (stdlib-importable), so router_worker stays test-friendly offline.
ROUTER_DISPATCH = (os.getenv("AIOS_ROUTER_DISPATCH") or "").strip().lower() in _TRUE


def dispatch_enabled() -> bool:
    """True only on explicit owner opt-in (AIOS_ROUTER_DISPATCH in {1,true,yes,on}). Read at
    call time so tests/ops flip router_dispatch.ROUTER_DISPATCH without re-import. INDEPENDENT
    of AIOS_UNIFIED_ROUTER / AIOS_ROUTER_ENCRYPT."""
    return ROUTER_DISPATCH


# The intents the dispatcher MAY handle. Anything else falls through to echo. `list` (Todoist task
# list) and `priority` (LLM-ranked task list) are read-only and dispatched whole (each writes only an
# INT-keyed task_map to conv_state for follow-up /done N, no external write). `notes` is the reference
# demo domain: sub-gated to its save_note / get_summary actions against the encrypted notes store.
DISPATCH_INTENTS: frozenset[str] = frozenset({"list", "priority", "notes"})

# The 'notes' demo domain routes exactly these two actions: 'save_note' (write one note to the
# encrypted-at-rest store) and 'get_summary' (read back a value-free count/preview summary). Any
# other action falls through to echo.
_NOTES_ACTIONS: frozenset[str] = frozenset({"save_note", "get_summary"})


class _UnsupportedSurface(AttributeError):
    """Raised when the real handler touches an attribute/kwarg the fakes do not
    model. Fails LOUD so the next intent's needs surface in a test, never silently."""


# --- fake telegram surface -------------------------------------------------------------

class _FakeChat:
    __slots__ = ("id", "type")

    def __init__(self, chat_id: int):
        self.id = chat_id
        # Private-chat-owner assumption: see §6. Revisit before routing any intent that
        # reads effective_user.id or calls is_authorized.
        self.type = "private"


class _FakeUser:
    __slots__ = ("id",)

    def __init__(self, user_id: int):
        self.id = user_id


class _FakeMessage:
    """Captures handler replies into reply_queue, in await order. `date` mirrors the inbound message
    timestamp (date-aware handlers read update.message.date); None -> the
    handler falls back to the worker's own now."""
    __slots__ = ("_chat_id", "date")

    def __init__(self, chat_id: int, date=None):
        self._chat_id = chat_id
        self.date = date

    async def reply_text(self, text, parse_mode=None, **kwargs):
        # SLICE-1: parse_mode is ACCEPTED and DROPPED (Option B -- send plain). Markdown
        # asterisks become literal; see §3 Option A for the reintroduction plan.
        if kwargs:
            # Loud, but name the real cause: reply_markup / disable_web_page_preview / etc.
            # belong to the callback-rows / richer-reply slice, not slice-1.
            raise _UnsupportedSurface(
                f"reply_text kwargs {sorted(kwargs)} require the callback-rows/richer-reply "
                f"slice, not slice-1"
            )
        reply_queue.enqueue_reply(self._chat_id, str(text), source="router")


class _WorkerUpdate:
    __slots__ = ("message", "effective_chat", "effective_user")

    def __init__(self, chat_id: int, owner_id: int, msg_date=None):
        self.message = _FakeMessage(chat_id, msg_date)
        self.effective_chat = _FakeChat(chat_id)
        # NOTE: effective_user.id == owner_id is a private-chat-owner assumption (§6). This is
        # the ONE modelled-but-substituted attribute, so it will NOT fail loud.
        self.effective_user = _FakeUser(owner_id)

    def __getattr__(self, name):
        # Any un-modelled attribute access fails LOUD (surfaces a future intent's needs).
        raise _UnsupportedSurface(f"_WorkerUpdate has no '{name}' (dispatch surface only)")


class _WorkerContext:
    __slots__ = ("_chat_data", "_user_data")

    def __init__(self, chat_data: dict, user_data: dict | None = None):
        self._chat_data = chat_data
        # user_data is PER-USER (distinct from per-chat chat_data). Dispatched read subs only READ it
        # (pre-checks return falsy on a fresh dict),
        # so it is a fresh {} and NOT persisted. A sub that WRITES user_data is a later (callback/
        # stateful) slice; the sub-gate keeps those out of this slice.
        self._user_data = {} if user_data is None else user_data

    @property
    def chat_data(self):
        return self._chat_data

    @chat_data.setter
    def chat_data(self, _value):
        # Mutate-in-place is the contract. A handler that REASSIGNS chat_data would otherwise
        # silently desync from what we persist -- fail loud instead.
        raise _UnsupportedSurface(
            "context.chat_data reassignment is unsupported; mutate the dict in place"
        )

    @property
    def user_data(self):
        return self._user_data

    @user_data.setter
    def user_data(self, _value):
        raise _UnsupportedSurface(
            "context.user_data reassignment is unsupported; mutate the dict in place"
        )

    def __getattr__(self, name):
        raise _UnsupportedSurface(f"_WorkerContext has no '{name}' (dispatch surface only)")


# --- the dispatch entrypoint ----------------------------------------------------------

async def run_intent(envelope: dict, *, owner_id: int) -> bool:
    """Dispatch a SINGLE classifier-merged envelope to its real handler.

    `envelope` MUST be a classifier-merged envelope: transport fields
    {kind, chat_id, message_id, text} PLUS the classifier result fields (it MUST carry
    `intent`, plus the handler's inputs e.g. `query`). The runner (router_worker) is the
    SOLE gate; this function trusts the already-decided intent and fails LOUD if the merge
    was skipped (rather than silently returning False).

    Returns True if it dispatched a handler, False otherwise.
    conv_state is persisted ATOMICALLY ON SUCCESS ONLY, and only if it actually changed.
    On handler exception, conv_state is NOT persisted (avoids a partial-write desync).
    """
    # The runner owns the gate; here we only assert the merge happened, so a caller that
    # forgot to merge fails loud instead of silently dropping the intent.
    if "intent" not in envelope:
        raise _UnsupportedSurface(
            "run_intent requires a classifier-merged envelope (missing 'intent')"
        )
    intent = envelope["intent"]
    if intent not in DISPATCH_INTENTS:
        return False
    # notes is sub-gated to its two demo actions; anything else falls through to echo.
    if intent == "notes" and envelope.get("action") not in _NOTES_ACTIONS:
        return False

    chat_id = int(envelope["chat_id"])
    # carry the inbound message timestamp (ISO in the envelope) so date-aware handlers
    # see update.message.date; absent/blank -> None -> the handler falls back to the worker's own now.
    _raw_date = envelope.get("msg_date")
    _msg_date = datetime.fromisoformat(_raw_date) if isinstance(_raw_date, str) and _raw_date else None
    update = _WorkerUpdate(chat_id, owner_id, _msg_date)
    chat_data = conv_state.load_state(chat_id)       # fresh {} when no row
    before = copy.deepcopy(chat_data)                # snapshot for change-detection
    context = _WorkerContext(chat_data)

    if intent == "list":
        from . import handlers as _handlers          # LAZY: worker-side only; (update, context, result)
        await _handlers._handle_list(update, context, envelope)
    elif intent == "priority":
        from . import handlers as _handlers          # LAZY: worker-side only; LLM-ranked read
        await _handlers._handle_priority(update, context, envelope)
    elif intent == "notes":
        from . import handlers as _handlers          # LAZY: worker-side only; encrypted notes demo
        await _handlers._handle_notes(update, context, envelope)
    else:  # pragma: no cover - DISPATCH_INTENTS guards this
        raise _UnsupportedSurface(f"no dispatch for intent '{intent}'")

    # Atomic-on-success: persist only if the handler actually changed chat_data, and never
    # fabricate an empty row for a chat that had none. Read back the LIVE reference so a
    # (now-forbidden) reassignment could not strand a stale local.
    live = context.chat_data
    if live != before and not (live == {} and before == {}):
        conv_state.save_state(chat_id, live)
    return True


# --- callback (button-tap) dispatch surface + entrypoint -------------------------------------
# Mirrors the text path above for kind='callback' envelopes. INERT: CALLBACK_HANDLERS is empty
# until callback handlers are registered, so run_callback returns False and
# the worker falls through to its existing echo path (zero live change).

class _FakeCallbackMessage:
    """The message a tapped button is attached to. Its text/caption/reply_markup come from the
    transport callback envelope (the worker has NO live Telegram Message). reply_text sends a NEW
    message (text -> enqueue_reply, text+keyboard -> enqueue_reply_kb, source='router')."""
    __slots__ = ("_chat_id", "message_id", "text", "caption", "reply_markup")

    def __init__(self, chat_id: int, message_id, text, has_markup: bool):
        self._chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.caption = None
        # a TRUTHY sentinel iff the tapped message carried a keyboard (strip logic checks `is not None`)
        self.reply_markup = object() if has_markup else None

    async def reply_text(self, text, parse_mode=None, reply_markup=None, **kwargs):
        if kwargs:
            raise _UnsupportedSurface(f"reply_text kwargs {sorted(kwargs)} not modelled on the callback surface")
        if reply_markup is None:
            reply_queue.enqueue_reply(self._chat_id, str(text), source="router")
        else:
            reply_queue.enqueue_reply_kb(self._chat_id, str(text), reply_markup, source="router")

    def __getattr__(self, name):
        raise _UnsupportedSurface(f"_FakeCallbackMessage has no '{name}' (callback dispatch surface only)")


class _FakeCallbackQuery:
    """Worker-side stand-in for a tapped InlineKeyboard button's CallbackQuery. The transport already
    answer()ed the real query (stopping the spinner) when it enqueued the tap, so .answer() is a no-op.
    Edits target message_id via reply_queue: edit_message_text -> enqueue_edit (no kb) / enqueue_edit_kb
    (with kb); edit_message_reply_markup keeps the (envelope-supplied) text and just swaps the keyboard.
    Keyboards passed in are PLAIN [[ {text, callback_data}, ... ]] dicts (worker imports no telegram type)."""
    __slots__ = ("data", "message", "_chat_id")

    def __init__(self, data: str, chat_id: int, message_id=None, message_text=None, has_markup: bool = False):
        self.data = data
        self._chat_id = chat_id
        self.message = _FakeCallbackMessage(chat_id, message_id, message_text, has_markup)

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
        if kwargs:
            raise _UnsupportedSurface(f"edit_message_text kwargs {sorted(kwargs)} not modelled")
        mid = self.message.message_id
        if reply_markup is None:
            reply_queue.enqueue_edit(self._chat_id, str(text), mid, source="router")
        else:
            reply_queue.enqueue_edit_kb(self._chat_id, str(text), mid, reply_markup, source="router")

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        if kwargs:
            raise _UnsupportedSurface(f"edit_message_reply_markup kwargs {sorted(kwargs)} not modelled")
        mid = self.message.message_id
        keep = str(self.message.text or "")            # keep the existing text; only the keyboard changes
        if reply_markup is None:
            reply_queue.enqueue_edit(self._chat_id, keep, mid, source="router")
        else:
            reply_queue.enqueue_edit_kb(self._chat_id, keep, mid, reply_markup, source="router")

    def __getattr__(self, name):
        raise _UnsupportedSurface(f"_FakeCallbackQuery has no '{name}' (callback dispatch surface only)")


class _WorkerCallbackUpdate:
    __slots__ = ("callback_query", "effective_chat", "effective_user")

    def __init__(self, data: str, chat_id: int, owner_id: int,
                 message_id=None, message_text=None, has_markup: bool = False):
        self.callback_query = _FakeCallbackQuery(data, chat_id, message_id, message_text, has_markup)
        self.effective_chat = _FakeChat(chat_id)
        self.effective_user = _FakeUser(owner_id)

    def __getattr__(self, name):
        raise _UnsupportedSurface(f"_WorkerCallbackUpdate has no '{name}' (callback dispatch surface only)")


# callback_data PREFIX -> async handler(update, context). Empty until handlers are ported;
# registering a prefix is what makes run_callback dispatch that family of taps.
CALLBACK_HANDLERS: dict = {}


async def run_callback(envelope: dict, *, owner_id: int) -> bool:
    """Dispatch a SINGLE inbound button tap to its real handler, mirroring run_intent.

    `envelope` is a transport CALLBACK envelope: {kind:'callback', chat_id, message_id,
    callback_data}. Routes by callback_data PREFIX (CALLBACK_HANDLERS). Returns True iff a handler
    ran, False if no prefix matches (caller falls through -- never a silent True). conv_state is
    persisted ATOMICALLY ON SUCCESS ONLY and only if chat_data actually changed; a handler exception
    propagates with NO partial persist (same contract as run_intent)."""
    if envelope.get("kind") != "callback":
        raise _UnsupportedSurface("run_callback requires a callback envelope (kind != 'callback')")
    data = envelope.get("callback_data")
    if not isinstance(data, str):
        raise _UnsupportedSurface("callback envelope missing a string 'callback_data'")
    handler = next((h for pfx, h in CALLBACK_HANDLERS.items() if data.startswith(pfx)), None)
    if handler is None:
        return False

    chat_id = int(envelope["chat_id"])
    # The transport ships the tapped message's id/text + whether it had a keyboard, so the worker-side
    # strip/edit logic (which the live handler reads off query.message) works with no live Message.
    update = _WorkerCallbackUpdate(
        data, chat_id, owner_id,
        message_id=envelope.get("message_id"),
        message_text=envelope.get("msg_text"),
        has_markup=bool(envelope.get("msg_has_markup", False)),
    )
    chat_data = conv_state.load_state(chat_id)
    before = copy.deepcopy(chat_data)
    context = _WorkerContext(chat_data)

    # A handler may DECLINE a tap it was prefix-routed but does not own by returning False -> the
    # caller falls through, exactly like the in-process callback handler's `return False`.
    # Any other return (None/True) = handled.
    if await handler(update, context) is False:
        return False

    live = context.chat_data
    if live != before and not (live == {} and before == {}):
        conv_state.save_state(chat_id, live)
    return True
