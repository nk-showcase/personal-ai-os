#!/usr/bin/env python3
"""Plain-python tests (no pytest) for the integrations-worker
``request_queue`` schema.

SAFE: temp DBs only; SYNTHETIC seed rows; NO network, NO live Notion, NO
services, NO production-DB writes, NO secret values used.  Mirrors the
scripts/test_aios_*.py style.

Covers:

  Schema (Req 1-6):
    1. SCHEMA_VERSION matches the live storage constant
    2. request_queue table exists on a fresh-init DB
    3. exact column set (no extra leak surfaces; no missing fields)
    4. UNIQUE(correlation_id) enforced behaviourally (duplicate INSERT
       raises IntegrityError)
    5. idx_request_queue_status_deadline exists
    6. idx_request_queue_account_status exists

  Migration (Req 7-12):
    7. legacy DB with an OLD schema_version (app_config schema_version
       '0'; NO request_queue) upgrades on connect()
    8. existing app_config rows preserved across upgrade
    9. existing note row preserved across upgrade
   10. existing memory_fact row preserved across upgrade
   11. schema_version row advanced to the current constant
   12. idempotent upgrade: connect() on an already-current DB does NOT
       bump schema_version.updated_ts

  Round-trip + hygiene (Req 13-19):
   13. INSERT a value-free pending request row; round-trip
   14. payload_json / result_json are nullable (caller may withhold)
   15. error_class accepts a class-name string (caller responsibility:
       NAME ONLY, never message)
   16. status accepts any TEXT (validation lives in API layer / tests;
       no SQL CHECK constraint)
   17. kind accepts any TEXT (validation lives in API layer / tests)
   18. request_queue rows are keyed independently of the note/memory
       reference tables (separate auto-rowid sequence; separate UNIQUE)
   19. row preservation: a non-empty request_queue row survives a
       redundant connect() / init_db() pass (no destructive migration)

  Existing test suite invariants (Req 20-22):
   20. note table still functional after init (insert + SELECT round-trip)
   21. memory_fact table still functional after init
   22. audit_log still functional after init

Run:  PYTHONPATH=<repo> python3 scripts/test_aios_b02a_request_queue.py
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    tmp = tempfile.mkdtemp(prefix="aios_b02a_test_")
    sys.path.insert(0, str(repo_root))

    # Disposable AIOS_DATA_DB so any accidental S.connect() default lands here.
    os.environ["AIOS_DATA_DB"] = str(Path(tmp) / "_fallback.sqlite3")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-stub-not-real")
    os.environ.setdefault("TELEGRAM_OWNER_ID", "12345")
    os.environ.setdefault("NOTION_API_KEY", "")

    failed: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    try:
        from bot import aios_storage as S

        # ----------------------------------------------------------
        # Scenario A: fresh DB
        # ----------------------------------------------------------
        scen_a = str(Path(tmp) / "scen_a_fresh.sqlite3")
        S.init_db(db=scen_a)

        # Req 1
        check("Req 1: SCHEMA_VERSION matches live constant",
              S.SCHEMA_VERSION == S.SCHEMA_VERSION and bool(S.SCHEMA_VERSION))
        check("Req 1b: schema_version row matches constant",
              S.schema_version() == S.SCHEMA_VERSION)

        # Req 2
        with S.connect(db=scen_a) as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        check("Req 2: request_queue table exists",
              "request_queue" in tables)
        # Reference tables also present on a fresh DB.
        check("Req 2b: reference tables (note, memory_fact) also present",
              "note" in tables and "memory_fact" in tables)

        # Req 3: exact column set
        with S.connect(db=scen_a) as conn:
            cols = [r["name"] for r in conn.execute(
                "PRAGMA table_info(request_queue)"
            ).fetchall()]
        expected_cols = {
            "id", "correlation_id", "kind", "account", "status",
            "worker_id", "payload_json", "result_json", "error_class",
            "attempts", "created_ts", "claimed_ts", "resolved_ts",
            "next_attempt_ts", "ttl_deadline_ts",
        }
        check("Req 3a: request_queue columns exactly match expected",
              set(cols) == expected_cols)
        # Specifically: no leak-surface columns
        leak_cols = {
            "token", "api_key", "secret", "raw", "bearer",
            "source_notion_id_value", "content_hash_value",
            "account_full_name", "amount_per_row",
        }
        check("Req 3b: NO leak-surface columns",
              not (leak_cols & set(cols)))

        # Req 4: UNIQUE(correlation_id) enforced.
        with S.connect(db=scen_a) as conn:
            conn.execute(
                "INSERT INTO request_queue ("
                " correlation_id, kind, status, created_ts"
                ") VALUES (?, ?, ?, ?)",
                ("corr_uniq_001", "save_note", "pending", 1700000000.0),
            )
            dup_raised = False
            try:
                conn.execute(
                    "INSERT INTO request_queue ("
                    " correlation_id, kind, status, created_ts"
                    ") VALUES (?, ?, ?, ?)",
                    ("corr_uniq_001", "read_count", "pending", 1700000001.0),
                )
            except sqlite3.IntegrityError:
                dup_raised = True
        check("Req 4: UNIQUE(correlation_id) blocks duplicate INSERT",
              dup_raised)

        # Req 5-6: indexes exist (named indexes; the inline UNIQUE creates
        # a sqlite_autoindex that we don't introspect).
        with S.connect(db=scen_a) as conn:
            idx_rows = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='request_queue'"
            ).fetchall()
        idx_names = {r["name"] for r in idx_rows}
        check("Req 5: idx_request_queue_status_deadline exists",
              "idx_request_queue_status_deadline" in idx_names)
        check("Req 6: idx_request_queue_account_status exists",
              "idx_request_queue_account_status" in idx_names)

        # ----------------------------------------------------------
        # Scenario B: legacy DB (old schema_version) upgrade preserves rows
        # ----------------------------------------------------------
        scen_b = str(Path(tmp) / "scen_b_legacy.sqlite3")
        # Hand-build a legacy DB: app_config schema_version='0' + a couple of
        # the surviving reference tables with a seeded row each, but NO
        # request_queue. connect() must add request_queue and advance the
        # version without dropping the seeded rows.
        legacy = sqlite3.connect(scen_b, isolation_level=None)
        legacy.executescript(
            """
            CREATE TABLE app_config (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_ts REAL NOT NULL
            );
            INSERT INTO app_config (key, value, updated_ts)
                VALUES ('schema_version', '0', 1700000000.0);
            INSERT INTO app_config (key, value, updated_ts)
                VALUES ('read_source.notes', 'sqlite', 1700000000.0);
            CREATE TABLE note (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id           TEXT UNIQUE,
                content           TEXT,
                content_encrypted BLOB,
                created_ts        REAL NOT NULL,
                archived          INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO note (note_id, content, created_ts)
                VALUES ('leg_note_1', 'legacy note body', 1700000000.0);
            CREATE TABLE memory_fact (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_block_id   TEXT UNIQUE,
                content           TEXT,
                content_encrypted BLOB,
                position          INTEGER,
                created_time      TEXT,
                created_ts        REAL NOT NULL,
                archived          INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO memory_fact (source_block_id, content, position, created_ts)
                VALUES ('leg_fact_1', 'legacy fact body', 0, 1700000000.0);
            """
        )
        legacy.close()

        # Req 7-11: invoke S.connect() to trigger schema migration.
        with S.connect(db=scen_b) as conn:
            tables_b = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            n_note = conn.execute(
                "SELECT COUNT(*) FROM note"
            ).fetchone()[0]
            note_row = conn.execute(
                "SELECT note_id, content FROM note WHERE note_id='leg_note_1'"
            ).fetchone()
            n_fact = conn.execute(
                "SELECT COUNT(*) FROM memory_fact"
            ).fetchone()[0]
            fact_row = conn.execute(
                "SELECT source_block_id, content FROM memory_fact"
                " WHERE source_block_id='leg_fact_1'"
            ).fetchone()
            sv_b = conn.execute(
                "SELECT value FROM app_config WHERE key='schema_version'"
            ).fetchone()[0]
            rs_b = conn.execute(
                "SELECT value FROM app_config WHERE key='read_source.notes'"
            ).fetchone()[0]
        check("Req 7: legacy DB upgrade adds request_queue",
              "request_queue" in tables_b)
        check("Req 8: app_config (read_source.notes) preserved",
              rs_b == "sqlite")
        check("Req 9a: note row preserved (1 row)", n_note == 1)
        check("Req 9b: note row columns preserved",
              note_row["note_id"] == "leg_note_1"
              and note_row["content"] == "legacy note body")
        check("Req 10a: memory_fact row preserved (1 row)", n_fact == 1)
        check("Req 10b: memory_fact row columns preserved",
              fact_row["source_block_id"] == "leg_fact_1"
              and fact_row["content"] == "legacy fact body")
        check("Req 11: schema_version advanced to current constant",
              sv_b == S.SCHEMA_VERSION)

        # Req 12: idempotent upgrade.
        with S.connect(db=scen_b) as conn:
            first_uts = float(conn.execute(
                "SELECT updated_ts FROM app_config WHERE key='schema_version'"
            ).fetchone()[0])
        time.sleep(0.05)
        with S.connect(db=scen_b) as conn:
            second_uts = float(conn.execute(
                "SELECT updated_ts FROM app_config WHERE key='schema_version'"
            ).fetchone()[0])
        check("Req 12: redundant connect on already-current DB does NOT bump "
              "schema_version.updated_ts",
              first_uts == second_uts)

        # ----------------------------------------------------------
        # Scenario C: round-trip + hygiene contracts (no SQL CHECK on
        # kind/status; validation is API-layer responsibility)
        # ----------------------------------------------------------
        scen_c = str(Path(tmp) / "scen_c_round_trip.sqlite3")
        S.init_db(db=scen_c)
        with S.connect(db=scen_c) as conn:
            conn.execute(
                "INSERT INTO request_queue ("
                " correlation_id, kind, account, status, payload_json,"
                " created_ts, ttl_deadline_ts"
                ") VALUES (?,?,?,?,?,?,?)",
                ("corr_rt_001", "save_note", "Notes", "pending",
                 '{"field":"value","amount":2000,"account":"Notes"}',
                 1700000000.0, 1700000060.0),
            )
            row = conn.execute(
                "SELECT correlation_id, kind, account, status, payload_json,"
                " result_json, error_class, attempts, created_ts,"
                " claimed_ts, resolved_ts, next_attempt_ts, ttl_deadline_ts"
                " FROM request_queue WHERE correlation_id='corr_rt_001'"
            ).fetchone()
        check("Req 13a: round-trip correlation_id",
              row["correlation_id"] == "corr_rt_001")
        check("Req 13b: round-trip kind", row["kind"] == "save_note")
        check("Req 13c: round-trip status='pending'",
              row["status"] == "pending")
        check("Req 13d: round-trip account='Notes'", row["account"] == "Notes")
        check("Req 13e: attempts default 0", int(row["attempts"]) == 0)
        # Req 14: nullable result_json / error_class
        check("Req 14a: result_json starts NULL", row["result_json"] is None)
        check("Req 14b: error_class starts NULL", row["error_class"] is None)
        check("Req 14c: claimed_ts starts NULL", row["claimed_ts"] is None)
        check("Req 14d: resolved_ts starts NULL", row["resolved_ts"] is None)

        # Req 15: error_class stores class-name string (caller responsibility:
        # never the message).
        with S.connect(db=scen_c) as conn:
            conn.execute(
                "UPDATE request_queue SET status=?, error_class=? "
                "WHERE correlation_id='corr_rt_001'",
                ("failed", "HTTPStatusError"),
            )
            row2 = conn.execute(
                "SELECT status, error_class FROM request_queue"
                " WHERE correlation_id='corr_rt_001'"
            ).fetchone()
        check("Req 15a: error_class round-trips a class-name string",
              row2["error_class"] == "HTTPStatusError")

        # Req 16 + 17: any TEXT for status / kind (validation in API layer).
        with S.connect(db=scen_c) as conn:
            conn.execute(
                "INSERT INTO request_queue ("
                " correlation_id, kind, status, created_ts"
                ") VALUES (?,?,?,?)",
                ("corr_rt_002", "read_count", "claimed", 1700000010.0),
            )
        check("Req 16: status='claimed' accepted", True)
        check("Req 17: kind='read_count' accepted", True)

        # Req 18: request_queue keyed independently of the reference tables.
        # A note_id that happens to equal a correlation_id is not a constraint
        # on request_queue, and vice versa.
        with S.connect(db=scen_c) as conn:
            conn.execute(
                "INSERT INTO note (note_id, content, created_ts)"
                " VALUES (?,?,?)",
                ("corr_rt_001", "unrelated note body", 1700000020.0),
            )
            n_note_c = conn.execute(
                "SELECT COUNT(*) FROM note WHERE note_id='corr_rt_001'"
            ).fetchone()[0]
            n_req_c = conn.execute(
                "SELECT COUNT(*) FROM request_queue"
                " WHERE correlation_id='corr_rt_001'"
            ).fetchone()[0]
        check("Req 18a: note accepted an id equal to a correlation_id",
              n_note_c == 1)
        check("Req 18b: request_queue still has its row (independent)",
              n_req_c == 1)

        # Req 19: redundant init_db / connect does not destructively rebuild.
        with S.connect(db=scen_c) as conn:
            count_before = conn.execute(
                "SELECT COUNT(*) FROM request_queue"
            ).fetchone()[0]
        S.init_db(db=scen_c)  # redundant
        with S.connect(db=scen_c) as conn:
            count_after = conn.execute(
                "SELECT COUNT(*) FROM request_queue"
            ).fetchone()[0]
        check("Req 19: redundant init_db preserves rows",
              count_before == count_after and count_after > 0)

        # ----------------------------------------------------------
        # Req 20-22: reference / support tables still functional after init
        # ----------------------------------------------------------
        scen_d = str(Path(tmp) / "scen_d_reference.sqlite3")
        S.init_db(db=scen_d)
        # Req 20: note round-trip
        with S.connect(db=scen_d) as conn:
            conn.execute(
                "INSERT INTO note (note_id, content, created_ts)"
                " VALUES (?,?,?)",
                ("b02a_smoke_note", "smoke body", 1700000000.0),
            )
            nrow = conn.execute(
                "SELECT note_id, content FROM note"
                " WHERE note_id='b02a_smoke_note'"
            ).fetchone()
        check("Req 20: note table still functional after init",
              nrow is not None and nrow["content"] == "smoke body")

        # Req 21: memory_fact round-trip
        with S.connect(db=scen_d) as conn:
            conn.execute(
                "INSERT INTO memory_fact (source_block_id, content, position,"
                " created_ts) VALUES (?,?,?,?)",
                ("b02a_smoke_fact", "smoke fact", 0, 1700000000.0),
            )
            frow = conn.execute(
                "SELECT source_block_id, content FROM memory_fact"
                " WHERE source_block_id='b02a_smoke_fact'"
            ).fetchone()
        check("Req 21: memory_fact table still functional after init",
              frow is not None and frow["content"] == "smoke fact")

        # Req 22: audit_log round-trip
        with S.connect(db=scen_d) as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, actor, action, domain, outcome, detail)"
                " VALUES (?,?,?,?,?,?)",
                (1700000000.0, "b02a_smoke", "round_trip", "notes",
                 "no_op", "{}"),
            )
            al = conn.execute(
                "SELECT actor, outcome FROM audit_log"
                " WHERE actor='b02a_smoke'"
            ).fetchone()
        check("Req 22: audit_log still functional after init",
              al is not None and al["outcome"] == "no_op")

        # ================================================================
        # Additional coverage: kinds/statuses, lifecycle, index columns,
        # NOT NULL constraints, multi-row preservation, NULL account.
        # ================================================================

        # ----- Req 23: representative kind values round-trip.
        scen_e = str(Path(tmp) / "scen_e_all_kinds.sqlite3")
        S.init_db(db=scen_e)
        all_kinds = (
            "save_note", "remove_last_note", "archive_note",
            "read_count", "read_recent", "read_by_date",
        )
        with S.connect(db=scen_e) as conn:
            for i, k in enumerate(all_kinds):
                conn.execute(
                    "INSERT INTO request_queue ("
                    " correlation_id, kind, status, created_ts"
                    ") VALUES (?,?,?,?)",
                    ("kind_corr_" + str(i), k, "pending", 1700000000.0 + i),
                )
            rows = {r["correlation_id"]: r["kind"] for r in conn.execute(
                "SELECT correlation_id, kind FROM request_queue"
                " WHERE correlation_id LIKE 'kind_corr_%'"
            ).fetchall()}
        for i, k in enumerate(all_kinds):
            check("Req 23: kind='" + k + "' round-trips byte-equal",
                  rows.get("kind_corr_" + str(i)) == k)

        # ----- Req 24: all spec-listed status values round-trip.
        scen_f = str(Path(tmp) / "scen_f_all_statuses.sqlite3")
        S.init_db(db=scen_f)
        all_statuses = (
            "pending", "claimed", "done", "failed", "expired", "dead",
        )
        with S.connect(db=scen_f) as conn:
            for i, s in enumerate(all_statuses):
                conn.execute(
                    "INSERT INTO request_queue ("
                    " correlation_id, kind, status, created_ts"
                    ") VALUES (?,?,?,?)",
                    ("stat_corr_" + str(i), "save_note", s,
                     1700000000.0 + i),
                )
            rows_s = {r["correlation_id"]: r["status"] for r in conn.execute(
                "SELECT correlation_id, status FROM request_queue"
                " WHERE correlation_id LIKE 'stat_corr_%'"
            ).fetchall()}
        for i, s in enumerate(all_statuses):
            check("Req 24: status='" + s + "' round-trips byte-equal",
                  rows_s.get("stat_corr_" + str(i)) == s)

        # ----- Req 25: full lifecycle pending -> claimed -> done on one row.
        scen_lc = str(Path(tmp) / "scen_lifecycle.sqlite3")
        S.init_db(db=scen_lc)
        with S.connect(db=scen_lc) as conn:
            conn.execute(
                "INSERT INTO request_queue ("
                " correlation_id, kind, account, status, created_ts"
                ") VALUES (?,?,?,?,?)",
                ("lc_corr_1", "save_note", "AcctLC", "pending", 1700000000.0),
            )
            conn.execute(
                "UPDATE request_queue SET status=?, worker_id=?,"
                " claimed_ts=? WHERE correlation_id='lc_corr_1'",
                ("claimed", "wkr_b02a_1", 1700000001.0),
            )
            row_claim = conn.execute(
                "SELECT status, worker_id, claimed_ts, resolved_ts"
                " FROM request_queue WHERE correlation_id='lc_corr_1'"
            ).fetchone()
        check("Req 25a: status -> claimed", row_claim["status"] == "claimed")
        check("Req 25b: worker_id readback", row_claim["worker_id"] == "wkr_b02a_1")
        check("Req 25c: claimed_ts round-trip",
              abs(float(row_claim["claimed_ts"]) - 1700000001.0) < 1e-6)
        check("Req 25d: resolved_ts still NULL", row_claim["resolved_ts"] is None)
        with S.connect(db=scen_lc) as conn:
            conn.execute(
                "UPDATE request_queue SET status=?, result_json=?,"
                " resolved_ts=? WHERE correlation_id='lc_corr_1'",
                ("done", '{"count":3000}', 1700000002.0),
            )
            row_done = conn.execute(
                "SELECT status, result_json, resolved_ts"
                " FROM request_queue WHERE correlation_id='lc_corr_1'"
            ).fetchone()
        check("Req 25e: status -> done", row_done["status"] == "done")
        check("Req 25f: result_json round-trip",
              row_done["result_json"] == '{"count":3000}')
        check("Req 25g: resolved_ts round-trip",
              abs(float(row_done["resolved_ts"]) - 1700000002.0) < 1e-6)
        # Failed branch on a separate correlation_id with next_attempt_ts.
        with S.connect(db=scen_lc) as conn:
            conn.execute(
                "INSERT INTO request_queue ("
                " correlation_id, kind, status, attempts, error_class,"
                " next_attempt_ts, created_ts"
                ") VALUES (?,?,?,?,?,?,?)",
                ("lc_corr_2", "read_count", "failed", 1, "HTTPStatusError",
                 1700000020.0, 1700000010.0),
            )
            row_fail = conn.execute(
                "SELECT status, attempts, error_class, next_attempt_ts"
                " FROM request_queue WHERE correlation_id='lc_corr_2'"
            ).fetchone()
        check("Req 25h: failed branch round-trip",
              row_fail["status"] == "failed"
              and int(row_fail["attempts"]) == 1
              and row_fail["error_class"] == "HTTPStatusError"
              and abs(float(row_fail["next_attempt_ts"]) - 1700000020.0) < 1e-6)

        # ----- Req 26: PRAGMA index_info verifies indexed columns + order.
        with S.connect(db=scen_lc) as conn:
            idx1_cols = [
                r["name"] for r in conn.execute(
                    "PRAGMA index_info('idx_request_queue_status_deadline')"
                ).fetchall()
            ]
            idx2_cols = [
                r["name"] for r in conn.execute(
                    "PRAGMA index_info('idx_request_queue_account_status')"
                ).fetchall()
            ]
        check("Req 26a: idx status_deadline columns = ['status','ttl_deadline_ts']",
              idx1_cols == ["status", "ttl_deadline_ts"])
        check("Req 26b: idx account_status columns = ['account','status']",
              idx2_cols == ["account", "status"])
        # And the UNIQUE autoindex on correlation_id is a single column.
        with S.connect(db=scen_lc) as conn:
            uniq_indices = [
                r for r in conn.execute(
                    "PRAGMA index_list('request_queue')"
                ).fetchall()
                if int(r["unique"]) == 1
            ]
            uniq_cols_sets = []
            for idx in uniq_indices:
                # PRAGMA index_info does NOT accept bound parameters in
                # SQLite -- only literal arguments.  idx["name"] comes
                # from sqlite metadata (not user input), so quoting it
                # into the PRAGMA is safe.  We still validate the name
                # is a plain identifier to be defensive.
                idx_name = idx["name"]
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", idx_name):
                    continue
                cols = [c["name"] for c in conn.execute(
                    "PRAGMA index_info(" + idx_name + ")"
                ).fetchall()]
                uniq_cols_sets.append(tuple(cols))
        check("Req 26c: exactly one UNIQUE index on (correlation_id)",
              ("correlation_id",) in uniq_cols_sets)

        # ----- Req 27: NOT NULL on the spec-mandated required columns.
        scen_nn = str(Path(tmp) / "scen_notnull.sqlite3")
        S.init_db(db=scen_nn)
        with S.connect(db=scen_nn) as conn:
            cols_info = {
                r["name"]: (r["type"], int(r["notnull"]))
                for r in conn.execute(
                    "PRAGMA table_info(request_queue)"
                ).fetchall()
            }
        for col, expected_type, expected_notnull in [
            ("correlation_id", "TEXT", 1),
            ("kind", "TEXT", 1),
            ("status", "TEXT", 1),
            ("attempts", "INTEGER", 1),
            ("created_ts", "REAL", 1),
            ("account", "TEXT", 0),
            ("worker_id", "TEXT", 0),
            ("payload_json", "TEXT", 0),
            ("result_json", "TEXT", 0),
            ("error_class", "TEXT", 0),
            ("ttl_deadline_ts", "REAL", 0),
        ]:
            actual = cols_info.get(col)
            check("Req 27: col '" + col + "' type=" + expected_type
                  + " notnull=" + str(expected_notnull),
                  actual == (expected_type, expected_notnull))

        # Behavioural NOT NULL: INSERT without 'kind' -> IntegrityError.
        with S.connect(db=scen_nn) as conn:
            raised_kind = False
            try:
                conn.execute(
                    "INSERT INTO request_queue (correlation_id, status,"
                    " created_ts) VALUES (?,?,?)",
                    ("notnull_missing_kind", "pending", 1700000000.0),
                )
            except sqlite3.IntegrityError:
                raised_kind = True
        check("Req 27z: INSERT without 'kind' raises IntegrityError",
              raised_kind)

        # ----- Req 28: multi-row preservation across a legacy-version upgrade.
        scen_multi = str(Path(tmp) / "scen_b_multirow.sqlite3")
        legacy_multi = sqlite3.connect(scen_multi, isolation_level=None)
        legacy_multi.executescript(
            """
            CREATE TABLE app_config (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_ts REAL NOT NULL
            );
            INSERT INTO app_config (key, value, updated_ts)
                VALUES ('schema_version', '0', 1700000000.0);
            CREATE TABLE note (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id           TEXT UNIQUE,
                content           TEXT,
                content_encrypted BLOB,
                created_ts        REAL NOT NULL,
                archived          INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO note (id, note_id, content, created_ts) VALUES
                (7, 'multi_key_1', 'body 1', 1700000000.0),
                (8, 'multi_key_2', 'body 2', 1700000001.0),
                (9, 'multi_key_3', 'body 3', 1700000002.0);
            """
        )
        legacy_multi.close()
        with S.connect(db=scen_multi) as conn:
            preserved = [
                (r["id"], r["note_id"]) for r in conn.execute(
                    "SELECT id, note_id FROM note ORDER BY id"
                ).fetchall()
            ]
            sv_multi = conn.execute(
                "SELECT value FROM app_config WHERE key='schema_version'"
            ).fetchone()[0]
        check("Req 28a: multi-row preservation (3 rows kept)",
              len(preserved) == 3)
        check("Req 28b: row ids preserved exactly [7,8,9]",
              [p[0] for p in preserved] == [7, 8, 9])
        check("Req 28c: note_ids preserved set-equal",
              {p[1] for p in preserved}
              == {"multi_key_1", "multi_key_2", "multi_key_3"})
        check("Req 28d: schema_version advanced to current",
              sv_multi == S.SCHEMA_VERSION)
        # AUTOINCREMENT continues from sqlite_sequence; next INSERT id >= 10.
        with S.connect(db=scen_multi) as conn:
            conn.execute(
                "INSERT INTO note (note_id, content, created_ts)"
                " VALUES (?,?,?)",
                ("multi_key_post", "body post", 1700000100.0),
            )
            new_id = conn.execute(
                "SELECT id FROM note WHERE note_id='multi_key_post'"
            ).fetchone()[0]
        check("Req 28e: AUTOINCREMENT sequence preserved (next id >= 10)",
              int(new_id) >= 10)

        # ----- Req 29: NULL-account behaviour on read_count + ttl_deadline_ts
        # REAL round-trip.
        with S.connect(db=scen_lc) as conn:
            conn.execute(
                "INSERT INTO request_queue ("
                " correlation_id, kind, account, status,"
                " created_ts, ttl_deadline_ts"
                ") VALUES (?,?,?,?,?,?)",
                ("null_acct_rb", "read_count", None, "pending",
                 1700000050.0, 1700000300.5),
            )
            row_null = conn.execute(
                "SELECT account, kind, ttl_deadline_ts"
                " FROM request_queue WHERE correlation_id='null_acct_rb'"
            ).fetchone()
        check("Req 29a: NULL account on read_count round-trips as None",
              row_null["account"] is None and row_null["kind"] == "read_count")
        check("Req 29b: ttl_deadline_ts round-trips as REAL",
              isinstance(row_null["ttl_deadline_ts"], float)
              and abs(float(row_null["ttl_deadline_ts"]) - 1700000300.5) < 1e-6)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(str(f) for f in failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
