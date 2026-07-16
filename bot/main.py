import os
import sys

# Optional Linux-only privilege drop for container runtimes that start the process as root
# (some platform-as-a-service runners bypass the image ENTRYPOINT and run `python -m bot.main`
# directly). The bot briefly runs as root to chown its working volume, then switches to an
# unprivileged user before importing anything else (the Claude CLI refuses to run with
# --dangerously-skip-permissions as root). Gated on AIOS_CONTAINER_DROP_PRIV so it NEVER fires
# on the always-on VPS, where a service manager already runs the bot as a dedicated non-root user.
if os.getenv("AIOS_CONTAINER_DROP_PRIV") == "1" and hasattr(os, "geteuid") and os.geteuid() == 0:
    import pwd
    import subprocess
    _drop_user = os.getenv("AIOS_CONTAINER_USER", "app")
    _drop_home = os.getenv("AIOS_CONTAINER_HOME", "/home/app")
    try:
        subprocess.run(
            ["chown", "-R", f"{_drop_user}:{_drop_user}",
             f"{_drop_home}/.claude", "/tmp/claude-bridge", "/app"],
            check=False,
        )
    except Exception:
        pass
    try:
        _pw = pwd.getpwnam(_drop_user)
        os.setgroups([])
        os.setgid(_pw.pw_gid)
        os.setuid(_pw.pw_uid)
        os.environ["HOME"] = _pw.pw_dir
        sys.stderr.write(f"[bootstrap] dropped privileges to uid={_pw.pw_uid} gid={_pw.pw_gid} HOME={_pw.pw_dir}\n")
    except Exception as _drop_err:
        sys.stderr.write(f"[bootstrap] WARN: privilege drop failed: {_drop_err!r}\n")

import logging

from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID
from . import config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# B-02H ops fix: redact secret-shaped strings (e.g. Telegram bot token in
# httpx request URLs) from all logs, and silence httpx/httpcore INFO
# request-URL logging. Central, import-light (see bot/log_redaction.py).
from .log_redaction import install_log_redaction
install_log_redaction()


def router_handlers():
    """S6 S-1: handlers the unified router contributes. Pure + token-free so a test can assert
    the list is empty at OFF and contains exactly the catch-all at ON. Returns list[(handler, group)].
    group=1 => structurally shadowed by group-0 handle_text (docs/architecture/s6-router-s1-skeleton.md
    §0); never intercepts live traffic in S-1. router_transport is imported LAZILY (only at ON), so the
    OFF default never pulls handlers into bot.main's import graph."""
    if not config.unified_router_enabled():
        return []
    from . import router_transport
    return [(
        MessageHandler(filters.TEXT & ~filters.COMMAND, router_transport.route_catch_all),
        1,  # group=1: cannot beat group-0 handle_text; live interception is S-5
    )]


def router_callback_handlers():
    """The button-tap producer the unified router contributes. Returns list[(handler, group)].
    Gated on router_dispatch.dispatch_enabled() — a tap is only worth routing once the worker
    actually DISPATCHES it (run_callback); with dispatch OFF a routed tap would be a meaningless
    echo. group=1, BUT — unlike the text catch-all — this does NOT shadow the live group-0
    handle_callback_query (PTB proceeds across groups; that handler never raises
    ApplicationHandlerStop), so the live handler keeps owning taps until the executor cutover
    single-paths the owned prefixes (see router_transport.route_callback's docstring). OFF default
    => [] => nothing registered => byte-identical. router_dispatch is import-light (conv_state +
    reply_queue; no telegram / no secret resolution -> the boundary stays green); router_transport
    imported lazily only at ON."""
    from . import router_dispatch
    if not router_dispatch.dispatch_enabled():
        return []
    from . import router_transport
    # PATTERN-FILTER to the EXACT callback prefixes the keyed worker owns. Without a pattern this
    # CallbackQueryHandler would catch EVERY inline tap and enqueue it; the worker declines any
    # unowned prefix (run_callback -> False), the router runner then falls to its ECHO path, and the
    # owner gets a junk "routed:" reply after every unowned button. The pattern makes route_callback
    # see ONLY the owned taps; everything else is handled solely by the live group-0 handler as
    # before.
    _OWNED = r"^(note_add_|note_tag_)"
    return [(CallbackQueryHandler(router_transport.route_callback, pattern=_OWNED), 1)]


