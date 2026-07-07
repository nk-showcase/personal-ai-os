"""bot/claude_worker.py — claude-worker entrypoint (durable queue consumer).

Runnable module: `python -m bot.claude_worker`. Loop: claim a task from task_queue ->
process_one(runner) -> done/failed. The consumer is built on claude_worker_core /
claude_worker_runner.

Properties:
  - Does NOT require TELEGRAM_BOT_TOKEN; does NOT import bot.main; does NOT run on import.
  - The real Claude runs via _claude_executor (claude_bridge._run_claude). In tests a fake
    runner is substituted and the real Claude does NOT run. claude_bridge is imported LAZILY
    inside the executor, so `import bot.claude_worker` is free of Telegram/Claude.
  - The Claude Code login is a local-only file ~/.claude/.credentials.json, installed OUTSIDE
    the bot. The worker does NOT configure the login from env/Telegram.

APPROVAL BOUNDARY: claude_bridge._run_claude runs the coding agent with the default
permission mode 'bypassPermissions', so per-tool Allow/Deny confirmations are not requested
for the agent's own tool calls in the default configuration (the gating mode is selectable
via claude_policy.resolve_claude_permission_mode). The bot/chat_id parameters of _run_claude
are unused, so the worker calls it with bot=None, chat_id=0 (no Telegram token needed). A
per-tool approval queue (make_queue_approver) exists but is not wired into the current
executor. See docs/security/threat-model.md.
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import claude_worker_core
from . import claude_worker_runner
from . import claude_policy
from . import reply_queue

log = logging.getLogger("aios.claude_worker")

WORKER_ID = os.getenv("AIOS_WORKER_ID", "claude-worker-1")
POLL_INTERVAL_S = float(os.getenv("AIOS_WORKER_POLL_S", "2"))


_BRIDGE_REPLY_CAP = 3500


def _notify_owner_done(chat_id, alias, read_only, result) -> None:
    """Reply-back: return a human-readable /bridge result to the operator via the reply queue
    (the transport delivers it). The agent's text is stripped of secrets (_redact) and
    truncated to the Telegram limit. Any failure here does NOT fail the task — it is already
    done."""
    if not chat_id:
        return
    try:
        # A clean reply for a non-technical operator: the outcome in words, without file paths,
        # without "saved to GitHub" and other technical noise. The agent's text is already made
        # plain by an instruction in the system prompt (see claude_bridge_worker).
        text = claude_worker_core._redact((result.get("text") or "").strip())
        verify = result.get("verify")
        # Mandatory verification: a failure => an honest header and NO output listing.
        if verify and not verify.get("ok"):
            head = f"⚠️ Checks did NOT pass ({alias})" if alias else "⚠️ Checks did NOT pass"
        else:
            head = f"✅ Done ({alias})" if alias else "✅ Done"
        body = text or "(done)"
        if verify:
            if verify.get("ok"):
                body += "\n\n🧪 Tests passed."
            else:
                body += "\n\n🧪 Post-edit check:\n" + "\n".join("• " + l for l in verify.get("lines", []))
                body += ("\n\nChanges were NOT published — the code is unchanged. "
                         "Error tail:\n" + (verify.get("fail_tail") or "(none)"))
        # When the edit actually reached the code (pushed), send ONE coherent message —
        # "Done … restarting". The restart is triggered by _claude_executor (an immediate sync
        # of the live repo); after the restart the transport sends "🟢 Bot restarted".
        # With no code change (read-only / no-op) there is NO restart promise — a plain "Done".
        suffix = (
            "\n\n💫 Applying the changes and restarting — everything updates in a few seconds."
            if result.get("pushed") else ""
        )
        budget = _BRIDGE_REPLY_CAP - len(head) - len(suffix) - 8
        if len(body) > budget:
            body = body[:budget] + "\n…(truncated)"
        msg = f"{head}\n\n{body}{suffix}"
        reply_queue.enqueue_reply(int(chat_id), msg, source="bridge")
    except Exception as e:  # noqa: BLE001 — the reply must not crash the worker
        log.warning("bridge done-reply enqueue failed: %s", type(e).__name__)


# Markers that mean "the Claude login on the server expired" rather than a real task failure.
_OAUTH_EXPIRY_MARKERS = (
    "oauth", "unauthorized", "401", "expired", "invalid token", "not logged in",
    "please run /login", "please sign in", "authentication failed", "credentials",
)

# Subscription temporarily exhausted (token window closed) — NOT a failure. Markers from the Claude client stderr.
_USAGE_LIMIT_MARKERS = (
    "session limit", "usage limit", "rate limit", "limit reached", "hit your limit",
    "usage cap", "out of usage", "limit will reset", "You've hit".lower(),
)


def _notify_owner_failed(chat_id, alias, exc) -> None:
    """Reply-back: return a clear /bridge error to the operator via the reply queue.

    If the failure looks like an EXPIRED Claude LOGIN (OAuth markers in stderr), instead of a
    terse "Failed" we give a human hint about what happened and what to do (without forwarding
    the token to chat — that is deliberately disabled as a leak channel). Otherwise we show a
    short real error tail, not just "failed"."""
    if not chat_id:
        return
    try:
        stderr_lines = getattr(exc, "bridge_stderr", []) or []
        probe_text = getattr(exc, "bridge_probe", "") or ""
        blob = ("\n".join(stderr_lines) + "\n" + probe_text + f"\n{type(exc).__name__}: {exc}").lower()
        if any(m in blob for m in _USAGE_LIMIT_MARKERS):
            # The subscription token window is closed. If the client named a reset time, show it.
            import re as _re
            _m = _re.search(r"resets?\s+([^\n·]{1,60})", blob)
            _when = _m.group(1).strip().rstrip(".") if _m else ""
            msg = (
                "⏳ The subscription token window is closed right now — Claude on the server is "
                "temporarily not accepting tasks.\n\n"
                + (f"It will reopen: {_when}.\n" if _when else "It will reopen after a while.\n")
                + "This is not a breakage, the task was not lost — just send it again after that time."
            )
            reply_queue.enqueue_reply(int(chat_id), msg, source="bridge")
            log.warning("bridge fail looks like a subscription usage limit (reset=%s)", _when or "?")
            return
        if any(m in blob for m in _OAUTH_EXPIRY_MARKERS):
            msg = (
                "⚠️ It looks like the Claude login on the server has expired — so coding through "
                "the bot is not working right now.\n\n"
                "This is not a breakage of your task: the login must be refreshed periodically (it "
                "lasts about a year). Mention it in the assistant chat — the login will be refreshed "
                "and the bot will work again."
            )
            reply_queue.enqueue_reply(int(chat_id), msg, source="bridge")
            log.warning("bridge fail looks like expired Claude login (OAuth markers in stderr)")
            return
        tail = "\n".join(stderr_lines[-12:]).strip()
        err = claude_worker_core._redact(tail or f"{type(exc).__name__}: {exc}")[:900]
        head = f"❌ Failed ({alias})" if alias else "❌ Failed"
        reply_queue.enqueue_reply(int(chat_id), f"{head}\n\n{err}", source="bridge")
    except Exception as e:  # noqa: BLE001
        log.warning("bridge fail-reply enqueue failed: %s", type(e).__name__)


async def _claude_executor(*, task, read_only, is_continue, resume_session_id, design_mode, approve):
    """The real coding executor: prepare the repo and run claude_bridge._run_claude.

    Called only at real runtime (not in tests). claude_bridge is imported lazily.
    bot/chat_id are unused in _run_claude -> None/0 (the worker needs no Telegram token).
    The Claude login is a local-only file on the server; the worker does not configure it
    from env. `approve` is accepted for compatibility with the executor contract, but the
    current _run_claude does not use it (default permission mode).

    Reply-back: on completion (success OR error) it puts a human-readable reply for the
    operator into reply_queue — the transport delivers it to Telegram. The returned result
    dict is unchanged.
    """
    from . import claude_bridge_worker as cb

    chat_id = task.get("chat_id")
    alias = task.get("alias") or ""
    try:
        # Fail-closed policy gate (known mode, allowlisted alias) before execution.
        claude_policy.validate_task_execution_policy(task, known_aliases=set(cb._load_projects()))

        if alias == cb._ASSISTANT_ALIAS:
            repo = cb._WORK_DIR / "_assistant"
            repo.mkdir(parents=True, exist_ok=True)
        else:
            proj = cb._load_projects().get(alias)
            if not proj:
                raise RuntimeError(f"unknown project alias: {alias}")
            repo = await cb._prepare_repo(alias, proj["url"], proj.get("branch", "main"))

        # Live progress for the operator: from ~3.5 minutes in, forward the MOST RECENT
        # progress line from the agent (it is prompted to write "Done: … Next: …" in the
        # operator's language). Nothing new — stay silent. Cancelled on completion.
        _sink: list = []

        async def _pulse():
            last_sent = None
            try:
                for _ in range(6):
                    await asyncio.sleep(210)
                    latest = _sink[-1] if _sink else None
                    if latest and latest != last_sent:
                        body = claude_worker_core._redact(latest.strip())[:600]
                        reply_queue.enqueue_reply(int(chat_id), f"🔧 {body}", source="bridge")
                        last_sent = latest
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001 — the pulse must not crash the task
                log.warning("bridge pulse failed: %s", type(e).__name__)

        _pulse_task = asyncio.ensure_future(_pulse()) if chat_id else None
        try:
            text, _stderr, session_id, edited = await cb._run_claude(
                repo, task["task_text"], None, 0, read_only,
                design_mode=design_mode, resume_session_id=resume_session_id,
                progress_sink=_sink,
            )
        finally:
            if _pulse_task:
                _pulse_task.cancel()
        pushed, push_msg = (False, "")
        verify = None
        if not read_only and edited:
            # Mandatory post-implementation check: compile + quick tests in the clone.
            # A failure => do NOT push (production code stays unchanged), and the operator gets
            # an honest "checks did not pass" instead of "Done". If no file changed (edited is
            # empty), there is nothing to test and the test block is not shown.
            verify = await cb._run_verification(repo)
            if verify["ok"]:
                pushed, push_msg = await cb._auto_push(repo, task["task_text"])
            else:
                log.warning("bridge verification FAILED — auto-push suppressed (%s)",
                            "; ".join(verify.get("lines", [])))
        result = {
            "text": text,
            "session_id": session_id,
            "edited_files": edited,
            "pushed": pushed,
            "push_msg": push_msg,
            "verify": verify,
        }
        # Record the session so the transport's picker shows it on the next Code press.
        # The shared file (bridge_constants._RECENT_SESSIONS_FILE) lives on the persistent
        # volume and is read by the transport's picker via _get_recent_sessions.
        try:
            from .bridge_constants import _record_session
            _record_session(chat_id, alias, session_id, task.get("task_text") or "")
        except Exception as e:  # noqa: BLE001 — history writing must never fail the task
            log.warning("bridge session-record failed: %s", type(e).__name__)
        _notify_owner_done(chat_id, alias, read_only, result)
        if pushed:
            # Immediately pull fresh code into the LIVE repo (don't wait for the 60s aios-sync),
            # so the restart happens right after the edit. This also removes the "double restart":
            # by the next should_restart check the live HEAD is already on the new commit.
            try:
                from . import sync_service
                sync_service.sync_once()
            except Exception as e:  # noqa: BLE001 — task is done, sync must not fail it
                log.warning("bridge post-push live sync failed: %s", type(e).__name__)
        return result
    except Exception as exc:
        # A "bare" failure with no stderr (the SDK swallowed the CLI output) — probe the cause:
        # if it is a subscription window limit, the operator gets a reset time, not "exit code 1".
        if not getattr(exc, "bridge_stderr", None):
            try:
                _probe = await cb.probe_claude_limit()
                if _probe:
                    exc.bridge_probe = _probe  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        _notify_owner_failed(chat_id, alias, exc)
        raise


def build_default_runner():
    """runner(task) for process_one with the real Claude executor (used in main())."""
    return claude_worker_runner.build_runner(_claude_executor)


async def run_once(runner):
    """Process one task from the queue (or a no-op if the queue is empty)."""
    return await claude_worker_core.process_one(WORKER_ID, runner)


async def run_loop(runner, *, max_iterations=None, poll_interval=POLL_INTERVAL_S, sleep=asyncio.sleep):
    """Consumer loop. max_iterations=None -> forever (real mode). Tests pass a finite number so
    the loop terminates. On an empty queue it waits poll_interval.

    Auto-restart: only in real mode (max_iterations is None) and ONLY when idle (the queue is
    empty — a safe point between tasks) do we check whether the code was updated. If it was and
    it compiles — exit, and systemd brings the process up on the fresh version. Broken new code
    is NOT loaded (see auto_restart.should_restart). Tests (finite max_iterations) do NOT run
    the check."""
    from . import auto_restart
    start_head = auto_restart.current_head() if max_iterations is None else None
    n = 0
    while max_iterations is None or n < max_iterations:
        res = await run_once(runner)
        n += 1
        if res.get("status") == "noop":
            if max_iterations is None and auto_restart.should_restart(start_head, smoke_module="bot.claude_worker"):
                log.info("claude-worker: fresh code — exiting, systemd will bring up the new version")
                os._exit(0)
            await sleep(poll_interval)
    return n


def main() -> None:  # pragma: no cover — real run, not in tests
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    # B-02H ops fix: central secret redaction for this service's logs.
    from .log_redaction import install_log_redaction
    install_log_redaction()
    log.info("claude-worker starting (worker_id=%s, poll=%ss)", WORKER_ID, POLL_INTERVAL_S)
    asyncio.run(run_loop(build_default_runner()))


if __name__ == "__main__":
    main()
