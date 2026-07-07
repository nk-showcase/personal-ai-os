"""bot/context_store.py - the single module that encrypts personal free-text at rest.

Implements the core of scheme 3 (see docs/security/adr-encryption-notion-migration.md, PART A):
  - per-row DEK (a fresh 256-bit key for EVERY field) - one row = one blast radius;
  - AEAD ChaCha20-Poly1305 (IETF, 96-bit nonce) for both the content and the DEK wrap;
  - the AAD binds the ciphertext to the row identity (domain|row_id|field) + scheme version + kek_gen,
    so the ciphertext CANNOT be silently moved to another row/field or have its KEK generation swapped;
  - a self-describing envelope, so a row decrypts under the same KEK generation that encrypted it.

Envelope format (bytes):
    version(1) || kek_gen(2, BE) || wrap_nonce(12) || wrapped_DEK(48) || content_nonce(12) || ciphertext+tag

BOUNDARIES (important):
  - This module does NOT manage the master key (KEK). The KEK arrives from OUTSIDE (32 bytes); its custody -
    the age wrapper / offline recovery - is a separate owner-gated layer (ADR §4, Part B). Here it is only
    symmetric cryptography over an ALREADY obtained KEK.
  - A decryption failure is a typed DecryptError, NEVER a "silent" None and NEVER a
    fallback to plaintext (SECURITY.md §3). The error text carries only the class/fact, NEVER a value/key.

Dependency: `cryptography` (ChaCha20Poly1305). Added to requirements.txt in this same change.
"""
from __future__ import annotations

import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

SCHEME_VERSION = 1
_KEK_LEN = 32          # 256-bit master key
_DEK_LEN = 32          # 256-bit per-row data key
_NONCE_LEN = 12        # 96-bit nonce (IETF ChaCha20-Poly1305)
_TAG_LEN = 16
_WRAPPED_DEK_LEN = _DEK_LEN + _TAG_LEN  # 48: DEK under KEK = ciphertext+tag
_HEADER_LEN = 1 + 2    # version + kek_gen
# Minimum length of a valid envelope (empty content still carries the 16-byte tag).
_MIN_ENVELOPE_LEN = _HEADER_LEN + _NONCE_LEN + _WRAPPED_DEK_LEN + _NONCE_LEN + _TAG_LEN


class DecryptError(Exception):
    """Decryption failed (AEAD/format/version). Text carries only the fact, NEVER a value/key."""


def _aad(domain: str, row_id: str, field: str, kek_gen: int) -> bytes:
    """Canonical AAD binding the ciphertext to (scheme|domain|row_id|field|kek_gen).
    Each part is length-prefixed (>I) - no delimiter ambiguity."""
    parts = [
        b"aios-ctx",
        bytes([SCHEME_VERSION]),
        domain.encode("utf-8"),
        str(row_id).encode("utf-8"),
        field.encode("utf-8"),
        struct.pack(">H", kek_gen),
    ]
    return b"".join(struct.pack(">I", len(p)) + p for p in parts)


def encrypt_field(
    plaintext: str,
    kek: bytes,
    *,
    domain: str,
    row_id: str,
    field: str,
    kek_gen: int = 1,
) -> bytes:
    """Encrypt one personal free-text field. Returns a self-describing envelope (bytes).
    A fresh DEK + fresh nonces per call -> two identical texts produce DIFFERENT envelopes."""
    if not isinstance(plaintext, str):
        raise TypeError("plaintext must be str")
    if not isinstance(kek, (bytes, bytearray)) or len(kek) != _KEK_LEN:
        raise ValueError("KEK must be exactly 32 bytes")
    if not (0 <= kek_gen <= 0xFFFF):
        raise ValueError("kek_gen out of range")

    aad = _aad(domain, row_id, field, kek_gen)

    dek = os.urandom(_DEK_LEN)
    wrap_nonce = os.urandom(_NONCE_LEN)
    wrapped_dek = ChaCha20Poly1305(bytes(kek)).encrypt(wrap_nonce, dek, aad)

    content_nonce = os.urandom(_NONCE_LEN)
    ciphertext = ChaCha20Poly1305(dek).encrypt(content_nonce, plaintext.encode("utf-8"), aad)

    header = bytes([SCHEME_VERSION]) + struct.pack(">H", kek_gen)
    return header + wrap_nonce + wrapped_dek + content_nonce + ciphertext


def decrypt_field(
    envelope: bytes,
    kek: bytes,
    *,
    domain: str,
    row_id: str,
    field: str,
) -> str:
    """Decrypt an envelope. Fail-closed: any failure/tamper -> DecryptError (never None/plaintext).
    kek_gen is taken FROM the envelope; the AAD is rebuilt for it (binding to row/field/generation)."""
    if not isinstance(kek, (bytes, bytearray)) or len(kek) != _KEK_LEN:
        raise ValueError("KEK must be exactly 32 bytes")
    if not isinstance(envelope, (bytes, bytearray)) or len(envelope) < _MIN_ENVELOPE_LEN:
        raise DecryptError("malformed envelope (too short)")
    try:
        off = 0
        version = envelope[off]
        off += 1
        if version != SCHEME_VERSION:
            raise DecryptError("unsupported scheme version")
        (kek_gen,) = struct.unpack(">H", envelope[off:off + 2])
        off += 2
        wrap_nonce = envelope[off:off + _NONCE_LEN]
        off += _NONCE_LEN
        wrapped_dek = envelope[off:off + _WRAPPED_DEK_LEN]
        off += _WRAPPED_DEK_LEN
        content_nonce = envelope[off:off + _NONCE_LEN]
        off += _NONCE_LEN
        ciphertext = envelope[off:]

        aad = _aad(domain, row_id, field, kek_gen)
        dek = ChaCha20Poly1305(bytes(kek)).decrypt(wrap_nonce, wrapped_dek, aad)
        plaintext = ChaCha20Poly1305(dek).decrypt(content_nonce, ciphertext, aad)
        return plaintext.decode("utf-8")
    except DecryptError:
        raise
    except Exception:
        # AEAD failure (corrupt/tampered ciphertext, wrong KEK, foreign AAD) or format.
        # NEVER print a value/key/material - only the fact.
        raise DecryptError("decrypt failed (AEAD/format)")
