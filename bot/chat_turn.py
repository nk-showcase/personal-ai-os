"""bot/chat_turn.py — PURE Claude-chat turn logic shared by the worker view (Gate 0)
and the bot fallback. No telegram imports.

The Anthropic call goes through integrations_proxy.anthropic_messages: in the KEYED
worker that is a direct call (the worker holds ANTHROPIC_API_KEY), so when the whole
turn runs in the worker the assistant reply, loaded memory and history never return
through the shared queue. run_claude_turn is pure w.r.t. storage — it returns the reply
text plus any memory facts the model asked to save; the caller persists them (locally in
the worker view, or via claude_storage in the bot fallback) — parity by construction.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("aios.chat_turn")

MODEL = "claude-opus-4-8"
MAX_TOKENS = 16000
MAX_HISTORY = 40  # individual messages (user + assistant)

SAVE_MEMORY_TOOL = {
    "name": "save_memory",
    "description": (
        "Save important information about the user to persistent memory. "
        "Use when you learn something new and important about the user: "
        "preferences, habits, context, work, relationships, or "
        "when they explicitly ask to remember something. "
        "Be concise - save facts, not conversations."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "Concise fact or preference to remember"}
        },
        "required": ["text"],
    },
}

SYSTEM_PROMPT = """\
You are a personal AI assistant in a Telegram chat. You have persistent memory about the user.

Style:
- Be direct and concise. No filler, no "Great question!", no unnecessary lists.
- Short paragraphs. Telegram messages have a 4096 char limit.
- Respond in the same language the user writes in.
- No emoji unless the user uses them.
- Lead with the answer, then explain if needed.
- If you can say it in one sentence, don't use three.

Memory:
- You have a save_memory tool. Use it to remember important facts about the user.
- Save preferences, habits, context, relationships, work details.
- Don't save trivial or temporary things.
- Don't save duplicates - check current memory first.

Current memory:
{memory}"""


class TurnTimeout(RuntimeError):
    """The channel returned no content in the allotted time."""


# Shown to the operator ONLY when a free-chat turn yields nothing TWICE (after one silent
# auto-retry that re-uses the same history — nothing is re-typed). Calm: no "Error:",
# no "conversation too long", no "start a new chat", no "try again".
CHAT_RETRY_SOFT_MSG = "\U0001faa9 Still thinking, one moment…"

# The placeholder run_claude_turn returns when the model produced no usable text (shared with
# the worker's empty-turn detection in aios_store_applier so the two never silently desync).
EMPTY_REPLY = "(empty reply)"


def build_system(memory: str) -> str:
    return SYSTEM_PROMPT.format(memory=memory if memory else "(empty)")


async def run_claude_turn(history: list, memory: str) -> dict:
    """Run one chat turn (adaptive thinking + the save_memory tool, up to 3 tool rounds).
    `history` already includes the new user message as its last item. Returns
    {"reply": str, "memory_saves": [str]} — pure w.r.t. storage; the caller persists.
    Raises TurnTimeout when the channel yields nothing (caller shows a friendly error)."""
    from .integrations_proxy import anthropic_messages

    system = build_system(memory)
    messages = list(history)
    memory_saves: list[str] = []
    text_reply = ""
    for _ in range(3):
        payload = {
            "model": MODEL,
            "max_tokens": MAX_TOKENS,
            "thinking": {"type": "adaptive"},
            "tools": [SAVE_MEMORY_TOOL],
            "system": system,
            "messages": messages,
        }
        resp = await anthropic_messages(payload, timeout=180, max_wait_seconds=180)
        content = resp.get("content") or []
        if not content:
            raise TurnTimeout(
                "could not reply in the allotted time — the conversation may have grown too "
                "long. Try again or start a new chat."
            )
        text_reply = ""
        tool_calls = []
        for block in content:
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                text_reply = block["text"].strip()
            elif btype == "tool_use":
                tool_calls.append(block)
        for tc in tool_calls:
            if tc.get("name") == "save_memory":
                fact = (tc.get("input") or {}).get("text", "")
                if fact:
                    memory_saves.append(fact)
        if resp.get("stop_reason") != "tool_use":
            return {"reply": text_reply or EMPTY_REPLY, "memory_saves": memory_saves}
        # Continue after tool use — keep the FULL content list (thinking block + signature)
        # in the assistant turn, as the API requires for tool follow-ups.
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tc["id"], "content": "Saved to memory."}
            for tc in tool_calls
        ]})
    return {"reply": text_reply or EMPTY_REPLY, "memory_saves": memory_saves}
