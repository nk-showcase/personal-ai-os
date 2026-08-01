"""bot/conv_state.py — S6 S-3 durable per-chat conversation-state store + dual blob codec.

SUBSTRATE ONLY: gated/inert, no handler wired. Mirrors bot/reply_queue.py for the durable
shell (shares ${AIOS_HOME}/.ai-os/queue/tasks.sqlite3, override AIOS_TASK_QUEUE_DB; WAL,
busy_timeout, fresh-conn-per-op, import side-effect-free, NO Telegram / secrets) and the
context stores for encryption-aware dual-read (cleartext when AIOS_CONTEXT_ENCRYPTION is OFF,
ciphertext via the context_cipher chokepoint when ON; fail-closed when ON + no KEK).

WHY A CODEC (the point of this slice): PTB chat_data carries Python sets and int-keyed dicts.
JSON cannot round-trip them — json.dumps({1:"x"}) == '{"1":"x"}', so after a reload
task_map.get(1) is None and every /done N, /del N, task_close_N breaks; sets come back as lists
and `in`/add/discard semantics drift. serialize() tags those known keys on the way out;
deserialize() restores int keys for INT_KEY_MAPS and sets for SET_FIELDS, nested sub-paths
included. Unknown keys pass through untouched.

CODEC SAFETY (registry-gated, LOUD, never silently lossy):
  - Sets are registry-gated BOTH ways. A set at a SET_FIELDS path is encoded as {"__set__":[...]}
    and decoded back to a set. A set at ANY OTHER path raises ValueError on encode (naming the
    path) — never a silent set->dict degradation, never an opaque json.dumps TypeError. Verified
    against the code: no persisted set nests under a dynamic int-key path; every set path
    is static.
  - The {"__set__":...} marker is honored on decode ONLY at a SET_FIELDS path, so a genuine
    user dict literally shaped {"__set__":[...]} at a non-registered path round-trips as a dict,
    not a set (encode never produces that shape from a real set at a non-registered path).
  - INT_KEY_MAP parents detect duplicate str(k) on encode and raise ValueError (no silent
    key-merge / lost task).
  - Set item order is process-independent (stable (type-name, repr) key), so two equal states
    serialize to identical bytes across processes (no spurious durable writes).
  - tuple / datetime / bytes are NOT silently degraded: tuple/bytes/datetime raise ValueError.

Single-encryptor (ADR barrier #1 / vps_spec_guard #14): this module imports context_cipher
ONLY. It NEVER imports cryptography, context_store, get_kek, or aios_storage.
"""
from __future__ import annotations

import contextvars
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from .context_cipher import decrypt_for_storage, encrypt_for_storage  # the ONLY crypto bridge

_BUSY_TIMEOUT_MS = 5000

# ---- codec registry (known keys; nested sub-paths use "parent.child") ----------------------
# A value is a Python set at these paths. Top-level "name" => chat_data[name] is a set.
# "parent.child" => chat_data[parent] is a dict and chat_data[parent][child] is a set.
# SET_FIELDS gates BOTH directions: encode emits the {"__set__":...} marker ONLY at these paths
# (a set anywhere else RAISES — loud, never a silent set->dict), and decode re-creates a set from
# the marker ONLY at these paths (so a real user dict shaped {"__set__":[...]} at a non-registered
# path stays a dict). Every registered set field must sit at a static path; none may nest under a
# dynamic int-key map.
#
# CONSTRAINT: a registry path MUST NOT traverse a list level or a dynamic int-key level.
# All entries are top-level or static dict-of-dict. A future set that lives inside a list
# element or under an int-key map requires a code change (path threading), not just a registry line.
#
# The example below shows the expected shape: a top-level set and a static "parent.child" set.
SET_FIELDS: frozenset[str] = frozenset({
    "note_tags_selected",          # TOP-LEVEL set (example neutral field)
    "note_draft.attachments",      # static "parent.child" set (example neutral field)
})

# chat_data[name] is a dict whose ORIGINAL keys are ints (JSON coerces them to str).
INT_KEY_MAPS: frozenset[str] = frozenset({
    "task_map",        # {int: task_id_str}    handlers.py (list -> task numbering)
})

