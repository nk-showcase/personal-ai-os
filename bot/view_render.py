"""bot/view_render.py — PURE renderers shared by the bot and the keyed worker (Gate 0).

For personal-content views the keyed worker renders and sends the Telegram message
itself, so the exact same formatting code must be importable from BOTH processes
without heavy dependencies. Everything here is pure (data in -> text out): no I/O, no
telegram imports, no storage imports. The bot's fallback path uses these same
functions — parity by construction, not by duplication.
"""
from __future__ import annotations

# The single route for personal views is: the keyed worker renders AND sends them itself.
# If the worker is unavailable (restart / no token / the queued row died on TTL), we show
# THIS instead of a false "no records". The keyless receiver NEVER renders personal data
# itself — no leak by design. Show ONLY on genuine unavailability (view_request ->
# unavailable), NOT on WRITE_ACCEPTED: there the worker durably took the row and will send
# the reply itself (see VIEW_ACCEPTED_MESSAGE).
VIEW_BUSY_MESSAGE = ("Could not show this right now — the server is busy with another task. "
                     "Please try again in a minute.")

# WRITE_ACCEPTED: the worker took the row but did not finish it within the ~12s poll window.
# It WILL still send the reply to Telegram itself (usually 20-40s). This is NOT unavailability —
# we must not invite "try again" (a repeat would create a second turn). Reassure WITHOUT a repeat.
VIEW_ACCEPTED_MESSAGE = "Thinking…"

# A WRITE view that ALREADY committed the change but whose Telegram send failed transiently.
# The write landed exactly once — the owner must NOT be told to retry (a retry would mint a
# fresh id and double-apply). Confirm success WITHOUT inviting a repeat.
VIEW_WRITE_DONE_MESSAGE = "Done. (Could not show it just now — open the view again.)"


def view_reply_for(res: dict | None) -> str | None:
    """How the receiver reacts to a view_request() result when it does not render personal
    data itself. Returns the text to show the user, or None (show nothing — the worker
    already sent the reply).

      worker rendered and sent (ok & sent)                 -> None (stay silent)
      worker durably took the row, will send (accepted)    -> VIEW_ACCEPTED_MESSAGE
                                                              (no invitation to repeat)
      genuine unavailability (no token / row died /         -> VIEW_BUSY_MESSAGE
        queue unavailable / corrupt result)                   (a retry is appropriate)
    """
    if not isinstance(res, dict):
        return VIEW_BUSY_MESSAGE
    if res.get("ok") and res.get("sent"):
        return None
    if res.get("send_failed"):
        # A write landed but the send failed transiently — confirm without retry.
        return VIEW_WRITE_DONE_MESSAGE
    if res.get("accepted"):
        return VIEW_ACCEPTED_MESSAGE
    return VIEW_BUSY_MESSAGE


def render_chat_resume(title: str, n: int) -> str:
    """The "Continue" resume line — the last chat's title + user-message count. The title is
    decrypted worker-side; only this rendered line reaches Telegram (never the transcript)."""
    return f"\U0001f47e {title} ({n} messages)"


def render_add_note_prompt(title: str | None) -> str:
    """The add-note SETUP prompt. When a decrypted record title is present, append it in
    parentheses — the title is rendered HERE so it never returns through the transport. A
    falsy title ('' / None) adds no line."""
    prompt = "Write your note:"
    if title:
        prompt += f"\n({title})"
    return prompt


def render_notes_summary(count: int, previews: list) -> str:
    """The notes-demo summary view. `count` = total saved notes; `previews` = value-limited
    preview strings (already truncated store-side). Empty -> a friendly empty message. The
    note bodies are decrypted worker-side; only this rendered text reaches Telegram."""
    if count == 0:
        return "You have no saved notes yet."
    lines = [f"You have {count} saved note(s). Most recent:"]
    lines.extend(f"- {p}" for p in previews)
    return "\n".join(lines)
