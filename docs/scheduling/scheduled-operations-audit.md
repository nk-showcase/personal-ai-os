# Scheduled-operations audit — plan

> **Principle: never move anything to cron blindly.** First find ALL current scheduled operations, understand what each one does, and only then decide where it should live. This is a plan for a future audit, not an action.

## What counts as a scheduled operation
Any logic that runs not in response to a user message, but on a timer / in the background / periodically.

## What to find and check (inventory)
1. **python-telegram-bot `job_queue`** — every registered job (`run_daily`, `run_repeating`, `run_once`). Where they are registered, on what schedule, and what they do.
2. **asyncio / background loops** — any `asyncio.create_task`, infinite `while` loops, background coroutines that run alongside the main bot.
3. **Periodic health/usage checks** — any daily check plus a reactive ping (for example, a threshold alert). These are JobQueue tasks.
4. **Reminders** — `bot/reminder_lib.py` and the schedule modules (`bot/schedule_jobs.py`, `bot/schedule_queue.py`): user reminders, their triggers and state.
5. **Daily / periodic jobs** — any daily/periodic tasks (digests, syncs, cleanups), if present.

For each task found, record: name, schedule/trigger, what it does, which secrets/resources it touches, and whether it holds state (stateful).

## Where to send it (target runtime) — pick ONE of three
For each task, determine the target runtime:

| Option | When it fits |
|---|---|
| **a) systemd long-running service** | the task must run continuously (an always-on process): the bot itself, workers, queue listeners |
| **b) cron / systemd timer** | the task is periodic and needs no persistent process: backup, healthcheck, periodic checks, digest |
| **c) keep inside bot logic** | if it is a user-specific reminder or a stateful Telegram flow (depends on the conversation/user state in the bot) — do not move it out |

## Boundary (important)
**cron / systemd timer is NOT the basis of the coding flow.** Coding runs through the always-on executor (option "a"), not on a schedule. But cron/timer **do** fit auxiliary scheduled operations: backup, healthcheck, periodic checks, digest.

## Audit result (what should come out of it)
- A table: task → what it does → stateful? → target runtime (a/b/c) → rationale.
- An explicit list: what moves to a systemd service, what to cron/timer, what stays in the bot.
- Nothing is moved until it has been through this review.
