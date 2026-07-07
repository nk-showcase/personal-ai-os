"""Precise reminder arming + reminder-schedule parsers.

Extracted helpers for one-time, exact-instant task reminders. The keyless receiver arms a reminder by
writing ONE value-free row to the shared schedule_queue; the keyed integrations-worker's schedule
executor claims it at its exact due instant and fires it (worker-side re-fetch + worker-side send).
"""
import logging
import os
from datetime import date, datetime, timezone, timedelta

logger = logging.getLogger(__name__)


def _reminder_tz() -> timezone:
    """Configurable fixed-offset timezone for reminders.

    Reads AIOS_REMINDER_UTC_OFFSET_HOURS (default 0 = UTC). Floating Todoist timestamps (no timezone
    info) are interpreted as the operator's wall-clock time in this zone.
    """
    try:
        hours = int(os.environ.get("AIOS_REMINDER_UTC_OFFSET_HOURS", "0"))
    except ValueError:
        hours = 0
    return timezone(timedelta(hours=hours))


REMINDER_LABELS = {
    0: "Today",
    1: "Tomorrow",
    2: "In 2 days",
    3: "In 3 days",
    7: "In a week",
    14: "In 2 weeks",
    30: "In a month",
}


def _parse_schedule(comment_texts: list[str]) -> list[int]:
    """Extract reminder schedule from comments like 'reminders: 30,7,1,0'.

    If multiple schedule comments exist, returns the LAST one (most recent).
    """
    result = []
    for text in comment_texts:
        if text.startswith("reminders:"):
            parts = text.split(":", 1)[1].strip().split(",")
            try:
                result = [int(p.strip()) for p in parts]
            except ValueError:
                continue
    return result


def _parse_sent(comment_texts: list[str]) -> set[int]:
    """Extract already-sent reminder days from comments like 'reminded_30d (2026-02-21)'."""
    sent = set()
    for text in comment_texts:
        if text.startswith("reminded_"):
            try:
                d = int(text.split("_")[1].split("d")[0])
                sent.add(d)
            except (ValueError, IndexError):
                continue
    return sent


def _parse_due_to_local(due_iso: str) -> datetime | None:
    """Parse a Todoist due_date (floating or Z-suffixed) into a timezone-aware datetime in the
    configured reminder zone.

    Floating timestamps (no timezone info) are assumed to be the operator's wall-clock time in the
    configured zone -- that is what they typed and what they expect.
    """
    tz = _reminder_tz()
    try:
        s = due_iso
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.astimezone(tz)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    except (ValueError, TypeError):
        return None


def arm_precise_reminder(task_id: str, due_iso: str, chat_id: int) -> bool:
    """Arm a one-time precise reminder by writing ONE row to the shared schedule_queue; the keyed
    integrations-worker's schedule executor claims it at its exact due instant and fires it via
    schedule_jobs._precise_reminder (worker-side re-fetch + worker-side Telegram send).

    This runs in the KEYLESS receiver, so it does NOT read Todoist, does NOT send, and does NOT log
    the task title. The row it writes is value-free ({task_id, chat_id}) -- the personal title is
    never logged in plaintext nor stored at rest in the queue; the worker reads it live at fire time.
    Returns True if a future-dated row was written, False otherwise (no time component, in the past,
    or unparseable due). An atomic upsert on cancel_key precise:{task_id} dedups (replaces any earlier
    pending row, mirroring the old JobQueue removal), so a re-created/edited task never double-fires.
    """
    if not due_iso or "T" not in due_iso:
        return False
    dt = _parse_due_to_local(due_iso)
    if dt is None:
        return False
    now = datetime.now(_reminder_tz())
    if dt <= now:
        return False

    from . import schedule_queue
    from .schedule_jobs import KIND_PRECISE_REMINDER
    # ATOMIC cancel-then-add: the receiver (here, at task creation) and the worker
    # (seed_precise_reminders, at startup) may arm the SAME task at once; upsert_scheduled serializes
    # them on the write lock so exactly one pending row survives (no double-fire), and a re-arm of an
    # edited task replaces the old row rather than stacking a second one.
    # Best-effort: task creation is a hot receiver path and must NOT break if the queue write hiccups
    # (e.g. a transient sqlite lock). On failure we report False (no reminder line -- honest) and the
    # worker's startup seed_precise_reminders reconciles this task on the next restart. Logged
    # class-name-only (no title); the worker-side seeder's 'armed=N' line remains the real tripwire.
    try:
        schedule_queue.upsert_scheduled(
            KIND_PRECISE_REMINDER, dt.timestamp(),
            {"task_id": task_id, "chat_id": chat_id},  # value-free: NO title (worker fetches it live)
            cancel_key=f"precise:{task_id}",
        )
    except Exception as e:  # noqa: BLE001 -- a queue hiccup must not break task creation
        logger.warning("arm precise reminder failed for %s (class=%s)", task_id, type(e).__name__)
        return False
    logger.info(f"Armed precise reminder for {task_id} at {dt.isoformat()}")  # id + time only, NO title
    return True
