"""bot/aios_request_queue.py — inert API for the integrations-worker
request lifecycle queue (``request_queue`` table).

INERT BY DEFAULT.  This module is the API surface the worker process and
the handlers use to enqueue / claim / resolve / expire rows in
:data:`request_queue`.  The module itself:

* Does NOT call live Notion / secret-vault / hosting-platform / Todoist.
* Does NOT start any service or background loop.
* Does NOT read or write any flag in :mod:`bot.aios_config`.
* Does NOT move any token (the Notion API key or any other) -- the
  worker process loads tokens at start; this layer never touches secrets
  directly.

Hygiene contract (enforced at the API layer; the table has no SQL CHECK
constraints by project style):

* ``kind`` MUST be one of :data:`ALLOWED_KINDS`; ValueError otherwise.
* ``status`` (when written from inside the module) MUST be one of
  :data:`ALLOWED_STATUSES`; ValueError otherwise.
* ``correlation_id`` is REQUIRED + UNIQUE.  Caller may supply or use
  :func:`generate_correlation_id`.  Duplicate enqueue is IDEMPOTENT --
  returns the existing correlation_id, does NOT mutate the existing row.
* ``payload`` and ``result`` are caller-supplied dicts.  Recursive
  secret-key scan rejects any dict key matching
  ``token|secret|api_key|password|bearer|auth|private_key|access_key|
  refresh_token|client_secret`` (case-insensitive) at ANY nesting depth.
  Value-shape scanning is NOT performed (operationally too brittle on
  real row identifiers); CALLER discipline still required for values.
* ``error_class`` stores the exception CLASS NAME ONLY (sanitized to
  ``[A-Za-z_][A-Za-z0-9_]*``, length-capped at 64 chars).  Never the
  exception message.
* ``account`` is the per-account serialization key for write operations.
  Read kinds may pass ``account=None``.

State machine (closed set):

    pending  -- enqueued; eligible to be claimed (subject to
                next_attempt_ts backoff)
    claimed  -- a worker is processing
    done     -- terminal: succeeded; ``result_json`` populated
    failed   -- transient: this attempt failed but ``attempts < max``;
                next_attempt_ts schedules retry; claimable when due
    expired  -- terminal: TTL elapsed before completion
    dead     -- terminal: ``attempts >= max_attempts``; manual review

Transitions:
    enqueue                -> pending
    claim_next_request     -> pending|failed (when due) -> claimed
    mark_done              -> claimed -> done
    mark_failed_or_dead    -> claimed -> failed (retry) or dead (exhausted)
    expire_due_requests    -> {pending|claimed|failed, ttl elapsed} -> expired

Public surface::

    ALLOWED_KINDS                              tuple of allowed kind values
    ALLOWED_STATUSES                           tuple of allowed status values
    MAX_ATTEMPTS_DEFAULT                       int (10)
    MAX_ERROR_CLASS_LEN                        int (64)

    InvalidKindError(ValueError)
    InvalidStatusError(ValueError)
    SecretLikePayloadError(ValueError)
    WrongStateError(RuntimeError)

    RequestRecord                              frozen dataclass; value-free repr

    generate_correlation_id()                  -> str  (UUID4 hex)
    enqueue_request(*, kind, account=None, payload=None, ttl_seconds=None,
                    correlation_id=None, db=None) -> str  (correlation_id)
    get_request(correlation_id, *, db=None)    -> RequestRecord | None
    claim_next_request(*, worker_id, kinds=None, account=None,
                       now_fn=time.time, db=None) -> RequestRecord | None
    mark_done(correlation_id, *, result=None, db=None) -> bool
    mark_failed_or_dead(correlation_id, *, error_class,
                        next_attempt_ts=None, max_attempts=10,
                        db=None) -> str  (new status: 'failed'|'dead')
    expire_due_requests(*, now_fn=time.time, db=None) -> int  (rows expired)
    get_request_queue_stats(*, db=None) -> dict  (aggregate counts only)
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional


# ---- Closed enums --------------------------------------------------------------

ALLOWED_KINDS = (
    # Path B: integration ops proxied from the keyless transport to the
    # integrations-worker (which holds NOTION/TODOIST keys). See bot/integrations_proxy.py.
    "notion_read",
    "notion_write",
    "todoist_read",
    "todoist_write",
    # Path B (LLM): classifier and similar read-like calls routed to a worker
    # that holds ANTHROPIC_API_KEY (transport has none). No persistent state.
    "llm",
    # Cutover (cutover-plan-amended.md, amendment 11): local ENCRYPTED store ops executed by the
    # keyed worker. Registered in the same commit as proxy INTEGRATION_KINDS + worker dispatch —
    # a kind present in one place but not the others silently strands rows.
    "store_read",
    "store_write",
    # Gate 0 / Part B: personal-content VIEW — the keyed worker renders AND sends the
    # Telegram message itself; decrypted text never returns through this queue. A view
    # has a SEND side effect -> WRITE semantics (dead-letter on TTL, never silent drop).
    "store_view",
    # Reference "notes" demo kinds. The real notes flow rides the generic store_* kinds above
    # (see bot/aios_store_applier.py); these named kinds exist so the worker-contract skeleton
    # test (scripts/test_aios_b02e_integrations_worker_skeleton.py) can pin the WRITE/READ split
    # with a concrete demo verb. add_note / update_note are WRITE (owner-visible dead-letter on
    # TTL); read_note is READ (may expire-and-drop). A kind present in ALLOWED_KINDS but missing
    # from the WRITE_KINDS/READ_KINDS split below would strand rows in the TTL sweep — register in
    # both places or not at all.
    "add_note",
    "update_note",
    "read_note",
)

# Write vs read split.  The per-account serialization lock applies to WRITES
# only.  TTL expiry sends WRITES to `dead` (owner-visible: an accepted write
# that never applied must not silently disappear -- the bot may have returned
# ACCEPTED to the user, and the owner needs to see that it never landed).
# READS may expire-and-drop ("expired") because a stale read is harmless once
# superseded.
WRITE_KINDS = (
    "notion_write",
    "todoist_write",
    "store_write",
    "store_view",
    # notes-demo WRITE verbs (see ALLOWED_KINDS note above).
    "add_note",
    "update_note",
)
READ_KINDS = (
    "notion_read",
    "todoist_read",
    "llm",
    "store_read",
    # notes-demo READ verb.
    "read_note",
)
_WRITE_KINDS_SET = frozenset(WRITE_KINDS)
_READ_KINDS_SET = frozenset(READ_KINDS)

ALLOWED_STATUSES = (
    "pending",
    "claimed",
    "done",
    "failed",
    "expired",
    "dead",
)

STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
STATUS_DEAD = "dead"

# Terminal statuses (caller MUST NOT transition out of these via the API).
_TERMINAL_STATUSES = frozenset({STATUS_DONE, STATUS_EXPIRED, STATUS_DEAD})
# Claimable statuses (pending = fresh; failed = retry-eligible when due).
_CLAIMABLE_STATUSES = (STATUS_PENDING, STATUS_FAILED)
# Expirable statuses (TTL sweep targets).
_EXPIRABLE_STATUSES = (STATUS_PENDING, STATUS_CLAIMED, STATUS_FAILED)


# ---- Constants -----------------------------------------------------------------

MAX_ATTEMPTS_DEFAULT = 10
MAX_ERROR_CLASS_LEN = 64

# Addendum invariant #2: WRITE kinds REQUIRE ttl_seconds at enqueue.
# Rationale: a write without TTL never reaches the dead-letter sweep, so
# a worker crash that's never recovered would leave the bot's
# ACCEPTED-acked write silently invisible to the owner forever.
# (READ kinds may pass ttl_seconds=None because read-staleness is benign.)
_WRITE_REQUIRES_TTL = True

# Addendum invariant #4: ttl_deadline for writes MUST exceed lease_seconds
# by at least RECOMMENDED_TTL_LEASE_RATIO so recover_stale_claims has a
# chance to flip a crashed claim back to 'failed' (retry-eligible) BEFORE
# expire_due_requests sweeps it to 'dead'.  This module exposes the
# ratio + a checked helper; callers compose their own (ttl, lease) pair
# and call check_write_ttl_over_lease() to fail-closed at config time.
RECOMMENDED_TTL_LEASE_RATIO = 2.0
MIN_WRITE_TTL_SECONDS = 1.0

# Addendum invariant #5: backoff helper.  Callers of mark_failed_or_dead
# MUST compute next_attempt_ts (immediate-retry is allowed by the API but
# must be explicit, never implicit).  This module provides
# compute_backoff() as a recommended exponential-with-cap helper.  Tests
# pin both the helper's correctness AND the no-storm contract (when
# next_attempt_ts is None the row is immediately claimable -- callers
# must opt-out explicitly).
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 300.0
DEFAULT_BACKOFF_FACTOR = 2.0

# Recursive scan of payload/result dict keys for secret-shaped names.
# Catches: api_key, apikey, api-key, ApiKey, token, access_token,
# refresh_token, secret, client_secret, password, passwd, pwd, bearer,
# auth, authentication, private_key, access_key, etc.  Case-insensitive
# substring match on key NAMES only (values are NOT scanned because
# legitimate Notion source_notion_id values are 32-hex UUIDs and would
# false-positive any value-shape rule).
_SECRET_KEY_RE = re.compile(
    r"(token|secret|api[_-]?key|password|passwd|pwd|bearer|auth|"
    r"private[_-]?key|access[_-]?key|refresh[_-]?token|"
    r"client[_-]?secret)",
    re.IGNORECASE,
)

# Sanitization regex for error_class.  Extracts the FIRST Python-identifier
# prefix from the input string.  This is the class NAME ONLY: callers that
# accidentally pass repr(exc) (e.g. "HTTPStatusError('https://api.notion.com/...')")
# get just "HTTPStatusError" -- never the URL fragment, never the exception
# message body.  Empty / non-identifier input yields "".
_ERROR_CLASS_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*")


# ---- Errors --------------------------------------------------------------------

class InvalidKindError(ValueError):
    """Raised when ``kind`` is not in :data:`ALLOWED_KINDS`."""


class InvalidStatusError(ValueError):
    """Raised when ``status`` is not in :data:`ALLOWED_STATUSES`."""


class SecretLikePayloadError(ValueError):
    """Raised when payload/result contains a key matching the
    secret-shape pattern (at any nesting depth)."""


class InvalidPayloadTypeError(ValueError):
    """Raised when payload/result is not a dict, list, or None at the
    root level (B-02C addendum-fix: previously raised
    SecretLikePayloadError, which was misleading)."""


class WrongStateError(RuntimeError):
    """Raised when a transition is attempted from an incompatible state
    (e.g. mark_done called on a row whose status is ``done`` or
    ``expired``).  The message names the EXPECTED and OBSERVED status
    keywords; never the row contents.
    """


# ---- Value-free record ---------------------------------------------------------

@dataclass(frozen=True)
class RequestRecord:
    """Value-free request-row dataclass.

    ``payload`` and ``result`` ARE the request contents (by contract,
    value-free of secrets per the API layer's recursive key scan).  The
    overridden :meth:`__repr__` shows only structural metadata
    (key names + counts) so an accidental ``logger.debug(record)`` cannot
    dump real account names or per-row amounts.
    """
    correlation_id: str
    kind: str
    account: Optional[str]
    status: str
    worker_id: Optional[str]
    payload: Optional[dict]
    result: Optional[dict]
    error_class: Optional[str]
    attempts: int
    created_ts: float
    claimed_ts: Optional[float]
    resolved_ts: Optional[float]
    next_attempt_ts: Optional[float]
    ttl_deadline_ts: Optional[float]

    def __repr__(self) -> str:  # type: ignore[override]
        pk = sorted(list((self.payload or {}).keys()))
        return (
            "RequestRecord("
            "correlation_id=" + repr(self.correlation_id)
            + ", kind=" + repr(self.kind)
            + ", account_present=" + str(self.account is not None)
            + ", status=" + repr(self.status)
            + ", worker_id_present=" + str(self.worker_id is not None)
            + ", payload_keys=" + repr(pk)
            + ", result_present=" + str(self.result is not None)
            + ", error_class=" + repr(self.error_class)
            + ", attempts=" + str(self.attempts)
            + ")"
        )


# ---- Internal helpers ----------------------------------------------------------

def _validate_kind(kind: str) -> None:
    if kind not in ALLOWED_KINDS:
        raise InvalidKindError(
            "kind not in allowed set; got <redacted>"
            if not isinstance(kind, str)
            else "kind '" + kind + "' not in ALLOWED_KINDS"
        )


def _validate_status(status: str) -> None:
    if status not in ALLOWED_STATUSES:
        raise InvalidStatusError(
            "status not in allowed set; got <redacted>"
            if not isinstance(status, str)
            else "status '" + status + "' not in ALLOWED_STATUSES"
        )


def _scan_for_secret_keys(obj, path: str = "") -> None:
    """Recursively walk a dict/list and raise :class:`SecretLikePayloadError`
    if any dict key matches :data:`_SECRET_KEY_RE`.

    The exception message names only the PATH and a hint -- never the
    matching value.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if _SECRET_KEY_RE.search(str(k)):
                raise SecretLikePayloadError(
                    "payload key matches secret pattern at path '"
                    + (path or "<root>") + "/<redacted>'"
                )
            _scan_for_secret_keys(v, path + "/" + str(k))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _scan_for_secret_keys(item, path + "[" + str(i) + "]")


def _sanitize_error_class(s: str) -> str:
    """Extract the FIRST Python-identifier prefix from ``s`` (truncated at
    64 chars).  This is the class NAME ONLY: a caller that accidentally
    passes ``repr(exc)`` (e.g.
    ``"HTTPStatusError('https://api.notion.com/...')"``) gets just
    ``"HTTPStatusError"`` -- never the URL fragment, never the exception
    message body.  Empty / non-identifier input yields ``""``.

    This addendum-fix replaces the prior "strip non-identifier chars"
    sanitizer which concatenated surviving letters across boundaries and
    silently leaked URL fragments through ``[A-Za-z0-9_]`` characters.
    """
    if not isinstance(s, str):
        s = str(s) if s is not None else ""
    m = _ERROR_CLASS_PREFIX_RE.match(s.strip())
    if not m:
        return ""
    return m.group(0)[:MAX_ERROR_CLASS_LEN]


def _row_to_record(row) -> RequestRecord:
    """Convert a sqlite3.Row into a :class:`RequestRecord`.  Deserializes
    payload_json and result_json with permissive None on parse error
    (corruption is the caller's problem, not this layer's)."""
    payload = None
    raw_payload = row["payload_json"]
    if raw_payload:
        try:
            payload = json.loads(raw_payload)
        except (json.JSONDecodeError, TypeError):
            payload = None
    result = None
    raw_result = row["result_json"]
    if raw_result:
        try:
            result = json.loads(raw_result)
        except (json.JSONDecodeError, TypeError):
            result = None
    return RequestRecord(
        correlation_id=row["correlation_id"],
        kind=row["kind"],
        account=row["account"],
        status=row["status"],
        worker_id=row["worker_id"],
        payload=payload,
        result=result,
        error_class=row["error_class"],
        attempts=int(row["attempts"] or 0),
        created_ts=float(row["created_ts"]),
        claimed_ts=(None if row["claimed_ts"] is None
                    else float(row["claimed_ts"])),
        resolved_ts=(None if row["resolved_ts"] is None
                     else float(row["resolved_ts"])),
        next_attempt_ts=(None if row["next_attempt_ts"] is None
                         else float(row["next_attempt_ts"])),
        ttl_deadline_ts=(None if row["ttl_deadline_ts"] is None
                         else float(row["ttl_deadline_ts"])),
    )


def _serialize_payload(payload) -> Optional[str]:
    if payload is None:
        return None
    if not isinstance(payload, (dict, list)):
        raise SecretLikePayloadError(
            "payload must be dict or list (got " + type(payload).__name__ + ")"
        )
    _scan_for_secret_keys(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# ---- Public API ----------------------------------------------------------------

def generate_correlation_id() -> str:
    """Return a fresh UUID4 hex string suitable for use as correlation_id."""
    return uuid.uuid4().hex


def enqueue_request(
    *,
    kind: str,
    account: Optional[str] = None,
    payload: Optional[dict] = None,
    ttl_seconds: Optional[float] = None,
    correlation_id: Optional[str] = None,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> str:
    """Insert a ``pending`` row.  Returns the correlation_id.

    IDEMPOTENT on duplicate correlation_id: returns the existing
    correlation_id WITHOUT mutating the existing row.  No exception is
    raised on duplicate -- a retry of enqueue is safe.

    Validates ``kind`` (must be in :data:`ALLOWED_KINDS`) and ``payload``
    (recursive secret-key scan).  ``ttl_seconds`` (if given) sets
    ``ttl_deadline_ts = now + ttl_seconds``; otherwise NULL = never
    expires.

    NEVER touches Notion / secret-vault / hosting-platform / any
    external system.
    Writes ONLY to the passed ``db`` (or the default
    :func:`bot.aios_storage.connect` target).
    """
    _validate_kind(kind)
    # Addendum invariant #2: WRITE kinds REQUIRE ttl_seconds.  A write
    # without TTL would never reach the dead-letter sweep, so a crashed
    # worker would leave the bot's ACCEPTED-acked write silently
    # invisible to the owner forever.  Fail-closed at enqueue time.
    if kind in _WRITE_KINDS_SET:
        if ttl_seconds is None:
            raise ValueError(
                "ttl_seconds is REQUIRED for WRITE kinds (addendum #2: "
                "write without TTL never reaches dead-letter, leaving "
                "an ACCEPTED-acked write silently invisible)"
            )
        if not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool):
            raise ValueError("ttl_seconds must be a number (int or float)")
        if float(ttl_seconds) < MIN_WRITE_TTL_SECONDS:
            raise ValueError(
                "ttl_seconds for WRITE kinds must be >= "
                + str(MIN_WRITE_TTL_SECONDS) + " seconds"
            )
    payload_json = _serialize_payload(payload)
    cid = correlation_id or generate_correlation_id()
    now = float(now_fn())
    ttl_deadline = None if ttl_seconds is None else (now + float(ttl_seconds))
    from . import aios_storage as S
    with S.connect(db=db) as conn:
        try:
            conn.execute(
                "INSERT INTO request_queue ("
                " correlation_id, kind, account, status, payload_json,"
                " attempts, created_ts, ttl_deadline_ts"
                ") VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (cid, kind, account, STATUS_PENDING, payload_json,
                 now, ttl_deadline),
            )
        except sqlite3.IntegrityError:
            # Duplicate correlation_id -> idempotent no-op.
            pass
    return cid


def get_request(
    correlation_id: str,
    *,
    db: Optional[str] = None,
) -> Optional[RequestRecord]:
    """Return the row as a :class:`RequestRecord`, or ``None`` if no such
    correlation_id."""
    from . import aios_storage as S
    with S.connect(db=db) as conn:
        row = conn.execute(
            "SELECT correlation_id, kind, account, status, worker_id,"
            " payload_json, result_json, error_class, attempts, created_ts,"
            " claimed_ts, resolved_ts, next_attempt_ts, ttl_deadline_ts"
            " FROM request_queue WHERE correlation_id=?",
            (correlation_id,),
        ).fetchone()
    if row is None:
        return None
    return _row_to_record(row)


def claim_next_request(
    *,
    worker_id: str,
    kinds: Optional[Iterable[str]] = None,
    account: Optional[str] = None,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> Optional[RequestRecord]:
    """Claim the next eligible request row (atomically: BEGIN IMMEDIATE
    around SELECT+UPDATE).  Returns a :class:`RequestRecord` on success,
    ``None`` if no eligible row.

    Eligible = status in {'pending', 'failed'} AND
               (next_attempt_ts IS NULL OR next_attempt_ts <= now) AND
               NOT (this row is a WRITE for an account that already has
                    another row in 'claimed' status for the same account
                    and kind family WRITE).

    Per-account serialization lock (addendum):
      * Applies to WRITE kinds only ({notion_write, todoist_write,
        store_write, store_view}).
      * A second WRITE for the same account is NOT eligible while the
        first WRITE for that account is in 'claimed' status.
      * READ kinds ({notion_read, todoist_read, llm, store_read},
        including rows with account=NULL) are NEVER blocked by the write
        lock -- they may be claimed independently and in parallel with any
        write.
      * Two writes for DIFFERENT accounts may proceed in parallel.

    Filters:
      * ``kinds`` -- only consider these kinds (each must be in
        :data:`ALLOWED_KINDS`; else InvalidKindError).
      * ``account`` -- only consider rows whose account == this value.

    Ordering: created_ts ASC, then id ASC as tiebreak.

    Atomicity (addendum):
      * BEGIN IMMEDIATE acquires the SQLite RESERVED lock before the
        SELECT, so two concurrent claimers serialize cleanly.
      * The UPDATE clause still asserts ``status IN ('pending','failed')``
        as a belt-and-braces guard; rowcount == 0 returns None.

    Side effect on the claimed row: status -> 'claimed', worker_id set,
    claimed_ts = now.  attempts is NOT incremented on claim; it
    increments on mark_failed_or_dead.
    """
    if not worker_id or not isinstance(worker_id, str):
        raise ValueError("worker_id is required (non-empty str)")
    if kinds is not None:
        kinds_tuple = tuple(kinds)
        for k in kinds_tuple:
            _validate_kind(k)
    else:
        kinds_tuple = None
    now = float(now_fn())
    from . import aios_storage as S
    # Base eligibility predicate.
    sql_where = (
        "status IN ('pending','failed') "
        "AND (next_attempt_ts IS NULL OR next_attempt_ts <= ?)"
    )
    params: list = [now]
    if account is not None:
        sql_where += " AND account = ?"
        params.append(account)
    if kinds_tuple:
        placeholders = ",".join(["?"] * len(kinds_tuple))
        sql_where += " AND kind IN (" + placeholders + ")"
        params.extend(kinds_tuple)
    # Per-account write lock: exclude rows that are WRITEs for accounts
    # whose write is currently claimed by another row.  The NULL-account
    # case is its own lock domain ("account IS NULL" matches via
    # `IS NOT DISTINCT FROM` semantics expressed with `(... IS NULL AND
    # ... IS NULL) OR ... = ...`).
    write_kinds_placeholders = ",".join(["?"] * len(WRITE_KINDS))
    sql_where += (
        " AND NOT ("
        " kind IN (" + write_kinds_placeholders + ")"
        " AND EXISTS ("
        "  SELECT 1 FROM request_queue locked"
        "  WHERE locked.status='claimed'"
        "    AND locked.kind IN (" + write_kinds_placeholders + ")"
        "    AND ("
        "      (locked.account IS NULL AND request_queue.account IS NULL)"
        "      OR locked.account = request_queue.account"
        "    )"
        " )"
        ")"
    )
    params.extend(WRITE_KINDS)  # kind IN (...) -- this row
    params.extend(WRITE_KINDS)  # locked.kind IN (...) -- existing claim
    with S.connect(db=db) as conn:
        # BEGIN IMMEDIATE acquires the RESERVED lock so concurrent
        # claimers don't both pick the same id.  COMMIT/ROLLBACK manual
        # because the connection is autocommit (isolation_level=None).
        conn.execute("BEGIN IMMEDIATE")
        try:
            sel = conn.execute(
                "SELECT id FROM request_queue WHERE " + sql_where
                + " ORDER BY created_ts ASC, id ASC LIMIT 1",
                tuple(params),
            ).fetchone()
            if sel is None:
                conn.execute("COMMIT")
                return None
            row_id = int(sel["id"])
            upd = conn.execute(
                "UPDATE request_queue SET status=?, worker_id=?,"
                " claimed_ts=?"
                " WHERE id=? AND status IN ('pending','failed')",
                (STATUS_CLAIMED, worker_id, now, row_id),
            )
            if upd.rowcount == 0:
                conn.execute("COMMIT")
                return None
            full = conn.execute(
                "SELECT correlation_id, kind, account, status, worker_id,"
                " payload_json, result_json, error_class, attempts,"
                " created_ts, claimed_ts, resolved_ts, next_attempt_ts,"
                " ttl_deadline_ts"
                " FROM request_queue WHERE id=?",
                (row_id,),
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return _row_to_record(full)


def mark_done(
    correlation_id: str,
    *,
    result: Optional[dict] = None,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> bool:
    """Transition a ``claimed`` row to ``done``.  Returns ``True`` iff
    the transition occurred.

    Raises :class:`WrongStateError` if the row exists but is NOT in
    ``claimed`` state (callers must claim first; this layer refuses to
    "force" a transition out of pending / done / expired / dead).

    Raises :class:`SecretLikePayloadError` if ``result`` contains a
    secret-shaped key at any depth.
    """
    result_json = _serialize_payload(result)
    now = float(now_fn())
    from . import aios_storage as S
    with S.connect(db=db) as conn:
        row = conn.execute(
            "SELECT status FROM request_queue WHERE correlation_id=?",
            (correlation_id,),
        ).fetchone()
        if row is None:
            return False
        if row["status"] != STATUS_CLAIMED:
            raise WrongStateError(
                "mark_done requires status='claimed'; observed='"
                + str(row["status"]) + "'"
            )
        cur = conn.execute(
            "UPDATE request_queue SET status=?, result_json=?,"
            " resolved_ts=? WHERE correlation_id=? AND status=?",
            (STATUS_DONE, result_json, now, correlation_id, STATUS_CLAIMED),
        )
        return cur.rowcount > 0


def mark_failed_or_dead(
    correlation_id: str,
    *,
    error_class: str,
    next_attempt_ts: Optional[float] = None,
    max_attempts: int = MAX_ATTEMPTS_DEFAULT,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> str:
    """Transition a ``claimed`` row to ``failed`` (retry-eligible) or
    ``dead`` (exhausted).  Increments ``attempts`` by 1.  Returns the
    new status ('failed' or 'dead').

    Decision: if ``attempts + 1 >= max_attempts`` -> 'dead'.  Else
    'failed' with ``next_attempt_ts`` set (caller decides backoff).

    Raises :class:`WrongStateError` if the row is not in 'claimed'.
    Raises :class:`ValueError` if the row does not exist.

    ``error_class`` is sanitized to ``[A-Za-z0-9_]`` and capped at 64
    chars.  Never store the exception MESSAGE here.
    """
    sanitized = _sanitize_error_class(error_class)
    now = float(now_fn())
    from . import aios_storage as S
    with S.connect(db=db) as conn:
        row = conn.execute(
            "SELECT status, attempts FROM request_queue"
            " WHERE correlation_id=?",
            (correlation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("correlation_id not found")
        if row["status"] != STATUS_CLAIMED:
            raise WrongStateError(
                "mark_failed_or_dead requires status='claimed'; observed='"
                + str(row["status"]) + "'"
            )
        new_attempts = int(row["attempts"] or 0) + 1
        if new_attempts >= int(max_attempts):
            new_status = STATUS_DEAD
            conn.execute(
                "UPDATE request_queue SET status=?, attempts=?,"
                " error_class=?, resolved_ts=?"
                " WHERE correlation_id=? AND status=?",
                (new_status, new_attempts, sanitized, now,
                 correlation_id, STATUS_CLAIMED),
            )
        else:
            new_status = STATUS_FAILED
            conn.execute(
                "UPDATE request_queue SET status=?, attempts=?,"
                " error_class=?, next_attempt_ts=?, worker_id=NULL,"
                " claimed_ts=NULL"
                " WHERE correlation_id=? AND status=?",
                (new_status, new_attempts, sanitized,
                 (None if next_attempt_ts is None else float(next_attempt_ts)),
                 correlation_id, STATUS_CLAIMED),
            )
    return new_status


def expire_due_requests(
    *,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> dict:
    """TTL sweep with WRITE/READ split (addendum invariant).

    For each row with ``ttl_deadline_ts <= now`` AND status in
    ``{pending, claimed, failed}``:

      * READ kind ({notion_read, todoist_read, llm, store_read}) ->
        ``expired`` (resolved_ts set).  Drop is acceptable: a stale read
        is harmless once the next read supersedes it.
      * WRITE kind ({notion_write, todoist_write, store_write,
        store_view}) -> ``dead`` (resolved_ts set).  A WRITE that the bot
        has potentially ACKED to the user as ``Outcome.ACCEPTED`` MUST
        NOT silently disappear.  ``dead`` is owner-visible: a future
        operator-facing queue inspector will list dead rows for
        reconciliation.

    DOES NOT touch ``done`` / ``expired`` / ``dead`` rows.

    Returns ``{'expired': N, 'dead_from_ttl': M, 'total': N+M}``.
    """
    now = float(now_fn())
    from . import aios_storage as S
    read_placeholders = ",".join(["?"] * len(READ_KINDS))
    write_placeholders = ",".join(["?"] * len(WRITE_KINDS))
    with S.connect(db=db) as conn:
        # Expire READ rows (drop allowed).
        cur_r = conn.execute(
            "UPDATE request_queue SET status=?, resolved_ts=?"
            " WHERE ttl_deadline_ts IS NOT NULL"
            "   AND ttl_deadline_ts <= ?"
            "   AND status IN ('pending','claimed','failed')"
            "   AND kind IN (" + read_placeholders + ")",
            (STATUS_EXPIRED, now, now, *READ_KINDS),
        )
        n_expired = int(cur_r.rowcount)
        # Send WRITE rows to dead (owner-visible).
        cur_w = conn.execute(
            "UPDATE request_queue SET status=?, resolved_ts=?"
            " WHERE ttl_deadline_ts IS NOT NULL"
            "   AND ttl_deadline_ts <= ?"
            "   AND status IN ('pending','claimed','failed')"
            "   AND kind IN (" + write_placeholders + ")",
            (STATUS_DEAD, now, now, *WRITE_KINDS),
        )
        n_dead = int(cur_w.rowcount)
    return {
        "expired": n_expired,
        "dead_from_ttl": n_dead,
        "total": n_expired + n_dead,
    }


def recover_stale_claims(
    *,
    lease_seconds: float,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> int:
    """Recover ``claimed`` rows whose ``claimed_ts`` is older than
    ``now - lease_seconds`` (the worker crashed or wedged before
    completing).  Transitions them BACK to ``failed`` with attempts++
    and ``next_attempt_ts = now`` (immediately retry-eligible) and
    clears ``worker_id`` / ``claimed_ts``.

    This is the explicit recovery path the addendum requires: a stale
    'claimed' row does NOT hang forever; it is moved to retry-failed
    so the next ``claim_next_request`` picks it up.  ``mark_failed_or_
    dead`` is NOT called from here (it expects status=claimed and
    sanitizes its own error_class); a synthetic error_class
    ``"StaleClaimRecovered"`` is recorded so operators can distinguish
    in the audit trail.

    Returns the number of rows recovered.  DOES NOT touch ``done`` /
    ``expired`` / ``dead`` rows.
    """
    if not isinstance(lease_seconds, (int, float)) or lease_seconds <= 0:
        raise ValueError("lease_seconds must be a positive number")
    now = float(now_fn())
    cutoff = now - float(lease_seconds)
    from . import aios_storage as S
    with S.connect(db=db) as conn:
        cur = conn.execute(
            "UPDATE request_queue SET status=?, attempts=attempts+1,"
            " error_class=?, next_attempt_ts=?, worker_id=NULL,"
            " claimed_ts=NULL"
            " WHERE status='claimed'"
            "   AND claimed_ts IS NOT NULL"
            "   AND claimed_ts < ?",
            (STATUS_FAILED, "StaleClaimRecovered", now, cutoff),
        )
        return int(cur.rowcount)


def compute_backoff(
    *,
    attempts: int,
    base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    factor: float = DEFAULT_BACKOFF_FACTOR,
    max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
) -> float:
    """Recommended exponential-with-cap backoff helper for callers of
    :func:`mark_failed_or_dead` (addendum invariant #5).

    Returns ``min(max_seconds, base_seconds * factor ** max(0, attempts))``.

    Callers compute ``next_attempt_ts = now + compute_backoff(
    attempts=current_attempts)`` and pass it explicitly.  The B-02C API
    layer DOES allow ``next_attempt_ts=None`` (immediate retry) but only
    as an explicit opt-in -- there is no implicit "retry immediately if
    omitted" path that would cause a retry storm.

    Raises ValueError on invalid args (non-numeric, bool, negative).
    """
    for name, val in (("attempts", attempts), ("base_seconds", base_seconds),
                      ("factor", factor), ("max_seconds", max_seconds)):
        if isinstance(val, bool):
            raise ValueError(name + " must be a number, not bool")
        if not isinstance(val, (int, float)):
            raise ValueError(name + " must be a number")
        if val < 0:
            raise ValueError(name + " must be non-negative")
    a = int(max(0, attempts))
    delay = float(base_seconds) * (float(factor) ** a)
    return float(min(float(max_seconds), delay))


def check_write_ttl_over_lease(
    *,
    ttl_seconds: float,
    lease_seconds: float,
    ratio: float = RECOMMENDED_TTL_LEASE_RATIO,
) -> None:
    """Fail-closed check that a WRITE-row's TTL exceeds the worker's
    lease by ``ratio`` (addendum invariant #4).

    Rationale: if a worker crashes mid-claim on a WRITE row, the recovery
    path is :func:`recover_stale_claims` (flips claimed -> failed when
    ``claimed_ts < now - lease_seconds``).  For that recovery to win the
    race against :func:`expire_due_requests` (which would flip claimed
    WRITE rows to ``dead`` when ``ttl_deadline_ts <= now``), the TTL
    deadline MUST be set far enough in the future that recovery has a
    chance.  Specifically: ``ttl_seconds >= ratio * lease_seconds``.

    Callers (the integrations-worker setup code in B-02D / B-02F) call
    this once at config-load time.  Raises ValueError on violation.

    NOTE: this is a CONFIG-TIME check, not enforced inside the API
    surface itself, because TTL is per-call and lease is a worker policy
    -- the boundary belongs to the worker init, not the queue layer.
    """
    for name, val in (("ttl_seconds", ttl_seconds),
                      ("lease_seconds", lease_seconds),
                      ("ratio", ratio)):
        if isinstance(val, bool):
            raise ValueError(name + " must be a number, not bool")
        if not isinstance(val, (int, float)):
            raise ValueError(name + " must be a number")
        if val <= 0:
            raise ValueError(name + " must be positive")
    required = float(ratio) * float(lease_seconds)
    if float(ttl_seconds) < required:
        raise ValueError(
            "WRITE ttl_seconds (" + str(float(ttl_seconds))
            + ") must be >= ratio*lease_seconds (" + str(required)
            + ") so recover_stale_claims wins the race vs expire_due_requests"
        )


def get_request_queue_stats(*, db: Optional[str] = None) -> dict:
    """Return aggregate counts (value-free).  Schema::

        {
            'total': int,
            'by_status': {status: count, ...},   # only non-zero buckets
            'by_kind':   {kind: count, ...},     # only non-zero buckets
            'oldest_pending_age_sec': float | None,
        }

    NEVER returns correlation_id values, payload contents, or any
    per-row data.
    """
    from . import aios_storage as S
    with S.connect(db=db) as conn:
        by_status_rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM request_queue"
            " GROUP BY status"
        ).fetchall()
        by_kind_rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM request_queue"
            " GROUP BY kind"
        ).fetchall()
        oldest_row = conn.execute(
            "SELECT MIN(created_ts) AS m FROM request_queue"
            " WHERE status='pending'"
        ).fetchone()
    by_status = {r["status"]: int(r["n"]) for r in by_status_rows}
    by_kind = {r["kind"]: int(r["n"]) for r in by_kind_rows}
    total = sum(by_status.values())
    oldest_age = None
    if oldest_row and oldest_row["m"] is not None:
        oldest_age = float(time.time() - float(oldest_row["m"]))
    return {
        "total": total,
        "by_status": by_status,
        "by_kind": by_kind,
        "oldest_pending_age_sec": oldest_age,
    }
