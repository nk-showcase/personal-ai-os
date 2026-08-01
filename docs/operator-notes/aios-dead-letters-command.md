# Operator note: `/aios_dead_letters` — owner-visible dead-letter summary

> Status: **active in the bot binary**. Required surface before any future flip of
> the async write route (`worker_route.<domain>`) to `on`.

## What it is

A **read-only, owner-only** Telegram slash command that prints a
value-free aggregate summary of write-type requests that have
landed in a terminal failure state (`dead` / `expired`) in the
durable write queue.

- **Command:** `/aios_dead_letters`
- **Where:** any chat with the bot — but the bot replies **only** if
  the caller's Telegram id equals `TELEGRAM_OWNER_ID`.
- **Access gate:** silent no-op for any non-owner caller (the bot
  does **not** reply at all to non-owners — same pattern as
  `/bridge`).
- **Module:** `bot/aios_dead_letters.py` (helper) +
  `bot/aios_dead_letters_handler.py` (Telegram handler).
- **Registered in:** `bot/main.py` via `CommandHandler(
  OWNER_COMMAND_NAME, cmd_aios_dead_letters)`. The command name is
  pulled from the helper module so the slash command and the data
  layer cannot drift.

## Why it exists

The worker contract introduces an `ACCEPTED` outcome: when an
integrations-worker is busy and cannot apply a write within the
bot's short poll window, the user-facing reply is "accepted for
processing" and the row stays in the queue. The worker may later
succeed (`done`) — or transition to `dead` (max-attempts exhausted)
or `expired` (TTL elapsed).

A `dead`/`expired` row means **the bot already told the user
"accepted for processing" but the write never actually happened**. The
pre-cutover audit explicitly calls this out as a **BLOCKER** for
flipping the async write route to `on`: a route that can
silently drop a user-acknowledged write must not be turned on without
an owner-visible recovery surface.

`/aios_dead_letters` **is** that surface. As long as the command
exists, is registered, and is documented for the operator, the
visibility precondition is satisfied.

## When to use

- **Routinely.** Whenever the operator wants to know whether any
  async writes have died since last check.
- **After a worker outage.** If the integrations-worker was offline
  for a stretch (process crash, VPS restart, network issue), some
  rows may have aged out. The command reports the count.
- **Before flipping any write-route flag.** Any planned change to
  a domain's write mode, read source, shadow-compare, or worker-route
  flag should be preceded by a glance at the dead-letter state.
- **As part of a future pre-cutover checklist.** The operator
  must see zero (or known-cleared) dead letters before approving
  the route-on flip.

## What it shows (allowed output)

The command replies with a single value-free message:

- **Total counts:** `dead`, `expired`.
- **Breakdown by kind:** counts for the closed enum of WRITE kinds.
  READ kinds are deliberately excluded (a stale read is benign;
  only writes need owner visibility).
- **Breakdown by error class:** sanitized identifier-only class
  names (e.g. `HTTPStatusError`, `ConnectionError`, `RuntimeError`),
  truncated to 64 chars; non-conforming values are replaced with
  the placeholder `(non_identifier_error_class)`; `NULL`
  error_class shows as `(no_error_class)`. The breakdown is capped
  at the top 20 most frequent classes with a `(other N classes): K`
  rollup so the rendered message stays under Telegram's 4096-char
  limit even at thousands of dead rows.
- **Age buckets:** under an hour / under a day / over a day
  (counted from `resolved_ts` or `created_ts` as fallback).
- **Attempts:** max / total across the dead/expired rows.

Empty queue: the reply is a plain "dead-letter queue is empty".

## What it never shows (forbidden output)

The command is **value-free** by design. The following are **never**
included in the reply text or in any helper return:

- **payload contents** of any row (no `payload_json` body);
- **result contents** of any row (no `result_json` body);
- **free text** typed by the owner;
- **account or label names** of any row;
- **target external ids / source page ids** of any row;
- **correlation_id values** of any row;
- **environment values, secrets, tokens, DB dumps**, raw SQL rows,
  full exception messages, or any per-row identifier.

