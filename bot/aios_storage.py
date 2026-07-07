"""bot/aios_storage.py — AI OS main SQLite storage FOUNDATION.

Foundation ONLY. No business logic, no network, no secrets. Mirrors the proven
bot/aios_request_queue.py pattern (out-of-repo DB, WAL, busy_timeout, foreign_keys,
migration-safe CREATE IF NOT EXISTS, lazy/idempotent init, 0700/0600).

DB lives OUTSIDE the repo:
    default  ~/.ai-os/data/aios.sqlite3
    override env AIOS_DATA_DB

Import has NO side effects: the DB + schema are created lazily on the first
connection. WAL + SHM siblings are protected by the 0700 data dir.

Scope: init DB, get connection, apply schema, schema_version / app_config, plus
tiny config helpers used by tests. No business data is imported here.

Reference domains (this published build): `chat` (conversation transcripts),
`memory` (long-term facts) and the demo `note` domain. Every personal free-text
field is stored encrypted at rest via the context_cipher chokepoint; a cleartext
companion column exists only for the flag-OFF path (NULL when encrypted).
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

# Bump on any schema change; pair with an additive ALTER-TABLE migration.
# v1: chat / chat_message / memory_fact / note reference domains + request_queue
#     RPC lifecycle + the generic support tables (unrouted_inbox, audit_log,
#     app_config, decrypt_quarantine). Free text is at-rest encrypted through the
#     context_cipher chokepoint; cleartext companion columns are NULL when encrypted.
SCHEMA_VERSION = "1"

_BUSY_TIMEOUT_MS = 5000


def db_path() -> Path:
    """Resolve the AI OS main DB path (env override or default, out of repo)."""
    raw = os.getenv("AIOS_DATA_DB")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".ai-os" / "data" / "aios.sqlite3"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_notion_page_id TEXT UNIQUE,
    title        TEXT,               -- NULL when encrypted (real title in title_encrypted)
    status       TEXT,
    created_time TEXT,
    created_ts   REAL NOT NULL,
    updated_ts   REAL NOT NULL
);

-- chat message free-text content is ALWAYS encrypted (age/KDB envelope): there is no
-- cleartext column, so a keyless process cannot store or serve message bodies.
CREATE TABLE IF NOT EXISTS chat_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER REFERENCES chat(id) ON DELETE CASCADE,
    source_block_id TEXT UNIQUE,
    role            TEXT,
    position        INTEGER,
    content_encrypted BLOB,
    created_ts      REAL NOT NULL
);

-- 'memory' domain: long-term facts. Free text -> encrypted (content_encrypted); the
-- cleartext column only serves the pre-encryption / flag-OFF path (dual-read).
CREATE TABLE IF NOT EXISTS memory_fact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_block_id   TEXT UNIQUE,
    content           TEXT,
    content_encrypted BLOB,
    position          INTEGER,
    created_time      TEXT,
    created_ts        REAL NOT NULL,
    archived          INTEGER NOT NULL DEFAULT 0
);

-- reference "notes" demo domain: one free-text note per row, encrypted at rest via the
-- context_cipher chokepoint (cleartext column only for the flag-OFF path; mirrors memory_fact).
-- No personal fields — a note is a single opaque string keyed on a bot-minted note_id.
CREATE TABLE IF NOT EXISTS note (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id           TEXT UNIQUE,
    content           TEXT,
    content_encrypted BLOB,
    created_ts        REAL NOT NULL,
    archived          INTEGER NOT NULL DEFAULT 0
);

-- no-silent-failure dead-letter. Holds raw user text -> sensitive tier
-- (0600 + retention limit + KDB candidate). Never to git/logs in cleartext.
CREATE TABLE IF NOT EXISTS unrouted_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 REAL NOT NULL,
    chat_id            INTEGER,
    raw_message        TEXT,
    classifier_attempt TEXT,
    replied            INTEGER NOT NULL DEFAULT 0
);

-- value-free audit log: no PII / no free-text. 'detail' carries redacted/structured only.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    actor     TEXT,
    action    TEXT,
    domain    TEXT,
    entity_id INTEGER,
    outcome   TEXT,
    detail    TEXT
);

-- config + migration state, e.g. read_source.<domain> = notion|sqlite|dual ; schema_version.
CREATE TABLE IF NOT EXISTS app_config (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_ts REAL NOT NULL
);

-- integrations-worker RPC request lifecycle queue: a request/response queue with a stable
-- correlation_id, payload_json, result_json, and a TTL deadline. The bot ENQUEUES requests;
-- the keyed integrations-worker process claims, executes, and resolves them.
--
--   * payload_json / result_json MUST be secret-free (no tokens, no env values). Enforced
--     at the API layer; the table schema has no leak-surface columns by design.
--   * error_class stores exception CLASS NAME ONLY (never message).
--   * status is one of {pending, claimed, done, failed, expired, dead}.
-- Validation of kind/status is performed in the API layer + tests (no SQL CHECK constraints).
CREATE TABLE IF NOT EXISTS request_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id   TEXT NOT NULL UNIQUE,
    kind             TEXT NOT NULL,
    account          TEXT,
    status           TEXT NOT NULL,
    worker_id        TEXT,
    payload_json     TEXT,
    result_json      TEXT,
    error_class      TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_ts       REAL NOT NULL,
    claimed_ts       REAL,
    resolved_ts      REAL,
    next_attempt_ts  REAL,
    ttl_deadline_ts  REAL
);
CREATE INDEX IF NOT EXISTS idx_request_queue_status_deadline
    ON request_queue(status, ttl_deadline_ts);
CREATE INDEX IF NOT EXISTS idx_request_queue_account_status
    ON request_queue(account, status);

-- value-free decrypt dead-letter. A decrypt failure (tamper / wrong key / malformed
-- envelope) quarantines the row and RAISES; the row is NEVER served cleartext. Value-free
-- at the schema level (like audit_log): NO column can hold ciphertext / plaintext / keys —
-- only row identity + the exception CLASS name.
CREATE TABLE IF NOT EXISTS decrypt_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    row_id      TEXT,
    domain      TEXT,
    field       TEXT,
    error_class TEXT,
    ts          REAL NOT NULL
);
"""


