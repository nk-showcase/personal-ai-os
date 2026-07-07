"""bot/router_transport.py — S6 S-1 unified-router transport catch-all (skeleton).

`route_catch_all` is the default-group catch-all the unified router registers ONLY when
AIOS_UNIFIED_ROUTER is ON (via main.router_handlers()), and even then at group=1 — so it is
structurally shadowed by group-0 handle_text and NEVER intercepts live owner traffic in S-1
(see docs/architecture/s6-router-s1-skeleton.md §0). It is exercised only by the offline test.

It auths (owner-only), builds a structural metadata envelope (NO classification, NO secrets),
and enqueues a 'route' row on the durable task queue. The worker (router_worker) consumes it and
replies via reply_queue. Imported LAZILY (only at flag ON) so the OFF default never pulls
handlers into bot.main's import graph.
"""
from __future__ import annotations

import json

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from . import config, task_queue
from .handlers import is_authorized


async def route_catch_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner text -> durable 'route' row. Mirrors handle_text's auth + empty/command drop
    (handlers.py:1048-1056). Enqueue-only; the reply returns via reply_queue + the wired poller.
    The trailing ApplicationHandlerStop encodes steady-state semantics; in S-1 it is unreachable
    in production (this handler is shadowed at group=1)."""
    if not is_authorized(update):
        return
    msg = update.message
    text = (msg.text if msg else "") or ""
    if not text or text.startswith("/"):
        return
    envelope = {
        "kind": "text",
        "chat_id": update.effective_chat.id,
        "message_id": msg.message_id,
        "text": text,
        # carry the send time so date-aware worker handlers see the real message date
        # at the live flip; run_intent parses it back via datetime.fromisoformat (absent/None -> now).
        "msg_date": (msg.date.isoformat() if msg and msg.date else None),
    }
    payload = json.dumps(envelope, ensure_ascii=False)
    if config.router_encrypt_enabled():
        from . import router_seal  # lazy: OFF path never pulls the seal/age import graph
        task_text = router_seal.seal_inbound(payload)  # base64 age ciphertext; raises -> not enqueued
        mode = "inbound-enc"                            # marks the row as sealed (no schema change)
    else:
        task_text = payload                            # S-1 plaintext, byte-identical
        mode = "inbound"
    task_queue.enqueue_task(
        chat_id=update.effective_chat.id,
        alias="router",
        mode=mode,
        task_text=task_text,
        source="telegram",
    )
    raise ApplicationHandlerStop


async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner button-tap -> answer the spinner + durable kind='callback' route row. The worker
    (router_worker, kind=='callback') routes it by callback_data PREFIX via router_dispatch.run_callback
    to the registered callback handlers. Mirrors route_catch_all's owner-auth + enqueue. Registered
    ONLY at AIOS_ROUTER_DISPATCH ON, via main.router_callback_handlers().

    INERTNESS — and why this is NOT "structurally shadowed" like the text catch-all:
      * Production has AIOS_ROUTER_DISPATCH OFF => main.router_callback_handlers() returns [] => this
        handler is NOT registered => the bot is byte-identical.
      * Even with dispatch ON, NO live worker loop consumes alias='router' 'callback' rows until the
        executor cutover, so the live group-0 handle_callback_query still processes each tap exactly
        once; the enqueued row is inert queue traffic.
      * PTB runs one handler per group and PROCEEDS to the next group unless a handler raises
        ApplicationHandlerStop; the live group-0 handle_callback_query NEVER raises it. So a group-1
        route_callback does NOT shadow it — both fire. CUTOVER PRECONDITION (hard): before any live
        router worker consumes callback rows, the live handler MUST stop dispatching the prefixes the
        worker now owns, so each tap is handled by exactly one path. By design each worker-side view
        is idempotent on a bot-minted id (a queue retry rewrites the SAME row), so a re-tap cannot
        double-apply; single-path routing is enforced regardless as a defence-in-depth invariant.

    owner_id: the worker derives owner_id from chat_id (private-chat-owner — is_authorized requires a
    private chat AND the owner id, so chat_id == owner user id here). The callback handlers never read
    effective_user.id, so the envelope need not carry it (kept minimal, exactly what run_callback consumes).
    """
    if not is_authorized(update):
        return
    query = update.callback_query
    if query is None:                      # CallbackQueryHandler guarantees a query; defensive only
        return
    await query.answer()                   # stop the client spinner (owner-only past the auth gate)
    data = query.data
    if not data:                           # nothing to route; the spinner is already stopped
        return
    msg = query.message
    envelope = {
        "kind": "callback",
        "chat_id": update.effective_chat.id,
        "message_id": msg.message_id if msg else None,
        "callback_data": data,
        "msg_text": (msg.text if msg else None),
        "msg_has_markup": bool(msg is not None and msg.reply_markup is not None),
    }
    payload = json.dumps(envelope, ensure_ascii=False)
    if config.router_encrypt_enabled():
        from . import router_seal          # lazy: OFF path never pulls the seal/age import graph
        task_text = router_seal.seal_inbound(payload)  # base64 age ciphertext; raises -> not enqueued
        mode = "inbound-enc"
    else:
        task_text = payload                            # plaintext, mirrors route_catch_all
        mode = "inbound"
    task_queue.enqueue_task(
        chat_id=update.effective_chat.id,
        alias="router",
        mode=mode,
        task_text=task_text,
        source="telegram",
    )
    raise ApplicationHandlerStop