If a future contributor accidentally stores a raw exception message
(e.g. `str(e)`) in the `error_class` column, the helper's defensive
whitelist (`^[A-Za-z_][A-Za-z0-9_]{0,63}$`) replaces it with the
constant placeholder `(non_identifier_error_class)`. The raw
message **does not** reach the owner.

## What it does NOT do

- **No auto-retry.** Dead rows are terminal. The command does
  **not** attempt to re-enqueue them. Recovery is a separate,
  explicit operator action (this block deliberately did **not**
  ship a requeue helper).
- **No auto-purge.** Viewing the summary does **not** mutate any
  row. The dead/expired rows stay in the write queue for
  operator inspection until a future explicit purge / requeue
  block. **Tested:** the row count before and after the command
  is identical.
- **No startup push (Phase 1).** The bot does **not** automatically
  send the owner a dead-letter notification at startup. The
  command is the Phase 1 owner-visible surface. A helper
  `check_dead_letters_on_startup()` exists for the future wiring
  block — its docstring carries a loud `CALLER CONTRACT` warning
  that any auto-push **must** gate on `TELEGRAM_OWNER_ID` and use
  `chat_id = TELEGRAM_OWNER_ID`.
- **No live Notion / Todoist / secret-manager contact.** The
  command reads only the local SQLite queue. The handler does not
  import the legacy direct-Notion write layer.

## Robustness — what the owner sees when something goes wrong

- **Empty queue (normal):** a plain "dead-letter queue is empty".
- **Many dead rows, many distinct error classes:** breakdown
  capped at top 20 classes plus an `(other N classes): K` rollup.
- **Helper internal error** (DB lock, schema mismatch, sqlite
  file missing, etc.): the handler catches the exception and
  replies with a value-free "dead-letter summary is temporarily
  unavailable (internal queue-read error — see log)". The actual
  exception class name is logged server-side (class name only, never
  the message body). The owner is **never** left with a silent
  no-reply due to an internal helper error — that would defeat the
  entire point of this surface.

## Why this blocks route-on until accepted

The pre-cutover audit lists the dead-letter owner-visibility surface
as a hard precondition:

> "An ACCEPTED write that later dies (dead/expired) must be
> OWNER-VISIBLE. ... If no notifier/owner-surface exists, MARK AS
> BLOCKER and do NOT flip the worker route on until it does. No
> silent dead rows; no permanent 'accepted-for-processing' state."

This command closes that precondition. The **other** route-on
preconditions remain pending:

- Distinct OS user `ai-os-integrations` provisioned on the VPS
  (currently the systemd template references the user but the
  user must be created out of band).
- `${AIOS_INTEGRATIONS_HOME}/.ai-os/env/integrations-worker.env`
  populated with the real Notion API key and per-service secret
  cache (managed via the secret manager; **never** committed,
  **never** printed).
- Real Notion-backed applier injected into integrations-worker
  (the skeleton currently uses a fail-loud inert stub).
- Systemd unit `aios-integrations-worker.service` installed into
  `/etc/systemd/system/`, enabled, and started (currently a
  target-only template, not installed).
- Production DB `~/.ai-os/data/aios.sqlite3` backed up with a
  documented restore drill before the route-on flip.
- Owner approval recorded for the route-on flip.
- Optionally: a future block that wires `check_dead_letters_on_
  startup` as an auto-push, **after** review against the loud
  `CALLER CONTRACT` documented on that helper.

Until all of those land **and** the owner explicitly approves,
the worker route stays at its default `'off'` and every write
call goes through the legacy direct-Notion path unchanged.

## File map

| File | Role |
|---|---|
| `bot/aios_dead_letters.py` | Value-free helper (`summarize_dead_letters`, `format_summary_owner_text`, `check_dead_letters_on_startup`) |
| `bot/aios_dead_letters_handler.py` | Owner-only Telegram handler `cmd_aios_dead_letters` with `try/except` value-free fallback |
| `bot/main.py` | Registers `CommandHandler(OWNER_COMMAND_NAME, cmd_aios_dead_letters)` via lazy import |
| `scripts/test_aios_dl01_owner_visible_dead_letters.py` | Plain-python tests (temp DB only, fake Telegram update, no live services) |
