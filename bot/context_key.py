"""bot/context_key.py — KEK custody seam for at-rest encryption (scheme 3).

SEPARATE from secrets_loader: the master key (KEK) NEVER flows through
get_secret / Bitwarden / the on-disk secret cache. In production the KEK is an
age-wrapped blob decrypted by a worker-only 0600 age-identity FILE — that key
ceremony is a LATER owner step (ADR §Phase 0). Until that file exists, get_kek()
returns None ("not provisioned") and encryption stays OFF — the running bot is
unaffected.

Gating (all env, all non-secret toggles/paths — never key values):
  AIOS_CONTEXT_ENCRYPTION       master feature flag (default OFF).
  AIOS_CONTEXT_IDENTITY_FILE    path to the worker-only age identity (deferred slice).
  AIOS_CONTEXT_ALLOW_TEST_KEK   dev/test sentinel — must be truthy to allow injection.
  AIOS_CONTEXT_TEST_KEK         dev/test KEK as 32-byte hex (engages ONLY with the sentinel).

No secret values are ever logged.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}

# Recovery public recipient — a git-pinned immutable value (NON-SECRET public key).
# See docs/security/key-custody-model.md. The recovery PRIVATE key is kept offline by
# the operator (removable media + passphrase) and never appears in code/git/chat.
RECOVERY_RECIPIENT = os.environ.get("RECOVERY_RECIPIENT", "age1PLACEHOLDER")

# sha256 of the provisioned KEK bytes (NON-SECRET one-way fingerprint). When set, get_kek
# verifies every unwrap against this pin, fail-closed: a swapped/attacker-known kek.age
# decrypts fine but can never be USED, so no ciphertext is ever produced under a foreign
# key. Ships EMPTY: record your own pin here (or via the KEK_SHA256_PIN env var) after
# provisioning the key - see docs/security/key-custody-model.md. With the pin empty,
# unwrap-verification is skipped and resealing refuses to run (fail-closed).
KEK_SHA256_PIN = os.environ.get("KEK_SHA256_PIN", "")


class KekUnavailable(RuntimeError):
    """Flag ON but the KEK is not usable. Never carries key material."""


def encryption_enabled() -> bool:
    return (os.getenv("AIOS_CONTEXT_ENCRYPTION") or "").strip().lower() in _TRUE


def _identity_path() -> Path | None:
    p = (os.getenv("AIOS_CONTEXT_IDENTITY_FILE") or "").strip()
    return Path(p).expanduser() if p else None


def _test_kek() -> bytes | None:
    """Dev/test injection — engages ONLY when the explicit sentinel
    AIOS_CONTEXT_ALLOW_TEST_KEK is truthy AND a valid 32-byte hex key is provided.
    Refused if a real identity file is present, so it can never engage in production."""
    if (os.getenv("AIOS_CONTEXT_ALLOW_TEST_KEK") or "").strip().lower() not in _TRUE:
        return None
    ident = _identity_path()
    if ident is not None and ident.exists():
        return None  # a real key is configured -> never accept an injected test key
    raw = (os.getenv("AIOS_CONTEXT_TEST_KEK") or "").strip()
    if not raw:
        return None
    try:
        kek = bytes.fromhex(raw)
    except ValueError:
        return None
    return kek if len(kek) == 32 else None


def _age_bin() -> str:
    return (os.getenv("AIOS_AGE_BIN") or "age").strip() or "age"


def _kek_path() -> Path | None:
    p = (os.getenv("AIOS_CONTEXT_KEK_FILE") or "").strip()
    return Path(p).expanduser() if p else None


def _require_mode_0600(path: Path) -> None:
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise KekUnavailable(f"identity file must be mode 0600 (got {oct(mode)})")


def _run_age(args, *, input_bytes: bytes | None = None) -> bytes:
    """Shell out to the pinned age binary (mirrors secrets_loader shelling to bws).
    Returns stdout bytes. Never surfaces age stderr raw (it may name paths)."""
    try:
        proc = subprocess.run(
            [_age_bin(), *args], input=input_bytes, capture_output=True, timeout=30
        )
    except FileNotFoundError:
        raise KekUnavailable("age binary not found (set AIOS_AGE_BIN or install age)")
    if proc.returncode != 0:
        raise KekUnavailable("age operation failed")
    return proc.stdout


def init_kek(
    *,
    vps_recipient: str,
    vps_identity_path,
    kek_path,
    recovery_recipient: str = RECOVERY_RECIPIENT,
) -> None:
    """Generate a fresh 32-byte KEK and seal it to BOTH recipients into kek_path
    (age, two X25519 stanzas: the VPS working key + the offline recovery key).
    Backup-first (retain prior generation) + atomic write + post-write verify.
    Owner-gated one-time op; the plaintext KEK never leaves this process."""
    kek_path = Path(kek_path).expanduser()
    vps_identity_path = Path(vps_identity_path).expanduser()
    kek = os.urandom(32)
    # Seal to the working recipient always; add the offline recovery recipient
    # only when a real one is configured. With the shipped placeholder (no
    # recovery key set) the KEK is sealed to a single recipient - the operator
    # supplies RECOVERY_RECIPIENT to enable the two-stanza recovery guarantee.
    recipients = ["-r", vps_recipient]
    have_recovery = bool(recovery_recipient) and recovery_recipient != "age1PLACEHOLDER"
    if have_recovery:
        recipients += ["-r", recovery_recipient]
    blob = _run_age(["--encrypt", *recipients], input_bytes=kek)
    expected_stanzas = 2 if have_recovery else 1
    if blob.count(b"-> X25519") < expected_stanzas:
        raise KekUnavailable(
            "sealed KEK is missing a recipient stanza (expected %d)" % expected_stanzas
        )
    tmp = Path(str(kek_path) + ".tmp")
    tmp.write_bytes(blob)
    os.chmod(tmp, 0o600)
    if kek_path.exists():  # never destroy the prior generation in place
        kek_path.replace(Path(str(kek_path) + ".prev"))
    tmp.replace(kek_path)
    # Verify on a durable read: the VPS identity recovers the EXACT KEK from the file.
    # (The recovery identity's decrypt is the separate owner air-gapped proof.)
    recovered = _run_age(["--decrypt", "-i", str(vps_identity_path), str(kek_path)])
    if recovered != kek:
        raise KekUnavailable("post-write verify failed: VPS identity did not recover the KEK")


def reseal_kek(
    *,
    recipients: list[str],
    held_identity_paths: list,
    current_identity_path,
    kek_path,
) -> str:
    """Re-seal the EXISTING KEK to a new recipient set (cutover amendment 1).

    NEVER generates key material — init_kek is FORBIDDEN for resealing (it makes a
    NEW KEK and silently orphans every existing ciphertext). Caller contract: an
    off-VPS copy of the current kek.age is confirmed BEFORE calling (ceremony step).

    Steps, fail-closed at each: timestamped backup of the current envelope (never
    overwrites .prev or an earlier backup) -> decrypt current envelope with
    current_identity_path -> verify the pinned fingerprint -> seal to `recipients`
    (must include the recovery recipient; one stanza per recipient) -> verify EVERY
    identity in held_identity_paths decrypts the .tmp blob to the identical 32
    bytes -> atomic rename. Returns the backup path. Because sha256(KEK) is
    unchanged and the pinned recovery recipient is present, the owner's recovery
    proof is inherited (transitive rule). Plaintext KEK never leaves this process.
    """
    import time as _time

    kek_path = Path(kek_path).expanduser()
    if RECOVERY_RECIPIENT not in recipients:
        raise KekUnavailable("reseal refused: recipient set must include the recovery recipient")
    if not held_identity_paths:
        raise KekUnavailable("reseal refused: need at least one held identity to verify against")
    if not KEK_SHA256_PIN:
        # The pin is the anti-swap oracle; resealing without it would happily re-envelope
        # a foreign key. Empty pin is a hard error here (audit finding 10).
        raise KekUnavailable("reseal refused: KEK_SHA256_PIN is empty")
    if not kek_path.exists():
        raise KekUnavailable("reseal refused: no existing kek.age (use init_kek for first provisioning)")

    def _write_0600(path: Path, data: bytes) -> None:
        # O_EXCL + 0600 at creation: no umask window, never overwrites (audit finding 8).
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    # 1. Timestamped backup FIRST (unique name — never clobbers .prev or older backups;
    #    numeric suffix disambiguates two reseal calls within the same second, e.g. seed+reduce).
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    backup = Path(f"{kek_path}.bak-{stamp}")
    n = 1
    while backup.exists():
        backup = Path(f"{kek_path}.bak-{stamp}-{n}")
        n += 1
    _write_0600(backup, kek_path.read_bytes())

    # 2. Open the CURRENT envelope and verify it is THE key (pin, fail-closed).
    kek = _run_age(["--decrypt", "-i", str(Path(current_identity_path).expanduser()),
                    str(kek_path)])
    if len(kek) != 32:
        raise KekUnavailable("reseal refused: recovered KEK is not 32 bytes")
    if KEK_SHA256_PIN:
        import hashlib
        if hashlib.sha256(kek).hexdigest() != KEK_SHA256_PIN:
            raise KekUnavailable("reseal refused: current KEK does not match the pinned fingerprint")

    # 3. Seal to the new recipient set; every recipient gets a stanza.
    args = ["--encrypt"]
    for r in recipients:
        args += ["-r", r]
    blob = _run_age(args, input_bytes=kek)
    if blob.count(b"-> X25519") != len(recipients):
        raise KekUnavailable("reseal refused: sealed blob stanza count != recipient count")
    tmp = Path(str(kek_path) + ".tmp")
    if tmp.exists():
        tmp.unlink()
    _write_0600(tmp, blob)

    # 4. EVERY held identity must recover the identical 32 bytes from the .tmp blob
    #    BEFORE the envelope is replaced — a reseal that locks any holder out never lands.
    try:
        for ident in held_identity_paths:
            recovered = _run_age(["--decrypt", "-i", str(Path(ident).expanduser()), str(tmp)])
            if recovered != kek:
                raise KekUnavailable(
                    "reseal refused: a held identity did not recover the identical KEK from the new envelope")
        tmp.replace(kek_path)  # 5. atomic swap only after all proofs
    finally:
        if tmp.exists():
            tmp.unlink()
    # Durable-read re-verify on the final file with the first held identity. If THIS
    # raises, the swap has ALREADY happened — say so honestly (audit finding 5).
    if _run_age(["--decrypt", "-i", str(Path(held_identity_paths[0]).expanduser()),
                 str(kek_path)]) != kek:
        raise KekUnavailable(
            f"reseal post-rename verify failed: the envelope WAS already swapped; "
            f"restore from backup {backup} if needed")
    return str(backup)


def get_kek() -> bytes | None:
    """Return the 32-byte KEK, or None when encryption is disabled / not provisioned.
    NEVER routes through secrets_loader/get_secret. Real path: age-decrypt kek.age with
    the worker-only 0600 identity FILE. honors the gated dev/test hook only otherwise."""
    if not encryption_enabled():
        return None
    tk = _test_kek()
    if tk is not None:
        return tk
    ident = _identity_path()
    kek_path = _kek_path()
    if ident is None or not ident.exists() or kek_path is None or not kek_path.exists():
        return None  # not provisioned (no identity file and/or no kek.age yet)
    _require_mode_0600(ident)
    kek = _run_age(["--decrypt", "-i", str(ident), str(kek_path)])
    if len(kek) != 32:
        raise KekUnavailable("recovered KEK is not 32 bytes")
    if KEK_SHA256_PIN:
        import hashlib
        if hashlib.sha256(kek).hexdigest() != KEK_SHA256_PIN:
            raise KekUnavailable("recovered KEK does not match the pinned fingerprint (possible kek.age swap)")
    return kek


def kek_status() -> str:
    """Value-free status for the owner health light: 'off' | 'provisioned' | 'missing-key'.
    Never raises — a corrupted/swapped envelope is exactly when the health light is
    needed most (audit finding 6)."""
    if not encryption_enabled():
        return "off"
    try:
        return "provisioned" if get_kek() is not None else "missing-key"
    except (KekUnavailable, OSError, subprocess.TimeoutExpired):
        # OSError: identity file vanished between exists() and stat();
        # TimeoutExpired: hung age binary — the light reports, never crashes.
        return "missing-key"
