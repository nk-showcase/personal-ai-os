"""bot/context_cipher.py — the single-encryptor chokepoint between storage and crypto.

The ONLY bridge from KEK custody (context_key.get_kek) to the crypto primitive
(context_store). Storage modules call encrypt_for_storage / decrypt_for_storage and
MUST NOT import context_store directly — single-encryptor invariant.

encrypt_for_storage three-state contract (no fourth):
    flag OFF          -> return None         (caller writes cleartext, exactly as today)
    flag ON + key      -> return ciphertext bytes
    flag ON + no key   -> raise KekUnavailable (NEVER silently write cleartext)

context_store (and therefore `cryptography`) is imported LAZILY inside the
encrypt/decrypt branch, so the flag-OFF cleartext path works even where `cryptography`
is absent and never drags the dependency in at import time.

Caller invariant (D3): compute encrypt_for_storage FIRST, then perform a SINGLE row
write. Never pre-insert cleartext and then UPDATE the BLOB — a raise must leave zero
rows touched.
"""
from __future__ import annotations

import time

from .context_key import KekUnavailable, encryption_enabled, get_kek


class DecryptQuarantine(RuntimeError):
    """Decrypt failed -> row quarantined + raised. Never carries value/key material."""


def _norm_row_id(row_id) -> str:
    return "" if row_id is None else str(row_id)


def encrypt_for_storage(domain: str, row_id, field: str, plaintext: str):
    """Return ciphertext bytes, or None when encryption is OFF. Raises KekUnavailable
    when ON but the KEK is missing (fail-closed — never a silent cleartext downgrade)."""
    if not encryption_enabled():
        return None
    kek = get_kek()
    if kek is None:
        raise KekUnavailable("context encryption is ON but the KEK is not provisioned")
    rid = _norm_row_id(row_id)
    if not rid:
        # A NULL/empty row_id would collapse the AAD binding (str(None) == "None"),
        # letting ciphertext be relocated between rows. Refuse, fail-closed.
        raise ValueError("encrypt_for_storage requires a non-empty row_id (AAD binding)")
    from .context_store import encrypt_field  # lazy: keep the cleartext path import-light
    return encrypt_field(plaintext, kek, domain=domain, row_id=rid, field=field)


def decrypt_for_storage(envelope, *, domain: str, row_id, field: str, db=None) -> str:
    """Decrypt a stored envelope. Fail-closed: any failure -> write a value-free
    decrypt_quarantine row + raise DecryptQuarantine. Never returns None-as-empty,
    never falls back to plaintext."""
    kek = get_kek()
    if kek is None:
        raise KekUnavailable("cannot decrypt: the KEK is not provisioned")
    rid = _norm_row_id(row_id)
    from .context_store import DecryptError, decrypt_field
    try:
        return decrypt_field(envelope, kek, domain=domain, row_id=rid, field=field)
    except DecryptError as e:
        _quarantine(domain, rid, field, type(e).__name__, db=db)
        raise DecryptQuarantine(f"decrypt quarantined: {domain}/{field}") from None


def _quarantine(domain: str, row_id: str, field: str, error_class: str, *, db=None) -> None:
    """Record a value-free decrypt dead-letter. Stores only identity + exception class."""
    from . import aios_storage as S
    try:
        with S.connect(db=db) as conn:
            conn.execute(
                "INSERT INTO decrypt_quarantine (row_id, domain, field, error_class, ts) "
                "VALUES (?,?,?,?,?)",
                (row_id, domain, field, error_class, time.time()),
            )
    except Exception:
        # Quarantine bookkeeping must NEVER mask the fail-closed raise above.
        pass