# Envelope marker so deserialize is self-describing and idempotent (see spec §2 contract).
_CODEC_VERSION = 1
_SET_MARKER = "__set__"


def _db_path() -> Path:
    raw = os.getenv("AIOS_TASK_QUEUE_DB")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".ai-os" / "queue" / "tasks.sqlite3"


def _now() -> float:
    return time.time()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conv_state (
            chat_id        INTEGER PRIMARY KEY,
            blob           BLOB,
            blob_encrypted BLOB,
            updated_ts     REAL NOT NULL,
            CHECK ((blob IS NULL) <> (blob_encrypted IS NULL))
        );
        """
    )


@contextmanager
def _conn():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Group-aware perms so the bot + worker can SHARE this queue DB (see queue_perms).
    from . import queue_perms
    _gid = queue_perms.apply_dir_perms(path.parent)
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        _ensure_schema(conn)
        queue_perms.apply_file_perms(path, _gid)  # every connect (incl. -wal/-shm), not only on create
        yield conn
    finally:
        conn.close()


# ------------------------------- codec ------------------------------------------------------
def _set_sort_key(v):
    """Process-independent total order for set items: (type-name, repr).
    Defeats PYTHONHASHSEED-dependent iteration order even for mixed-type sets, so equal
    states serialize byte-identically across processes."""
    return (type(v).__name__, repr(v))


def serialize(state: dict) -> str:
    """chat_data dict -> deterministic UTF-8 JSON string (ensure_ascii=False, sorted keys).

    A set at a registered SET_FIELDS path -> {"__set__": [stably-ordered list]} ; a set at any
    other path RAISES ValueError (loud). Int-keyed maps keep str JSON keys on the wire but the
    PARENT name in INT_KEY_MAPS lets decode rebuild int keys. Unknown keys pass through untouched.
    Idempotent: serialize(deserialize(serialize(x))) == serialize(x).

    Raises ValueError if an INT_KEY_MAP dict produces a duplicate str(k) (would silently drop a
    task on reload), if a set sits at an unregistered path, or if a tuple/bytes/datetime value is
    encountered (not a supported persisted shape)."""
    encoded = _encode(state, prefix="")
    envelope = {"__cv__": _CODEC_VERSION, "data": encoded}
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encode(value, prefix: str):
    # Sets are registry-gated on encode too: a set is tagged ONLY at a SET_FIELDS path; a set
    # anywhere else RAISES (loud) rather than silently degrading to a dict on decode or crashing
    # opaquely in json.dumps. Verified: no real persisted set nests under a dynamic int-key path.
    if isinstance(value, set):
        if prefix not in SET_FIELDS:
            raise ValueError(
                f"set at {prefix!r} is not a registered SET_FIELDS path; register it (static "
                f"path only) or thread the path (it must not sit under a list / int-key level)"
            )
        items = sorted(value, key=_set_sort_key)
        return {_SET_MARKER: [_encode(v, "") for v in items]}
    if isinstance(value, dict):
        int_parent = prefix in INT_KEY_MAPS
        out = {}
        for k, v in value.items():
            sk = str(k)
            if int_parent and sk in out:
                raise ValueError(
                    f"INT_KEY_MAP {prefix!r} key collision on {sk!r}: int/str keys "
                    f"would merge and lose an entry"
                )
            child = f"{prefix}.{sk}" if prefix else sk
            out[sk] = _encode(v, child)   # JSON keys are always str on the wire
        return out
    if isinstance(value, tuple):
        raise ValueError(
            f"tuple at {prefix!r} is not a supported persisted shape (would silently "
            f"degrade to list on reload); store a list or add a tuple marker upstream"
        )
    if isinstance(value, (bytes, bytearray)):
        raise ValueError(f"bytes at {prefix!r} is not a supported persisted shape")
    if isinstance(value, (datetime, date)):
        raise ValueError(
            f"raw datetime/date at {prefix!r} is not supported; isoformat() upstream"
        )
    if isinstance(value, list):
        # list elements have no registry sub-path; recurse so nested dicts still encode.
        return [_encode(v, "") for v in value]
    return value  # str / int / float / bool / None — JSON-native


def deserialize(text: str) -> dict:
    """UTF-8 JSON string (from serialize) -> chat_data dict with sets and int keys restored.

    Restores: SET_FIELDS marker dicts -> set(...) ; INT_KEY_MAPS top-level dict keys -> int.
    Unknown keys untouched — including a genuine dict literally shaped {"__set__":[...]} at a
    non-SET_FIELDS path, which stays a dict. Empty/missing payload -> {}.

    A bare legacy dict that predates the envelope is REJECTED (returns {}): for a brand-new inert
    store no such row can exist, and best-effort decoding it would silently re-lose int keys (the
    headline bug). Requiring the envelope is the safe choice."""
    if not text:
        return {}
    envelope = json.loads(text)
    if not isinstance(envelope, dict) or "__cv__" not in envelope or "data" not in envelope:
        # No envelope marker -> not produced by this codec. Do NOT best-effort decode (would
        # silently fail to restore int keys). New store: this row cannot legitimately exist.
        return {}
    data = envelope.get("data") or {}
    return _decode(data, prefix="")


def _is_set_marker(value) -> bool:
    return isinstance(value, dict) and set(value.keys()) == {_SET_MARKER}


def _decode(value, prefix: str):
    # The set marker is honored ONLY at a registered SET_FIELDS path. Anywhere else a dict
    # literally shaped {"__set__":[...]} is just a dict (no false-positive set coercion).
    if prefix in SET_FIELDS and _is_set_marker(value):
        return set(_decode(v, "") for v in value[_SET_MARKER])
    if isinstance(value, dict):
        restore_int_keys = prefix in INT_KEY_MAPS
        out = {}
        for k, v in value.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            key = _coerce_int_key(k) if restore_int_keys else k
            out[key] = _decode(v, child)
        return out
    if isinstance(value, list):
        return [_decode(v, "") for v in value]
    return value


def _coerce_int_key(k: str):
    """INT_KEY_MAPS keys were ints; JSON made them str. Restore int where it round-trips,
    else keep the original string (collision-safe: never silently merge '1' and 1).
    Rejects '01', '+1', ' 1' — keep as-is so a non-canonical token never collides with a real
    int key."""
    try:
        i = int(k)
    except (TypeError, ValueError):
        return k
    return i if str(i) == k else k


# ------------------------------- public store API -------------------------------------------
def load_state(chat_id: int) -> dict:
    """Return the chat's decoded conversation state (sets + int keys restored), or {} if NO ROW.

    Dual-read: encrypted blob present -> decrypt via context_cipher; else cleartext.

    FAIL-CLOSED CONTRACT: a present-but-undecryptable row (tampered ciphertext / wrong KEK)
    RAISES (DecryptQuarantine / KekUnavailable). Callers MUST NOT catch-and-return-{} — only a
    MISSING row yields {}. Converting a decrypt failure into silent empty-state would replay the
    whole conversation from scratch and lose task_map, exactly the personal-state-loss the
    encryption barrier forbids.

    The whole serialized chat_data is a SINGLE cipher field (field="blob"); there is no per-field
    recovery — any decrypt failure quarantines the entire chat row. AAD collision-freedom relies
    on chat_id being the PRIMARY KEY (unique row_id), so chat A's envelope cannot be relocated to
    chat B's row and decrypt."""
    if chat_id is None:
        raise ValueError("load_state requires a non-empty chat_id")
    with _conn() as conn:
        row = conn.execute(
            "SELECT blob, blob_encrypted FROM conv_state WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if row is None:
        return {}
    if row["blob_encrypted"] is not None:
        # decrypt_for_storage RAISES on failure (and quarantines); we deliberately do not catch.
        text = decrypt_for_storage(
            row["blob_encrypted"], domain="conv_state", row_id=chat_id, field="blob"
        )
    elif row["blob"] is not None:
        raw = row["blob"]
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    else:
        return {}
    return deserialize(text)


def save_state(chat_id: int, state: dict) -> None:
    """Encode the chat's state and upsert one row. encrypt-or-raise happens BEFORE the write:
    AIOS_CONTEXT_ENCRYPTION ON+key -> ciphertext in blob_encrypted (blob NULL); OFF -> cleartext
    UTF-8 in blob (blob_encrypted NULL); ON+no key -> KekUnavailable, ZERO rows touched. Exactly
    one payload column is non-NULL (DB CHECK enforces it)."""
    if chat_id is None:
        raise ValueError("save_state requires a non-empty chat_id")
    text = serialize(state)
    env = encrypt_for_storage("conv_state", chat_id, "blob", text)  # None when OFF; raises ON+no KEK
    now = _now()
    with _conn() as conn:
        if env is None:
            conn.execute(
                "INSERT INTO conv_state (chat_id, blob, blob_encrypted, updated_ts) "
                "VALUES (?,?,NULL,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET blob=excluded.blob, "
                "blob_encrypted=NULL, updated_ts=excluded.updated_ts",
                (chat_id, text.encode("utf-8"), now),
            )
        else:
            conn.execute(
                "INSERT INTO conv_state (chat_id, blob, blob_encrypted, updated_ts) "
                "VALUES (?,NULL,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET blob=NULL, "
                "blob_encrypted=excluded.blob_encrypted, updated_ts=excluded.updated_ts",
                (chat_id, env, now),
            )


# ------------------------------- atomic mutate (substrate-3.5) -------------------------------
# Timers-as-writers (§3.5/§3.6): an inbound-router turn and a schedule/timer handler can both do a
# non-atomic read-modify-write of the SAME chat blob and silently lose one update. mutate() makes
# the whole read->mutate->write ONE SQLite BEGIN IMMEDIATE write-transaction on one connection — the
# lock IS the write-transaction (no separate lock table / reaper / token / asyncio.Lock). Spec:
# docs/architecture/s6-thin-transport-plan.md (per-chat mutate/lock design). INERT: no live loop calls mutate yet.

class ReentrantMutateError(RuntimeError):
    """mutate(C) called from inside a mutator that already holds chat C — compose into one mutator."""


class ConvLockBusyError(RuntimeError):
    """Could not obtain the SQLite write-lock within the bounded retry budget."""


# Reentrancy guard: chat_ids this execution context is currently mutating. A contextvar (not a
# module global) so concurrent asyncio tasks / threads don't see each other's in-flight chats.
# Fail-fast ONLY — it is NOT the cross-process lock (that is the BEGIN IMMEDIATE write-transaction).
_inflight: contextvars.ContextVar = contextvars.ContextVar("conv_mutate_inflight", default=frozenset())

_LOCK_RETRY_S = 0.025   # backoff between BUSY retries
_LOCK_BUDGET_S = 15.0   # total wall-clock budget before ConvLockBusyError (must exceed any single
                        # writer's hold; nothing here awaits I/O, so the only wait is another short mutate)


def mutate(chat_id: int, mutator, *, force: bool = False) -> bool:
    """Atomic read-modify-write of ONE chat's conv_state, serialized cross-process by holding
    SQLite's BEGIN IMMEDIATE write-lock across the WHOLE read->mutate->write. The ONLY safe way for
    two writers (an inbound-router turn and a schedule/timer handler) to touch the SAME chat blob
    without a last-writer-wins clobber. load_state/save_state alone are a non-atomic two-connection
    RMW and MUST NOT be called directly by a flow another loop can also write.

    mutator(state: dict) -> dict | None : mutate the decoded state and RETURN the new dict, or
    mutate IN PLACE and return None. Runs INSIDE the held write-transaction, so it MUST be tight:
    no Telegram/Claude/sleep/network. Returns True iff a durable write happened (state changed),
    False if the encoded payload is byte-identical to what is on disk. force=True always writes
    (e.g. a heartbeat that only advances updated_ts). NOT reentrant: a nested mutate on the same
    chat raises ReentrantMutateError immediately. Crash-safe: SQLite frees the write-lock when the
    connection dies; a raising mutator (or encode/encrypt raise / KekUnavailable) ROLLBACKs with NO
    partial write and propagates. Encryption-agnostic: reuses serialize/deserialize + the same
    context_cipher bridge load_state/save_state use; adds no crypto import (vps_spec_guard #14 clean)."""
    if chat_id is None:
        raise ValueError("mutate requires a non-empty chat_id")
    held = _inflight.get()
    if chat_id in held:
        raise ReentrantMutateError(
            f"nested mutate on chat {chat_id}: compose all changes into one mutator"
        )
    deadline = _now() + _LOCK_BUDGET_S
    while True:
        try:
            return _mutate_once(chat_id, mutator, force=force, held=held)
        except sqlite3.OperationalError as exc:
            # BEGIN IMMEDIATE (or COMMIT) lost the single-writer slot under WAL. Treat 'database is
            # locked/busy' as contention and RETRY within budget — not a hard mutate failure.
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if _now() >= deadline:
                raise ConvLockBusyError(
                    f"conv_state write-lock busy for chat {chat_id} after {_LOCK_BUDGET_S}s"
                ) from exc
            time.sleep(_LOCK_RETRY_S)


def _mutate_once(chat_id: int, mutator, *, force: bool, held: frozenset) -> bool:
    with _conn() as conn:                          # WAL, busy_timeout=5000, isolation_level=None
        conn.execute("BEGIN IMMEDIATE")            # THE LOCK: SQLite write-lock NOW, held to COMMIT/ROLLBACK
        try:
            row = conn.execute(
                "SELECT blob, blob_encrypted FROM conv_state WHERE chat_id=?", (chat_id,)
            ).fetchone()
            # before_text = the EXACT on-disk text (None if no row). Decrypt an encrypted row via
            # the SAME bridge load_state uses — fail-closed: a quarantined row RAISES here, and the
            # except arm ROLLBACKs (releasing the lock). row_id=chat_id is the AAD (PRIMARY KEY).
            if row is None:
                before_text = None
            elif row["blob_encrypted"] is not None:
                before_text = decrypt_for_storage(
                    row["blob_encrypted"], domain="conv_state", row_id=chat_id, field="blob"
                )
            elif row["blob"] is not None:
                raw = row["blob"]
                before_text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
            else:
                before_text = None

            state = {} if not before_text else deserialize(before_text)   # {} when no row / empty

            token = held | {chat_id}               # mark this chat in-flight (reentrancy guard)
            reset = _inflight.set(token)
            try:
                result = mutator(state)            # runs INSIDE the held tx; must be tight
            finally:
                _inflight.reset(reset)

            new_state = state if result is None else result
            after_text = serialize(new_state)      # the only re-encode (deterministic codec)

            if not force and before_text is not None and after_text == before_text:
                conn.execute("ROLLBACK")           # byte-identical on disk -> no write, wrote=False
                return False

            env = encrypt_for_storage("conv_state", chat_id, "blob", after_text)  # None OFF; raises ON+no KEK
            now = _now()
            if env is None:
                conn.execute(
                    "INSERT INTO conv_state (chat_id, blob, blob_encrypted, updated_ts) "
                    "VALUES (?,?,NULL,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET blob=excluded.blob, "
                    "blob_encrypted=NULL, updated_ts=excluded.updated_ts",
                    (chat_id, after_text.encode("utf-8"), now),
                )
            else:
                conn.execute(
                    "INSERT INTO conv_state (chat_id, blob, blob_encrypted, updated_ts) "
                    "VALUES (?,NULL,?,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET blob=NULL, "
                    "blob_encrypted=excluded.blob_encrypted, updated_ts=excluded.updated_ts",
                    (chat_id, env, now),
                )
            conn.execute("COMMIT")
            return True
        except BaseException:
            try:
                conn.execute("ROLLBACK")           # mutator/encode/encrypt raise -> NO partial write
            except sqlite3.OperationalError:
                pass                               # never mask the real exception with a rollback error
            raise