async def _reply_poller_job(context):
    """S6 0c: deliver worker-enqueued replies to Telegram. The transport is the ONLY Telegram-
    token holder, so it polls the durable reply queue and sends; the worker never touches Telegram.
    Send-then-mark with idempotent delivery (reply_queue.deliver_pending) — no lost/double sends."""
    from . import reply_queue
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    async def _send(chat_id, text):
        await context.bot.send_message(chat_id=chat_id, text=text)

    async def _send_kb(chat_id, text, keyboard):
        # keyboard: list of button ROWS, each a list of {"text":..., "callback_data":...} dicts
        # (the plain-JSON shape enqueue_reply_kb stores). Rebuild the InlineKeyboardMarkup here —
        # the worker never imports a telegram type. This branch fires for the coding worker's
        # approval card (make_approval_notifier -> enqueue_reply_kb) and any other KB-bearing reply.
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton(**btn) for btn in row] for row in (keyboard or [])]
        )
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)

    try:
        await reply_queue.deliver_pending(_send, send_kb_fn=_send_kb)
    except Exception as e:
        logger.warning(f"reply poller failed: {e}")


async def _auto_restart_job(context):
    """Auto-restart the transport when the code updates (the operator opted into fully automatic
    updates). Every interval it checks the working copy's git HEAD: if the code changed (the sync
    service pulled it) and it COMPILES — exit, and the service manager brings the process up on the
    fresh version. If the new code has a syntax error (a bad self-edit) — do NOT exit; warn the
    operator and keep running the previous version."""
    from . import auto_restart
    data = context.job.data if context.job else None
    start_head = data.get("start_head") if data else None
    if not auto_restart.code_changed(start_head):
        return
    if not (auto_restart.code_compiles() and auto_restart.imports_ok("bot.main")):
        cur = auto_restart.current_head()
        # Do not spam the warning every N seconds about the same broken commit.
        if data is not None and data.get("warned_head") == cur:
            return
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_OWNER_ID,
                text="⚠️ The new bot code has an error — not restarting, "
                     "still running the previous version. Ask Claude to fix it.",
            )
            if data is not None:
                data["warned_head"] = cur
        except Exception as e:
            logger.warning(f"auto-restart broken-code notice failed: {e}")
        return
    # Do NOT send a separate "picked up the fresh code, restarting…". For edits made via the "Code"
    # button the worker already sent "Done — restarting", and post_init after the restart sends
    # "🟢 Bot restarted" — one coherent dialog with no duplicates. (A direct code push with no task
    # is rare: the operator only sees "restarted", which is enough.)
    logger.info("transport: fresh code — exiting, the service manager will bring up the new version")
    os._exit(0)


async def post_init(application):
    """Remove old bot commands, send the keyboard, reschedule background jobs."""
    from .shell import PERSISTENT_KEYBOARD
    from . import claude_bridge
    # ensure_skills() is deliberately NOT run from transport startup — it resolved the worker-only
    # GitHub token and was a NO-OP here anyway (the telegram-bot holds no GitHub token). Skills reach
    # the VPS via the ${AIOS_HOME}/.claude/skills -> repo/claude/skills symlink (auto-pulled by the
    # sync service); the clone path lives in claude_bridge_worker.
    try:
        claude_bridge._hydrate_last_sessions()
    except Exception as e:
        logger.warning(f"hydrate_last_sessions failed at startup: {e}")
    # S6 0c: deliver worker-enqueued replies (every AIOS_REPLY_POLL_S, default 5s). The worker
    # holds no Telegram token; the transport polls the durable reply queue and sends.
    try:
        _reply_interval = float(os.environ.get("AIOS_REPLY_POLL_S", "2"))
        application.job_queue.run_repeating(
            _reply_poller_job, interval=_reply_interval, first=_reply_interval, name="reply_poller",
        )
        logger.info("Scheduled reply poller every %ss", _reply_interval)
    except Exception as e:
        logger.warning(f"Failed to schedule reply poller: {e}")
    # Auto-restart on code updates (the operator opted into fully automatic updates). Every
    # AIOS_AUTO_RESTART_POLL_S seconds we check the working copy's git HEAD; on a change (and if the
    # new code compiles) we exit, and the service manager brings up the fresh version.
    try:
        from . import auto_restart
        # Check for fresh code often (5s by default) so that after an edit via the "Code" button the
        # change becomes visible almost immediately. This is only a read of the local git HEAD — free.
        _ar_interval = float(os.environ.get("AIOS_AUTO_RESTART_POLL_S", "5"))
        _ar_head = auto_restart.current_head()
        application.job_queue.run_repeating(
            _auto_restart_job, interval=_ar_interval, first=_ar_interval,
            name="auto_restart", data={"start_head": _ar_head},
        )
        logger.info("Scheduled auto-restart watcher every %ss (head=%s)", _ar_interval, _ar_head)
    except Exception as e:
        logger.warning(f"Failed to schedule auto-restart watcher: {e}")
    await application.bot.delete_my_commands()
    logger.info("Cleared old bot commands")
    # Send the keyboard to the operator so it appears without /start
    try:
        await application.bot.send_message(
            chat_id=TELEGRAM_OWNER_ID,
            text="\U0001f7e2 Bot restarted",
            reply_markup=PERSISTENT_KEYBOARD,
        )
    except Exception as e:
        logger.warning(f"Failed to send startup keyboard: {e}")
    # Time-specific task reminders are NOT scheduled from this transport. The transport holds no
    # integration keys, so it must never read a task title or fire a reminder itself (that would
    # log/send content from the keyless side — the boundary the worker-sends design enforces).
    # Precise reminders are armed as value-free schedule_queue rows (reminder_lib.arm_precise_reminder,
    # at task creation in handlers.py) and reconciled at startup worker-side; the keyed
    # integrations-worker's schedule executor fires each one worker-side (title read + send happen
    # only there, never logged/sent by this transport).


