# Cutover plan (AMENDED) — bot reads/writes -> local encrypted store

> Result of an adversarial design review (3 lenses + judge, 18 findings -> 13 amendments,
> verdict AMEND-THEN-BUILD). This file is the source of truth for the cutover build. The original
> D1-D5 sketch is superseded where amended below.

## Gate 0 — plaintext boundary (DECIDED)
The standing direction is "correct architecture and maximum security", and Part B
(chat never transits the bot) was already chosen. Therefore option (a): for personal-content reads
the KEYED WORKER renders and sends the Telegram message itself (bot supplies chat_id + view intent;
plaintext never returns through the shared queue). This merges the cutover read-path with Part B.
The chokepoint test stays as in the ADR.

## Key custody amendments
1. **reseal_kek(recipients) — new dedicated function; init_kek FORBIDDEN for resealing** (it
   generates a NEW KEK). reseal_kek: timestamped backup of the current kek.age (never overwrite .prev)
   + confirmed off-VPS copy BEFORE starting; encrypt the in-memory KEK to the integrations identity
   + git-pinned RECOVERY_RECIPIENT (a 3rd stanza only time-boxed for seeding, then reseal to 2);
   verify EVERY held identity decrypts the .tmp blob to identical 32 bytes; only then atomic rename.
   Recovery proof: transitive rule (KEK sha256 unchanged + pinned recipient + all-held-identity
   decrypt => proof inherited) OR the operator re-runs the air-gap proof on the new envelope.
2. **Pin sha256(KEK) as a git constant** next to RECOVERY_RECIPIENT; get_kek verifies, fail-closed.
   kek.age: 0600, owned by the reading user, one private copy per keyed user, never group-writable;
   _require_mode_0600 applies to kek.age too. app_config is bot-writable => NOT a trustworthy gate
   oracle: destruction-gate facts move to a root-owned file (or HMAC by worker-only material).
3. **Trust boundary**: before granting the identity, the integrations-worker gets a code path the
   coding worker cannot write (root-owned read-only checkout / bind-mount) — else the two service
   users are one trust domain and the 3rd-stanza math changes.

## Data-path amendments
4. **Raw-persist gate becomes destination-aware**: notion_raw_allowed() gates ONLY the Notion path.
   The local encrypted store MAY persist raw text (that is its purpose). Audit every gated site
   (message save, memory append, note/title fields, page notes, chat titles); per-field decision
   table; post-flip verification asserts content actually landed.
5. **Read+write flip TOGETHER per domain** (no read-first window): freeze writes ->
   catch-up -> verify -> flip both via ONE combined app_config flag; the config validity check
   forbids read=sqlite while write=notion_only. Pre-flip validation via the dual-write + compare
   (shadow-compare) pattern, not a live stale-read window.
6. **Deletions representable**: archived INTEGER column + store archive/restore keyed on source id;
   delete_page/restore_page route through store_write; local reads filter archived=0. Catch-up
   reconciliation: diff local id set vs full Notion id set, tombstone missing, report count. Archive
   gate additionally requires local_row_count == source_read_count.
7. **Importer safety**: every importer aborts if the domain's write flag is already sqlite; final
   catch-up runs under an explicit write freeze (pause scheduled jobs + maintenance notice).
8. **Synthetic ids minted BOT-side at enqueue** ("local-<uuid4>", also chat block ids) and carried
   in the payload; applier upserts on the payload id => crash/retry idempotent.
9. **Read surface built before any flip**: per-domain query layer (type/date filters, Date DESC +
   created_time DESC tiebreak, limit), decrypt-scan focus search in the worker, chat last-activity
   timestamp for get_latest_chat. Importer re-run capturing created/last_edited BEFORE seeding
   (cheap now, unrecoverable after archive). Rule: no domain flips until its full read surface
   shadow-compares clean.

## Ops amendments
10. **Seeding only via a wrapper** that exports AIOS_DATA_DB + AIOS_DATA_DB_GROUP (a bare shell run
    clamps the shared DB to 0700/0600 and takes the data plane down). Harden aios_storage.connect():
    never DOWNGRADE existing perms.
11. **Queue kinds in ONE commit in all four places** (ALLOWED/WRITE/READ_KINDS + proxy INTEGRATION
    _KINDS/dispatch + worker kinds filter) + membership test; explicit TTL on store_read; deploy and
    restart the worker BEFORE any flag flip. Poll deadlines <=12s (hard limit), not 24s.
12. **Loud failures + honest defaults**: store_read failures distinguishable from "empty" (error
    suffix), dead-letter summary includes read kinds, daily owner heartbeat (row counts + quarantine
    count). At each domain's write-flip the code-level default flips 'notion'->'sqlite' in the same
    deploy; Notion-archive gates on the default already being 'sqlite'.

## Slice 1 (staging only; no production flip, no reseal, no archive)
1. Queue plumbing (am. 11). 2. Bot-side id minting + retry-idempotency test (am. 8).
3. Domain store completion: archived col, archive/restore, query layer, destination-aware gate
   (am. 4/6/9). 4. Combined per-domain flag + validity check (am. 5). 5. Importer hardening: wrapper,
   connect() no-downgrade, write-flag abort, reconciliation + count-parity gate (am. 7/10/6).
6. KEK fingerprint pin in get_kek (am. 2 — 3 lines, do first). 7. Importer re-run with
   timestamps on staging (am. 9).
Exit: shadow-compare clean on staging across add/delete/undo/summary paths incl.
read-after-write; kind-registration + idempotency tests green; wrapper is the only seed path.

## Planned for later slices
Reseal ceremony + recipient reduction (am. 1), read-only code mount (3), gate-oracle relocation (2),
heartbeat + default flips (12), the remaining content domains, production flips, Notion archive.

> Boundary: the amendments deferred to later slices (items 1, 2, 3, and 12) are planned but NOT yet
> proven implemented in code. Until each is landed and verified, treat it as an open control and see
> docs/security/threat-model.md for the current boundary.
