"""bot/aios_worker_contract.py -- inert worker contract.

INERT BY DEFAULT.  This module wires the queue API
(:mod:`bot.aios_request_queue`) into the *application-level*
bot/worker contract that the handlers and the integrations worker use.
It introduces NO new live behaviour:

* Does NOT contact live Notion / hosting-platform / secret-vault.
* Does NOT start a service, thread, loop, or systemd unit.
* Does NOT read or write any flag in :mod:`bot.aios_config`.
* Does NOT move any token (the Notion key or any other) -- the worker
  process loads tokens at start; this layer is purely deterministic
  Python over SQLite.
* The :func:`apply_claimed_request_with_stub` helper takes an INJECTED
  callable (a stub in tests; a real Notion-backed callable in production).

Surface (every name is a stable contract; B-02F builds on top of these):

  Constants
    DEFAULT_LEASE_SECONDS         (= 30)
    DEFAULT_WRITE_TTL_SECONDS     (= 600)   # >> 2 * lease (B-02C check)
    DEFAULT_READ_TTL_SECONDS      (= 90)
    DEFAULT_POLL_DEADLINE_SECONDS (= 10)    # strictly < 15 (Notion ceil)
    POLL_DEADLINE_CEILING_SECONDS (= 15)
    DEFAULT_POLL_INTERVAL_SECONDS (= 0.5)
    DEFAULT_MAX_ATTEMPTS          (= 10)
    DEFAULT_BACKOFF_BASE_SECONDS  (= 1.0)
    DEFAULT_BACKOFF_MAX_SECONDS   (= 300.0)
    LOCAL_FALLBACK_NOTE           (= "from the local copy")

  Enums + value objects
    PollResultState (Enum: WRITE_SAVED / WRITE_ACCEPTED / WRITE_DEAD /
                          READ_RESULT / READ_LOCAL_FALLBACK /
                          READ_UNAVAILABLE)
    PollOutcome     (frozen dataclass: state, correlation_id, result,
                     note, source_status); value-free repr

  Errors
    PollDeadlineError    (ValueError)

  Public functions
    build_stable_correlation_id(*, user_id, kind, business_key,
                                attempt_window=None) -> str
    enqueue_write_request(*, kind, user_id, business_key,
                          account=None, payload=None,
                          lease_seconds=DEFAULT_LEASE_SECONDS,
                          ttl_seconds=DEFAULT_WRITE_TTL_SECONDS,
                          attempt_window=None, now_fn=time.time,
                          db=None) -> str
    enqueue_read_request(*, kind, user_id, business_key,
                         account=None, payload=None,
                         ttl_seconds=DEFAULT_READ_TTL_SECONDS,
                         attempt_window=None, now_fn=time.time,
                         db=None) -> str
    poll_request_result(correlation_id, *,
                        deadline_seconds=DEFAULT_POLL_DEADLINE_SECONDS,
                        poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
                        local_fallback_fn=None,
                        sleep_fn=time.sleep, now_fn=time.time,
                        db=None) -> PollOutcome
    poll_outcome_to_user_outcome(poll_outcome) -> bot.aios_outcomes.Outcome
    apply_claimed_request_with_stub(record, *, applier) -> dict
    process_one_request(*, worker_id, applier, kinds=None, account=None,
                        max_attempts=DEFAULT_MAX_ATTEMPTS,
                        backoff_base_seconds=DEFAULT_BACKOFF_BASE_SECONDS,
                        backoff_max_seconds=DEFAULT_BACKOFF_MAX_SECONDS,
                        now_fn=time.time, db=None) -> Optional[str]
    sweep_request_queue(*, lease_seconds=DEFAULT_LEASE_SECONDS,
                        now_fn=time.time, db=None) -> dict

Stable-correlation invariant (addendum #1):

  build_stable_correlation_id is a pure deterministic function of its
  inputs (sha256 of a canonical "user_id|kind|business_key[|window]"
  string).  Two calls with the same inputs return the same hex digest;
  the B-02C UNIQUE index on ``correlation_id`` then makes
  :func:`enqueue_request` idempotent on retry -- the second attempt
  reuses the first row instead of creating a second one.  This is the
  invariant that PREVENTS double-credit on user-triggered retry.

TTL / lease invariant (addendum #3):

  Every WRITE enqueue passes ttl_seconds; B-02C
  :func:`bot.aios_request_queue.enqueue_request` fail-closes if
  it is missing.  At config time, the caller verifies
  ``ttl_seconds >= RECOMMENDED_TTL_LEASE_RATIO * lease_seconds`` via
  :func:`bot.aios_request_queue.check_write_ttl_over_lease`
  (we call this inside :func:`enqueue_write_request` as a belt-and-
  braces check).

Backoff invariant (addendum #4):

  :func:`process_one_request` ALWAYS computes ``next_attempt_ts`` via
  :func:`bot.aios_request_queue.compute_backoff` before calling
  :func:`bot.aios_request_queue.mark_failed_or_dead`.  No
  ``next_attempt_ts=None`` path exists for failure -- there is no
  implicit immediate retry.

Sweep ordering invariant (addendum #7):

  :func:`sweep_request_queue` calls ``recover_stale_claims`` BEFORE
  ``expire_due_requests``.  A WRITE row whose worker crashed mid-claim
  must be recovered to ``failed`` BEFORE the TTL sweep would otherwise
  flip it to ``dead`` -- otherwise an in-flight crashed retry would
  be silently lost.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Optional

from . import aios_outcomes as O
from . import aios_request_queue as RQ


# ---- Constants ------------------------------------------------------------------

DEFAULT_LEASE_SECONDS = 30
DEFAULT_WRITE_TTL_SECONDS = 600
DEFAULT_READ_TTL_SECONDS = 90
DEFAULT_POLL_DEADLINE_SECONDS = 10
POLL_DEADLINE_CEILING_SECONDS = 15
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_MAX_ATTEMPTS = 10
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 300.0

LOCAL_FALLBACK_NOTE = "from the local copy"


# ---- Errors ---------------------------------------------------------------------

class PollDeadlineError(ValueError):
    """Raised when ``deadline_seconds`` is not strictly less than the
    POLL_DEADLINE_CEILING_SECONDS (Notion's 15-second envelope).
    """


# ---- PollOutcome value object ---------------------------------------------------

class PollResultState(str, Enum):
    """Closed-set classification returned by :func:`poll_request_result`.

    The B-02F handler layer will call :func:`poll_outcome_to_user_outcome`
    to translate these into :class:`bot.aios_outcomes.Outcome` instances
    for the user-facing reply.

    Mapping (frozen contract):

        WRITE_SAVED        -> Outcome.SAVED              ("saved")
        WRITE_ACCEPTED     -> Outcome.ACCEPTED           ("accepted for
                                                          processing")
        WRITE_DEAD         -> Outcome.STORAGE_UNAVAILABLE("could not save")
        READ_RESULT        -> Outcome.SAVED              ("saved"
                                                          -- read success)
        READ_LOCAL_FALLBACK-> Outcome.SAVED              (+note "from the
                                                          local copy")
        READ_UNAVAILABLE   -> Outcome.STORAGE_UNAVAILABLE("could not
                                                          save")
    """
    WRITE_SAVED = "write_saved"
    WRITE_ACCEPTED = "write_accepted"
    WRITE_DEAD = "write_dead"
    READ_RESULT = "read_result"
    READ_LOCAL_FALLBACK = "read_local_fallback"
    READ_UNAVAILABLE = "read_unavailable"


@dataclass(frozen=True)
class PollOutcome:
    """Value-free poll result.

    ``result`` holds the worker's structured return dict (READ payload
    or WRITE confirmation); for poll-deadline / dead / unavailable
    branches it is ``None``.  ``note`` carries the
    :data:`LOCAL_FALLBACK_NOTE` literal only when the state is
    ``READ_LOCAL_FALLBACK``; never any other free text.
    ``source_status`` snapshots the underlying queue-row status at the
    moment the decision was made (useful for tests and audit -- value-
    free since status is a closed enum).

    :meth:`__repr__` redacts ``result`` to a structural summary (key
    list + length) so an accidental ``logger.debug(poll)`` cannot dump
    account names or amounts from a result payload.
    """
    state: PollResultState
    correlation_id: str
    result: Optional[dict]
    note: Optional[str]
    source_status: Optional[str]

    def __repr__(self) -> str:  # type: ignore[override]
        # Value-free repr: never dump payload values.  Defensive against
        # non-dict result types (a future READ result could be a list of
        # dicts -- e.g. read_recent returning a list of items) so
        # logger.debug(po) cannot crash AND cannot leak.
        result_summary = _summarize_result_value_free(self.result)
        return (
            "PollOutcome("
            "state=" + repr(self.state.value)
            + ", correlation_id=" + repr(self.correlation_id)
            + ", result=" + result_summary
            + ", note=" + repr(self.note)
            + ", source_status=" + repr(self.source_status)
            + ")"
        )


def _summarize_result_value_free(result: Any) -> str:
    """Render a structural summary of ``result`` that NEVER leaks
    values.  Defensive against dict / list-of-dicts / list-of-other /
    scalar / None.  Returns a single-line str safe to embed in repr.
    """
    if result is None:
        return "None"
    if isinstance(result, dict):
        return "dict(keys=" + repr(sorted([str(k) for k in result.keys()])) + ")"
    if isinstance(result, list):
        n = len(result)
        if n == 0:
            return "list(empty)"
        # If every element is a dict, aggregate unique key names; never
        # values.  Otherwise just report the type.
        if all(isinstance(item, dict) for item in result):
            keys: set = set()
            for item in result:
                for k in item.keys():
                    keys.add(str(k))
            return "list(len=" + str(n) + ",item_keys=" + repr(sorted(keys)) + ")"
        return "list(len=" + str(n) + ",item_types=" + repr(sorted({type(x).__name__ for x in result})) + ")"
    return "<" + type(result).__name__ + ">"


# ---- correlation_id minting -----------------------------------------------------

def build_stable_correlation_id(
    *,
    user_id: Any,
    kind: str,
    business_key: Any,
    attempt_window: Optional[Any] = None,
) -> str:
    """Return a deterministic 64-char hex correlation_id.

    Pure function of inputs.  Same ``(user_id, kind, business_key
    [, attempt_window])`` -> same hex digest -> B-02C :func:`enqueue_
    request` will reuse the existing row (UNIQUE on correlation_id),
    so the user's natural retry is idempotent and CANNOT double-credit
    a WRITE.

    The ``attempt_window`` parameter is optional.  Use it when the
    SAME logical action should yield a NEW id on a new day (e.g.
    pass the local-timezone date string for a "daily read"); leave it None
    when the business_key alone identifies the action across all time.

    Args:
        user_id: caller's user identifier.  Coerced to str; ``None``
                 is allowed (encoded as the literal "None").
        kind:    one of :data:`bot.aios_request_queue.
                 ALLOWED_KINDS`; validated.
        business_key: caller's stable semantic key for the action
                      (e.g. for ``store_write``: a "domain + item id"
                      tuple expressed as a str).
        attempt_window: optional coarse time bucket (e.g.
                        "2026-06-07"); two enqueues with the
                        same business_key but different attempt_window
                        get DIFFERENT ids.

    Returns:
        A 64-char lowercase hex sha256 digest.
    """
    if not isinstance(kind, str):
        raise ValueError("kind must be str")
    if kind not in RQ.ALLOWED_KINDS:
        raise RQ.InvalidKindError(
            "kind not in ALLOWED_KINDS"
        )
    # Use JSON canonical encoding (not a "|"-joined str) so that
    # (a) None and the literal string "None" are DISTINCT (None
    #     encodes as JSON null; "None" encodes as the string "None");
    # (b) int 42 and str "42" are DISTINCT (JSON encodes them
    #     differently);
    # (c) a business_key containing the literal "|" character cannot
    #     collide with a different (user_id, kind, attempt_window)
    #     tuple via boundary ambiguity.
    # The shape is a fixed-length list so attempt_window=None always
    # occupies the same slot (vs the old append-or-skip pattern that
    # made attempt_window=None and attempt_window="None" collide).
    canonical = json.dumps(
        [user_id, kind, business_key, attempt_window],
        sort_keys=False,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


# ---- Enqueue helpers ------------------------------------------------------------

def enqueue_write_request(
    *,
    kind: str,
    user_id: Any,
    business_key: Any,
    account: Optional[str] = None,
    payload: Optional[dict] = None,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ttl_seconds: float = DEFAULT_WRITE_TTL_SECONDS,
    attempt_window: Optional[Any] = None,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> str:
    """Enqueue a WRITE request with the B-02D contract guarantees.

    1. Validates ``kind`` is in :data:`bot.aios_request_queue.
       WRITE_KINDS` (the integrations-worker WRITE set).
    2. Validates ``ttl_seconds >= RECOMMENDED_TTL_LEASE_RATIO *
       lease_seconds`` via :func:`bot.aios_request_queue.
       check_write_ttl_over_lease`.
    3. Mints a stable correlation_id via
       :func:`build_stable_correlation_id`.
    4. Calls :func:`bot.aios_request_queue.enqueue_request`
       (B-02C); a duplicate correlation_id is silently de-duplicated
       (idempotent retry).

    Returns the correlation_id.
    """
    if kind not in RQ.WRITE_KINDS:
        raise ValueError(
            "enqueue_write_request: kind '" + str(kind)
            + "' is not a WRITE kind"
        )
    RQ.check_write_ttl_over_lease(
        ttl_seconds=ttl_seconds, lease_seconds=lease_seconds,
    )
    cid = build_stable_correlation_id(
        user_id=user_id, kind=kind, business_key=business_key,
        attempt_window=attempt_window,
    )
    RQ.enqueue_request(
        kind=kind, account=account, payload=payload,
        ttl_seconds=ttl_seconds, correlation_id=cid,
        now_fn=now_fn, db=db,
    )
    return cid


def enqueue_read_request(
    *,
    kind: str,
    user_id: Any,
    business_key: Any,
    account: Optional[str] = None,
    payload: Optional[dict] = None,
    ttl_seconds: float = DEFAULT_READ_TTL_SECONDS,
    attempt_window: Optional[Any] = None,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> str:
    """Enqueue a READ request with the B-02D contract guarantees.

    1. Validates ``kind`` is in :data:`bot.aios_request_queue.
       READ_KINDS`.
    2. Mints a stable correlation_id.
    3. Calls :func:`bot.aios_request_queue.enqueue_request`.

    READS do NOT require TTL (B-02C allows NULL), but B-02D defaults
    to :data:`DEFAULT_READ_TTL_SECONDS` so stale READS get reaped.

    Returns the correlation_id.
    """
    if kind not in RQ.READ_KINDS:
        raise ValueError(
            "enqueue_read_request: kind is not a READ kind"
        )
    # Per addendum #3 + reviewer finding: the B-02C layer permits
    # ttl_seconds=None for READ kinds, but at the B-02D contract layer
    # we require an explicit positive number so READ rows always get
    # reaped by the TTL sweep and never pile up indefinitely.
    if ttl_seconds is None:
        raise ValueError(
            "enqueue_read_request: ttl_seconds is REQUIRED (None forbidden "
            "at B-02D layer so READ rows never accumulate unbounded)"
        )
    if isinstance(ttl_seconds, bool) or not isinstance(
            ttl_seconds, (int, float)):
        raise ValueError(
            "enqueue_read_request: ttl_seconds must be a number"
        )
    if math.isnan(float(ttl_seconds)) or math.isinf(float(ttl_seconds)):
        raise ValueError(
            "enqueue_read_request: ttl_seconds must be a finite number"
        )
    if float(ttl_seconds) <= 0:
        raise ValueError(
            "enqueue_read_request: ttl_seconds must be positive"
        )
    cid = build_stable_correlation_id(
        user_id=user_id, kind=kind, business_key=business_key,
        attempt_window=attempt_window,
    )
    RQ.enqueue_request(
        kind=kind, account=account, payload=payload,
        ttl_seconds=ttl_seconds, correlation_id=cid,
        now_fn=now_fn, db=db,
    )
    return cid


# ---- Poll ----------------------------------------------------------------------

_WRITE_STATUS_TERMINAL_DEAD = frozenset({RQ.STATUS_DEAD, RQ.STATUS_EXPIRED})


def poll_request_result(
    correlation_id: str,
    *,
    deadline_seconds: float = DEFAULT_POLL_DEADLINE_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    local_fallback_fn: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> PollOutcome:
    """Bot-side short-window poll on a queue row.

    Sleeps in :data:`poll_interval_seconds` ticks until ``now >= start
    + deadline_seconds``, re-reading the row each tick.

    Outcome mapping (closed contract):

      WRITE row, status=done                  -> WRITE_SAVED + result
      WRITE row, status=dead OR expired       -> WRITE_DEAD
      WRITE row, deadline elapsed, NOT done   -> WRITE_ACCEPTED
      READ row, status=done                   -> READ_RESULT + result
      READ row, status=dead OR expired
                          OR deadline elapsed
        AND local_fallback_fn provided        -> READ_LOCAL_FALLBACK +
                                                  fallback result +
                                                  note=LOCAL_FALLBACK_NOTE
        AND no local_fallback_fn              -> READ_UNAVAILABLE

    Notes:
      * READS are NEVER mapped to WRITE_ACCEPTED.  A read that hasn't
        landed in the deadline window is either a local-fallback (with
        the explicit "from the local copy" label) or a frank
        unavailable -- never the optimistic "accepted for processing" reply that
        is reserved for a durably queued WRITE.
      * No fake empty result.  ``READ_UNAVAILABLE`` carries
        ``result=None`` (not ``{}`` or ``[]``).  Handlers must show
        the user an explicit "unavailable" message, NOT silently
        report an empty success.

    Args:
        correlation_id: as returned by enqueue_*_request.
        deadline_seconds: poll budget; MUST be strictly less than
                          :data:`POLL_DEADLINE_CEILING_SECONDS` (15s
                          Notion envelope).  Raises
                          :class:`PollDeadlineError` otherwise.
        poll_interval_seconds: sleep between polls.
        local_fallback_fn: optional callable invoked when a READ
                           cannot be served from the queue in time.
                           Called as ``local_fallback_fn(record)``.
                           Return value goes into PollOutcome.result.
                           If None, no fallback is attempted.
        sleep_fn / now_fn: injectable for tests.
        db: passed through to :mod:`bot.aios_request_queue`.

    Returns a :class:`PollOutcome`.
    """
    if not isinstance(deadline_seconds, (int, float)) or isinstance(
            deadline_seconds, bool):
        raise PollDeadlineError("deadline_seconds must be a number")
    if math.isnan(float(deadline_seconds)):
        raise PollDeadlineError("deadline_seconds must not be NaN")
    if math.isinf(float(deadline_seconds)):
        raise PollDeadlineError("deadline_seconds must not be infinite")
    if deadline_seconds <= 0:
        raise PollDeadlineError("deadline_seconds must be positive")
    if deadline_seconds >= POLL_DEADLINE_CEILING_SECONDS:
        raise PollDeadlineError(
            "deadline_seconds must be strictly < "
            + str(POLL_DEADLINE_CEILING_SECONDS)
            + " (Notion ceiling)"
        )
    if not isinstance(poll_interval_seconds, (int, float)) or isinstance(
            poll_interval_seconds, bool):
        raise PollDeadlineError("poll_interval_seconds must be a number")
    if math.isnan(float(poll_interval_seconds)) or math.isinf(
            float(poll_interval_seconds)):
        raise PollDeadlineError(
            "poll_interval_seconds must be a finite number"
        )
    if poll_interval_seconds <= 0:
        raise PollDeadlineError("poll_interval_seconds must be positive")
    # poll_interval > deadline would make the loop block longer than
    # the caller's budget and bust Notion's 15-second envelope.
    if poll_interval_seconds >= deadline_seconds:
        raise PollDeadlineError(
            "poll_interval_seconds must be strictly < deadline_seconds"
        )

    start = float(now_fn())
    deadline = start + float(deadline_seconds)
    rec = None
    while True:
        rec = RQ.get_request(correlation_id, db=db)
        if rec is None:
            raise ValueError(
                "poll_request_result: no row with correlation_id "
                + repr(correlation_id)
            )
        is_write = rec.kind in RQ._WRITE_KINDS_SET
        # Terminal states first.
        if rec.status == RQ.STATUS_DONE:
            if is_write:
                return PollOutcome(
                    state=PollResultState.WRITE_SAVED,
                    correlation_id=correlation_id,
                    result=rec.result, note=None,
                    source_status=rec.status,
                )
            return PollOutcome(
                state=PollResultState.READ_RESULT,
                correlation_id=correlation_id,
                result=rec.result, note=None,
                source_status=rec.status,
            )
        if rec.status in _WRITE_STATUS_TERMINAL_DEAD:
            if is_write:
                return PollOutcome(
                    state=PollResultState.WRITE_DEAD,
                    correlation_id=correlation_id,
                    result=None, note=None,
                    source_status=rec.status,
                )
            # READ that hit a terminal failure: try local fallback;
            # else READ_UNAVAILABLE.
            return _read_fallback_or_unavailable(
                correlation_id, rec, local_fallback_fn,
            )
        # Not terminal yet -- check deadline.
        now_v = float(now_fn())
        if now_v >= deadline:
            if is_write:
                return PollOutcome(
                    state=PollResultState.WRITE_ACCEPTED,
                    correlation_id=correlation_id,
                    result=None, note=None,
                    source_status=rec.status,
                )
            # READ deadline -- NEVER ACCEPTED.
            return _read_fallback_or_unavailable(
                correlation_id, rec, local_fallback_fn,
            )
        # Clamp sleep to remaining budget so wall-time never busts the
        # caller's deadline_seconds (and therefore the 15s Notion
        # envelope).
        remaining = deadline - now_v
        sleep_for = min(float(poll_interval_seconds), remaining)
        if sleep_for > 0:
            sleep_fn(sleep_for)


def _read_fallback_or_unavailable(
    correlation_id: str,
    record,
    local_fallback_fn: Optional[Callable[..., Any]],
) -> PollOutcome:
    """Internal helper for the two READ-non-success branches.

    Either invokes the caller's local fallback (returning READ_LOCAL_
    FALLBACK + note) or surfaces READ_UNAVAILABLE.  NEVER returns a
    fake empty result.

    If the local_fallback_fn itself RAISES, the exception is
    swallowed and we fall through to READ_UNAVAILABLE.  Rationale: a
    fallback that crashes is operationally equivalent to no fallback;
    the user must not get a hard exception from the poll layer when a
    deterministic READ_UNAVAILABLE outcome is well-defined and safe.
    The exception's class name is preserved (no message, no path) in
    the result, value-free.
    """
    if local_fallback_fn is not None:
        try:
            fb = local_fallback_fn(record)
        except Exception as exc:  # noqa: BLE001 -- fallback is caller code
            return PollOutcome(
                state=PollResultState.READ_UNAVAILABLE,
                correlation_id=correlation_id,
                result=None,
                note="fallback_raised:" + type(exc).__name__,
                source_status=record.status,
            )
        return PollOutcome(
            state=PollResultState.READ_LOCAL_FALLBACK,
            correlation_id=correlation_id,
            result=fb, note=LOCAL_FALLBACK_NOTE,
            source_status=record.status,
        )
    return PollOutcome(
        state=PollResultState.READ_UNAVAILABLE,
        correlation_id=correlation_id,
        result=None, note=None,
        source_status=record.status,
    )


def poll_outcome_to_user_outcome(poll_outcome: PollOutcome) -> O.Outcome:
    """Translate a :class:`PollOutcome` into a user-facing
    :class:`bot.aios_outcomes.Outcome`.  The mapping is the
    frozen-contract table in :class:`PollResultState`.

    NOTE on the READ_LOCAL_FALLBACK case: the Outcome dataclass has
    no "note" field, so the LOCAL_FALLBACK_NOTE ("from the local
    copy") is NOT carried in the returned Outcome.  Callers that
    need the labelled user-facing string MUST use
    :func:`compose_user_reply_text` instead -- that helper
    concatenates the base user_message with the note in parens.
    """
    s = poll_outcome.state
    if s == PollResultState.WRITE_SAVED:
        return O.saved(tag="b02d_write_done")
    if s == PollResultState.WRITE_ACCEPTED:
        return O.accepted(tag="b02d_write_async")
    if s == PollResultState.WRITE_DEAD:
        return O.storage_unavailable(tag="b02d_write_dead")
    if s == PollResultState.READ_RESULT:
        return O.saved(tag="b02d_read_done")
    if s == PollResultState.READ_LOCAL_FALLBACK:
        return O.saved(tag="b02d_read_local")
    if s == PollResultState.READ_UNAVAILABLE:
        return O.storage_unavailable(tag="b02d_read_unavail")
    raise ValueError("unknown PollResultState: " + repr(s))


def compose_user_reply_text(poll_outcome: PollOutcome) -> str:
    """Return the FULL user-facing reply string for a
    :class:`PollOutcome`, including the
    :data:`LOCAL_FALLBACK_NOTE` label when applicable
    (addendum #5 requires the local-fallback path to be labelled
    "from the local copy").

    Callers that surface text to the user MUST use this helper rather
    than calling :func:`bot.aios_outcomes.user_message` directly --
    otherwise a stale local-fallback read is indistinguishable from a
    fresh read in the UI.
    """
    outcome = poll_outcome_to_user_outcome(poll_outcome)
    base = O.user_message(outcome)
    if poll_outcome.state == PollResultState.READ_LOCAL_FALLBACK:
        return base + " (" + LOCAL_FALLBACK_NOTE + ")"
    return base


# ---- Worker side ---------------------------------------------------------------

def apply_claimed_request_with_stub(
    record,
    *,
    applier: Callable[[Any], dict],
) -> dict:
    """Call the injected ``applier(record)`` and return its dict result.

    This is intentionally a thin wrapper: it carries NO retry logic,
    NO network code, NO secret access -- the applier is responsible
    for whatever side effect it represents (Notion call in B-02F;
    in-memory stub in tests).

    Exceptions raised by ``applier`` PROPAGATE -- :func:`process_one_
    request` is the place that converts an exception into a
    mark_failed_or_dead call.

    Defensive copy: ``record.payload`` is a plain dict that the
    applier could mutate.  To uphold the addendum-#2 freeze invariant
    even if the applier misbehaves, we hand the applier a deepcopy of
    record (with deepcopied payload).  The original record (and the
    serialized row in SQLite) cannot be touched.

    Result contract (reviewer-tightened): applier MUST return a dict.
    ``None`` is REJECTED -- silently coercing None to {} would
    conflate "applier did not produce anything" with "applier returned
    an empty dict", breaking the addendum-#5 "no fake empty result"
    contract.  Callers MUST be explicit (return {} or raise).
    """
    if applier is None:
        raise ValueError("applier is required")
    # Build a defensive copy so applier mutation cannot leak into the
    # original RequestRecord (frozen at the dataclass level but its
    # ``payload`` field is a live dict).
    safe_record = _deep_copy_record(record)
    result = applier(safe_record)
    if result is None:
        raise ValueError(
            "applier must return a dict (explicit empty dict if "
            "intentional); None is rejected to avoid conflation with "
            "empty result"
        )
    if not isinstance(result, dict):
        raise ValueError(
            "applier must return a dict; got " + type(result).__name__
        )
    return result


def _deep_copy_record(record):
    """Return a defensive deepcopy of a RequestRecord.

    RequestRecord is frozen, so we cannot mutate it.  We construct a
    new instance with a deepcopied payload / result so the applier
    can mutate freely without touching the original.
    """
    return RQ.RequestRecord(
        correlation_id=record.correlation_id,
        kind=record.kind,
        account=record.account,
        status=record.status,
        worker_id=record.worker_id,
        payload=copy.deepcopy(record.payload),
        result=copy.deepcopy(record.result),
        error_class=record.error_class,
        attempts=record.attempts,
        created_ts=record.created_ts,
        claimed_ts=record.claimed_ts,
        resolved_ts=record.resolved_ts,
        next_attempt_ts=record.next_attempt_ts,
        ttl_deadline_ts=record.ttl_deadline_ts,
    )


def process_one_request(
    *,
    worker_id: str,
    applier: Callable[[Any], dict],
    kinds: Optional[Iterable[str]] = None,
    account: Optional[str] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> Optional[str]:
    """Claim the next eligible row, run the applier, mark the result.

    Flow:
      1. ``claim_next_request`` (B-02C; atomic BEGIN IMMEDIATE).
      2. If None claimed -> return None.
      3. Call ``apply_claimed_request_with_stub(record, applier=applier)``.
      4. On success -> ``mark_done(result=...)``.
      5. On exception -> compute ``next_attempt_ts`` via
         ``compute_backoff`` and call ``mark_failed_or_dead`` with the
         exception's CLASS NAME only.  ``next_attempt_ts`` is ALWAYS
         set -- no implicit immediate retry.

    Returns the correlation_id processed, or None if no row claimed.

    No network, no secret read, no global state.
    """
    # Reviewer-tightened: validate applier BEFORE claiming so that a
    # misconfigured worker (applier=None) cannot retry-storm by
    # silently catching its own ValueError in the row-failure path
    # below.
    if applier is None:
        raise ValueError("applier is required")
    rec = RQ.claim_next_request(
        worker_id=worker_id, kinds=kinds, account=account,
        now_fn=now_fn, db=db,
    )
    if rec is None:
        return None
    cid = rec.correlation_id
    try:
        result = apply_claimed_request_with_stub(rec, applier=applier)
    except Exception as exc:  # noqa: BLE001 -- applier may raise anything
        ec = type(exc).__name__  # class name only, never the message
        now = float(now_fn())
        # Pass rec.attempts (BEFORE this failure increment) to
        # compute_backoff: the helper's `attempts` parameter is the
        # number of PREVIOUSLY-failed attempts (0 for the first
        # failure -> base_seconds delay; 1 for the second failure ->
        # base * factor; etc).  Note: recover_stale_claims ALSO
        # increments attempts; a row whose claim crashed once then
        # whose retry-claim fails will have rec.attempts already
        # incremented by recovery, so its backoff curve starts
        # one notch ahead.  This is INTENTIONAL: a crash-prone row
        # earns a longer pause than a row whose only history is a
        # logical failure.
        delay = RQ.compute_backoff(
            attempts=int(rec.attempts),
            base_seconds=backoff_base_seconds,
            max_seconds=backoff_max_seconds,
        )
        next_ts = now + float(delay)
        RQ.mark_failed_or_dead(
            cid, error_class=ec, next_attempt_ts=next_ts,
            max_attempts=int(max_attempts),
            now_fn=now_fn, db=db,
        )
        return cid
    RQ.mark_done(cid, result=result, now_fn=now_fn, db=db)
    return cid


def sweep_request_queue(
    *,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> dict:
    """Run the periodic sweep in the addendum-mandated order:
    ``recover_stale_claims`` FIRST, then ``expire_due_requests``.

    Rationale: a WRITE row whose worker crashed mid-claim must be
    flipped back to ``failed`` (re-claimable) BEFORE the TTL sweep
    can turn it into ``dead``.  Without this ordering, a crashed
    write past its TTL would skip the retry stage entirely and land
    in dead-letter -- silently losing a user-acked
    Outcome.ACCEPTED.

    Returns a value-free dict::

        {
            'recovered': int,        # claimed -> failed (retry)
            'expired':   int,        # READ TTL elapsed -> 'expired'
            'dead_from_ttl': int,    # WRITE TTL elapsed -> 'dead'
        }
    """
    recovered = RQ.recover_stale_claims(
        lease_seconds=float(lease_seconds), now_fn=now_fn, db=db,
    )
    expire_result = RQ.expire_due_requests(now_fn=now_fn, db=db)
    return {
        "recovered": int(recovered),
        "expired": int(expire_result.get("expired", 0)),
        "dead_from_ttl": int(expire_result.get("dead_from_ttl", 0)),
    }
