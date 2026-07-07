"""Claude Chat: Opus 4.7 + extended thinking + persistent memory via Telegram."""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from . import claude_storage
from .chat_turn import MAX_HISTORY

logger = logging.getLogger(__name__)


def _get_state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if "claude_state" not in context.chat_data:
        context.chat_data["claude_state"] = {
            "active": False,
            "chat_page_id": None,
            "history": [],
        }
    return context.chat_data["claude_state"]


async def start_new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a new chat and enter Claude mode."""
    state = _get_state(context)

    # Archive previous active chat
    old_page_id = state.get("chat_page_id")
    if old_page_id:
        await claude_storage.archive_chat(old_page_id)

    page_id = await claude_storage.create_chat("New chat")

    state["active"] = True
    state["chat_page_id"] = page_id
    state["history"] = []

    await update.message.reply_text("\U0001f47e New chat. Go ahead.")


async def continue_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Continue the last chat, or create new if none exists.

    The real source of truth is the durable current-chat pointer written by the keyed worker
    (which owns the transcript since the Gate-0 migration). The RAM fast-path below is effectively
    dead — after Gate 0 the receiver never populates state["history"] — so we do NOT rely on it;
    the pointer path is the fix. The worker owns the transcript, so we resume by page id WITHOUT
    loading any history into the receiver (history stays [] — the worker rebuilds it each turn)."""
    state = _get_state(context)

    # Already have an active chat with history in RAM - just resume (dead post-Gate-0, kept as a
    # harmless fast-path; the pointer path below is what actually resumes the last chat).
    if state.get("chat_page_id") and state.get("history"):
        state["active"] = True
        n = len([m for m in state["history"] if m["role"] == "user"])
        await update.message.reply_text(f"\U0001f47e Resuming ({n} msg.)")
        return

    # Durable pointer: resume the owner's genuinely-last chat by page id (works even when that
    # chat is archived — get_latest_chat, filtering active-only, would miss it). Structure only:
    # {page_id, msg_count}; NO decrypted transcript reaches the receiver.
    cur = await claude_storage.get_current_chat()
    if cur and cur.get("page_id") and cur.get("msg_count", 0) > 0:
        state["active"] = True
        state["chat_page_id"] = cur["page_id"]
        state["history"] = []  # worker owns history; it rebuilds from the store each turn
        await update.message.reply_text(f"\U0001f47e Resuming ({cur['msg_count']} msg.)")
        return

    # Fallback: latest active chat (pre-pointer chats, or pointer at a fresh empty new-chat).
    # The keyed worker resolves the newest active chat, SENDS the resume line itself, and returns
    # STRUCTURE ONLY (page_id + msg_count) — no title, no transcript. This is what closes the last
    # read-back leak: the receiver no longer calls load_chat_messages nor sees the personal title.
    from .integrations_proxy import view_request
    from . import view_render
    res = await view_request("chat", "resume", {"chat_id": update.effective_chat.id})
    if res.get("empty"):
        await start_new_chat(update, context)
        return
    if res.get("ok") and res.get("sent") and res.get("page_id"):
        state["active"] = True
        state["chat_page_id"] = res["page_id"]
        state["history"] = []   # worker owns history; it sent the resume line itself
        return
    # unavailable / accepted -> neutral busy message, do NOT load anything into the receiver
    reply = view_render.view_reply_for(res)
    if reply:
        await update.message.reply_text(reply)


async def stop_claude(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Exit Claude chat mode. Returns True if was active."""
    state = context.chat_data.get("claude_state")
    if state and state.get("active"):
        state["active"] = False
        return True
    return False


def is_claude_active(context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.chat_data.get("claude_state")
    return bool(state and state.get("active"))


async def _worker_chat_turn(update_or_query, context, page_id: str, text: str) -> dict:
    """SINGLE conversation route: the ENTIRE turn (memory, history, model call, reply) runs in
    the keyed worker, which sends to Telegram itself — decrypted content never passes through the
    receiver. Returns the view_request result (dict); the caller decides what to show via
    view_render.view_reply_for: sent -> stay silent; accepted -> "responding" (the worker
    delivers it later); unavailable -> "busy". Accepts an Update or a CallbackQuery."""
    msg = getattr(update_or_query, "message", None)
    chat = getattr(msg, "chat", None)
    if chat is None:
        return {"ok": False, "unavailable": True}
    from .integrations_proxy import view_request
    try:
        await chat.send_action("typing")
    except Exception:
        pass
    return await view_request("chat", "chat_turn",
                              {"chat_id": chat.id, "page_id": page_id, "user_text": text})


async def handle_claude_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Handle text while in Claude chat mode. Returns True if consumed.

    SINGLE safe route: the whole conversation turn runs in the worker; the receiver never assembles
    the reply itself. Worker unavailable -> a clear "busy" message (not a silent failure)."""
    state = context.chat_data.get("claude_state")
    if not state or not state.get("active"):
        return False

    text = update.message.text.strip()
    if not text:
        return False

    from . import view_render
    page_id = state.get("chat_page_id")
    if page_id:
        res = await _worker_chat_turn(update, context, page_id, text)
    else:
        res = {"ok": False, "unavailable": True}
    reply = view_render.view_reply_for(res)
    if reply:
        await update.message.reply_text(reply)
    return True
