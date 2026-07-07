"""bot/aios_migrate.py — generic, NON-DESTRUCTIVE Notion -> local-encrypted migration engine.

ADR adr-encryption-notion-migration.md, Phase 3-4. For each source record:
    read  ->  encrypt-on-save  ->  fresh-connection verify (decrypt & byte-compare source)

This engine NEVER archives, deletes, or mutates Notion — reading the source is read-only and
the source stays the system of record until a SEPARATE, recovery-gated archive step. Safe to
re-run: `save_one` upserts by the stable source id, and every row is re-verified. A verify
mismatch is counted and the id recorded; the source row is left completely untouched.

Domain-agnostic by design — each domain plugs in two callables:
  * save_one(rec, *, db)      -> encrypt (via the context_cipher chokepoint) + a SINGLE durable
                                 upsert keyed on rec["source_notion_id"] (e.g. aios_notes_store.save_note).
  * verify_one(sid, *, db)    -> {field: decrypted_text_or_None} read on a FRESH connection.
`verify_fields` names EVERY field whose stored value must match source (not just the encrypted
free-text ones) — structured columns are saved too and must be verified before `all_verified`
can gate an archive. Numeric fields are compared numerically (5 == 5.0), text byte-exact.

Encryption itself is the flag/KEK-gated chokepoint (context_cipher) INSIDE save_one; this engine
refuses to run when encryption is OFF (migrating to cleartext would be pointless and unsafe),
unless allow_plaintext=True is passed explicitly (used only by tests of the OFF path).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping


@dataclass
class MigrateResult:
    domain: str
    read: int = 0            # records pulled from the source
    saved: int = 0           # rows written (encrypted) into the local store
    verified: int = 0        # rows that decrypted back byte-identical to source
    mismatch: int = 0        # saved but verify failed (decrypt error OR byte mismatch) -> NOT safe to archive
    skipped_no_id: int = 0   # source record without a usable source_notion_id -> refused (AAD identity required)
    errors: int = 0          # save_one raised
    mismatch_ids: list = field(default_factory=list)
    error_ids: list = field(default_factory=list)

    @property
    def all_verified(self) -> bool:
        """True only when EVERY read row was saved AND verified (0 errors, 0 mismatch, 0 skipped).
        The archive step must gate on this per domain — never archive a source whose local copy
        did not verify."""
        return (self.read > 0
                and self.errors == 0 and self.mismatch == 0 and self.skipped_no_id == 0
                and self.saved == self.read and self.verified == self.read)

    def as_dict(self) -> dict:
        return {
            "domain": self.domain, "read": self.read, "saved": self.saved,
            "verified": self.verified, "mismatch": self.mismatch,
            "skipped_no_id": self.skipped_no_id, "errors": self.errors,
            "all_verified": self.all_verified,
            "mismatch_ids": list(self.mismatch_ids), "error_ids": list(self.error_ids),
        }


def _norm(v) -> str:
    """Compare as text; None and '' are equal (Notion empty vs stored NULL)."""
    return "" if v is None else str(v)


def _values_equal(a, b) -> bool:
    """True if two field values are equivalent. Numbers compared numerically so a Notion
    number (5.0) and an INTEGER-affinity column read-back (5) match; everything else as text
    (None == '' == missing). bool is treated as identity to avoid True==1 surprises."""
    if a is None and b is None:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return _norm(a) == _norm(b)


def _fields_match(rec: Mapping, got: Mapping, fields: Iterable[str]) -> bool:
    return all(_values_equal(rec.get(f), got.get(f)) for f in fields)


def refuse_if_flipped(domain: str) -> None:
    """Abort an importer against a domain already cut over to sqlite (amendment 3/7): a re-import
    of the stale Notion copy would clobber post-flip local edits (chat save even DELETEs+reinserts).
    Call FIRST in every import_<domain>() — before any source read."""
    from . import aios_config as CFG
    if CFG.get_store_mode(domain) == "sqlite":
        raise RuntimeError(
            f"import refused: domain '{domain}' is already flipped to sqlite — a re-import would "
            "overwrite live local data with the stale source copy"
        )


def reconcile_deletions(domain, source_ids, list_local_ids, archive_local, *, db=None) -> dict:
    """Catch-up reconciliation (amendment 5): tombstone local ACTIVE rows whose id is absent from
    the FULL source id set (deleted at source since the seed). Returns counts + count_parity —
    True only when active-local exactly matches source (the archive gate requires it)."""
    src = {str(s) for s in source_ids}
    local = {str(s) for s in list_local_ids(db=db)}
    missing = sorted(local - src)
    for sid in missing:
        archive_local(sid, db=db)
    active_after = len(local) - len(missing)
    return {
        "domain": domain, "source_count": len(src), "local_active_before": len(local),
        "tombstoned": len(missing), "tombstoned_ids": missing,
        "count_parity": active_after == len(src),
    }


def migrate_records(
    domain: str,
    records: Iterable[Mapping],
    save_one: Callable[..., None],
    verify_one: Callable[..., Mapping],
    *,
    verify_fields: Iterable[str],
    db=None,
    allow_plaintext: bool = False,
) -> MigrateResult:
    """Migrate an iterable of source records into the local encrypted store, verifying each.

    Non-destructive: touches only the local store; never the source. Returns a MigrateResult;
    check `.all_verified` before any downstream archive of the corresponding source rows.
    """
    if not allow_plaintext:
        # Guard: the whole point is encryption. Refuse to silently write cleartext.
        from .context_key import encryption_enabled
        if not encryption_enabled():
            raise RuntimeError(
                "aios_migrate: encryption is OFF (AIOS_CONTEXT_ENCRYPTION unset) — refusing to "
                "migrate to cleartext. Enable encryption + provision the KEK first."
            )

    fields = list(verify_fields)
    res = MigrateResult(domain=domain)
    for rec in records:
        res.read += 1
        sid = rec.get("source_notion_id")
        if sid is None or (isinstance(sid, str) and not sid.strip()):
            res.skipped_no_id += 1
            continue
        sid_s = str(sid)
        # 1. encrypt-on-save (single durable upsert). A raise here leaves zero rows touched (D3).
        try:
            save_one(rec, db=db)
            res.saved += 1
        except Exception:
            res.errors += 1
            res.error_ids.append(sid_s)
            continue
        # 2. verify on a FRESH read (proves the ciphertext is durable AND decryptable, not just
        #    that the in-memory value was fine). Any decrypt failure quarantines inside the
        #    chokepoint and raises -> counted as mismatch (never archive this source row).
        try:
            got = verify_one(sid, db=db)
        except Exception:
            res.mismatch += 1
            res.mismatch_ids.append(sid_s)
            continue
        if _fields_match(rec, got, fields):
            res.verified += 1
        else:
            res.mismatch += 1
            res.mismatch_ids.append(sid_s)
    return res
