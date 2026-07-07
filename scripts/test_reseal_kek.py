"""reseal_kek test (cutover amendment 1). Throwaway keys + temp dir only.

Proves: reseal keeps the SAME KEK (fingerprint unchanged), adds/removes recipients,
refuses to drop the recovery recipient, refuses on pin mismatch, refuses (and leaves
the old envelope intact) when a held identity cannot decrypt the new blob, and always
leaves a timestamped backup. Run:
    PYTHONPATH=. .venv/bin/python scripts/test_reseal_kek.py
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.getcwd())
fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


TMP = tempfile.mkdtemp(prefix="reseal-")
AGE = os.environ.get("AIOS_AGE_BIN") or shutil.which("age") or os.path.expanduser("~/.local/bin/age")
KG = os.path.join(os.path.dirname(AGE), "age-keygen")
os.environ["AIOS_AGE_BIN"] = AGE


def keypair(tag):
    idf = os.path.join(TMP, f"{tag}.txt")
    subprocess.run([KG, "-o", idf], check=True, capture_output=True)
    pub = subprocess.run([KG, "-y", idf], check=True, capture_output=True, text=True).stdout.strip()
    os.chmod(idf, 0o600)
    return idf, pub


A_id, A_pub = keypair("vps")        # current VPS identity (ai-os)
B_id, B_pub = keypair("worker")     # new keyed-worker identity (ai-os-integrations)
R_id, R_pub = keypair("recovery")   # recovery (held here only for the test)
C_id, C_pub = keypair("outsider")   # never a recipient

from bot import context_key as CK  # noqa: E402

CK.RECOVERY_RECIPIENT = R_pub  # test stand-ins for the git-pinned constants
CK.KEK_SHA256_PIN = ""
KEKF = os.path.join(TMP, "kek.age")
CK.init_kek(vps_recipient=A_pub, vps_identity_path=A_id, kek_path=KEKF)
KEK = subprocess.run([AGE, "--decrypt", "-i", A_id, KEKF], check=True,
                     capture_output=True).stdout
CK.KEK_SHA256_PIN = hashlib.sha256(KEK).hexdigest()

# refusal: init_kek path is not reseal — first check reseal without kek.age refuses
try:
    CK.reseal_kek(recipients=[B_pub, R_pub], held_identity_paths=[B_id],
                  current_identity_path=A_id, kek_path=os.path.join(TMP, "nope.age"))
    check("reseal refuses when no existing envelope", False)
except CK.KekUnavailable:
    check("reseal refuses when no existing envelope", True)

# refusal: recovery recipient must never be dropped
try:
    CK.reseal_kek(recipients=[B_pub], held_identity_paths=[B_id],
                  current_identity_path=A_id, kek_path=KEKF)
    check("reseal refuses to drop the recovery recipient", False)
except CK.KekUnavailable:
    check("reseal refuses to drop the recovery recipient", True)

# seeding form: 3 stanzas (worker + recovery + time-boxed old VPS), all held decrypt
bak = CK.reseal_kek(recipients=[B_pub, R_pub, A_pub],
                    held_identity_paths=[A_id, B_id, R_id],
                    current_identity_path=A_id, kek_path=KEKF)
blob = open(KEKF, "rb").read()
check("seeding reseal: 3 stanzas", blob.count(b"-> X25519") == 3)
check("timestamped backup exists, mode 0600",
      os.path.exists(bak) and (os.stat(bak).st_mode & 0o777) == 0o600)
same = all(subprocess.run([AGE, "--decrypt", "-i", i, KEKF], check=True,
                          capture_output=True).stdout == KEK for i in (A_id, B_id, R_id))
check("SAME KEK under every identity (fingerprint unchanged)", same)

# reduction form: back to 2 stanzas; old VPS identity is locked out
CK.reseal_kek(recipients=[B_pub, R_pub], held_identity_paths=[B_id, R_id],
              current_identity_path=A_id, kek_path=KEKF)
check("reduction reseal: 2 stanzas", open(KEKF, "rb").read().count(b"-> X25519") == 2)
a_out = subprocess.run([AGE, "--decrypt", "-i", A_id, KEKF], capture_output=True)
check("dropped identity can no longer decrypt", a_out.returncode != 0)
check("worker identity still recovers the SAME KEK",
      subprocess.run([AGE, "--decrypt", "-i", B_id, KEKF], check=True,
                     capture_output=True).stdout == KEK)

# refusal: a held identity NOT in the recipient set -> abort, old envelope intact
before = open(KEKF, "rb").read()
try:
    CK.reseal_kek(recipients=[B_pub, R_pub], held_identity_paths=[B_id, C_id],
                  current_identity_path=B_id, kek_path=KEKF)
    check("reseal refuses when a held identity cannot decrypt the new blob", False)
except CK.KekUnavailable:
    check("reseal refuses when a held identity cannot decrypt the new blob", True)
check("old envelope untouched after refusal (+ no .tmp left)",
      open(KEKF, "rb").read() == before and not os.path.exists(KEKF + ".tmp"))

# refusal: pin mismatch (swapped envelope) -> never resealed
CK.KEK_SHA256_PIN = "0" * 64
try:
    CK.reseal_kek(recipients=[B_pub, R_pub], held_identity_paths=[B_id],
                  current_identity_path=B_id, kek_path=KEKF)
    check("reseal refuses on pinned-fingerprint mismatch", False)
except CK.KekUnavailable:
    check("reseal refuses on pinned-fingerprint mismatch", True)
check("envelope unchanged after pin refusal", open(KEKF, "rb").read() == before)

# refusal: EMPTY pin is a hard error for reseal (fail-open would re-envelope anything)
CK.KEK_SHA256_PIN = ""
try:
    CK.reseal_kek(recipients=[B_pub, R_pub], held_identity_paths=[B_id],
                  current_identity_path=B_id, kek_path=KEKF)
    check("reseal refuses on EMPTY pinned fingerprint", False)
except CK.KekUnavailable:
    check("reseal refuses on EMPTY pinned fingerprint", True)

print("")
print("RESULT: ALL PASS" if fails == 0 else f"RESULT: {fails} FAIL")
sys.exit(0 if fails == 0 else 1)
