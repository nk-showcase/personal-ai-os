"""Test the STORE inbound seal (asymmetric age) for integrations_proxy + the worker dual-read.

Proves, all OFFLINE on a developer machine's bare python3 with an EPHEMERAL age keypair (never
the real worker/context/recovery key):
  1. Flag ON  — store_request builds {"op_b64_sealed": …} (NOT {"op_b64": …}), the operator's
     cleartext (non-ASCII + emoji) is NOT recoverable from the payload by base64, and
     the worker dual-read unseals+decodes it back to the EXACT {domain,op,args}.
  2. Legacy   — a {"op_b64": …} row still decodes via the base64 branch (transition safety).
  3. Flag OFF — store_request produces EXACTLY {"op_b64": …}, byte-identical to hand-built.
  4. Fail-closed — placeholder recipient + flag ON -> seal raises -> store_request returns
     {"ok": False, "unavailable": True}, nothing enqueued.

Run:  PYTHONPATH=. python3 scripts/test_store_seal.py
"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.getcwd())

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


def _resolve(name):
    p = os.path.expanduser("~/.local/bin/" + name)
    if os.path.exists(p):
        return p
    return shutil.which(name)


age = _resolve("age")
keygen = _resolve("age-keygen")
if not age or not keygen:
    print("FAIL: age/age-keygen not found (need ~/.local/bin/age{,-keygen} or on PATH)")
    sys.exit(1)
os.environ["AIOS_AGE_BIN"] = age   # context_key._run_age shells to the SAME binary

# --- ephemeral worker keypair: -o writes a fresh identity, -y derives the public recipient
#     value-free. The PRIVATE half is never read/printed. The worker opens with THIS identity
#     via env AIOS_CONTEXT_IDENTITY_FILE (open_store_inbound reads the CONTEXT identity). ---
_kd = tempfile.mkdtemp(prefix="aios_store_age_")
idf = os.path.join(_kd, "worker.key")
subprocess.run([keygen, "-o", idf], capture_output=True, check=True)
os.chmod(idf, 0o600)
pub = subprocess.run([keygen, "-y", idf], capture_output=True, text=True, check=True).stdout.strip()
os.environ["AIOS_CONTEXT_IDENTITY_FILE"] = idf

import bot.store_seal as store_seal            # noqa: E402
from bot import integrations_proxy             # noqa: E402
from bot.integrations_proxy import _b64, _unb64  # noqa: E402

store_seal.STORE_SEAL_RECIPIENT = pub          # override placeholder for the real code path

# The store applier lives in aios_store_applier and calls _require_keyed() + the real store
# handlers. We exercise ONLY the dual-read prologue in isolation (the exact lines added to
# store_op_applier / store_view_applier), so no DB / keyed store is needed locally.
def _worker_dual_read(payload):
    """Byte-for-byte copy of the applier dual-read prologue (op_b64_sealed -> open, else b64)."""
    if "op_b64_sealed" in payload:
        return _unb64(store_seal.open_store_inbound(payload["op_b64_sealed"]))
    elif "op_b64" in payload:
        return _unb64(payload["op_b64"])       # == integrations_proxy.decode_op(payload)
    else:
        return payload


# A fake config module so store_request reads OUR flag without pulling python-dotenv / the bot
# token at import (config resolves the token at import; absent on bare python3). We inject it into
# sys.modules under 'bot.config' so the lazy `from . import config` inside _build_store_payload
# picks it up.
_fake_config = types.ModuleType("bot.config")
_fake_config._STORE_SEAL = False
_fake_config.store_seal_enabled = lambda: _fake_config._STORE_SEAL
sys.modules["bot.config"] = _fake_config


def _set_flag(on: bool):
    _fake_config._STORE_SEAL = on


# A stub queue round-trip so store_request runs without a real DB: it captures the enqueued
# payload and reports a saved write, letting us assert exactly what would hit the queue.
_ENQUEUED = []
_Outcome = types.SimpleNamespace


def _fake_round_trip(kind, payload, *, is_write, business_key=None):
    from bot.aios_worker_contract import PollResultState as S
    _ENQUEUED.append((kind, payload))
    # Echo a decoded result so store_request returns {"ok": True} on the happy path.
    return _Outcome(state=S.WRITE_SAVED if is_write else S.READ_RESULT,
                    result=integrations_proxy.encode_result({"ok": True}))


integrations_proxy._blocking_round_trip = _fake_round_trip

DOMAIN, OP = "notes", "entry_save"
ARGS = {"chat_id": 7, "note": "un journal privé ☕ confidential entry"}
SECRET_SUB = "confidential entry"   # operator cleartext that MUST NOT be base64-recoverable
EMOJI_SUB = "☕"


def _payload_bytes_contains_cleartext(payload):
    """True if the personal substring is recoverable from ANY payload value by base64-decode
    (or is present verbatim). Proves the sealed payload is NOT reversible like plain op_b64."""
    for v in payload.values():
        if SECRET_SUB in str(v) or EMOJI_SUB in str(v):
            return True
        try:
            dec = base64.b64decode(str(v).encode("ascii"), validate=False).decode("utf-8", "replace")
            if SECRET_SUB in dec or EMOJI_SUB in dec:
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


# ---- 1. Flag ON: sealed payload, no cleartext, worker dual-read recovers the exact op ----
_set_flag(True)
_ENQUEUED.clear()
res_on = asyncio.run(integrations_proxy.store_request(DOMAIN, OP, ARGS, is_write=True,
                                                      business_key="k-on"))
check("ON: exactly one row enqueued", len(_ENQUEUED) == 1)
kind_on, payload_on = _ENQUEUED[0] if _ENQUEUED else (None, {})
check("ON: payload has op_b64_sealed and NOT op_b64",
      "op_b64_sealed" in payload_on and "op_b64" not in payload_on)
check("ON: operator cleartext (non-ASCII + emoji) NOT recoverable from payload by base64",
      not _payload_bytes_contains_cleartext(payload_on))
check("ON: store_request returned the worker result ({'ok': True})", res_on == {"ok": True})
recovered = _worker_dual_read(payload_on)
check("ON: worker dual-read unseals + decodes back to the EXACT {domain,op,args}",
      recovered == {"domain": DOMAIN, "op": OP, "args": ARGS})

# also prove the op_b64_sealed key passes the queue secret-scan (same as op_b64)
from bot.aios_request_queue import _SECRET_KEY_RE  # noqa: E402
check("ON: 'op_b64_sealed' passes the queue _SECRET_KEY_RE scan (no secret-shaped substring)",
      _SECRET_KEY_RE.search("op_b64_sealed") is None)

# ---- 2. Legacy fallback: a {"op_b64": …} row still decodes via the base64 branch ----
legacy_payload = {"op_b64": _b64({"domain": DOMAIN, "op": OP, "args": ARGS})}
check("Legacy: worker dual-read decodes a plain op_b64 row (transition safety)",
      _worker_dual_read(legacy_payload) == {"domain": DOMAIN, "op": OP, "args": ARGS})

# ---- 3. Flag OFF: byte-identical to a hand-built {"op_b64": …} ----
_set_flag(False)
_ENQUEUED.clear()
res_off = asyncio.run(integrations_proxy.store_request(DOMAIN, OP, ARGS, is_write=True,
                                                       business_key="k-off"))
hand_built = {"op_b64": _b64({"domain": DOMAIN, "op": OP, "args": ARGS})}
kind_off, payload_off = _ENQUEUED[0] if _ENQUEUED else (None, {})
check("OFF: enqueued payload is EXACTLY {'op_b64': …}, byte-identical to hand-built",
      payload_off == hand_built)
check("OFF: no op_b64_sealed key present (OFF changes nothing)",
      "op_b64_sealed" not in payload_off)

# ---- 4. Fail-closed: placeholder recipient + flag ON -> unavailable, nothing enqueued ----
_set_flag(True)
_saved_rcpt = store_seal.STORE_SEAL_RECIPIENT
store_seal.STORE_SEAL_RECIPIENT = "age1PLACEHOLDER0000000000000000000000000000000000000000000000000"
_ENQUEUED.clear()
res_fc = asyncio.run(integrations_proxy.store_request(DOMAIN, OP, ARGS, is_write=True,
                                                      business_key="k-fc"))
store_seal.STORE_SEAL_RECIPIENT = _saved_rcpt   # restore
check("Fail-closed: placeholder recipient -> {'ok': False, 'unavailable': True}",
      res_fc == {"ok": False, "unavailable": True})
check("Fail-closed: NOTHING enqueued (no plaintext fallback)", len(_ENQUEUED) == 0)

# also assert seal itself raises on the placeholder (the fail-closed root cause)
store_seal.STORE_SEAL_RECIPIENT = "age1PLACEHOLDER0000000000000000000000000000000000000000000000000"
raised = False
try:
    store_seal.seal_store_inbound(_b64({"domain": DOMAIN, "op": OP, "args": ARGS}))
except store_seal.RouterSealError:
    raised = True
finally:
    store_seal.STORE_SEAL_RECIPIENT = _saved_rcpt   # restore for any later use
check("Fail-closed: seal_store_inbound RAISES RouterSealError on a placeholder recipient",
      raised)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
