"""bot/aios_dead_letters.py -- DL-01 owner-visible dead-letter summary.

INERT BY DEFAULT.  Read-only over request_queue (B-02C
schema).  Returns a value-free aggregate dictionary suitable for an
owner-only Telegram command (cmd_aios_dead_letters in
:mod:`bot.aios_dead_letters_handler`).

Purpose (per DL-01 spec):
  When the bot has acknowledged a write as ACCEPTED ("accepted for
  processing") but the worker-side row later transitions to ``dead``
  or ``expired``, the owner must SEE it.  No silent dead rows.  No
  permanent "accepted for processing".  No payload / activity /
  account name leaking through the visibility surface.

Scope:
  * READ-ONLY.  Does NOT mutate any request_queue row.
  * Aggregates over WRITE-kind rows ONLY (the user-visible
    ACCEPTED contract is for writes; reads expire silently per
    the B-02D contract and do not need owner visibility).
  * Returns only counts / class names / age buckets / attempts.
  * NEVER returns / prints / logs:
      - row payload-blob contents
      - row result-blob contents
      - activity strings
      - account names
      - source_notion_id values (or any external row id)
      - target_external_id values
      - correlation_id values (the owner does not need per-row
        identifiers for visibility; ops-level forensics is a
        separate operator path, NOT DL-01)
      - secrets / env values / DB dumps

DL-01 inertness:
  * Default routing flag stays off (legacy path wins).  When off,
    no enqueue happens, so this helper returns all-zero summaries.
  * Does NOT auto-send Telegram messages.  The startup-push
    HELPER (check_dead_letters_on_startup) is implemented + tested
    but is NOT auto-called from main.py in DL-01.  A future block
    may wire it.
  * Does NOT requeue dead rows.  Manual recovery is the operator's
    explicit step (a future helper may add owner-only requeue
    under the SAME correlation_id; DL-01 keeps it manual-only).
  * Does NOT touch finance_ / claude_bridge / unrelated domains.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from . import aios_request_queue as RQ


# Age bucket thresholds (seconds).
AGE_BUCKET_LT_1H_SECONDS = 60 * 60
AGE_BUCKET_LT_24H_SECONDS = 60 * 60 * 24

# Owner command name.  Imported by aios_dead_letters_handler and
# registered in bot.main.  Kept here so the data layer owns the
# canonical command name.
OWNER_COMMAND_NAME = "aios_dead_letters"

# Tag used in dead-letter summary when error_class is NULL on a
# row (defensive; the TTL-sweep path leaves error_class NULL while
# the worker-failure path records the exception class name).
_NO_ERROR_CLASS_TAG = "(no_error_class)"

# Review-fix: defensive whitelist for error_class values that DL-01
# emits into user-visible text.  B-02C/D promise error_class is a
# sanitized Python-identifier prefix (class name only), but if a
# future contributor accidentally stores str(e) or e.args[0]
# instead, the raw exception MESSAGE (which can contain URLs,
# account names, payload fragments) would otherwise leak into the
# owner's Telegram reply.  DL-01 RE-validates on the way out and
# replaces non-conforming values with this placeholder.
_ERROR_CLASS_WHITELIST_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_NON_IDENTIFIER_ERROR_CLASS_TAG = "(non_identifier_error_class)"

# Telegram single-message hard cap is 4096 chars.  DL-01 caps the
# error_class breakdown at this many distinct values; any further
# distinct values are rolled into '(other: N)' so the rendered
# text stays under the limit even at thousands of dead rows.
_ERROR_CLASS_TOP_N = 20


def summarize_dead_letters(
    *,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> dict:
    """Aggregate value-free summary of WRITE rows in terminal-
    failure states.

    Per B-02C semantics:
      * WRITE rows with elapsed TTL transition to ``dead`` (owner-
        visible, per addendum #3).
      * WRITE rows whose worker reports terminal failure with
        attempts >= max_attempts also land in ``dead`` (via the
        B-02C worker-fail path).
      * WRITE rows should NOT land in ``expired`` via normal paths
        (expire_due_requests routes WRITE -> ``dead``); we still
        count ``expired`` WRITE rows DEFENSIVELY in case a future
        contributor changes the mapping or an out-of-band edit
        leaves a row in that state.

    Returns dict::

        {
            'dead_total':    int,     # WRITE rows in 'dead'
            'expired_total': int,     # WRITE rows in 'expired'
                                      # (defensive; usually 0)
            'by_kind': {kind: count, ...},        # non-zero only
            'by_error_class': {cls: count, ...},  # non-zero only;
                                                  # NULL -> tag
                                                  # _NO_ERROR_CLASS_TAG
            'by_age_bucket': {        # by resolved_ts age
                'lt_1h':  int,
                'lt_24h': int,
                'older':  int,
            },
            'attempts_max':   int,    # max attempts across rows
            'attempts_total': int,    # sum of attempts across rows
        }

    NEVER includes correlation_id values, payload, account names,
    activity text, source_notion_id, or any per-row identifier.
    """
    from . import aios_storage as S
    now = float(now_fn())
    write_kinds = RQ.WRITE_KINDS  # tuple from B-02C
    # Review-fix: defensive guard against an empty WRITE_KINDS tuple.
    # If a future B-02C edit empties the tuple, the SQL would otherwise
    # become "kind IN ()" which is invalid SQLite -> the helper would
    # raise and the visibility surface would silently die.
    if not write_kinds:
        return {
            "dead_total": 0,
            "expired_total": 0,
            "by_kind": {},
            "by_error_class": {},
            "by_age_bucket": {"lt_1h": 0, "lt_24h": 0, "older": 0},
            "attempts_max": 0,
            "attempts_total": 0,
        }
    kind_placeholders = ",".join(["?"] * len(write_kinds))
    out_by_kind: dict = {}
    out_by_error: dict = {}
    bucket_lt_1h = 0
    bucket_lt_24h = 0
    bucket_older = 0
    dead_total = 0
    expired_total = 0
    attempts_max = 0
    attempts_total = 0
    with S.connect(db=db) as conn:
        rows = conn.execute(
            "SELECT status, kind, error_class, attempts, "
            "       resolved_ts, created_ts "
            "FROM request_queue "
            "WHERE status IN ('dead', 'expired') "
            "  AND kind IN (" + kind_placeholders + ")",
            tuple(write_kinds),
        ).fetchall()
    for r in rows:
        status = r["status"]
        kind = r["kind"]
        raw_err = r["error_class"]
        if raw_err is None or raw_err == "":
            err = _NO_ERROR_CLASS_TAG
        elif _ERROR_CLASS_WHITELIST_RE.match(raw_err):
            err = raw_err
        else:
            # Defensive: B-02C/D promise a Python-identifier prefix,
            # but if the value is anything else (e.g. raw str(e) text
            # with URLs / account names / payload fragments), replace
            # it with a constant placeholder.  Avoids leaking
            # exception messages into the owner-visible summary.
            err = _NON_IDENTIFIER_ERROR_CLASS_TAG
        attempts = int(r["attempts"] or 0)
        # Age computed from resolved_ts (set on terminal transition)
        # or created_ts as a fallback.
        age_ref = r["resolved_ts"]
        if age_ref is None:
            age_ref = r["created_ts"]
        age_sec = max(0.0, now - float(age_ref or 0))
        if status == "dead":
            dead_total += 1
        elif status == "expired":
            expired_total += 1
        out_by_kind[kind] = out_by_kind.get(kind, 0) + 1
        out_by_error[err] = out_by_error.get(err, 0) + 1
        if age_sec < AGE_BUCKET_LT_1H_SECONDS:
            bucket_lt_1h += 1
        elif age_sec < AGE_BUCKET_LT_24H_SECONDS:
            bucket_lt_24h += 1
        else:
            bucket_older += 1
        attempts_total += attempts
        if attempts > attempts_max:
            attempts_max = attempts
    return {
        "dead_total": dead_total,
        "expired_total": expired_total,
        "by_kind": out_by_kind,
        "by_error_class": out_by_error,
        "by_age_bucket": {
            "lt_1h": bucket_lt_1h,
            "lt_24h": bucket_lt_24h,
            "older": bucket_older,
        },
        "attempts_max": attempts_max,
        "attempts_total": attempts_total,
    }


def format_summary_owner_text(summary: dict) -> str:
    """Render the value-free summary as plain text.

    Output contains ONLY:
      * total counts
      * kind names (closed enum: notion_write, todoist_write,
        store_write, store_view)
      * error_class names (sanitized identifiers from B-02C/D)
      * age bucket counts
      * attempts numbers

    NEVER includes payload / account / activity / source ids /
    correlation_id values.
    """
    dead = int(summary.get("dead_total", 0))
    expired = int(summary.get("expired_total", 0))
    total = dead + expired
    if total == 0:
        return "Dead-letter queue is empty."
    lines = ["Dead-letter queue (write operations):",
             "  dead:    " + str(dead),
             "  expired: " + str(expired)]
    by_kind = summary.get("by_kind") or {}
    if by_kind:
        lines.append("By operation kind:")
        for k in sorted(by_kind.keys()):
            lines.append("  " + str(k) + ": " + str(by_kind[k]))
    by_error = summary.get("by_error_class") or {}
    if by_error:
        lines.append("By error class:")
        # Review-fix: cap to TOP-N most frequent classes so the
        # rendered text stays under Telegram's 4096-char single-
        # message limit even at thousands of dead rows.  Tie-break
        # by class name (lexicographic) for deterministic output.
        sorted_items = sorted(
            by_error.items(),
            key=lambda kv: (-int(kv[1]), str(kv[0])),
        )
        top = sorted_items[:_ERROR_CLASS_TOP_N]
        rest = sorted_items[_ERROR_CLASS_TOP_N:]
        for ec, count in top:
            lines.append("  " + str(ec) + ": " + str(count))
        if rest:
            rest_count = sum(int(c) for _, c in rest)
            lines.append("  (other " + str(len(rest))
                         + " classes): " + str(rest_count))
    ages = summary.get("by_age_bucket") or {}
    lines.append("By age:")
    lines.append("  under 1 hour: " + str(int(ages.get("lt_1h", 0))))
    lines.append("  under 1 day:  " + str(int(ages.get("lt_24h", 0))))
    lines.append("  over 1 day:   " + str(int(ages.get("older", 0))))
    lines.append("Attempts (max/total): "
                 + str(int(summary.get("attempts_max", 0))) + " / "
                 + str(int(summary.get("attempts_total", 0))))
    return "\n".join(lines)


def check_dead_letters_on_startup(
    *,
    db: Optional[str] = None,
    now_fn: Callable[[], float] = time.time,
) -> int:
    """Startup-helper: return the count of dead+expired WRITE rows.

    Implemented + tested but NOT auto-called from main.py in DL-01.
    A future block may wire it to send the owner a single-line
    notification at bot startup if count > 0.

    DOES NOT mutate any row.  DOES NOT send Telegram messages.

    CALLER CONTRACT (review-fix; loud warning for the future block
    that wires the startup-push):

      The COUNT returned here is value-free, but the DELIVERY
      SURFACE that ships the count to the owner is NOT.  Any
      Telegram send built on top of this helper MUST:

        1. Read the chat_id ONLY from bot.config.TELEGRAM_OWNER_ID
           (NEVER from arbitrary chat-context / message-source
           state).
        2. Send to chat_id = TELEGRAM_OWNER_ID; never to any other
           chat id (the count carries no row data but knowing that
           dead-letter rows exist is itself operator-only signal).
        3. Reuse the same owner-gate pattern as
           bot.aios_dead_letters_handler.cmd_aios_dead_letters
           (silent no-op for any non-owner caller path).
        4. NEVER include payload / activity / account / source_id
           strings in the rendered text -- only the integer count
           and (optionally) a short "summary ready" hint.

      If you cannot satisfy ALL FOUR, do NOT wire the auto-push;
      keep DL-01's command-only surface and address the gap in a
      separate owner-approved block.
    """
    summary = summarize_dead_letters(now_fn=now_fn, db=db)
    return int(summary["dead_total"] + summary["expired_total"])