def main():
    from . import handlers
    from . import claude_bridge
    # DL-01 owner-only dead-letter visibility command.  Lazy import
    # to keep main.py module-load surface unchanged in non-runtime
    # contexts (tests / linters).  Gated inside the handler by
    # TELEGRAM_OWNER_ID -- non-owner gets silent no-op.
    from . import aios_dead_letters_handler as _dl_handler

    # Chat memory that survives restarts (an os._exit auto-restart must not wipe the active chat).
    # PicklePersistence with on_flush=False writes chat_data to disk after EVERY update, so an
    # os._exit auto-restart loses nothing. Stored LOCAL-ONLY on the VPS in a 0700 owner-only dir
    # (never git/cloud). BOUNDARY: this PTB pickle file is the framework's own conversation-state
    # store and is distinct from the encrypted-at-rest context stores; it is protected by directory
    # permissions and local-only placement, not by the at-rest cipher. See docs/security/threat-model.md.
    from telegram.ext import PicklePersistence
    from pathlib import Path as _Path
    _persist_dir = _Path(os.getenv("AIOS_PTB_STATE_DIR",
                                   str(_Path.home() / ".ai-os" / "data" / "ptb_state")))
    _persist_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_persist_dir, 0o700)
    except OSError:
        pass
    _persistence = PicklePersistence(
        filepath=str(_persist_dir / "chat_state.pkl"), on_flush=False,
    )

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(_persistence)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("help", handlers.cmd_start))
    app.add_handler(CommandHandler("list", handlers.cmd_list))
    app.add_handler(CommandHandler("l", handlers.cmd_list))
    app.add_handler(CommandHandler("done", handlers.cmd_done))
    app.add_handler(CommandHandler("d", handlers.cmd_done))
    app.add_handler(CommandHandler("del", handlers.cmd_delete))
    app.add_handler(CommandHandler("bridge", claude_bridge.cmd_bridge))
    # DL-01: owner-only dead-letter summary.
    app.add_handler(CommandHandler(
        _dl_handler.OWNER_COMMAND_NAME,
        _dl_handler.cmd_aios_dead_letters,
    ))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, claude_bridge.intercept_task_text),
        group=-1,
    )
    app.add_handler(
        MessageHandler(filters.PHOTO, claude_bridge.intercept_task_photo),
        group=-1,
    )
    app.add_handler(
        MessageHandler(filters.Document.ALL, claude_bridge.intercept_credentials_document),
        group=-1,
    )

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_text)
    )
    app.add_handler(
        MessageHandler(filters.PHOTO, handlers.handle_photo)
    )
    # Unified-router text catch-all. Default OFF => router_handlers() == [] => nothing added
    # (bot byte-identical). At ON it adds ONE catch-all at group=1, structurally shadowed by the
    # group-0 handle_text above; live interception is deferred.
    for _handler, _group in router_handlers():
        app.add_handler(_handler, group=_group)
    # Button-tap producer. Default OFF => router_callback_handlers() == [] => nothing added
    # (byte-identical). At ON it adds ONE CallbackQueryHandler at group=1; the live group-0
    # handle_callback_query below keeps owning taps until the executor cutover single-paths the owned
    # prefixes (route_callback does NOT shadow the live handler — see its docstring).
    for _handler, _group in router_callback_handlers():
        app.add_handler(_handler, group=_group)
    app.add_handler(
        CallbackQueryHandler(claude_bridge.cb_approve, pattern=r"^bridge:")
    )
    app.add_handler(
        CallbackQueryHandler(handlers.handle_callback_query)
    )

    # Belt-and-suspenders: any handler exception that would otherwise bubble up (e.g. a future
    # secret-policy denial or a DB hiccup) gets logged instead of silently leaving the user with no
    # reply. Registered last so it covers every handler above.
    async def _on_error(update, context):
        logger.exception("unhandled handler error", exc_info=context.error)
    app.add_error_handler(_on_error)

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
