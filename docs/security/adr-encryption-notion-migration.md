# Design: Encryption at-rest (scheme 3) + encrypted Notion→VPS migration — FINAL

> Status: **Proposed** (design-only). No code written, no state changed.
> Scope: the AI OS bot in `${AIOS_HOME}/apps/ai-os`.
> Locked decisions are fixed inputs, designed around, never re-litigated.
> This revision incorporates two adversarial review passes (key-loss-and-recovery; partial-migration-corruption; key-leak-and-boundary-break). Every load-bearing code fact below was re-verified against the repo before finalizing (citations inline). The single unifying theme of the rejected-draft weaknesses: **recovery and durability were RECORDED but never PROVEN, and the boundary was defined on the key rather than on the plaintext.** Both are now closed as enforced barriers.

---

# PART A — ADR: Encryption at-rest (scheme 3) + encrypted Notion migration

## Status

**Proposed.** Supersedes the placeholder in `config/secrets-map.yaml` (`planned_future.CONTEXT_STORE_IDENTITY`, lines 60–67) on one point: the working master key is an **on-disk `0600` age identity file**, *not* a machine-account secret-manager secret. This correction is forced by the locked decision and confirmed against `SECURITY.md` §4 ("the encryption master key and the age identity are 'never' class secrets: not in git, not in the secret manager; the working key is a local `0600` file").

## Context

- AI OS stores the operator's most sensitive free-text: chat transcripts, long-term memory facts, and per-domain free-text notes.
- Today that data is **plaintext in Notion** and in some **raw `.jsonl` / `_pc_tasks` history** outside this repo. `SECURITY.md` §4 mandates encrypted-only at-rest writes through a single encryptor; the schema is pre-shaped (`bot/aios_storage.py:140` comment "encrypted at Stage 9 (age/KDB); BLOB stays NULL until then"; `chat_message.content_encrypted BLOB` line 148; the UNIQUE `source_block_id` + `position` columns lines 145, 147 already exist but **nothing populates them today** — see Decision §5).
- The system targets a **single non-technical operator**. Key custody must be operable by the operator with the fewest terminal steps and a single human-readable health signal (`CLAUDE.md`, operator-communication style).
- `telegram-bot` is the most-attacked surface (`secrets-map.yaml` line 6) and must **never** hold the decryption key — and (new, from review) **never see decrypted personal plaintext either**.

## Decision

### 1. Two-layer envelope encryption

**Data plane (per row).** Every personal free-text field is sealed with its own freshly generated **256-bit data key (DEK)** using **ChaCha20-Poly1305** (IETF, 96-bit nonce). Bound as Associated Data (AAD): the row's stable identity (`source_notion_id`/`source_block_id` + domain + field), a 1-byte scheme/version tag, **and the KEK generation id** (new — see §6). So a ciphertext cannot be silently moved to another row, downgraded, or mis-attributed to the wrong KEK generation. Stored BLOB layout per field:

```
version(1) || kek_gen(2) || nonce(12) || wrapped_DEK || nonce_for_content(12) || ciphertext+tag
```

A fresh DEK per row (never per table) gives a one-row blast radius for any nonce mistake or key compromise, and makes per-row crypto-erase (delete the wrapped DEK) trivial for Block -1.

**Key plane.** All per-row DEKs are wrapped under one long-lived 256-bit master key (KEK) with ChaCha20-Poly1305 (AAD = row id + kek_gen). The KEK exists ONLY as an age-encrypted blob `kek.age`, produced by:

```
age -r <vps_recipient> -r <recovery_recipient>
```

ChaCha20-Poly1305 is the exact AEAD the age spec standardizes on (C2SP age v1: file-key wrap and STREAM payload), so the system rests on one well-reviewed primitive rather than mixing AES-GCM.

### 2. Key custody — the two age recipients

`kek.age` contains two independent X25519 stanzas wrapping the same file key; the payload is encrypted once, so adding the recovery recipient is non-destructive (C2SP age v1). Either identity alone recovers the KEK.

