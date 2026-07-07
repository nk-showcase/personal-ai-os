"""bot/store_seal.py — ASYMMETRIC inbound seal for the STORE-op transport.

Same age-recipient model as router_seal (§ S6 S-2), applied to the store queue instead
of the router pipe: integrations_proxy seals the {domain,op,args} envelope to the WORKER's
PUBLIC recipient before enqueue; the keyed worker opens it with its OWN 0600 context
identity FILE (the SAME identity it already uses to decrypt the store — env
AIOS_CONTEXT_IDENTITY_FILE). The transport holds only the public half, so it can never
decrypt what it sealed.

This module deliberately does NOT enable the unified router. It reuses two primitives:
  * router_seal.seal_inbound(payload, recipient=…) for the public-side encrypt (which already
    RAISES RouterSealError on a placeholder / unprovisioned recipient — fail-closed);
  * context_key._run_age / _require_mode_0600 for the worker-side decrypt with the context
    identity (NOT the router identity file).

No plaintext temp file (ADR §4): ciphertext rides age stdin/stdout in memory; the base64
wrapper keeps the queue's TEXT payload column ascii. No secret values are ever logged.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path

from .context_key import KekUnavailable, _require_mode_0600, _run_age
from .router_seal import RouterSealError, seal_inbound

# WORKER public recipient (NON-SECRET — the public half of the worker's context identity;
# derived on the server via `age-keygen -y ${AIOS_HOME}/.ai-os/keys/context-worker.identity`).
# Sealing to it means only that worker identity can open the payload. Supply the real public
# key via the STORE_SEAL_RECIPIENT env var; with the placeholder, sealing stays inert.
# Sealing still happens ONLY when AIOS_STORE_SEAL=1 (default OFF -> byte-identical).
# Overridable in tests via the module attribute.
STORE_SEAL_RECIPIENT = os.environ.get("STORE_SEAL_RECIPIENT", "age1PLACEHOLDER")


def _context_identity_path() -> Path | None:
    """The WORKER's context identity FILE (same one it uses to open the store), NOT the
    router identity. Value-free env lookup — never the key material."""
    p = (os.getenv("AIOS_CONTEXT_IDENTITY_FILE") or "").strip()
    return Path(p).expanduser() if p else None


def seal_store_inbound(inner_b64: str) -> str:
    """Transport side (public-only): age-encrypt the base64 store-op envelope to the worker
    recipient. Returns base64 ascii for the queue TEXT payload. Fail-closed: a placeholder /
    unprovisioned recipient (or any age failure) raises RouterSealError — never returns
    plaintext. Delegates to router_seal.seal_inbound, which owns the placeholder guard."""
    return seal_inbound(inner_b64, recipient=STORE_SEAL_RECIPIENT)


def open_store_inbound(sealed_b64: str) -> str:
    """Worker side (private identity): base64-decode + age-decrypt with the CONTEXT identity
    FILE (env AIOS_CONTEXT_IDENTITY_FILE). Fail-closed: missing/!0600 identity, bad base64, or
    age failure raises RouterSealError — never returns ciphertext or partial plaintext. Mirrors
    router_seal.open_inbound but reads the context identity, not the router identity."""
    ident = _context_identity_path()
    if ident is None or not ident.exists():
        raise RouterSealError("store worker context identity not provisioned")
    try:
        _require_mode_0600(ident)               # reuse context_key 0600 enforcement
    except KekUnavailable as exc:               # it raises KekUnavailable, not RouterSealError
        raise RouterSealError("store worker context identity not usable") from exc
    try:
        ct = base64.b64decode(sealed_b64.encode("ascii"), validate=True)
    except Exception as exc:                     # noqa: BLE001
        raise RouterSealError("sealed store payload is not valid base64") from exc
    try:
        pt = _run_age(["--decrypt", "-i", str(ident)], input_bytes=ct)
    except KekUnavailable as exc:
        raise RouterSealError("store inbound open failed") from exc
    return pt.decode("utf-8")
