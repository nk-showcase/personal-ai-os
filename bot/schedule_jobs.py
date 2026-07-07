"""bot/schedule_jobs.py — demo daily jobs migrated onto the schedule executor (INERT).

Payload-driven handlers (no PTB context/job). The keyed worker holds the Telegram token and sends
personal text DIRECTLY (worker-sends): the decrypted personal content (e.g. Todoist event titles)
never passes through a reply_queue row that the KEYLESS receiver would poll and send — that would be
a Gate-0 leak once the seal is armed. NO live caller: the live run_daily registrations in bot/main.py
are UNTOUCHED (the JobQueue path keeps firing today); this module is exercised only by
scripts/test_schedule_jobs.py. The cutover (remove the run_daily registrations + run schedule_executor
in a worker poll loop) is DEFERRED and operator-gated — the worker service that runs the executor must
hold TODOIST_API_KEY for the event-reminders job (see secrets-map.yaml, integrations-worker).

Data modules are imported LAZILY inside each handler (worker-side), keeping this module's import light.
Two demo daily jobs are shown here: a Todoist event-reminder scan and a one-shot precise reminder.
The default job timezone is configurable (AIOS_SCHEDULE_TZ); a per-job payload may override hour/minute/tz.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import worker_telegram

# Configurable default timezone for scheduled jobs (IANA name). Override via AIOS_SCHEDULE_TZ.
SCHEDULE_TZ = os.getenv("AIOS_SCHEDULE_TZ", "UTC")

KIND_EVENT_REMINDERS = "event_reminders"
KIND_PRECISE_REMINDER = "precise_reminder"  # one-shot per time-specific Todoist task (NOT in _DEFAULTS)

# Mirror the live run_daily registration (bot/main.py): check_reminders at the configured time.
# The add_scheduled payload may override hour/minute/tz.
# precise_reminder is DELIBERATELY absent: it is a ONE-SHOT (fires once at the task's exact due
# instant), so next_due_fn returns None for it (no re-arm) — the row is armed per task by
# reminder_lib.arm_precise_reminder (receiver, at task creation) and by
# schedule_seed.seed_precise_reminders (worker, at startup reconcile).
_DEFAULTS = {
    KIND_EVENT_REMINDERS: {"hour": 7, "minute": 0, "tz": SCHEDULE_TZ},
}


def _next_daily(now_epoch: float, hour: int, minute: int, tz_name: str) -> float:
    """Next occurrence of hour:minute in tz_name STRICTLY after now_epoch, as an epoch float.
    tz-aware via the IANA zone. If today's instant already passed (target <= now) -> tomorrow, so a
    job that just fired never re-arms onto the same instant (no same-tick double-fire)."""
    tz = ZoneInfo(tz_name)
    now = datetime.fromtimestamp(now_epoch, tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return target.timestamp()


async def _event_reminders(payload: dict) -> None:
    """Port of reminder.check_reminders: scan Todoist 'event' tasks, and for each event whose
    countdown hits a scheduled milestone today (default 7/1/0 days, or a 'reminders: a,b,c' comment)
    that has not already been marked sent, emit ONE consolidated reminder via the keyed worker's OWN
    Telegram send (never a reply_queue row — the personal Todoist titles must not transit the keyless
    receiver) and write a 'reminded_Nd (date)' comment back to Todoist (so it never re-sends). Reuses
    reminder_lib._parse_schedule/_parse_sent/REMINDER_LABELS. Silent when nothing is due."""
    chat_id = payload["chat_id"]
    from . import storage  # LAZY: Todoist events (worker holds TODOIST_API_KEY)
    from .reminder_lib import _parse_schedule, _parse_sent, REMINDER_LABELS
    today = datetime.now(ZoneInfo(SCHEDULE_TZ)).date()
    try:
        tasks = await storage.list_tasks()
    except Exception:
        return
    event_tasks = [
        t for t in tasks
        if "event" in t.get("labels", []) and t.get("due_date") and not t.get("parent_id")
    ]
    if not event_tasks:
        return
    reminders_to_send = []
    for task in event_tasks:
        try:
            due = date.fromisoformat(task["due_date"][:10])
        except (ValueError, TypeError):
            continue
        days_until = (due - today).days
        if days_until < 0:
            continue  # past event
        try:
            comments = await storage.get_comments(task["id"])
        except Exception:
            continue
        comment_texts = [c.get("content", "") for c in comments]
        schedule = _parse_schedule(comment_texts) or [7, 1, 0]
        sent = _parse_sent(comment_texts)
        for d in schedule:
            if days_until == d and d not in sent:
                label = REMINDER_LABELS.get(d, f"In {d} days")
                reminders_to_send.append((task, label, d))
                break
    if not reminders_to_send:
        return
    lines = ["Reminders:\n"]
    for task, label, d in reminders_to_send:
        lines.append(f"{label}: {task['content']} [{task['due_date']}]")
        try:
            await storage.add_comment(task["id"], f"reminded_{d}d ({today.isoformat()})")
        except Exception:
            pass  # mark-sent best-effort, mirrors the live job
    worker_telegram.send_message(chat_id, "\n".join(lines))


async def _precise_reminder(payload: dict) -> None:
    """Port of reminder._precise_reminder_callback: fire the one-time reminder for a specific task
    at its exact due instant. Re-fetches the task worker-side (keyed -> direct Todoist) to skip a
    task that has since been closed/deleted AND to read its CURRENT title, then sends via the keyed
    worker's OWN Telegram send. GATE 0: the personal task title is NEVER logged in plaintext and
    NEVER transits the keyless receiver — it is read and sent only here, worker-side. The payload is
    value-free ({task_id, chat_id}); the title is not stored at rest in the queue. A missing/failed
    fetch falls back to a GENERIC word (no title), never a stale cached title."""
    chat_id = payload["chat_id"]
    task_id = payload.get("task_id")
    from . import storage  # LAZY: Todoist (worker holds TODOIST_API_KEY)
    content = "task"  # generic fallback; the real title comes only from the live fetch below
    if task_id:
        try:
            t = await storage.get_task(task_id)
            if t is None or t.get("checked") or t.get("is_deleted"):
                return  # closed/deleted since it was armed -> no reminder (mirrors the live callback)
            content = t.get("content", content)
        except Exception:
            pass  # network/API hiccup -> send the generic fallback rather than drop (best-effort)
    worker_telegram.send_message(chat_id, f"Reminder: {content}")


def next_due_fn(kind: str, payload: dict):
    """Recurrence policy injected into schedule_executor.run_due_jobs. None => one-shot. A daily
    job returns the next occurrence (epoch) from the CURRENT wall clock (the executor re-arms right
    after a fire); paired with a cancel_key the executor dedups to one pending row."""
    defaults = _DEFAULTS.get(kind)
    if defaults is None:
        return None
    now_epoch = datetime.now(ZoneInfo("UTC")).timestamp()
    return _next_daily(now_epoch,
                       payload.get("hour", defaults["hour"]),
                       payload.get("minute", defaults["minute"]),
                       payload.get("tz", defaults["tz"]))


# The registry the FUTURE live-executor wiring imports (kind -> async handler); next_due_fn above
# is the matching recurrence policy. Registering a kind is REQUIRED because schedule_queue.claim_due
# fires the whole due batch atomically and drops a due row whose kind has no handler.
HANDLERS = {
    KIND_EVENT_REMINDERS: _event_reminders,
    KIND_PRECISE_REMINDER: _precise_reminder,
}
