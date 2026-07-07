"""Test S-3.5 conv_state.mutate + per-chat write-lock (timers-as-writers).

Cases 1-7 (encryption OFF): mac bare python3 (3.9.6), stdlib + threads (real cross-connection
contention on the SQLite BEGIN IMMEDIATE write-lock). Cases 8-10 (encryption ON) need the
`cryptography` lib (VPS venv) + a GATED test KEK (no init_kek / age / Bitwarden); auto-skipped
where cryptography is absent.
Run (mac, 1-7):   PYTHONPATH=. python3 scripts/test_conv_state_mutate.py
Run (VPS venv):   PYTHONPATH=. .venv/bin/python scripts/test_conv_state_mutate.py
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.getcwd())
_d = tempfile.mkdtemp(prefix="aios_mutate_")
_DB = os.path.join(_d, "q.sqlite3")
os.environ["AIOS_TASK_QUEUE_DB"] = _DB
# Case #9 (encryption ON) triggers context_cipher's fail-closed quarantine write, which lands in the
# AIOS_DATA_DB store (NOT conv_state's AIOS_TASK_QUEUE_DB). Redirect it to the throwaway dir too, else a
# decrypt_quarantine row leaks into the owner's REAL ~/.ai-os/data/aios.sqlite3 on any crypto-present run.
os.environ["AIOS_DATA_DB"] = os.path.join(_d, "data.sqlite3")
os.environ.pop("AIOS_CONTEXT_ENCRYPTION", None)        # OFF for cases 1-7
os.environ.pop("AIOS_CONTEXT_ALLOW_TEST_KEK", None)
os.environ.pop("AIOS_CONTEXT_TEST_KEK", None)

from bot import conv_state as cs  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


def raises(fn, exc):
    try:
        fn()
        return None
    except exc as e:  # noqa: BLE001
        return e


def _count(chat_id):
    with sqlite3.connect(_DB) as c:
        return c.execute("SELECT COUNT(*) FROM conv_state WHERE chat_id=?", (chat_id,)).fetchone()[0]


def _updated_ts(chat_id):
    with sqlite3.connect(_DB) as c:
        r = c.execute("SELECT updated_ts FROM conv_state WHERE chat_id=?", (chat_id,)).fetchone()
        return r[0] if r else None


# --- 1. NO LOST UPDATE — proves the lock (two threads increment the SAME chat concurrently) ---
def _inc_slow(s):
    s["n"] = s.get("n", 0) + 1
    time.sleep(0.03)            # widen the clobber window (test-only; mutators are tight in prod)


b1 = threading.Barrier(2)


def _w_mutate():
    b1.wait()
    cs.mutate(1, _inc_slow)


t1 = threading.Thread(target=_w_mutate)
t2 = threading.Thread(target=_w_mutate)
t1.start(); t2.start(); t1.join(); t2.join()
check("#1 two concurrent mutate(C) -> NO lost update (n==2): the write-lock serializes the RMW",
      cs.load_state(1).get("n") == 2)

# sanity: raw load/save RMW under the same contention LOSES one (proves the test detects a missing lock)
b1s = threading.Barrier(2)


def _w_raw():
    b1s.wait()
    s = cs.load_state(7)
    s["n"] = s.get("n", 0) + 1
    time.sleep(0.03)
    cs.save_state(7, s)


u1 = threading.Thread(target=_w_raw)
u2 = threading.Thread(target=_w_raw)
u1.start(); u2.start(); u1.join(); u2.join()
check("#1 sanity: raw load/save RMW LOSES an update (n==1) -> the test CAN detect a missing lock",
      cs.load_state(7).get("n") == 1)

# --- 2. DIFFERENT chats don't block (tight mutators both land) ---
b2 = threading.Barrier(2)


def _m_chat(cid):
    b2.wait()
    cs.mutate(cid, lambda s: s.__setitem__("n", s.get("n", 0) + 1))


ta = threading.Thread(target=_m_chat, args=(20,))
tb = threading.Thread(target=_m_chat, args=(21,))
ta.start(); tb.start(); ta.join(); tb.join()
check("#2 different chats both land, no deadlock/ConvLockBusyError",
      cs.load_state(20).get("n") == 1 and cs.load_state(21).get("n") == 1)

# --- 3. raising mutator: ROLLBACK (no partial write) + lock released ---
def _boom(s):
    s["x"] = 1
    raise RuntimeError("boom in mutator")


e3 = raises(lambda: cs.mutate(30, _boom), RuntimeError)
check("#3 raising mutator propagates; partial change NOT persisted",
      isinstance(e3, RuntimeError) and "x" not in cs.load_state(30))
cs.mutate(30, lambda s: s.__setitem__("y", 2))
check("#3 lock was released: a subsequent mutate(C) succeeds (y==2)", cs.load_state(30).get("y") == 2)

# --- 4. wrote flag honest + force ---
w_a = cs.mutate(40, lambda s: s.__setitem__("k", 1))   # real change -> True
w_b = cs.mutate(40, lambda s: None)                    # no-op -> False (byte-identical)
ts_before = _updated_ts(40)
time.sleep(0.01)
w_c = cs.mutate(40, lambda s: None, force=True)        # force -> True (row re-written)
ts_after = _updated_ts(40)
check("#4 wrote flag: real-change=True, no-op=False, force=True",
      w_a is True and w_b is False and w_c is True)
check("#4 force=True re-wrote the row (updated_ts advanced)",
      ts_after is not None and ts_before is not None and ts_after > ts_before)

# --- 5. in-place (return None) AND return-new both persist ---
cs.mutate(50, lambda s: s.__setitem__("a", 1))         # in-place
cs.mutate(51, lambda s: {**s, "z": 9})                 # return new dict
check("#5 in-place persists (a==1) AND return-new persists (z==9)",
      cs.load_state(50).get("a") == 1 and cs.load_state(51).get("z") == 9)

# --- 6. reentrancy fails FAST (not a 15s ConvLockBusyError) ---
def _nested(s):
    cs.mutate(60, lambda t: t.__setitem__("inner", 1))   # nested mutate on the SAME chat


_t0 = time.time()
e6 = raises(lambda: cs.mutate(60, _nested), cs.ReentrantMutateError)
check("#6 nested mutate on same chat raises ReentrantMutateError PROMPTLY (<1s, not a 15s busy)",
      isinstance(e6, cs.ReentrantMutateError) and (time.time() - _t0) < 1.0)
check("#6 outer did not partially commit (no row written)", _count(60) == 0)

# --- 7. BUSY: a holder past busy_timeout -> mutate RETRIES within budget, does not surface OperationalError ---
parked = threading.Event()
release = threading.Event()


def _park():
    with cs._conn() as c:
        c.execute("BEGIN IMMEDIATE")               # grab the db writer slot
        parked.set()
        release.wait(7.0)                          # hold it ~5.5s (past the 5s busy_timeout)
        c.execute("ROLLBACK")


pt = threading.Thread(target=_park)
pt.start()
parked.wait()
res = {}


def _b():
    res["wrote"] = cs.mutate(71, lambda s: s.__setitem__("n", 1))


bt = threading.Thread(target=_b)
bt.start()
time.sleep(5.5)                                    # let B burn through one full busy_timeout, then release A
release.set()
pt.join(); bt.join()
check("#7 mutate RETRIES through a >busy_timeout hold and succeeds (n==1), no OperationalError surfaced",
      res.get("wrote") is True and cs.load_state(71).get("n") == 1)

# --- 8-10. encryption-ON cases (need cryptography) ---
try:
    import cryptography  # noqa: F401
    _HAS_CRYPTO = True
except Exception:  # noqa: BLE001
    _HAS_CRYPTO = False

if not _HAS_CRYPTO:
    print("NOTE: cryptography absent (mac bare python3) -> skipping encryption-ON cases #8-#10")
else:
    from bot.context_key import KekUnavailable          # noqa: E402
    from bot.context_cipher import DecryptQuarantine     # noqa: E402

    os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"
    os.environ["AIOS_CONTEXT_ALLOW_TEST_KEK"] = "1"
    os.environ["AIOS_CONTEXT_TEST_KEK"] = os.urandom(32).hex()

    # 8. ON happy-path round-trip
    cs.mutate(80, lambda s: s.__setitem__("k", 1))
    with sqlite3.connect(_DB) as c:
        r = c.execute("SELECT blob, blob_encrypted FROM conv_state WHERE chat_id=80").fetchone()
    check("#8 ON round-trip: k==1; row has blob_encrypted set + blob NULL",
          cs.load_state(80).get("k") == 1 and r[0] is None and r[1] is not None)

    # 9. ON quarantined row -> mutate raises DecryptQuarantine + lock released (different chat then works)
    cs.mutate(82, lambda s: s.__setitem__("k", 1))
    with sqlite3.connect(_DB) as c:
        enc = c.execute("SELECT blob_encrypted FROM conv_state WHERE chat_id=82").fetchone()[0]
        corrupted = bytes([enc[0] ^ 0xFF]) + enc[1:]    # flip first ciphertext byte
        c.execute("UPDATE conv_state SET blob_encrypted=? WHERE chat_id=82", (corrupted,))
        c.commit()
    e9 = raises(lambda: cs.mutate(82, lambda s: s.__setitem__("z", 9)), DecryptQuarantine)
    cs.mutate(83, lambda s: s.__setitem__("ok", 1))     # different chat: lock MUST be free
    check("#9 ON corrupted row -> mutate raises DecryptQuarantine inside tx; lock released (other chat ok)",
          isinstance(e9, DecryptQuarantine) and cs.load_state(83).get("ok") == 1)

    # 10. ON + no KEK -> raises KekUnavailable, ZERO rows written (no false False-as-success)
    os.environ.pop("AIOS_CONTEXT_ALLOW_TEST_KEK", None)
    os.environ.pop("AIOS_CONTEXT_TEST_KEK", None)        # ON but no KEK
    e10 = raises(lambda: cs.mutate(81, lambda s: s.__setitem__("k", 1)), KekUnavailable)
    check("#10 ON+no KEK -> raises KekUnavailable AND zero rows written (not False-as-success)",
          isinstance(e10, KekUnavailable) and _count(81) == 0)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
