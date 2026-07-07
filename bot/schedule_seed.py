"""bot/schedule_seed.py — startup seeder that ensures EXACTLY ONE pending
schedule_queue row per seeded kind (cancel-then-add, idempotent across restarts).

Runs only in the real integrations-worker service loop (same process as the schedule
executor), so the row it writes is scanned by schedule_executor on the next ~60s tick and
fires at its due wall-clock time. Idempotent: cancel(cancel_key) then add_scheduled(...) =>
exactly one pending row even after N restarts, and the executor's own re-arm uses the SAME
cancel_key so pending rows never accumulate.

Owner id is resolved via get_secret("TELEGRAM_OWNER_ID") — the SAME canonical path config.py
uses (env first, then the secret manager) — so it resolves wherever the rest of the bot
resolves it. A missing owner id seeds NOTHING and logs a loud WARNING (never seeds chat_id=0);
that warning is the post-deploy tripwire (its presence means the seeding silently lost the
reminder).

Import side-effect-free; reads no secret at import time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import schedule_jobs, schedule_queue

log = logging.getLogger("aios.schedule_seed")

# Seeded kinds. Each kind MUST already be in schedule_jobs.HANDLERS, else claim_due drops a
# due row whose kind has no handler (a schedule_executor KNOWN LIMITATION).
_SEED_KINDS = (schedule_jobs.KIND_EVENT_REMINDERS,)


def _owner_chat_id() -> int:
    """Owner Telegram id via the canonical get_secret path (env then the secret manager). 0 if unset."""
    from .secrets_loader import get_secret  # lazy: keeps this module import-side-effect-free
    raw = get_secret("TELEGRAM_OWNER_ID", default="") or ""
    try:
        return int(str(raw).strip() or "0")
    except (TypeError, ValueError):
        return 0


def seed_schedules() -> int:
    """Ensure exactly one pending schedule_queue row per _SEED_KINDS. Returns the count seeded.

    Skips everything (loud WARNING, returns 0) if the owner id is unset, so a misconfiguration
    can never seed chat_id=0 — but that same skip means the reminder is NOT armed, so the WARNING
    below is the deploy tripwire: the post-deploy check asserts the POSITIVE 'seeded ...' line."""
    owner = _owner_chat_id()
    if not owner:
        log.warning("schedule seeder: TELEGRAM_OWNER_ID unresolved — NOTHING seeded "
                    "(event reminders would be silently lost; provision the owner id)")
        return 0
    payload = {"chat_id": owner}
    seeded = 0
    for kind in _SEED_KINDS:
        cancel_key = f"schedule:{kind}"
        schedule_queue.cancel(cancel_key)                 # drop any stale pending row (idempotent)
        due = schedule_jobs.next_due_fn(kind, payload)    # next occurrence, strictly future
        if due is None:
            log.warning("schedule seeder: no next_due for kind=%s — skipped", kind)
            continue
        schedule_queue.add_scheduled(kind, due, payload, cancel_key=cancel_key)
        due_iso = datetime.fromtimestamp(due, timezone.utc).isoformat()
        # Breadcrumb for post-deploy verification (private db is unreadable over read-only ssh).
        # kind + due (UTC ISO) + total pending — NEVER the chat_id value (it is the owner id).
        log.info("seeded %s due=%s pending=%d", kind, due_iso, schedule_queue.pending_count())
        seeded += 1
    return seeded


async def seed_precise_reminders() -> int:
    """Worker-side startup reconcile for one-shot PRECISE task reminders (each fires once at an
    ARBITRARY per-task due instant, unlike the fixed-daily _SEED_KINDS above). Scans Todoist
    WORKER-SIDE (the worker holds TODOIST_API_KEY) and arms ONE value-free schedule_queue row per
    future time-specific task via reminder_lib.arm_precise_reminder (cancel-then-add by precise:{task_id},
    idempotent across restarts). Persistent rows survive restarts, so this is a RECONCILE (pick up
    tasks created/edited while down + arm existing ones on first deploy), not a rebuild-from-empty.

    Privacy-preserving by design: the task title is read and sent ONLY worker-side at fire time
    (schedule_jobs._precise_reminder), never logged in plaintext nor sent by the keyless receiver.
    Returns the count armed (logged as a COUNT only).

    Skips everything (loud WARNING, returns 0) if the owner id is unset (never arms chat_id=0)."""
    owner = _owner_chat_id()
    if not owner:
        log.warning("precise seeder: TELEGRAM_OWNER_ID unresolved — NOTHING seeded "
                    "(precise reminders would be silently lost; provision the owner id)")
        return 0
    from . import storage, reminder_lib  # LAZY: keeps this module import-side-effect-free
    try:
        tasks = await storage.list_tasks()
    except Exception as e:  # noqa: BLE001 -- a fetch hiccup must not crash the worker startup
        log.warning("precise seeder: list_tasks failed (class=%s) — NOTHING seeded", type(e).__name__)
        return 0
    armed = 0
    for t in tasks:
        if t.get("parent_id"):
            continue  # subtasks never carry their own reminder (mirrors the old rescan)
        due = t.get("due_date")
        if not due or "T" not in due:
            continue  # date-only tasks have no precise instant
        if reminder_lib.arm_precise_reminder(t["id"], due, owner):
            armed += 1
    # Breadcrumb for post-deploy verification: counts only, NEVER any task title or the chat_id value.
    log.info("precise seeder: armed=%d pending=%d", armed, schedule_queue.pending_count())
    return armed
