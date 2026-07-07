#!/usr/bin/env python3
"""scripts/aios_backup.py — Stage 4 BACKUP SKELETON (no upload, no key generation).

Creates a clean, live-safe single-file snapshot of the AI OS SQLite DB via
`VACUUM INTO` (SQLite checkpoints + compacts into a consistent copy — safe while
the DB is in use, unlike a raw file copy that can miss/break the -wal).

Snapshots go to a backup dir OUTSIDE the repo (default ~/.ai-os/backups/),
chmod 0700 dir / 0600 file where supported.

Optional age-encryption is a CONFIGURABLE PLACEHOLDER:
    AIOS_AGE_BIN              path to the `age` binary
    AIOS_BACKUP_AGE_RECIPIENT age public recipient (NON-secret)
If both are set, the snapshot is wrapped with `age -r <recipient>`; otherwise the
snapshot stays plaintext locally. This script does NOT generate keys and does NOT
upload anywhere — Stage 9 wires real encrypted off-box backups (age + Litestream).

Usage:  python3 scripts/aios_backup.py [<db_path>] [<dest_dir>]
        (defaults: db = aios_storage.db_path(), dest = ~/.ai-os/backups/)
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def backup_dir() -> Path:
    raw = os.getenv("AIOS_BACKUP_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".ai-os" / "backups"


def snapshot(db_path, dest_dir=None) -> Path:
    """VACUUM INTO a timestamped snapshot. Returns the snapshot path.

    Refuses to run if the source DB does not exist: an EXPLICIT exists-check BEFORE
    connecting, so a missing path can never be auto-created as an empty DB. (We do not
    open the source read-only, because SQLite rejects VACUUM INTO on a mode=ro
    connection with "attempt to write a readonly database" — a known limitation; the
    exists-guard makes the normal read-write open safe instead.) The destination is a
    properly-escaped SQLite string literal (single quotes doubled) — injection-safe.
    """
    src = Path(db_path).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"source DB not found (refusing to create it): {src}")
    d = Path(dest_dir).expanduser() if dest_dir else backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = d / f"aios-snapshot-{ts}.sqlite3"
    safe = str(dest).replace("'", "''")
    conn = sqlite3.connect(str(src))
    try:
        conn.execute(f"VACUUM INTO '{safe}'")
    finally:
        conn.close()
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
    return dest


def maybe_encrypt(path) -> Path:
    """Placeholder age hook. Wrap the snapshot with age ONLY if both env vars are set;
    otherwise return the input unchanged (plaintext, Stage 4). Never generates keys."""
    age_bin = os.getenv("AIOS_AGE_BIN")
    recipient = os.getenv("AIOS_BACKUP_AGE_RECIPIENT")
    if not (age_bin and recipient):
        return Path(path)
    out = Path(str(path) + ".age")
    subprocess.run([age_bin, "-r", recipient, "-o", str(out), str(path)], check=True)
    try:
        os.remove(path)
    except OSError:
        pass
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    return out


def main(argv) -> int:
    # Resolve default db path from the foundation module without importing at top
    # (keeps this script usable standalone in tests that import only `snapshot`).
    db = argv[1] if len(argv) > 1 else None
    if db is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from bot import aios_storage
        db = str(aios_storage.db_path())
    dest = argv[2] if len(argv) > 2 else None
    snap = snapshot(db, dest)
    final = maybe_encrypt(snap)
    print(f"snapshot: {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