| Key | Type | Where it lives | Who reads it |
|---|---|---|---|
| VPS working identity | age X25519 | `${AIOS_WORKER_HOME}/.ai-os/keys/context-vps.identity`, file `0600`, dir `0700`, owner **`ai-os-worker`** (see §7 — NOT the bot's user) | claude-worker / context service ONLY |
| `kek.age` (two-recipient KEK) | ciphertext | next to the encrypted store, `0600`, owner `ai-os-worker`; **≥2 generations kept on-disk + 1 off-VPS cold backup** (§6) | claude-worker |
| Per-row DEK-wrap + row ciphertext | ciphertext | encrypted-store SQLite (`*_encrypted` BLOB columns) | claude-worker |
| **Offline recovery identity** | age X25519 | **air-gapped with owner, ≥2 copies (paper + offline USB), NEVER on VPS/cloud**; each copy proven to decrypt a canary at creation (§Part B owner steps) | owner only, on the air-gapped machine |
| telegram-bot | — | receives NONE of the above; runs as a **separate unix user** that cannot read the key file (§7) | — (hard barrier) |

The recovery PUBLIC recipient (`age1…`, a non-secret) is **pinned as a git-tracked constant** so the two-recipient assertion has an immutable oracle an attacker/bug cannot edit alongside `kek.age` (fix from review — closes the circular-check hole). File-mode discipline mirrors the repo's local-only Claude-credential pattern (`secrets-map.yaml`, on_disk_cache.excluded line 98).

### 3. Implementation — hybrid CLI + library

| Operation | Tool | Why |
|---|---|---|
| KEK envelope: keygen, `age -r -r` wrap, `age -d` recover (rare, owner-gated) | **age/rage CLI** (shell out) | matches the repo's CLI-shelling pattern (`bot/secrets_loader.py` shells out to `bws`); keeps the asymmetric step in the audited reference implementation |
| Per-row content AEAD + DEK wrapping (high volume) | **in-process `cryptography.ChaCha20Poly1305`** | shelling out per row = process-spawn + plaintext-on-argv disaster; `cryptography` is a mature audited binding |

Pin the age binary version, verify checksum/signature on install, resolve by absolute path (same hygiene as `bws`). pyrage is an acceptable library alternative for the KEK step.

### 4. Key lifecycle (every kek.age-regenerating op is backup-first + proven)

| Operation | Trigger | What changes | Post-condition asserted in code |
|---|---|---|---|
| **Generate** (one-time, owner-gated) | initial setup | `age-keygen` → VPS identity (`0600`) + recovery identity (on the operator's own machine; private half NEVER to VPS — only recovery PUBLIC recipient comes to VPS). KEK = CSPRNG 32 bytes in worker memory → `age -r vps_pub -r recovery_pub` → `kek.age`; wipe plaintext KEK from memory | `kek.age` decodes to exactly {vps_pub, **git-pinned** recovery_pub}; **owner has performed one live air-gapped decrypt of this kek.age** (sets `recovery_verified_ts` + recovery key fingerprint — §6) |
| **DEK rotation** | per-row crypto-erase; re-seal a row whose AEAD failed | new DEK + nonce, rewrap under SAME KEK | cheap, frequent-safe |
| **KEK rotation** | suspected VPS compromise; recovery-key change | **staged**: produce `kek.age'` for NEW generation; re-wrap each per-row DEK old→new **idempotently, crash-resumable** (kek_gen tag in every envelope makes a half-finished rotation detectable + resumable); retire old `kek.age` ONLY after a full decrypt-self-test of the whole store under the new generation passes | both recipients' identities test-decrypt `kek.age'` (recovery = owner-gated air-gapped proof) BEFORE the live blob is replaced; prior generation retained (§6) |
| **VPS-identity rotation** | replacing the VPS box | new VPS age identity → re-run `age -r new_vps_pub -r recovery_pub` on the SAME decrypted KEK → new `kek.age`; nothing else changes | same two-recipient + retain-prior post-conditions; refuse to overwrite live `kek.age` until the fresh blob is test-decryptable by both |
| **Recovery** | VPS working key lost/destroyed | on air-gapped machine `age -d -i recovery.identity kek.age` → KEK in memory; provision fresh VPS identity; re-wrap KEK to (new_vps_pub, recovery_pub); ship only new `kek.age` + new VPS identity to rebuilt VPS. Row ciphertext + DEK-wraps recovered untouched | the recovery path itself is the proof; it was already exercised once at Generate-time and at each Block -1 gate |

Rotation is **event-driven, not timer-driven** (matches the repo's rotate-on-suspicion philosophy, `docs/security/secrets-management.md`), avoiding the "forgot to renew → system dies" class. **Every operation that regenerates `kek.age` is gated on: (a) the freshly produced blob test-decrypts under BOTH recipients at that moment — for the recovery recipient this is the owner-gated air-gapped proof, not a string match; (b) the prior `kek.age` is retained until the new one is proven.** (Fix from review — closes the silent-recovery-drop hole for the routine case.)

### 5. Boundary: KEY never crosses AND PLAINTEXT never crosses (hardened)

The original draft guaranteed only "the key never crosses to telegram-bot". Review showed three ways personal **plaintext** still reached the bot, all confirmed in code. The boundary is now defined on **plaintext**, enforced by **OS-level user isolation**, not by a same-user honor system.

**(a) Separate unix users (load-bearing control).** `telegram-bot` runs as user `ai-os-bot`; `claude-worker`/context-service runs as `ai-os-worker`. The `0600` key file and the encrypted store are owned by `ai-os-worker` → kernel-unreadable by the bot. This is the real barrier; everything below is defense-in-depth.
*Why this is necessary:* the whole architecture today runs services as one user (`CLAUDE.md`, `secrets-map.yaml`), so a `0600` `ai-os`-owned file is readable by EVERY `ai-os` process including the bot — barrier #4 ("0600 + right owner") would not exclude the bot at all. And because the age identity is a **file**, the secret-manager allowed_secrets allowlist never gates file reads. Only filesystem ownership under a distinct user does.

**(b) Decrypted answers are delivered to Telegram by the worker, never handed back through the shared DB.** The draft proposed routing decrypt over the existing `aios_request_queue`-style RPC — but `result_json` lives in the same `aios_storage` SQLite file the bot opens (`bot/aios_storage.py:257`, `request_queue.result_json`), and the bot-side reader deserializes it directly (`bot/aios_request_queue.py raw_result = row["result_json"]`). The existing `_scan_for_secret_keys` guard (line 298) blocks secret-shaped **keys**, not personal **values** — zero protection here. **Resolution:** the worker/context-service that holds the key assembles the answer and sends the Telegram message for that path itself; the telegram-bot process is a dumb transport that never reads personal rows. Invariant: `result_json`/`payload_json` for any context-decrypt kind is **forbidden from carrying personal free-text**, enforced by a dedicated value-free result schema + a chokepoint test that round-trips a `SECRETZEBRA` marker and asserts it never appears in any column the bot process reads.

**(c) The startup invariant moves to the real entrypoint.** The draft put it in `bot/telegram_bot.py` — confirmed a **24-line lazy delegator** that loads nothing and only calls `bot.main.main()` (verified). An assert there protects nothing. It moves into `bot/main.py`'s real startup path (and any handler-process entrypoint). It asserts the actual leak conditions, not a deployment-config promise: (i) the identity FILE path is not `os.access`-readable by this process (true under separate users), and (ii) the crypto/context module is not imported in the bot process. Note `get_secret` resolves `_from_env` FIRST (`bot/secrets_loader.py:128`), so a `get_secret(...) is None` assert alone only proves the var is absent from THIS process's env — necessary but not the barrier; the file-unreadable check under a separate user is.

The bot still requests decryption over an RPC, but through a **dedicated request kind** whose resolver does NOT inherit the request queue's "permissive None on parse error" deserialization (`bot/aios_request_queue.py`). That fail-soft path is structurally silent and violates `SECURITY.md` §3 "never silent". The context-decrypt path distinguishes three states that are never collapsed to `None`: **pending** / **decrypt-failed (`DecryptQuarantine`)** / **legitimately-empty**. The caller MUST check an explicit status/`error_class` and raise on failure.

### 6. The single `kek.age` is no longer a single point of total loss

The draft chained every row through ONE `kek.age` with no versioning, no retention, and a destructive whole-store KEK rotation. Hardened:

- **Never exactly one live copy.** Keep current `kek.age` + the immediately-previous generation (both `0600`), plus the mandatory **off-VPS cold backup** (below). Write `kek.age` atomically: temp + `fsync` + `rename`, never in-place.
- **KEK generation id** is recorded in the `kek.age` sidecar AND in every per-row envelope header + AAD, so a row decrypts under whichever generation wrapped it; a half-finished rotation is detectable and resumable (mirrors the importer's `source_notion_id`/`content_hash` resume discipline).
- **Recovery is PROVEN, not recorded.** Redefine "Recovery: armed" → **"the offline recovery private key has actually decrypted THIS `kek.age` and reached real wrapped data at least once"**, persisting `recovery_verified_ts` + recovery key fingerprint. The two-recipient string check is **necessary but NOT sufficient** — the age spec confirms a wrong/typo'd recovery recipient yields a valid stanza no private key can ever open, so a single mis-transcribed character at keygen would otherwise pass every barrier with a green light while being permanently unopenable.
- **HARD-GATE Block -1 on `recovery_verified_ts`** (see Part B). No Notion archive and no `.jsonl`/history destruction for ANY row until recovery is proven for the current `kek.age`. This keeps the locked "cleanup together with migration" — it is still done together, merely ordered strictly behind a demonstrated (not assumed) recovery.

### 7. Decryption boundary — enforced as code

- `config/secrets-map.yaml`: `telegram-bot.allowed_secrets` = `TELEGRAM_BOT_TOKEN` only; `CONTEXT_STORE_IDENTITY` only under `planned_future` with `decryption_boundary: "telegram-bot NEVER receives this key"` (lines 60–67); also added to `on_disk_cache.excluded` (line 98) so the policy file matches code.
- **OS isolation** (§5a) — the kernel-enforced barrier.
- **Startup invariant** in `bot/main.py` (§5c) — fail-closed (`SystemExit`) if the identity is resolvable in-process or the crypto module is imported.
- **The age identity never flows through `get_secret`** — it is resolved only by a dedicated file-read in the context module, never via `secrets_loader`/the secret manager/disk-cache. Belt-and-suspenders: `CONTEXT_STORE_IDENTITY` in `NO_DISK_CACHE` (`bot/secrets_loader.py:34`) + a test asserting `get_secret` is never called with the identity name.

### 8. Failure modes (fail-closed / quarantine)

Per `SECURITY.md` §3 ("decryption failed -> quarantine + warning, never silent") and `memory-safety.md` Block -1B:

| Failure | Behavior |
|---|---|
| Row AEAD / DEK-unwrap failure | mark row in a **dedicated value-free `decrypt_quarantine` table** (NOT `unrouted_inbox` — see below); do NOT serve; do NOT fall back to plaintext; emit a value-free owner warning (row id + exception **class** name only, capped via the existing `MAX_ERROR_CLASS_LEN` machinery); raise typed `DecryptQuarantine` so callers cannot treat it as empty/success |
| KEK age-decrypt failure at startup (corrupt `kek.age`, wrong/missing/wrong-mode VPS identity) | context/claude-worker REFUSES to start (startup invariant), same posture `SECURITY.md` mandates for missing `AIOS_SECRETS_BACKEND` |
| Any encrypt path | single-encryptor invariant → there is NO plaintext-write code path to fall through to; the one writer writes ciphertext or raises |

**Dedicated quarantine table, not `unrouted_inbox`.** The draft routed crypto dead-letters into `unrouted_inbox.raw_message`, which is a TEXT column explicitly documented "Holds raw user text → sensitive tier" (`bot/aios_storage.py:154–161`) and owned by a different writer. A dedicated `decrypt_quarantine` table has **no column capable of holding ciphertext/plaintext/keys** — only `row_id` (FK), `domain`, `field`, `error_class`, `ts`. The value-free property is enforced at the **schema level** (mirroring `shadow_compare_log`'s "deliberately no leak-surface columns by design", lines 208–229), not a code-comment promise.

### Consequences

**Positive.**
- One reviewed primitive (ChaCha20-Poly1305) end to end; reference-grade key custody where it matters (the one `kek.age`); fast leak-free symmetric crypto where volume matters.
- Single-owner disaster recovery with zero shared-secret exposure, no Shamir/quorum complexity.
- Per-row crypto-erase makes Block -1 cleanup trivial.
- Routine ops never touch bulk ciphertext.
- **Recovery is provable, durability is enforced, and the bot is kernel-walled from both the key and the plaintext** — the three things the first draft only promised.

**Costs / risks accepted.**
- Recovery depends on the air-gapped private key never touching the VPS — an operator-safe runbook with **per-copy decrypt proof + periodic re-test** is mandatory (Part B).
- Loss of all recovery copies = permanent loss → ≥2 copies in two physical places, ≥1 on paper, each independently proven, re-tested annually.
- Separate unix users add a small ops cost (two systemd `User=`); justified — it converts the boundary from honor-system to kernel-enforced.

**Operator operability.** The operator deals with exactly **two artifacts**: (1) a working key on the VPS that is never touched by hand; (2) an offline recovery key created once on the operator's own machine, proven to decrypt a canary, printed/copied to paper + USB, stored in two safe places. The bot reports **two distinct lights** — **"Encryption: healthy"** (the live path works) and **"Recovery: proven · last re-test <date>"** (the offline path has actually opened `kek.age` and reached real data) — and proactively warns (degraded mode, mirroring `cache-fallback-warning`) on any break, with NO secret values and NO personal content (COUNTS ONLY).

---

# PART B — BUILD PLAN

## Phase ordering

| Phase | Name | Gate to next |
|---|---|---|
| 0 | Crypto layer + key generation (owner-gated keygen) + **two-user systemd split** | round-trip unit tests green; `kek.age` has TWO recipients incl. **git-pinned** recovery_pub; **owner air-gapped decrypt proof recorded (`recovery_verified_ts` set)**; bot user cannot stat the key file |
| 1 | Single-encryptor chokepoint module + chokepoint test | marker-absence test green (incl. RPC round-trip `SECRETZEBRA` never reaches a bot-readable column) |
| 2 | Schema extension (`*_encrypted` BLOBs + `decrypt_quarantine` + per-row migration-state) | additive ALTER + SCHEMA_VERSION bump verified |
| 3 | Per-domain importers (dry-run / staging only) | counts-only summary, no Notion writes |
| 4 | read→encrypt→**durable-commit**→verify→archive flow (prod cutover, per domain, **writes frozen per domain**) | per-row verify (fresh-connection decrypt-and-compare) before any archive; page-atomic for block domains |
| 5 | Block -1 cleanup interleaved with Phase 4, **HARD-GATED behind `recovery_verified_ts` + off-VPS cold backup** | owner sign-off on each destructive step |
| 6 | Barriers as code + owner status/recovery docs | all barrier tests green |

## Crypto layer module(s)

**`bot/context_store.py`** — the single new module and the ONLY writer of personal free-text into any `*_encrypted` BLOB. Confirmed it does **not** yet exist. Public surface:

- `encrypt_and_store(domain, row_id, field, plaintext) -> envelope` — generate per-row DEK, AEAD-encrypt with AAD=`(row_id||domain||field||version||kek_gen)`, wrap DEK under the current KEK generation, return a self-describing envelope. The ONLY function that writes personal content.
- `decrypt_field(envelope) -> plaintext` — fail-closed; on any AEAD/MAC failure → `decrypt_quarantine` row + raise `DecryptQuarantine`. Three explicit states (pending/failed/empty), never collapsed to `None`.
- KEK lifecycle helpers (`init_kek`, `rewrap_kek`, `rotate_vps_identity`, `recover_kek`) shell out to the pinned `age` CLI for the envelope step only; each asserts the §4 post-conditions (both-recipient test-decrypt, retain-prior, atomic write).
- In-process per-row AEAD via `cryptography.ChaCha20Poly1305`.

## Per-domain importers

One importer per domain `bot/aios_<domain>_import.py`, each mirroring the four pure pieces of the ONE reference importer, with encrypt inserted between map and write. The domains fall into three shapes:

| Shape | Storage module | Free-text → encrypted | Structured → cleartext |
|---|---|---|---|
| Free-text notes domain (Notion DB id from env `<NOTION_DB_ID>`) | `aios_notes_store.py` | note body | date/type/tags/numeric fields |
| Structured tracker domain (Notion DB id from env `<NOTION_DB_ID>`) | domain `*_storage.py` | (optional free-text field) | enums/numbers/checkboxes |
| Chat transcripts (Notion DB id from env `<NOTION_DB_ID>`) | `claude_storage.py` | transcript blocks (two-level: query DB → GET `/v1/blocks/{page}/children`), title | status |
| Long-term memory (Notion page id from env) | `claude_storage.py` | paragraph blocks (long-term memory facts) | — |
| Local-only records (a local SQLite DB, **no Notion**) | domain `*_storage.py` | free-text fields | structured fields |

A static catalog domain (a read-only reference table) holds NO personal data → **out of scope**.

Each importer keeps the reference contract: read-only cursor pagination (`page_size 100`, `start_cursor`/`has_more`), pure `map_page()` (returns None on missing required fields), `content_hash()` over **source plaintext computed BEFORE encryption** (deterministic, nonce-independent) — used **only for change-detection within an already-identified row, NEVER as an identity/dedup key** (see chat-identity fix), idempotent upsert, **COUNTS-ONLY** summary. Structured-vs-free-text split per Block -1C so trackers/summaries keep working on cleartext.

### Chat-message identity (critical fix — the highest-volume, most-sensitive domain)

The draft's idempotency key was `source_block_id`, but the only existing chat reader **discards** it: `bot/claude_storage.py:load_chat_messages` (lines 118–128) reads `block["type"]`, `callout`, `rich_text` and never keeps `block["id"]`. On crash-and-resume the importer cannot match re-fetched blocks to already-encrypted rows → **duplicate** messages; or, if it dedups on content hash, two identical lines ("ok", "yes") collapse → **lost** message.

**Required before any chat cutover:**
- Capture `block["id"]` in the migration reader and persist it into `chat_message.source_block_id`, plus a 0-based `position` per page (columns already exist, `aios_storage.py:145,147`).
- Idempotency key = **(chat page id, source_block_id)** — unique even when text is identical. `content_hash` is change-detection only.
- A fetched block lacking a stable id → **quarantine the whole page, never archive** (hard precondition).
- Verify by decrypt-and-compare keyed on (page id, block id) AND assert the per-page block **count** and ordered **position** sequence match the source before archiving the page.

## The read → encrypt → durable-commit → verify → archive flow (per record)

1. **READ** page (or blocks) via read-only Notion query/GET.
2. **MAP** to a normalized record; missing required fields → count error, do NOT touch Notion.
3. **content_hash** over source plaintext (change-detection only).
4. **ENCRYPT** each personal field through `context_store.encrypt_and_store` (per-row DEK + AEAD, AAD binds row identity + kek_gen).
5. **WRITE/upsert** into the foundation SQLite table inside an **explicit `BEGIN IMMEDIATE` … `COMMIT`** so the encrypted row + its migration-state flag commit atomically. (The connection is autocommit `isolation_level=None`, `bot/aios_storage.py:317`, with `synchronous=NORMAL` line 323 — neither atomic nor fsync-on-commit; the importer must force `PRAGMA synchronous=FULL` or `wal_checkpoint(FULL)` so the encrypted copy is on disk before any archive.)
6. **VERIFY** through a **FRESH connection AFTER commit** (proving durability, not in-memory state): decrypt every personal BLOB, **byte-compare** decrypted plaintext against the in-memory source, AND assert structured fields + `content_hash` + (for block domains) count/position match. Verify by decrypt-and-compare on a durable read — this is what makes archiving the source safe.
7. **Only on a fully passing verify of a durably-committed row** → archive the Notion original via the store module's Notion archive helper. **Note:** that helper PATCHes `{"archived": true}` against a `Notion-Version` constant — the **deprecated** parameter; before relying on archive-as-reversible, empirically confirm `restore_page` works on the live API version and pin/upgrade to `in_trash` if required (open question 2). For MEMORY, archive the whole page (per-block `DELETE` is irreversible) — and only after the read-source flip (below).
8. **On ANY failure** → fail-closed: quarantine, leave the Notion original untouched, value-free warning, NEVER archive on doubt.

Strict ordering so a network side-effect never becomes the source of truth: **(1) commit encrypted row + state='verified' in one local transaction → (2) THEN call Notion archive → (3) commit state='archived' + manifest entry.** On resume, a row at state='verified' with the Notion page still live is the safe re-runnable state (`PATCH archived:true` twice is harmless). Never derive "already done" from an un-committed network side-effect.

No plaintext temp files at any step (`SECURITY.md` §4: "read -> encrypt -> verify -> delete the original; no open temp files").

### Block-structured domains (chat, memory) — page-atomic archive

A page is archivable ONLY if EVERY child block mapped, encrypted, verified, AND stored block count == source block count. Any single block error quarantines the whole page and leaves it un-archived. Counts-only summary distinguishes "pages fully verified" from "pages with any errored block". A 99/100 page is NOT "verified enough".

### Single-page domains (memory) — read-source ordering

CLAUDE-MEMORY is one page of paragraph blocks; archiving is all-or-nothing and the live bot reads it at runtime (`load_memory`). Sequence: migrate+verify ALL facts → **flip `read_source.memory` to sqlite** (bot now reads the encrypted store) → ONLY THEN archive the Notion page. Never archive while any reader still points at Notion.

## Idempotency, dry-run, resume

- **Resume reads ONLY the committed encrypted-store row state machine** (`migrated`/`archived_ts` flags), never `audit_log`. `audit_log` (`bot/aios_storage.py:164`) stays advisory/observability-only — it and the row state are separate autocommit statements and can diverge; if both must update, write them in the same explicit transaction.
- Per-row state machine `exported→encrypted→verified→archived`; a crash re-runs from where it stopped, zero duplication. Strict ordering: never archive before a passing verify of the **durably-committed** local row.
- **run_id** per invocation + stable **correlation_id** per record (`domain+source id`) in the value-free `audit_log` (actor/action/domain/entity_id/outcome/detail, redacted).
- **Dry-run vs prod:** the reference importer's two-DB split — dry-run writes to STAGING (`AIOS_STAGING_DB`, `0700`, outside repo) and NEVER archives Notion; prod writes to `~/.ai-os/data/aios.sqlite3` and only it may archive. A `read_source.<domain>` flag (`notion|sqlite|dual`, `app_config`) lets each domain cut over independently after its verify passes.
- **Freeze writes per domain during its cutover.** Notion pagination is not snapshot-consistent; a concurrent edit can skip or duplicate a row across a page boundary (Block -1C gates only NEW raw writes; structured writes continue). Put the domain's handlers into read-only/queue mode (or use a maintenance window) for its cutover, and run a **final reconciliation pass**: re-scan `last_edited_time > migration-start` on already-archived pages and re-migrate any post-verify edits (`notion_last_edited` is already mapped).
- **Notion rate limits** (`developers.notion.com`): token-bucket ~3 req/s, exponential backoff on HTTP 429 (do NOT rely on `Retry-After`), page reads at the 100-row max.

## Schema extension

Extend the EXISTING `bot/aios_storage.py` foundation tables (additive `ALTER TABLE ADD COLUMN` guarded by `PRAGMA table_info`, with a `SCHEMA_VERSION` bump — the file's documented migration discipline, lines 26–32, 273–296). Reuse `chat_message.content_encrypted` (line 148) and populate the unused `source_block_id`/`position` (145,147). Add `*_encrypted` BLOBs for each domain's free-text fields (e.g. `chat.title_encrypted`, a notes domain's `body_encrypted`, and the free-text columns of any local-only records table). Purely structured tables stay cleartext. Add per-row migration-state columns (`migrated`, `archived_ts`). Add the dedicated **`decrypt_quarantine`** table (value-free schema, §A.8). Each BLOB is a self-describing envelope (alg/version + kek_gen + nonce + wrapped DEK + ciphertext) to support generationed rotation.

### Local-only records domain — its own atomic migration (second destructive migration, guarded)

A local-only records domain has NO Notion source and NO migration scaffolding: its storage module has no `source_notion_id`/`content_hash`/state, and it may carry live foreign keys between its tables. A SQLite `DROP COLUMN` requires a table rebuild; a crash mid-rebuild can corrupt the DB or orphan referencing rows.

**Required:** treat such a domain as its own atomic migration. (1) Copy the DB file first (backup-before-change). (2) Encrypt in place as **additive `*_encrypted` columns ONLY**. (3) Verify by in-transaction decrypt-compare against the pre-encryption cleartext read within one transaction. (4) **NEVER drop the cleartext column as part of the migration** — defer to a separate, backed-up, FK-checked table rebuild after full verification, with `PRAGMA foreign_key_check` before and after. Add `source`-style migration state so a crash is resumable. (Open question 6: confirm the brief dual-column clear+encrypted window before any later drop.)

## Enforced barriers (each with its exact test/hook/startup-check)

| # | Barrier | Enforcement |
|---|---|---|
| 1 | **Single encryptor** — only `bot/context_store.py` writes personal free-text, only into `*_encrypted` BLOBs | (a) **marker test** (a chokepoint test modeled on `scripts/test_notion_raw_gate.py`, the `SECRETZEBRA` idiom): insert a marker through every personal-write path AND through a decrypt-RPC round trip, assert it NEVER appears in any TEXT column or any column the **bot process** reads, only inside the opaque ciphertext BLOB, and `decrypt(envelope)==plaintext` round-trips. (b) **structural lint** = new numbered check in `scripts/vps_spec_guard.sh` that flags any import of the crypto binding (`cryptography.ChaCha20Poly1305` / pyrage / subprocess `age`) outside `context_store.py` AND any `INSERT`/`UPDATE` of a `*_encrypted` column elsewhere. **Documented as a tripwire, not the load-bearing control** — the real barrier for "only context_store decrypts" is user isolation (#4): if the bot user cannot read the key file, clever importing in bot code cannot help. Run it in CI/pre-push, not only locally. |
| 2 | **telegram-bot never holds the key OR the plaintext** | (a) config: `secrets-map.yaml` telegram-bot allowed = `TELEGRAM_BOT_TOKEN` only. (b) static guard: `vps_spec_guard.sh` check 6 (greps `systemd/aios-telegram-bot.service` for `CONTEXT_STORE_IDENTITY\|CONTEXT_KDB_MASTER_KEY`, verified lines 69–88) **plus a new check asserting the two services run as DIFFERENT `User=`** and the key path is not group/other readable. (c) **startup invariant in `bot/main.py`** (NOT the `telegram_bot.py` delegator): `SystemExit` if the identity file is `os.access`-readable in-process or the crypto module is imported; a guard check asserts that assertion exists in `main.py`. (d) decrypted answers delivered to Telegram by the worker, never via shared `result_json`. |
| 3 | **Decrypt failure → quarantine + warn, never silent** | `context_store.decrypt_field` try/except → write a value-free reference (row id + exception **class** name only) into the dedicated **`decrypt_quarantine`** table (NOT `unrouted_inbox`), warn owner, re-raise `DecryptQuarantine`. The context-decrypt RPC does NOT inherit the request queue's permissive-None deserialization (`aios_request_queue.py`); pending/failed/empty are three distinct states. **Test** (a decrypt-quarantine test): corrupted envelope → (i) raised/quarantined, (ii) dead-letter row created, (iii) corrupted RPC result → caller raises, never returns None-as-empty, (iv) marker-absence: no plaintext/no ciphertext bytes/no key bytes in the row or any log capture. |
| 4 | **Key file is `0600`, owned by `ai-os-worker`, UNREADABLE by the bot user** | startup check in the worker `os.stat()`s the working-key path, hard-exits unless `mode==0600` and `owner==ai-os-worker`. **Test** (a key-permissions test): 0644 → rejected; 0600 owned by worker → passes; **and asserts the telegram-bot runtime user CANNOT stat/open the key** (the necessary-and-sufficient condition under separate users; 0600 alone is necessary but not sufficient). |
| 5 | **Secret-scan stays green pre-commit/pre-push** | build the git secret-scan (`SECURITY.md` §3 marks it TODO): a git-secret-scan script runs the staged diff through `bot/log_redaction.scan_counts` (engine behind `aios_log_scan.py`, exit 0 clean / 1 match, never prints values) AND blocks committing any path matching the `.gitignore` key/credential patterns (incl. the `AGE-SECRET-KEY-` form and the identity file). Wire **two ways**: a real git pre-commit + pre-push hook AND the `claude/settings.json` `PreToolUse` slot. **Test** (a git-secret-scan test): secret-shaped diff → exit≠0; clean diff → exit==0. Runnable as a CI/cron ratchet to catch `--no-verify` bypass. |
| 6 | **Recovery is PROVEN, not merely armed** | startup invariant fails closed if `kek.age` is NOT encrypted to BOTH recipients where the recovery recipient == the **git-pinned non-secret `age1…` constant** (immutable oracle an attacker/bug cannot edit alongside `kek.age`). **But the string check is necessary, not sufficient:** surface the status line **"Recovery: proven"** only when `recovery_verified_ts` is set for the current `kek.age` — i.e. the offline private key has actually decrypted THIS blob and reached real wrapped data. A canary self-test at startup proves the WORKING key works; it does NOT prove recovery — keep both lights separate. **Block -1 destruction is hard-gated on `recovery_verified_ts` + an off-VPS cold backup that itself passed a recovery-key decrypt test.** |

> Note on bash: guard checks must avoid heredoc-in-command-substitution (the macOS bash 3.2 `$(...)`-with-`)` bug) — write checks plainly.

## Block -1 cleanup interleaving (hard-gated behind proven recovery)

Block -1 (mandatory, **together** with migration, per-record, never big-bang). **Two plaintext stores, two mechanisms, different risk — never share a code path. Nothing destructive runs until `recovery_verified_ts` is set for the current `kek.age` AND an off-VPS cold backup of the encrypted store + `kek.age` exists and has passed a recovery-key decrypt test.**

1. **Live Notion DBs** — cleaned **per-record inside Phase 4**: archive fires only after that record's encrypted copy is verified (decrypt-and-compare on a durable read). **Do NOT rely on Notion's archive bin as the rollback store** — `developers.notion.com` specifies no retention/auto-purge duration; the "~30-day window" in the draft was invented and Notion's auto-purge fires on a clock the owner does not control. Instead, keep a **separate owner-controlled non-expiring cold snapshot** (a full encrypted export of each source page, same envelope) before archiving — OR keep the Notion page un-archived until a full per-domain end-to-end re-decrypt audit passes. Add a startup/cron check that fails loudly if any page is near Notion auto-purge while its domain audit is unsigned. Empirically confirm `restore_page` works on the live API version and pin `in_trash` if `archived` is deprecated there. Final permanent purge is a **separate owner-gated decision**, only after full end-to-end verification.
2. **Git-history / `.jsonl`** — scoped by verified inspection:
   - **(A) This code repo** — run a read-only `git log --all` inventory for `.jsonl`/`_pc_tasks` bodies at build time; if clean, **no history rewrite**, keep `notion_raw_allowed()` OFF + a forward ratchet guard that FAILS if new plaintext `.jsonl`/`_pc_tasks` bodies are ever added.
   - **(B) The bridge repo (`<YOUR_ORG>/<bridge-repo>`)** — the history-rewrite target (`bot/pc_tasks.py` pushes `_pc_tasks/<id>.md` bodies). Scrub via `git-filter-repo --invert-paths` on the `_pc_tasks/` prefix (path-based, no content regex), after a `clone --mirror` backup, rehearsed on a throwaway mirror first. Delete working-tree copies too, not just history blobs.
   - **(C) VPS-local raw Claude `.jsonl`** (outside any tracked repo) — file deletion + secure-overwrite. Requires a read-only VPS inventory (`find ~ -name '*.jsonl'`) to size.

New plaintext is already stemmed (`notion_raw_allowed()` default OFF, confirmed across `claude_storage.py` save_messages/append_memory/create_chat), so Block -1 is pure backlog drainage.

## OWNER-ONLY STEPS (sudo / offline-key handling)

Each gated by `CLAUDE.md` approval policy / `SECURITY.md` §1.2; none can be done by the bot or code.

1. **Generate the offline recovery key** — on the operator's **own machine (NOT the VPS)**, walked one line at a time:
   - `age-keygen -o recovery-key.txt` (writes private `AGE-SECRET-KEY-1…`, prints public `age1…`)
   - `age-keygen -y recovery-key.txt` (prints ONLY the public recipient, value-free)
   The bot records only the public `age1…` recipient — **and pins it as a git-tracked constant**, not in a mutable store (closes the circular-oracle hole).
2. **Do not trust a hand-read-back recipient — prove it round-trips.** Before any real data is encrypted, the owner performs **one live decrypt of the real `kek.age` with the actual offline recovery identity on the air-gapped machine**, and feeds back a small proof (a hash of the recovered KEK / a canary sealed under the recovered KEK) that the VPS verifies equals the working-key result. Only then is `recovery_verified_ts` + the recovery key fingerprint persisted. A single mis-transcribed recovery character otherwise produces a valid stanza no key can ever open, passing every string check — this step is what makes "Recovery: proven" real.
3. **Make physical copies AND prove each one independently.** Print `recovery-key.txt` on paper AND copy to one offline USB; keep **≥2 copies in two different physical places**, ≥1 on paper. For EACH copy: read it back → on the air-gapped machine decrypt the canary → confirm, at creation time (not "just make 2 copies" — copies can share a transcription error). Record the recovery key **fingerprint** (non-secret) so any future copy can be checked back to the right key without exposing the private half. Prefer a transcription-robust paper format (a checksummed mnemonic/paperkey of the age identity) over a raw string a human must retype perfectly. Label each: "AI OS recovery key — keep offline — never type into an online computer — created <date>". Then wipe `recovery-key.txt` from that machine.
4. **The four hard NEVERs** (backed by barrier #5, not a promise): never put the recovery **private** key on the VPS/cloud; never paste it into Telegram/any bot message; never store it in the secret manager (`forbidden_everywhere`, `secrets-map.yaml`); never type it on an online computer.
5. **Sudo on the VPS** — provisioning the `0600` working-key file under owner **`ai-os-worker`**, dir `0700`, AND creating the separate **`ai-os-bot`** unix user + setting the two systemd `User=` directives, done by the owner/operator over ssh, not by the bot.
6. **Recovery drill** (rehearse ONCE on a throwaway envelope first, then the real one per step 2): retrieve a recovery copy → on the air-gapped machine `age -d -i recovery.identity kek.age` → KEK in memory → provision fresh VPS identity → re-wrap KEK to (new_vps_pub, recovery_pub) → ship only the new `kek.age` + new VPS identity to the rebuilt VPS (read→use→verify→DELETE temp, no plaintext temp files). The recovery private key never touches the VPS.
7. **Periodic re-test** — an owner-gated annual recovery re-test, so a silently-dead USB or degraded paper is found while the encrypted store is still intact, not at the disaster. "Recovery: proven" shows the last re-test date.
8. **Destructive sign-offs** — bridge-repo history rewrite + force-push, and the final Notion permanent purge, each require explicit owner approval, and only after `recovery_verified_ts` + off-VPS cold backup exist.

## Open questions carried into build

1. `secrets-map.yaml` `planned_location` (reuse a machine-account secret manager) **conflicts** with the locked 0600-file decision → correct the placeholder to "on-disk 0600 age identity, never in the secret manager". (Recommendation: correct it.)
2. **Notion archive semantics** — empirically confirm `restore_page` works on the live API version; the Notion archive helper uses the possibly-deprecated `{"archived": …}`. Pin/upgrade to `in_trash` if needed before relying on archive-as-reversible. Do NOT gate any decision on an assumed Notion retention period.
3. Scope of encryption: content only (row ids cleartext as index, bound as AAD) vs row ids also protected (keyed-HMAC ids — adds complexity). Recommendation: content only, unless ids are themselves sensitive.
4. Whether free-text titles / a domain record's "name" are personal (encrypt) or structured (cleartext) — Block -1C already neutralizes chat titles to "Chat <date>" (`claude_storage.py:_neutral_chat_title`).
5. Verify fidelity for free text: compare against the raw exported plaintext we encrypted (recommended) vs a Notion re-fetch (whitespace normalization → false verify-failures).
6. Recovery-key passphrase (`age-keygen | age -p`): stronger if a physical copy is stolen, but a forgotten passphrase = permanent loss for a non-technical operator. Default: **plain** air-gapped key (threat model is VPS compromise; home-safe theft is lower-probability than a forgotten passphrase).
7. Local-only records dual-column window — confirm the brief clear+encrypted window and the FK-checked table rebuild plan before any cleartext-column drop.

---

### Grounding index (repo paths and docs cited; all re-verified this pass)
- `SECURITY.md` §3 (barriers, never-silent), §4 (at-rest rules)
- `bot/aios_storage.py` — `content_encrypted BLOB` (140), `chat_message.source_block_id`/`position` UNIQUE but unused (145,147), `unrouted_inbox` raw-text/sensitive (152–161), `audit_log` value-free (164), `app_config` (176), `shadow_compare_log` "no leak-surface columns by design" (208–229), `request_queue.result_json` (257), `connect()` autocommit `isolation_level=None` (317) + `synchronous=NORMAL` (323), `0600`/`0700` idiom (314,328–333)
- the reference importer (`bot/aios_*_import.py`) — staging-only, dry-run, idempotency (source_notion_id UNIQUE + content_hash), counts-only
- `bot/aios_request_queue.py` — `result_json` shared-channel read, permissive-None deserialization, `_scan_for_secret_keys` blocks keys not values
- `bot/telegram_bot.py` — confirmed 24-line lazy delegator (startup invariant must NOT live here)
- `bot/claude_storage.py` — `load_chat_messages` discards `block["id"]` (118–128); `notion_raw_allowed()` gating in save_messages/append_memory/create_chat
- the Notion archive helper — `delete_page`/`restore_page` PATCH `{"archived": …}`, `Notion-Version` constant (part of the full private system, not included in this public slice)
- `bot/secrets_loader.py` — `get_secret` resolves `_from_env` FIRST, then the secret manager, then disk-cache; `NO_DISK_CACHE` string set (34)
- a local-only records storage module — no `source_notion_id`/`content_hash`/state; live foreign keys between its own tables
- `config/secrets-map.yaml` — telegram-bot allowed (6–13), `planned_future.CONTEXT_STORE_IDENTITY` + `decryption_boundary` (60–67), `forbidden_everywhere` (71), `on_disk_cache.excluded` (98)
- `scripts/vps_spec_guard.sh` — check 6 greps `systemd/*.service` for secret NAMES (69–88); no `User=` separation check today
- `scripts/test_notion_raw_gate.py` (`SECRETZEBRA` marker idiom); `scripts/aios_log_scan.py` + `bot/log_redaction.py`; `bot/config.py` (six `NOTION_*` ids; `notion_raw_allowed()`); `bot/main.py` (real bot entrypoint, 257 lines)
- `docs/security/memory-safety.md` (Block -1A/-1B/-1C); `docs/security/secrets-management.md`; `docs/security/guards-and-hooks.md`; `CLAUDE.md` (approval policy, owner style)
- C2SP age v1 spec (`github.com/C2SP/C2SP/blob/main/age.md` — independent X25519 stanzas; ChaCha20-Poly1305 construction); `github.com/FiloSottile/age`; `github.com/str4d/rage`; `developers.notion.com` (request-limits ~3 req/s, page_size 100, HTTP 429; archive/restore semantics, no specified retention window)
- `bot/context_store.py` — confirmed does NOT yet exist; the central module to build

---

### FINALIZE decision log — adversarial fixes applied vs rejected

**Applied (12 REQUIRED fixes, all verified against code):**
1. **Recovery PROVEN not recorded** (key-loss CRITICAL) — redefined "Recovery: armed" → "Recovery: proven" = offline key actually decrypted THIS `kek.age` and reached real wrapped data; persist `recovery_verified_ts` + fingerprint; owner air-gapped decrypt proof at keygen. **Verified:** age spec confirms a typo'd recipient yields a valid-but-unopenable stanza.
2. **Block -1 hard-gated on proven recovery + off-VPS cold backup** (key-loss CRITICAL/HIGH) — destruction blocked until recovery proven and an independent cold backup exists; keeps locked "together with migration", merely orders it behind proof.
3. **Plaintext boundary via separate unix users** (key-leak CRITICAL/HIGH) — `ai-os-bot` vs `ai-os-worker`; key file kernel-unreadable by the bot. **Verified:** single-user model made 0600 barrier #4 a no-op for excluding the bot.
4. **Decrypted answers delivered by the worker, not via shared `result_json`** (key-leak CRITICAL) — **verified** `result_json` is read by the bot from the shared DB (line 350) and `_scan_for_secret_keys` only blocks keys.
5. **Startup invariant moved to `bot/main.py`** (key-leak CRITICAL) — **verified** `telegram_bot.py` is a 24-line delegator; assert there is inert; `get_secret` is env-first.
6. **Chat-message identity = (page id, block id, position)** (partial-migration CRITICAL) — **verified** `load_chat_messages` discards `block["id"]`; columns exist but are unpopulated.
7. **Explicit transactions + fresh-connection durable verify + `synchronous=FULL`** (partial-migration CRITICAL) — **verified** `isolation_level=None` + `synchronous=NORMAL`.
8. **No reliance on Notion archive bin / the invented "~30-day window"; owner-controlled non-expiring cold snapshot; confirm/pin `in_trash`** (both lenses HIGH) — **verified** code uses deprecated `archived` param; Notion docs specify no retention window.
9. **Single `kek.age` hardened**: ≥2 generations + off-VPS backup, atomic write, kek_gen in every envelope, crash-resumable staged KEK rotation (key-loss HIGH).
10. **Dedicated value-free `decrypt_quarantine` table** instead of `unrouted_inbox.raw_message` (key-leak MEDIUM) — **verified** `unrouted_inbox` is documented raw-text/sensitive.
11. **Context-decrypt RPC does not inherit permissive-None deserialization; pending/failed/empty are distinct** (key-leak HIGH) — **verified** lines 338–354.
12. **Recovery PUBLIC recipient pinned as git constant** (immutable oracle) + **per-copy decrypt proof, transcription-robust paper format, annual re-test, every kek.age-regenerating op is backup-first + both-recipient-proven** (key-loss HIGH/MEDIUM); **age identity kept entirely out of `get_secret`/disk-cache** + grep-guard documented as tripwire, OS isolation as load-bearing (key-leak MEDIUM/LOW).

**Rejected / down-scoped (with reason):**
- **Local-only records same-transaction column-drop:** rejected doing it as part of migration; **kept** the stronger version (additive columns only, defer FK-checked rebuild) — applied, not rejected. **Verified** the foreign keys.
- **Treating "1368 commits / 0 .jsonl" as established fact:** rejected — this is a design-only, read-only task; git was not run, so the figure is marked **to-be-verified at build time** rather than asserted. (Strengthens honesty; the reviewer's underlying point — don't lean on an unverified safety net — is honored.)
- **Mandatory recovery-key passphrase:** rejected as default (open question 6) — for a non-technical operator, a forgotten passphrase is a higher-probability permanent-loss path than home-safe theft; left as an explicit owner-gated option, not a default. This is a threat-model trade-off, not a security weakness.
- No locked decision was re-litigated: scheme 3, verbatim-encrypted Notion→VPS migration, mandatory Block -1 together with migration, the decryption boundary, and "discipline = code" are all preserved; the fixes only add proofs and ordering in front of them.