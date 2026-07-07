"""bot/aios_dead_letters_handler.py -- DL-01 owner-only Telegram
command surface.

Provides cmd_aios_dead_letters, registered in :mod:`bot.main` as
``/aios_dead_letters``.  Gated by ``TELEGRAM_OWNER_ID`` via the
same silent-no-op pattern as :func:`bot.claude_bridge.cmd_bridge`
(non-owner gets no reply).

Output is the value-free summary text from
:func:`bot.aios_dead_letters.format_summary_owner_text` -- only
counts, kind names, error_class names, age buckets, attempts.
NEVER prints payload, row contents, account / activity names,
source_notion_id, target_external_id, correlation_id values, or
any secret.

DL-01 invariants enforced here:
  * READ-ONLY: viewing dead letters does NOT mutate any
    request_queue row.
  * No auto-retry / no auto-purge / no requeue (the command does
    NOT call any mark_* or DELETE).
  * No live Notion / Todoist / Bitwarden contact.
  * No secret read (the helper opens the DB read-only via
    bot.aios_storage.connect; only TELEGRAM_OWNER_ID is consulted).
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from .config import TELEGRAM_OWNER_ID
from . import aios_dead_letters as DL

log = logging.getLogger("aios.dead_letters_handler")


# Re-exported so :mod:`bot.main` and tests can reference one canonical
# name for the registered slash command.
OWNER_COMMAND_NAME = DL.OWNER_COMMAND_NAME  # "aios_dead_letters"


async def cmd_aios_dead_letters(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Owner-only Telegram command: report value-free dead-letter
    summary for write-operations.

    Non-owner: silent no-op (matches cmd_bridge pattern; no
    sensitive output is produced for non-owners).

    Owner: computes
    :func:`bot.aios_dead_letters.summarize_dead_letters` and
    replies with
    :func:`bot.aios_dead_letters.format_summary_owner_text`.

    Does NOT mutate any row.
    Does NOT contact any external service.
    Does NOT read any secret beyond the already-loaded
    TELEGRAM_OWNER_ID from bot.config.
    """
    # Owner gate -- silent no-op for non-owner.  Matches existing
    # bot.claude_bridge.cmd_bridge pattern (handlers do not emit
    # sensitive output for non-owners).
    if (update.effective_user is None
            or update.effective_user.id != TELEGRAM_OWNER_ID):
        return
    if update.message is None:
        return
    # Review-fix: wrap the read+render+reply in try/except so the
    # visibility surface itself does NOT become a silent-failure
    # point.  Per DL-01 contract, the owner must SEE something
    # even if the dead-letter helper hits a DB lock, schema
    # mismatch, or any other internal error.  The fallback message
    # is value-free (no payload, no row contents) and the actual
    # exception class name is logged (NOT sent to the owner) for
    # operator diagnosis.
    try:
        summary = DL.summarize_dead_letters()
        text = DL.format_summary_owner_text(summary)
        await update.message.reply_text(text)
    except Exception as exc:  # noqa: BLE001 -- value-free fallback
        log.warning(
            "cmd_aios_dead_letters failed (class=%s); "
            "sending fallback to owner",
            type(exc).__name__,
        )
        try:
            await update.message.reply_text(
                "Dead-letter summary is temporarily unavailable "
                "(internal error reading the queue -- see the log)."
            )
        except Exception:  # noqa: BLE001 -- last-resort no-op
            # If even the fallback reply fails, do NOT raise into
            # python-telegram-bot's error handler (it would create
            # log noise but not help the owner).  Owner will retry
            # the command.
            pass
