"""bot/router_seal.py — S6 S-2 ASYMMETRIC inbound seal for the unified-router pipe.

R14: the transport seals the inbound envelope to the WORKER public recipient (it holds
ONLY the public half). The worker opens it with its 0600 private identity FILE. This is
the age recipient model from context_key (init_kek/get_kek), NOT the symmetric KEK path —
the transport never imports get_kek / context_cipher, so it can never decrypt what it sealed.

Storage note: age output is BINARY; task_text is a TEXT column, so seal_inbound returns a
base64-ascii string and open_inbound base64-decodes before age --decrypt. No plaintext temp
file (ADR §4): ciphertext rides stdin/stdout in memory.

Typed-error contract: every failure raises RouterSealError (including a !0600 identity,
which context_key raises as KekUnavailable and we re-wrap). No secret values are ever logged.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from .context_key import KekUnavailable, _require_mode_0600, _run_age  # reuse, don't dup

# Git-pinned WORKER public recipient (NON-SECRET placeholder until the real worker key is
# provisioned on the VPS — a separate owner/ops step, OUT of scope here). Pairs with
# RECOVERY_RECIPIENT in context_key.py:29. Overridable in tests via the module attribute.
ROUTER_WORKER_RECIPIENT = "age1PLACEHOLDER0000000000000000000000000000000000000000000000000"


class RouterSealError(RuntimeError):
    """Seal/open failed. Never carries plaintext or key material."""


def _identity_path() -> Path | None:
    p = (os.getenv("AIOS_ROUTER_IDENTITY_FILE") or "").strip()
    return Path(p).expanduser() if p else None


def seal_inbound(payload: str, *, recipient: str | None = None) -> str:
    """Transport side (public-only): age-encrypt the envelope JSON to the worker recipient.
    Returns base64 ascii for the TEXT task_text column. Fail-closed: any failure raises
    RouterSealError (never returns plaintext)."""
    rcpt = (recipient or ROUTER_WORKER_RECIPIENT).strip()
    if not rcpt or rcpt.startswith("age1PLACEHOLDER"):
        raise RouterSealError("router worker recipient not provisioned")
    try:
        blob = _run_age(["--encrypt", "-r", rcpt], input_bytes=payload.encode("utf-8"))
    except KekUnavailable as exc:           # age missing / non-zero exit (no values leaked)
        raise RouterSealError("inbound seal failed") from exc
    return base64.b64encode(blob).decode("ascii")


def open_inbound(blob_b64: str) -> str:
    """Worker side (private identity): base64-decode + age-decrypt with the 0600 identity FILE.
    Fail-closed: missing/!0600 identity, bad base64, or age failure raises RouterSealError —
    never returns ciphertext or partial plaintext."""
    ident = _identity_path()
    if ident is None or not ident.exists():
        raise RouterSealError("router worker identity not provisioned")
    try:
        _require_mode_0600(ident)           # reuse context_key 0600 enforcement
    except KekUnavailable as exc:           # it raises KekUnavailable, not RouterSealError
        raise RouterSealError("router worker identity not usable") from exc
    try:
        ct = base64.b64decode(blob_b64.encode("ascii"), validate=True)
    except Exception as exc:                # noqa: BLE001
        raise RouterSealError("inbound payload is not valid base64") from exc
    try:
        pt = _run_age(["--decrypt", "-i", str(ident)], input_bytes=ct)
    except KekUnavailable as exc:
        raise RouterSealError("inbound open failed") from exc
    return pt.decode("utf-8")