# Personal free-text -> *_encrypted BLOB columns. Added via guarded ALTER in _ensure_schema
# so legacy DBs upgrade additively. NULL = cleartext (back-compatible dual-read). The remaining
# encrypted columns (chat_message.content_encrypted, memory_fact.content_encrypted,
# note.content_encrypted) are already inline in _SCHEMA; only chat.title has a cleartext
# companion added by ALTER here.
_ENC_COLUMNS = (
    ("chat", "title_encrypted"),
)

# Soft-delete flag: deletions must be representable locally (local reads filter archived=0;
# catch-up reconciliation tombstones rows missing from the source). Guarded ALTER, additive,
# DEFAULT 0 backfills legacy rows. (memory_fact / note carry `archived` inline in _SCHEMA.)
_EXTRA_COLUMNS = (
    ("chat", "archived", "INTEGER NOT NULL DEFAULT 0"),
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: CREATE IF NOT EXISTS for all tables/indexes + seed/upgrade schema_version.

    Migration-safe pattern: future schema changes add columns via
    `ALTER TABLE ... ADD COLUMN ... DEFAULT ...` guarded by PRAGMA table_info, and bump
    SCHEMA_VERSION. No destructive rebuild.

    Upgrade path: if the stored ``schema_version`` differs from the current constant, this
    rewrites it after the DDL pass (all schema changes to date are additive CREATE IF NOT
    EXISTS — new tables/indexes appear on the legacy DB without touching existing rows).
    """
    conn.executescript(_SCHEMA)
    # Additive *_encrypted BLOB columns (guarded). No DEFAULT -> NULL means "not encrypted"
    # -> back-compatible dual-read. Table/column names are hardcoded constants -> no
    # SQL-injection surface.
    for _t, _c in _ENC_COLUMNS:
        _cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_t})").fetchall()}
        if _c not in _cols:
            conn.execute(f"ALTER TABLE {_t} ADD COLUMN {_c} BLOB")
    for _t, _c, _ddl in _EXTRA_COLUMNS:
        _cols = {r[1] for r in conn.execute(f"PRAGMA table_info({_t})").fetchall()}
        if _c not in _cols:
            conn.execute(f"ALTER TABLE {_t} ADD COLUMN {_c} {_ddl}")
    row = conn.execute("SELECT value FROM app_config WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO app_config (key, value, updated_ts) VALUES ('schema_version', ?, ?)",
            (SCHEMA_VERSION, time.time()),
        )
    elif row["value"] != SCHEMA_VERSION:
        conn.execute(
            "UPDATE app_config SET value=?, updated_ts=? WHERE key='schema_version'",
            (SCHEMA_VERSION, time.time()),
        )


def _shared_db_gid():
    """Resolve the GID of AIOS_DATA_DB_GROUP, or None.

    When set, connect() uses group-rw perms so two service users (the transport user +
    the keyed integrations worker) can share ONE queue DB. Unset -> single-user 0700/0600
    default (unchanged). grp is POSIX-only; any failure -> None (safe)."""
    name = os.getenv("AIOS_DATA_DB_GROUP", "").strip()
    if not name:
        return None
    try:
        import grp
        return grp.getgrnam(name).gr_gid
    except (KeyError, ImportError, OSError):
        return None


@contextmanager
def connect(db=None):
    """Yield a fresh autocommit connection. DB dir/file created lazily; schema applied.

    `db` optionally overrides the target path (used by tests to write to a SEPARATE DB,
    never the production data DB). Default (db=None) -> db_path() -> production behavior.

    PRAGMAs: WAL, busy_timeout, foreign_keys ON, synchronous NORMAL. The data dir is
    chmod 0700; the DB file and its WAL/SHM siblings are chmod 0600 on EVERY connect
    (even if the DB pre-existed with loose perms) where the OS supports it.
    """
    path = Path(db).expanduser() if db is not None else db_path()
    # No-downgrade: a maintenance/import run WITHOUT AIOS_DATA_DB_GROUP must never clamp a
    # pre-existing shared DB back to 0700/0600 (that locks out the service users and takes
    # the data plane down). Single-user tightening applies only to paths this very call
    # CREATES; group mode (env set) keeps the full re-assert behaviour.
    _db_existed = path.exists()
    _dir_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Group-shared perms when AIOS_DATA_DB_GROUP is set, else the unchanged single-user
    # 0700/0600. dir 2770 (setgid -> new files inherit the group); files 0660; chgrp to
    # the shared group.
    _gid = _shared_db_gid()
    _dir_mode = 0o2770 if _gid is not None else 0o700
    _file_mode = 0o660 if _gid is not None else 0o600
    if _gid is not None or not _dir_existed:
        try:
            os.chmod(path.parent, _dir_mode)
            if _gid is not None:
                os.chown(path.parent, -1, _gid)
        except OSError:
            pass
    conn = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_MS / 1000.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        _ensure_schema(conn)
        # Group mode: re-assert 0660+group on EVERY connect (a file created by the other user
        # gets re-grouped). Single-user mode: tighten to 0600 ONLY when this call created the
        # DB — never downgrade a pre-existing (possibly shared) file.
        if _gid is not None or not _db_existed:
            for _p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
                if _p.exists():
                    try:
                        os.chmod(_p, _file_mode)
                        if _gid is not None:
                            os.chown(_p, -1, _gid)
                    except OSError:
                        pass
        yield conn
    finally:
        conn.close()


def init_db(db=None) -> Path:
    """Create the DB + schema if missing (idempotent). Returns the DB path.

    `db` optionally overrides the target path (default -> db_path())."""
    with connect(db):
        pass
    return Path(db).expanduser() if db is not None else db_path()


def list_tables() -> list[str]:
    """Return user table names (excludes sqlite_* internal tables)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r["name"] for r in rows]


def get_config(key: str, default=None):
    """Read an app_config value by key (or default)."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    """Upsert an app_config key/value."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_config (key, value, updated_ts) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
            (key, value, time.time()),
        )


def schema_version():
    """Return the stored schema_version string (or None if uninitialized)."""
    return get_config("schema_version")
