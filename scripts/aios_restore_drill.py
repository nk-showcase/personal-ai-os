#!/usr/bin/env python3
"""scripts/aios_restore_drill.py — Stage 4 RESTORE-DRILL SKELETON.

Opens a backup snapshot READ-ONLY and reports a VALUE-FREE health summary:
  - table list,
  - per-table row counts,
  - latest date/timestamp where a date column exists.
NO writes. NO row contents printed (counts + max-date only). Stage 4 uses a TEST
DB only; no real data. The chat-driven restore drill (Stage 4/10) builds on this:
the bot pulls the latest off-box snapshot into a sandbox and reports these numbers
so the (non-developer) owner can confirm a backup actually restores — no terminal.

Usage:  python3 scripts/aios_restore_drill.py <snapshot.sqlite3>
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# table -> the column whose MAX() is a meaningful "latest" marker (date or epoch ts).
# These are dates/timestamps (value-free metadata), never user free-text.
_DATE_COL = {
    "chat": "created_ts",
    "chat_message": "created_ts",
    "memory_fact": "created_ts",
    "note": "created_ts",
    "unrouted_inbox": "ts",
    "audit_log": "ts",
    # integrations-worker RPC queue (created_ts is the canonical age marker).
    "request_queue": "created_ts",
}


def quote_ident(name: str) -> str:
    """Safely quote a SQLite identifier: double any internal double-quotes, then wrap
    the whole thing in double-quotes.  e.g.  weird"name  ->  "weird""name".
    Use for table/column names interpolated into generated SQL."""
    return '"' + str(name).replace('"', '""') + '"'


def inspect(path) -> dict:
    """Open the snapshot read-only and return {snapshot, tables:{name:{rows,latest}}}."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(str(p))
    uri = f"file:{p.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        out = {"snapshot": str(p), "tables": {}}
        for t in tables:
            rows = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(t)}").fetchone()[0]
            latest = None
            col = _DATE_COL.get(t)
            if col:
                try:
                    latest = conn.execute(
                        f"SELECT MAX({quote_ident(col)}) FROM {quote_ident(t)}"
                    ).fetchone()[0]
                except sqlite3.Error:
                    latest = None
            out["tables"][t] = {"rows": rows, "latest": latest}
        return out
    finally:
        conn.close()


def main(argv) -> int:
    if len(argv) < 2:
        print("usage: aios_restore_drill.py <snapshot.sqlite3>")
        return 2
    rep = inspect(argv[1])
    print(f"snapshot: {rep['snapshot']}")
    for t, info in rep["tables"].items():
        print(f"  {t}: rows={info['rows']} latest={info['latest']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
