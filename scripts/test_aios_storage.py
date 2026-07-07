#!/usr/bin/env python3
"""Plain-python tests (no pytest) for the AI OS SQLite FOUNDATION.

SAFE: temp DB only, SYNTHETIC data, no network / no secrets / no services.

Covers: schema init, idempotent init twice, WAL enabled, foreign_keys enabled,
permissions 0600/0700 (POSIX), chat/chat_message FK cascade, app_config/schema_version,
backup snapshot creation, restore drill read.

Run:  PYTHONPATH=<repo> python3 scripts/test_aios_storage.py   (exit 0 = OK, 1 = FAIL)
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="aios_data_test_")
    db = str(Path(tmp) / "aios.sqlite3")
    os.environ["AIOS_DATA_DB"] = db
    # repo root (for `from bot import ...`) and scripts dir (for sibling script imports)
    repo_root = Path(__file__).resolve().parent.parent
    scripts_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(scripts_dir))

    from bot import aios_storage as S
    import aios_backup
    import aios_restore_drill

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    try:
        check("import has no DB side effect (lazy create)", not Path(db).exists())

        S.init_db()
        check("db file created on init", Path(db).exists())

        expected = {
            "chat", "chat_message", "memory_fact", "note",
            "unrouted_inbox", "audit_log", "app_config",
            "request_queue", "decrypt_quarantine",
        }
        tables = set(S.list_tables())
        check("all foundation tables present", expected.issubset(tables))

        # idempotent init twice
        S.init_db()
        check("idempotent init twice (tables stable)", set(S.list_tables()) == tables)
        check("schema_version seeded", S.schema_version() == S.SCHEMA_VERSION)

        with S.connect() as conn:
            jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        check("WAL mode enabled", str(jm).lower() == "wal")
        check("foreign_keys enabled", int(fk) == 1)

        # permissions (POSIX only; Windows chmod is a no-op for these bits)
        if os.name == "posix":
            file_mode = os.stat(db).st_mode & 0o777
            dir_mode = os.stat(Path(db).parent).st_mode & 0o777
            check("db file mode 0600 (posix)", file_mode == 0o600)
            check("data dir mode 0700 (posix)", dir_mode == 0o700)
        else:
            check("permission check skipped on non-posix (informational)", True)

        # chat + chat_message: FK cascades on parent delete (child rows removed)
        with S.connect() as conn:
            cur = conn.execute(
                "INSERT INTO chat (source_notion_page_id, status, created_ts, updated_ts) "
                "VALUES ('local-chat-1','active',?,?)", (time.time(), time.time())
            )
            cid = cur.lastrowid
            conn.execute(
                "INSERT INTO chat_message (chat_id, source_block_id, role, position, created_ts) "
                "VALUES (?, 'blk-1', 'user', 0, ?)", (cid, time.time())
            )
            # UNIQUE(source_block_id) rejects a duplicate block id
            dup_rejected = False
            try:
                conn.execute(
                    "INSERT INTO chat_message (chat_id, source_block_id, role, position, created_ts) "
                    "VALUES (?, 'blk-1', 'assistant', 1, ?)", (cid, time.time())
                )
            except sqlite3.IntegrityError:
                dup_rejected = True
        check("chat_message duplicate source_block_id rejected", dup_rejected)

        with S.connect() as conn:
            conn.execute("DELETE FROM chat WHERE id=?", (cid,))
            left = conn.execute(
                "SELECT COUNT(*) FROM chat_message WHERE chat_id=?", (cid,)
            ).fetchone()[0]
        check("FK cascade deletes child messages on parent delete", left == 0)

        # app_config upsert / get
        S.set_config("read_source.chat", "notion")
        check("app_config set/get", S.get_config("read_source.chat") == "notion")
        S.set_config("read_source.chat", "sqlite")
        check("app_config upsert overwrites", S.get_config("read_source.chat") == "sqlite")
        check("app_config get missing -> default", S.get_config("nope", "dflt") == "dflt")

        # permissions: no-downgrade — connect() must NOT re-clamp a pre-existing DB file's
        # perms (single-user mode tightens ONLY paths it creates, so a shared DB is never
        # locked out from under the other service user). A loosened existing file is left as-is.
        if os.name == "posix":
            os.chmod(db, 0o644)  # loosen on purpose
            with S.connect():
                pass
            check("connect leaves a pre-existing DB file's perms untouched (no-downgrade)",
                  (os.stat(db).st_mode & 0o777) == 0o644)
        else:
            check("perms no-downgrade check skipped on non-posix (informational)", True)

        # seed a couple of chat rows so the restore drill can count + report a latest ts
        with S.connect() as conn:
            conn.execute(
                "INSERT INTO chat (source_notion_page_id, status, created_ts, updated_ts) "
                "VALUES ('local-chat-a','active',100.0,100.0)")
            conn.execute(
                "INSERT INTO chat (source_notion_page_id, status, created_ts, updated_ts) "
                "VALUES ('local-chat-b','active',200.0,200.0)")

        # a weird table name must be handled via safe identifier quoting in the restore drill
        with S.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS "weird ""tbl""" (id INTEGER)')
            conn.execute('INSERT INTO "weird ""tbl""" (id) VALUES (1)')

        # backup snapshot creation (VACUUM INTO) from the test DB
        snap = aios_backup.snapshot(db, dest_dir=str(Path(tmp) / "backups"))
        check("backup snapshot file created", Path(snap).exists())

        # backup of a MISSING source DB must fail (FileNotFoundError) and must NOT create it
        missing = str(Path(tmp) / "does_not_exist.sqlite3")
        miss_raised = False
        try:
            aios_backup.snapshot(missing, dest_dir=str(Path(tmp) / "b2"))
        except FileNotFoundError:
            miss_raised = True
        check("backup of missing DB raises FileNotFoundError", miss_raised)
        check("backup of missing DB did NOT create the source", not Path(missing).exists())

        # maybe_encrypt no-op: clear age env first so the test is deterministic
        os.environ.pop("AIOS_AGE_BIN", None)
        os.environ.pop("AIOS_BACKUP_AGE_RECIPIENT", None)
        same = aios_backup.maybe_encrypt(snap)
        check("maybe_encrypt is a no-op without age config", str(same) == str(snap) and Path(same).exists())

        # quote_ident unit test
        check("quote_ident doubles internal quotes and wraps",
              aios_restore_drill.quote_ident('weird"name') == '"weird""name"')

        # restore drill reads the snapshot read-only and reports counts/tables (+ weird table)
        rep = aios_restore_drill.inspect(str(snap))
        check("restore drill lists foundation tables", expected.issubset(set(rep["tables"].keys())))
        check("restore drill counts chat rows (2)", rep["tables"]["chat"]["rows"] == 2)
        check("restore drill reports a latest marker for chat",
              rep["tables"]["chat"]["latest"] == 200.0)
        check("restore drill handles a weird-quoted table name via quote_ident",
              rep["tables"].get('weird "tbl"', {}).get("rows") == 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
