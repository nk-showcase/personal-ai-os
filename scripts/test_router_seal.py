"""Test the router INBOUND seal (asymmetric age): ON seals task_text, the worker round-trips it
via open_inbound, reply redaction+cap, the documented reply-at-rest boundary, fail-closed on a
missing/!0600 identity, and OFF byte-identity to the plaintext path.

Offline, with no live server / Telegram / token: an EPHEMERAL age keypair
(~/.local/bin/age{,-keygen} or on PATH), a throwaway temp DB, and a fresh worker recipient.
Never the real worker or recovery key. Uses the sealed enqueue_task path +
claude_worker_core.process_one, so it needs neither `config`/python-dotenv nor telegram and runs
on a bare python3 with `age` available. The OFF byte-identity is also proven independently by
scripts/test_router_worker.py; this file re-proves OFF parity locally with its own envelope.

Run:  PYTHONPATH=. python3 scripts/test_router_seal.py
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.getcwd())

_d = tempfile.mkdtemp(prefix="aios_router_seal_")
os.environ["AIOS_TASK_QUEUE_DB"] = os.path.join(_d, "q.sqlite3")   # isolate queues (tasks + replies)

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

# --- ephemeral worker keypair (per verified age-keygen v1.3.1: -o writes a fresh identity,
#     -y derives the public recipient value-free). The PRIVATE half is never read/printed. ---
_kd = tempfile.mkdtemp(prefix="aios_age_")
idf = os.path.join(_kd, "worker.key")
subprocess.run([keygen, "-o", idf], capture_output=True, check=True)
os.chmod(idf, 0o600)
pub = subprocess.run([keygen, "-y", idf], capture_output=True, text=True, check=True).stdout.strip()
os.environ["AIOS_ROUTER_IDENTITY_FILE"] = idf

from bot import task_queue, reply_queue, router_worker, claude_worker_core  # noqa: E402
import bot.router_seal as router_seal  # noqa: E402

router_seal.ROUTER_WORKER_RECIPIENT = pub   # override the placeholder for the real code path

# Envelopes are defined LOCALLY here — NOT imported from test_router_worker.py.
ENV_SPECIAL = {"kind": "text", "chat_id": 7, "message_id": 1, "text": "café ☕"}  # non-ASCII round-trip


def _run_worker_once():
    """One worker iteration via the core (no claude_worker import → no telegram/dotenv on Mac).
    alias='router' rows are claimable only through claim_next_route_task (the generic FIFO claim
    excludes them), so the route claim is passed explicitly."""
    return asyncio.run(claude_worker_core.process_one(
        "test-seal-worker", router_worker.build_route_runner(),
        claim_fn=task_queue.claim_next_route_task))


def _seal_enqueue(env, chat_id):
    """Simulate the transport ON path (spec §4): seal -> base64 -> enqueue mode='inbound-enc'."""
    payload = json.dumps(env, ensure_ascii=False)
    blob = router_seal.seal_inbound(payload)
    return task_queue.enqueue_task(chat_id=chat_id, alias="router", mode="inbound-enc",
                                   task_text=blob, source="telegram")


def _replies_for(chat_id):
    return [r for r in reply_queue.claim_undelivered() if r["chat_id"] == chat_id]


# --- 6. Direct seal/open round-trip (asymmetric: pub seals, identity opens) ---
payload6 = json.dumps(ENV_SPECIAL, ensure_ascii=False)
blob6 = router_seal.seal_inbound(payload6)
check("seal output is base64 ascii with NO cleartext substring",
      "café" not in blob6 and "☕" not in blob6 and blob6 == blob6.encode("ascii").decode("ascii"))
check("open_inbound(seal_inbound(x)) == x byte-exact (non-ASCII + emoji)",
      router_seal.open_inbound(blob6) == payload6)

# --- 1. ON — transport seals: persisted task_text is ciphertext, mode='inbound-enc' ---
tid1 = _seal_enqueue(ENV_SPECIAL, chat_id=7)
row1 = task_queue.get_task(tid1)
check("ON: persisted task_text has NO cleartext substring (sealed)",
      bool(row1) and "café" not in row1["task_text"] and "☕" not in row1["task_text"])
check("ON: row mode == 'inbound-enc'", row1 and row1["mode"] == "inbound-enc")
check("ON: task_text is not the plain JSON envelope",
      row1 and row1["task_text"] != json.dumps(ENV_SPECIAL, ensure_ascii=False))

# --- 2. ON — worker round-trip: open_inbound recovers UTF-8 through non-ASCII + emoji ---
res2 = _run_worker_once()
check("ON: worker processed the sealed row (status done)", res2.get("status") == "done")
check("ON: task flipped to done", task_queue.get_task(tid1)["status"] == "done")
und2 = _replies_for(7)
check("ON: echo reply == 'routed: ' + text byte-exact (decrypt recovered cleanly)",
      len(und2) == 1 and und2[0]["text"] == "routed: " + ENV_SPECIAL["text"])

# --- 3. ON — reply redacted + capped ---
SECRET_TOKEN = "sk-ant-" + "A" * 40                       # matches _SECRET_PATTERNS
ENV_SECRET = {"kind": "text", "chat_id": 11, "message_id": 1,
              "text": SECRET_TOKEN + " " + "a" * 5000}     # >4096 after echo prefix
_seal_enqueue(ENV_SECRET, chat_id=11)
_run_worker_once()
und3 = _replies_for(11)
rtext3 = und3[0]["text"] if und3 else ""
check("ON: secret-shaped token scrubbed to <redacted>", "<redacted>" in rtext3)
check("ON: raw secret token absent from reply", SECRET_TOKEN not in rtext3 and "sk-ant-" not in rtext3)
check("ON: reply capped to <= 4096 chars", len(rtext3) <= 4096)

# --- 3b. ON — documented boundary: reply content is plaintext at rest ---
# The inbound seal covers only task_text; reply content is stored plaintext at rest (redaction
# + cap only). Reply-at-rest encryption is a separate mechanism (docs/security/threat-model.md).
PROSE = "sample note 12345"
ENV_PROSE = {"kind": "text", "chat_id": 12, "message_id": 1, "text": PROSE}
_seal_enqueue(ENV_PROSE, chat_id=12)
_run_worker_once()
und3b = _replies_for(12)
check("ON: reply content returns to replies.text as PLAINTEXT (reply-at-rest NOT encrypted)",
      len(und3b) == 1 and PROSE in und3b[0]["text"])

# --- 5. OFF — byte-identical to S-1 (plain JSON in task_text, mode='inbound') ---
ENV_OFF = {"kind": "text", "chat_id": 8, "message_id": 2, "text": "plain text ☕"}
tid5 = task_queue.enqueue_task(chat_id=8, alias="router", mode="inbound",
                               task_text=json.dumps(ENV_OFF, ensure_ascii=False), source="telegram")
row5 = task_queue.get_task(tid5)
check("OFF: task_text is plain JSON parsing back to the envelope",
      json.loads(row5["task_text"]) == ENV_OFF and row5["mode"] == "inbound")
_run_worker_once()
und5 = _replies_for(8)
check("OFF: worker round-trip identical to S-1 (echo reply)",
      len(und5) == 1 and und5[0]["text"] == "routed: " + ENV_OFF["text"])

# --- 7. !0600 identity = typed RouterSealError, not raw KekUnavailable ---
from bot.context_key import KekUnavailable  # noqa: E402
blob7 = router_seal.seal_inbound(json.dumps(ENV_SPECIAL, ensure_ascii=False))
os.chmod(idf, 0o644)
err7 = None
try:
    router_seal.open_inbound(blob7)
except router_seal.RouterSealError as e:
    err7 = ("RouterSealError", str(e))
except KekUnavailable as e:
    err7 = ("KekUnavailable", str(e))
os.chmod(idf, 0o600)   # restore for any later use
check("!0600 identity raises RouterSealError (typed contract, NOT raw KekUnavailable)",
      err7 is not None and err7[0] == "RouterSealError")
check("!0600 error message is secret-free (no key material / no path value leaked literally)",
      err7 is not None and "AGE-SECRET-KEY" not in err7[1])

# --- 4. ON + missing identity = fail-closed (task failed, no reply, typed error) ---
tid4 = _seal_enqueue(ENV_SPECIAL, chat_id=9)   # sealing needs only the pub recipient
before4 = len(_replies_for(9))
_saved_idf = os.environ.pop("AIOS_ROUTER_IDENTITY_FILE", None)
res4 = _run_worker_once()
if _saved_idf is not None:
    os.environ["AIOS_ROUTER_IDENTITY_FILE"] = _saved_idf   # restore
check("ON+no identity: task failed (fail-closed)",
      res4.get("status") == "failed" and task_queue.get_task(tid4)["status"] == "failed")
err4 = task_queue.get_task(tid4)["error"] or ""
check("ON+no identity: error originates as RouterSealError (typed) and is secret-free",
      err4.startswith("RouterSealError:") and "AGE-SECRET-KEY" not in err4)
check("ON+no identity: no reply row was written (no plaintext/ciphertext echo)",
      len(_replies_for(9)) == before4)

print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print("RESULT: %d FAIL" % fails)
    sys.exit(1)
