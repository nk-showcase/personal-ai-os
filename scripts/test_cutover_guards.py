"""Cutover guards test (slice 1.4/1.5): combined store_mode flag, importer refusal when flipped,
deletion reconciliation + count parity, and connect() no-downgrade. Temp DB only.

    PYTHONPATH=. .venv/bin/python scripts/test_cutover_guards.py
"""
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.getcwd())
fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


os.environ.pop("AIOS_DATA_DB_GROUP", None)
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)
os.environ.update({"TELEGRAM_BOT_TOKEN": "dummy:test", "TELEGRAM_OWNER_ID": "1"})
TMP = tempfile.mkdtemp(prefix="cutguards-")
DBF = os.path.join(TMP, "aios.sqlite3")
os.environ["AIOS_DATA_DB"] = DBF

from bot import aios_config as CFG            # noqa: E402
from bot import aios_migrate as M             # noqa: E402
from bot import aios_storage as S             # noqa: E402

# 1. Combined flag: defaults, set, validation. ALL FIVE domains flipped 2026-07-02 —
# every code default is 'sqlite' (amendment 12/13).
check("store_mode defaults match flip state (all cutover domains = sqlite)",
      all(CFG.get_store_mode(d) == "sqlite" for d in CFG.CUTOVER_DOMAINS))
S.init_db()
CFG.set_store_mode("notes", "sqlite")
check("store_mode set+get round-trip", CFG.get_store_mode("notes") == "sqlite")
try:
    CFG.set_store_mode("notes", "dual")
    check("invalid value rejected", False)
except CFG.InvalidFlagValue:
    check("invalid value rejected", True)
try:
    CFG.get_store_mode("finance")
    check("unknown domain rejected", False)
except CFG.InvalidFlagValue:
    check("unknown domain rejected", True)

# 2. Importer refusal when flipped (checked BEFORE any source read)
try:
    M.refuse_if_flipped("notes")
    check("refuse_if_flipped raises on a flipped domain", False)
except RuntimeError:
    check("refuse_if_flipped raises on a flipped domain", True)
CFG.set_store_mode("notes", "notion")
M.refuse_if_flipped("notes")  # must not raise
check("refuse_if_flipped passes on 'notion'", True)

# 3. Reconciliation: tombstones local-not-in-source, count parity
_local = ["a", "b", "c"]
_archived = []
info = M.reconcile_deletions(
    "notes", source_ids=["a", "b"],
    list_local_ids=lambda db=None: list(_local),
    archive_local=lambda sid, db=None: _archived.append(sid),
)
check("reconcile tombstones the missing id", _archived == ["c"] and info["tombstoned"] == 1)
check("count parity True after tombstone", info["count_parity"] is True)
info2 = M.reconcile_deletions(
    "notes", source_ids=["a", "b", "x"],
    list_local_ids=lambda db=None: ["a", "b"],
    archive_local=lambda sid, db=None: None,
)
check("parity False when source has rows local lacks (seed incomplete)", info2["count_parity"] is False)

# 4. connect() no-downgrade: pre-existing group-shared file must KEEP its perms in single-user mode
os.chmod(DBF, 0o660)
with S.connect() as conn:
    conn.execute("SELECT 1")
mode = stat.S_IMODE(os.stat(DBF).st_mode)
check("pre-existing 0660 DB NOT clamped to 0600 without group env", mode == 0o660)
# fresh create still tightens
DBF2 = os.path.join(TMP, "fresh", "new.sqlite3")
with S.connect(db=DBF2) as conn:
    conn.execute("SELECT 1")
check("fresh-created DB tightened to 0600", stat.S_IMODE(os.stat(DBF2).st_mode) == 0o600)

print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
