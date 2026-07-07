"""bot/queue_perms.py — group-aware permissions for the shared task-queue SQLite DBs.

Mirrors aios_storage's `_shared_db_gid`/`connect` idiom so the queue DB (tasks.sqlite3, used by
task_queue / reply_queue / schedule_queue / conv_state) can be shared READ-WRITE between the
telegram-bot user and the worker user via a common group — instead of the single-user 0700/0600
default that re-clamped the shared dir back to owner-only on EVERY connect (the two-user-split
killer, see docs/security/two-user-split-plan.md).

When AIOS_TASK_QUEUE_GROUP (fallback AIOS_DATA_DB_GROUP) is set:
  - dir  -> 0o2770 (setgid: new files inherit the group) + chown group
  - file + its -wal/-shm siblings -> 0o660 + chown group, on EVERY connect
When unset -> unchanged 0o700 / 0o600 single-user default.

All chmod/chown are BEST-EFFORT: a non-owner cannot chmod/chown a pre-existing file (EPERM). The
one-time provisioning migration (owner/root) fixes existing files' group once; the setgid dir +
UMask=0007 on every writer unit make NEW files land group-shared. POSIX-only; any failure is ignored.
"""
from __future__ import annotations

import os
from pathlib import Path


def queue_gid() -> int | None:
    """GID of AIOS_TASK_QUEUE_GROUP, or None (single-user mode).

    ONLY the explicit AIOS_TASK_QUEUE_GROUP activates group-sharing on the QUEUE DB — no fallback
    to AIOS_DATA_DB_GROUP, so this stays fully INERT (unchanged 0700/0600) on the current bot and
    only turns on when the two-user-split unit sets AIOS_TASK_QUEUE_GROUP explicitly."""
    name = (os.getenv("AIOS_TASK_QUEUE_GROUP") or "").strip()
    if not name:
        return None
    try:
        import grp
        return grp.getgrnam(name).gr_gid
    except (KeyError, ImportError, OSError):
        return None


def apply_dir_perms(dir_path: Path) -> int | None:
    """Set the queue DIR: 0o2770+setgid+group when shared, else 0o700. Returns the gid (or None)."""
    gid = queue_gid()
    mode = 0o2770 if gid is not None else 0o700
    try:
        os.chmod(dir_path, mode)
        if gid is not None:
            os.chown(dir_path, -1, gid)
    except OSError:
        pass
    return gid


def apply_file_perms(db_path: Path, gid: int | None) -> None:
    """Set the DB file + its -wal/-shm siblings to 0o660+group (shared) or 0o600. Call on EVERY
    connect so a file created by the OTHER user gets re-grouped. Best-effort."""
    mode = 0o660 if gid is not None else 0o600
    for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if p.exists():
            try:
                os.chmod(p, mode)
                if gid is not None:
                    os.chown(p, -1, gid)
            except OSError:
                pass
