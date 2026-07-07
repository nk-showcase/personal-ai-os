"""Master-key custody test (KEK custody, scheme 3) against the REAL age binary.
Ephemeral age keys in a temp dir; no real recovery key is used.
Proves the key property: the sealed KEK opens with BOTH the server key AND the recovery
key (insurance against "encrypted but nothing left to open it with"). Prints no values.
Requires the `age` binary (+ age-keygen). Run:  PYTHONPATH=. python3 scripts/test_context_key.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.getcwd())
for _k in ("AIOS_CONTEXT_ENCRYPTION", "AIOS_CONTEXT_ALLOW_TEST_KEK", "AIOS_CONTEXT_TEST_KEK",
           "AIOS_CONTEXT_IDENTITY_FILE", "AIOS_CONTEXT_KEK_FILE", "AIOS_AGE_BIN"):
    os.environ.pop(_k, None)

fails = 0


def check(name, cond):
    global fails
    print(("PASS: " if cond else "FAIL: ") + name)
    if not cond:
        fails += 1


def _find(name):
    cand = os.path.expanduser("~/.local/bin/" + name)
    if os.path.exists(cand):
        return cand
    return shutil.which(name)


AGE = _find("age")
AGEKG = _find("age-keygen")
if not AGE or not AGEKG:
    print("SKIP: age/age-keygen not found — this test requires age")
    sys.exit(0)
os.environ["AIOS_AGE_BIN"] = AGE

from bot import context_key as K  # noqa: E402

d = tempfile.mkdtemp(prefix="aios_kek_")


def keygen(path):
    r = subprocess.run([AGEKG, "-o", path], capture_output=True, text=True)
    os.chmod(path, 0o600)
    for line in r.stderr.splitlines():
        if "public key:" in line.lower():
            return line.split(":", 1)[1].strip()
    return ""


vps_key = os.path.join(d, "vps.key")
rec_key = os.path.join(d, "rec.key")
kek = os.path.join(d, "kek.age")
vps_pub = keygen(vps_key)
rec_pub = keygen(rec_key)
check("generated 2 ephemeral age keys (server + recovery)",
      vps_pub.startswith("age1") and rec_pub.startswith("age1"))

# 1. init_kek seals the KEK to BOTH recipients
K.init_kek(vps_recipient=vps_pub, vps_identity_path=vps_key, kek_path=kek, recovery_recipient=rec_pub)
check("kek.age created", os.path.exists(kek))
blob = open(kek, "rb").read()
check("kek.age has TWO recipients (2 X25519 stanzas)", blob.count(b"-> X25519") >= 2)

# 2. get_kek returns a 32-byte KEK via the server key
os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"
os.environ["AIOS_CONTEXT_IDENTITY_FILE"] = vps_key
os.environ["AIOS_CONTEXT_KEK_FILE"] = kek
# init_kek generated a RANDOM KEK (bytes unknown to the test) — disable the fingerprint
# pin check for this custody test; the pin logic itself is covered by the domain tests.
K.KEK_SHA256_PIN = ""
k1 = K.get_kek()
check("get_kek returned a 32-byte KEK (server key)", isinstance(k1, (bytes, bytearray)) and len(k1) == 32)

# 3. The RECOVERY KEY also opens the same KEK — insurance
rec_dec = subprocess.run([AGE, "--decrypt", "-i", rec_key, kek], capture_output=True).stdout
check("recovery key opens the SAME master key (disaster recovery works)", rec_dec == k1)

# 4. identity not 0600 -> fail-closed refusal
os.chmod(vps_key, 0o644)
raised = False
try:
    K.get_kek()
except K.KekUnavailable:
    raised = True
check("identity file not 0600 -> refusal (fail-closed)", raised)
os.chmod(vps_key, 0o600)

# 5. switch OFF -> None (bot unaffected)
os.environ["AIOS_CONTEXT_ENCRYPTION"] = "off"
check("switch OFF -> get_kek None", K.get_kek() is None)
os.environ["AIOS_CONTEXT_ENCRYPTION"] = "on"

# 6. no kek.age -> not provisioned (None, does not crash)
os.environ.pop("AIOS_CONTEXT_KEK_FILE", None)
check("no kek.age -> not provisioned (None)", K.get_kek() is None)

# 7. the recovery public key is embedded in code (git-pinned oracle)
check("RECOVERY_RECIPIENT is embedded in code and public (age1...)", K.RECOVERY_RECIPIENT.startswith("age1"))

shutil.rmtree(d, ignore_errors=True)
print("")
if fails == 0:
    print("RESULT: ALL PASS")
    sys.exit(0)
else:
    print(f"RESULT: {fails} FAIL")
    sys.exit(1)
