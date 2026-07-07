"""bot/worker_telegram.py — direct Telegram sender for the KEYED worker (Gate 0 / Part B).

Personal-content views are rendered and sent by the keyed worker itself so decrypted
text never returns through the shared queue to the transport. Fail-closed: without
the token every send raises WorkerSendUnavailable — the view op reports it and the
bot falls back to today's queue-transit path (no silent drops, no behavior change
until the owner grants the token via the ceremony button).

The token comes from get_secret (the worker's own env partition). It is NEVER logged,
NEVER included in raised messages, and the API URL containing it is never printed.
"""
from __future__ import annotations

import logging

import httpx

from .secrets_loader import get_secret

log = logging.getLogger("aios.worker_telegram")

_TG_TEXT_LIMIT = 4096


class WorkerSendUnavailable(RuntimeError):
    """Token absent or Telegram unreachable — the caller must fall back, never drop."""


def token_present() -> bool:
    try:
        return bool(get_secret("TELEGRAM_BOT_TOKEN", default=""))
    except Exception:
        return False


def send_message(chat_id, text: str, reply_markup: dict | None = None,
                 timeout: float = 15.0) -> dict:
    """Send `text` to `chat_id` (chunked to Telegram's 4096 limit; the optional
    inline keyboard goes on the LAST chunk). Returns {"ok": True, "parts": n}.
    Raises WorkerSendUnavailable on missing token / network failure; raises
    RuntimeError on a Telegram API refusal (status printed, body truncated,
    no token ever included)."""
    token = get_secret("TELEGRAM_BOT_TOKEN", default="")
    if not token:
        raise WorkerSendUnavailable("telegram token not provisioned for this worker")
    chunks = [text[i:i + _TG_TEXT_LIMIT] for i in range(0, len(text), _TG_TEXT_LIMIT)] or [""]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    parts = 0
    for i, chunk in enumerate(chunks):
        payload: dict = {"chat_id": chat_id, "text": chunk}
        if reply_markup is not None and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        try:
            r = httpx.post(url, json=payload, timeout=timeout)
        except (httpx.HTTPError, httpx.TimeoutException) as e:
            raise WorkerSendUnavailable(f"telegram unreachable: {type(e).__name__}") from None
        if r.status_code != 200:
            # Never echo the URL (contains the token); body is Telegram's error JSON.
            raise RuntimeError(f"telegram send failed: HTTP {r.status_code} {r.text[:200]}")
        parts += 1
    return {"ok": True, "parts": parts}
