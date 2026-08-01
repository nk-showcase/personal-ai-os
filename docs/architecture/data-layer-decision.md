# AI OS — Data-Layer Decision (FINAL)

> Status: **DECIDED.** Lead-architect synthesis of code ground-truth, web research, and the 4-member council (CTO/Architect, CISO/Security, SRE, CFO/Pragmatist).
> Scope: the database engine choice for AI OS on a dedicated always-on VPS, in light of (1) the unified-router migration, (2) the locked at-rest encryption ADR, (3) the Notion→VPS migration, (4) the non-developer "must not die" mandate.
> **Snapshot: written at decision time** (like the encryption ADR, this document describes the tree as it stood when the decision was made). Every load-bearing fact below was re-verified against the repo in that session (citations inline). Several items have **since been implemented** — those are marked inline with **Update (current tree):** notes. For the current state see `README.md` and `SECURITY.md`. No secret values appear anywhere in this doc.

---

## 1. Question

**Does AI OS need to change its database engine — move off SQLite to SQLCipher, PostgreSQL, or a hybrid — for the VPS / encryption / unified-router era?**

Plainly: the encryption ADR (`docs/security/adr-encryption-notion-migration.md`) *assumed* SQLite continues but never analyzed the engine choice; the unified-router transport plan (`docs/architecture/s6-thin-transport-plan.md`) *adds* cross-process write traffic that some would reflexively "solve" with a server database. This document settles the engine question on its own merits.

**Answer (decided): NO. Keep SQLite. Stay on it deliberately, with named hardening.** The forcing functions people attribute to "we need Postgres" are, at this scale, either imaginary or are application-logic bugs that no engine swap fixes. Rationale and the explicit trigger-to-revisit are in §5.

---

## 2. Where the data lives today

SQLite only, multiple stdlib `sqlite3` files, all out-of-repo under `~/.ai-os/`, all lazy-created (no file exists until first write). Access pattern is **fresh-connection-per-operation, autocommit (`isolation_level=None`), WAL journal**, with `0700` dir / `0600` file discipline.

| Store | File | Defined in | PRAGMAs set | Write pattern |
|---|---|---|---|---|
| Coding/inbound queue + approvals | `~/.ai-os/queue/tasks.sqlite3` | `bot/task_queue.py:43-47`, conn `:97-121` | WAL, `busy_timeout=5000`, `foreign_keys=ON` (`:110-112`) | bare `INSERT` enqueue (`:139-146`); atomic claim via `BEGIN IMMEDIATE` (`:157-170`) |
| Reply channel (worker→transport) | shares `tasks.sqlite3` | `bot/reply_queue.py:24-28`, `:35-48` | WAL, `busy_timeout=5000` (`:63-64`); **no `foreign_keys`** | bare `INSERT` + `delivered_ts` stamp |
| Durable scheduler | shares `tasks.sqlite3` | `bot/schedule_queue.py:25-29`, `:36-51` | WAL, `busy_timeout=5000` (`:66-67`); **no `foreign_keys`** | bare `INSERT` (`add_scheduled`); claim via `BEGIN IMMEDIATE` |
| Main store (lazy, may not exist yet) | `~/.ai-os/data/aios.sqlite3` (env `AIOS_DATA_DB`) | `bot/aios_storage.py:37-42`, `connect()` `:299-336` | WAL, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL` (`:320-323`); re-chmods `-wal`/`-shm` siblings every connect (`:328-333`) | upserts; importers use explicit `BEGIN IMMEDIATE` |
| Notes — **Update (current tree):** shares the main store | `~/.ai-os/data/aios.sqlite3` via `aios_storage.connect` | `bot/aios_notes_store.py:17,32` | same as main store (WAL, `busy_timeout=5000`, `foreign_keys=ON`) | encrypted upserts via `context_cipher` |
| Import staging | `~/.ai-os/staging/import_staging.sqlite3` | via `aios_storage.connect(db=...)` override (`:300-311`) | same as main store | dry-run import only |
| Reference catalog | static catalog (module part of the full private system, not included in this public slice) | — | — | read-mostly; the ADR marks it personal-data-free, out of scope |

Key schema facts the engine decision rests on: `tasks.task_text` is a plaintext `TEXT NOT NULL` column (`aios_storage`/`task_queue.py:62`); `replies.text` is plaintext `TEXT NOT NULL` (`reply_queue.py:41`); the tasks table's `status`/`priority`/`idempotency_key` are structured queryable columns (`aios_storage.py:83-94`); `audit_log` and `shadow_compare_log` are schema-enforced value-free (`aios_storage.py:164-173`, `:208-229`); `unrouted_inbox.raw_message` is a plaintext `TEXT` raw-user-text column (`aios_storage.py:154-161`).

**Crypto substrate at decision time: none.** `requirements.txt` then listed only `python-telegram-bot[job-queue]`, `python-dotenv`, `httpx`, `openai`, `anthropic`, `claude-agent-sdk` — no `cryptography`/`pynacl`/`age`/`sqlcipher`, and at-rest encryption had zero implementation (matching the `adr` context and the S6 transport plan's inbound-encryption prerequisite). **Update (current tree):** the scheme-3 AEAD has since shipped — `requirements.txt` includes `cryptography`, and `bot/context_cipher.py` + `bot/context_store.py` implement it (free-text encrypted, structured columns cleartext).

**Service isolation at decision time: not real yet.** The transport, coding-worker, and sync units then all ran `User=ai-os`; only the (then-OFF) integrations unit was provisioned with a distinct account. **Update (current tree):** the split is implemented — `aios-telegram-bot.service:12` runs `User=aios-bot`, `aios-integrations-worker.service:40` runs `User=ai-os-integrations`, `aios-claude-worker.service:14` and `aios-sync.service:13` run `User=ai-os` (see §6 and `README.md` §3).

---

## 3. The real forcing functions — honest triage

The reason to change a database engine is a *forcing function the current engine cannot satisfy*. Four are claimed. Only one is real, and it is **not** an engine problem.

### (a) Multi-process write contention under the unified-router — REAL pressure, but NOT an engine forcing function
The router makes transport a catch-all that enqueues every inbound message while the worker writes `reply_queue` + the durable per-chat `conv_state` blob (see `docs/architecture/s6-thin-transport-plan.md`). That multiplies writes onto the single `tasks.sqlite3` file (the redesign estimates ~6 serialized writes per turn). **At this scale WAL absorbs it** — research is unambiguous: SQLite's own site runs ~400-500K requests/day on one server (`sqlite.org/whentouse.html`, `sqlite.org/np1queue.html`), and this is a one-owner bot orders of magnitude below that. The *actual* breaking point the S6 transport plan itself flags (its per-`chat_id` ordering requirement) is **application-level write ordering on the hot `conv_state` row**: a timer firing mid-turn does read-modify-write on the same chat's blob → silent last-writer-wins clobber. **PostgreSQL does not fix this for you** — its MVCC prevents the *lock error*, not the *logical clobber*; you would still have to build the same per-`chat_id` serialization. So this pressure is real but it is solved by a `chat_id` lock in application code, identical work under any engine.

### (b) At-rest encryption — REAL requirement, ENGINE-NEUTRAL
The locked ADR is per-row app-level AEAD (ChaCha20-Poly1305), free-text fields only, structured columns stay cleartext-queryable, KEK wrapped with `age` (`adr:26-41`, `:222`). This is **engine-agnostic by construction** — it operates above the storage layer. Research confirms the alternatives buy nothing here: PostgreSQL has **no native at-rest (TDE) encryption** in the community edition, and its own documentation (`postgresql.org/docs/current/pgcrypto.html`, `…/encryption-options.html`) lands on **client-side AEAD with the key off the server** — which *is* scheme 3. SQLCipher gives whole-file encryption but breaks the ADR's selective-column requirement and widens the key boundary (see §4). **No engine delivers the ADR's column-level boundary for free; the column-level scheme is the requirement.**

### (c) Notion migration volume — IMAGINARY as a forcing function
The migration is thousands of personal rows (chat/memory/notes/tasks), one owner, read-only Notion pagination at the 100-row page max with ~3 req/s rate limits (`adr:218`, `:176`). This is throughput SQLite handles trivially; the migration's hard problems are *idempotency, crash-resume, and per-row crypto-erase* (`adr:188-199`, chat-identity fix `:178-186`) — all of which the existing per-domain importer pattern (`bot/aios_chat_import.py` feeding `bot/aios_migrate.py`) already solves on SQLite. Volume exerts **zero** pressure toward a server engine.

### (d) Non-developer "must not die" ops — REAL, and it pushes HARD toward SQLite, away from a server
This is the owner's #1 stated value, and it is the decisive lens. SQLite disaster recovery is "put the one file back"; the repo already ships a WAL-aware backup (`scripts/aios_backup.py` uses `VACUUM INTO`, `:60`) and a no-terminal restore drill (`scripts/aios_restore_drill.py`). A server engine replaces file-copy restore with `pg_dump`/`pg_restore` and — the real killer — a manual major-version `pg_upgrade` that can strand the data directory unbootable, a failure mode no non-developer recovers from at 3am. This forcing function actively **forbids** adding a daemon.

**Triage verdict:** of four claimed forcing functions, (a) is a real application bug that no engine swap fixes, (b)/(c) are engine-neutral, and (d) is a hard constraint *against* changing engines. **Net pressure toward a new database engine: zero.**

---

## 4. Options compared

Scored 1-5 (higher = better fit for THIS system), summarizing the four council lenses. Where a council claim is wrong against code, the table notes it.

| Option | Architecture (CTO) | Security (CISO) | Reliability (SRE) | Cost-simplicity (CFO) | Net |
|---|---|---|---|---|---|
| **1. SQLite tuned — WAL + busy_timeout + app-level per-field AEAD** (the locked scheme 3) | **5** — already the engine; WAL + `busy_timeout` + `BEGIN IMMEDIATE` claim already correct; AEAD is engine-agnostic, leaves structured cols queryable | **5** — encryption boundary = the *column*; queue/state stay plaintext so transport never holds the data-key; smallest key-exposure surface; *is* the ADR | **5** — one file per store, backup = `VACUUM INTO` file, no daemon to crash, no major-version upgrade event; only failure mode (`database is locked`) is config-fixable | **5** — zero new daemon/RAM; only new dep is `cryptography`; closes the real plaintext leak without an engine swap | **WIN** |
| 2. SQLite + SQLCipher (whole-file) | 2 — whole-file opacity kills selective encryption; every process needs the key just to enqueue; wrong/lost key bricks the whole DB | 2 — file-key means *every* opener (incl. transport) holds the data-key in memory — widens exactly the boundary the ADR shrinks (`adr:80-85`) | 2 — single lost key or one bad page = total loss; worst "must not die" blast radius | 2 — adds a C-extension; default binding `pysqlcipher3` is dead, only `sqlcipher3-binary` alive; breaks cleartext-queryable structured cols | reject |
| 3. PostgreSQL on the VPS | 2 — buys true multi-writer MVCC, the one thing SQLite is weakest at — but this system never hits the switch-trigger, and it leaves the `conv_state` ordering bug unsolved | 2 — no native TDE; `pgcrypto` puts the key on the same box; adds a network socket + second daemon as new attack surface; **still needs scheme 3 anyway** | 1 — persistent daemon (a second thing that dies), ~17-step manual major-version upgrade non-recoverable by a non-dev, dump/restore, continuous RAM | 1 — worst on cost: daemon, RAM, `pg_dump` ops, upgrade path — for **zero** encryption gain | reject |
| 4. Hybrid | — | — | — | — | see note |

**On "Hybrid":** the four councils used the word for **three different things**, which is itself a finding:
- **CTO/SRE "hybrid" = Option 1 + three named fixes** (per-`chat_id` lock, `SQLITE_BUSY` retry on bare INSERTs, WAL-aware backup). This is *not a fourth engine* — it is Option 1 done correctly. **Adopted as the recommendation.**
- **CISO "hybrid" = SQLCipher under the queue + app-AEAD on free-text** → scored **2/5, reject**: doubles key custody, still forces transport to hold the file-key.
- **CFO "hybrid" = LUKS full-disk under SQLite** → scored **4/5**: cheap one-time OS-level insurance for stolen-disk only, no per-write cost, *complements* (never replaces) app-AEAD. **Optional, deferrable.**

There is no engine-level "hybrid" worth taking. The only "hybrid" the council actually converges on is "SQLite + the three hardening rules."

---

## 5. RECOMMENDATION

**Stay on SQLite. Adopt Option 1 (tuned SQLite + the locked scheme-3 per-field AEAD), plus the four named hardening items in §6. Do not introduce SQLCipher, do not introduce PostgreSQL, do not add an engine-level hybrid.**

### Strongest rationale (single argument)
**The engine was never the constraint, and the one thing that actually breaks under the unified-router — silent last-writer-wins clobber of the per-chat `conv_state` blob — is an application write-ordering bug that PostgreSQL does *not* fix for you.** Postgres prevents the lock error, not the logical clobber; you would still have to build a per-`chat_id` serialization lock (see the S6 transport plan). So moving engines pays the full migration + ops cost (daemon, RAM, `pg_dump`, non-recoverable major-version upgrade) and **leaves the real bug unsolved**, while throwing away the file-copy backup and zero-daemon simplicity the non-developer owner's "must not die" mandate depends on. Fix the bug where it lives (a `chat_id` lock), keep the single-file recoverability.

### Council disagreement, resolved explicitly
All four councils independently rank Option 1 first and Postgres last — there is **no disagreement on the decision**. The only divergences are about emphasis of the *single biggest risk*, and two of them must be corrected against code:

1. **"Biggest risk = silent state-clobber" (CTO, SRE-secondary) vs "= same-user cosmetic encryption" (CISO, CFO) — both are real, neither is the engine, and I rank them: cosmetic encryption FIRST.** The CISO/CFO point is verified and load-bearing: encrypting columns while `aios-telegram-bot` and `aios-claude-worker` both ran `User=ai-os` (decision-time state; the user split has since shipped — see §6) meant same-UID `0600` does **not** isolate — the internet-facing transport can read the worker's key file, so a transport compromise hands the attacker ciphertext *and* key. The ADR's whole isolation premise (`adr:82`) is currently false. This gates the encryption work and aligns with the owner's "leaks first" priority, so it outranks the clobber risk, which gates only the *router* work (a later phase).

2. **"Biggest risk = your backups may be silently incomplete" (SRE) — PARTIALLY STALE, corrected.** Verified: `scripts/aios_backup.py:60` already uses `VACUUM INTO` (WAL-aware, single consistent file) and a restore drill exists. The SRE's catastrophic framing is already mitigated *for the main store*. The **precise** residual gap: `aios_backup.py` defaults to `aios_storage.db_path()` (`:91-97`), so the **queue DB `tasks.sqlite3`** — which under the router holds inbound envelopes + `conv_state` + replies + schedules, i.e. the live conversational state — is only snapshotted if the tool is invoked with that path explicitly. The backup *mechanism* is correct; its *coverage* must be extended to the queue DB before the router ships.

### The trigger to revisit (the condition under which this decision changes)
Revisit the engine **only if a genuine multi-writer switch-trigger fires** — none of which this system is near:
- **Sustained concurrent writers > a few cooperating processes** (e.g. the bot becomes multi-tenant / multi-owner), such that per-`chat_id` application serialization can no longer keep write contention single-effective-writer; OR
- **Working set materially exceeds single-box / sub-terabyte** scale (`sqlite.org/whentouse.html` boundary); OR
- **A true network-access requirement** (a second host must write the same store concurrently), which SQLite explicitly does not serve.

Until one of those is *demonstrated with data* (not anticipated), SQLite is the decision. App-level AEAD and the unix-user split are required regardless and never trigger an engine change.

---

## 6. What it means for the existing docs

### Does the encryption ADR's SQLite assumption hold?
**Yes — the ADR's SQLite assumption is correct and is now affirmed, not merely assumed.** Scheme 3 (per-row AEAD, structured columns cleartext, `age`-wrapped KEK) is engine-agnostic and is the right design *because* it stays above SQLite rather than depending on engine TDE that Postgres doesn't have and SQLCipher can't do selectively (`adr:26-41`). **One correction the ADR must absorb (it already half-states it at `adr:82`, `:307`):** the load-bearing isolation control is the **separate unix users**, not the encryption. That split **is shipped** in this tree — the systemd units run under distinct accounts (`telegram-bot → User=aios-bot`, `integrations-worker → User=ai-os-integrations`, `claude-worker → User=ai-os`), and the integrations unit refuses to start if its separate account is absent (see `README.md` §3, `SECURITY.md` §4). Barrier #4 ("0600 + right owner") is therefore active, not a no-op. This is the single most important hardening and it is an OS/systemd control, not a database change.

### Does the s6 redesign need a new "data layer" section?
**Yes — add a short "Data layer (settled)" subsection** stating: engine stays SQLite (decided here); the S6 transport plan's own per-`chat_id` lock is reclassified from "open design choice" to the **single required serialization point covering BOTH the inbound router loop AND the schedule executor**; and the queue-DB write multiplication is explicitly accepted as within WAL capacity at this scale. No engine swap is part of the migration.

### Concrete hardening required regardless of engine (this is the real work)
**Security / isolation (gates the encryption work — see §7):**
1. **Split `ai-os` into `ai-os-bot` (transport) + `ai-os-worker` (holds key, decrypts)** before any AEAD column goes live; key file `0600` owned by `ai-os-worker` only (`adr:266`; the S6 transport plan's user-split prerequisite). Watch the cross-user `$HOME` trap: a new user's `Path.home()` silently repoints `aios.sqlite3` unless `AIOS_DATA_DB` is pinned (`aios_storage.py:39-42`).
2. **Add `cryptography` to `requirements.txt`** (the one new dependency; nothing else).

**Reliability / correctness (gates the router work):**
3. **Per-`chat_id` ordering lock** covering inbound router + schedule executor, before the router ships — the one thing that silently eats data with no lock error (per the S6 transport plan). Build it as either a hard single-worker constraint asserted at startup, or an explicit in-flight `chat_id` claim guard.
4. **`SQLITE_BUSY` retry wrapper on the bare-INSERT enqueue paths** (`enqueue_task` `task_queue.py:139-146`, `enqueue_reply`, `add_scheduled`) before the router multiplies write traffic; the read→write upgrade can throw instant `SQLITE_BUSY` even with `busy_timeout` set, and the claim paths already retry correctly but these INSERTs don't. Optionally bump worker `busy_timeout` to 15s.
5. **Extend the WAL-aware backup to the queue DB.** `aios_backup.py` already does the right thing (`VACUUM INTO`, `:60`) but defaults to the main store; snapshot `~/.ai-os/queue/tasks.sqlite3` too once it carries `conv_state`/replies/schedules.

**Latent-divergence fixes (cheap, fold in while touching the code):**
6. `notes.db` is **missing `busy_timeout`** (`bot/aios_notes_store.py`) — add it. (Correcting the council: notes is the *only* store missing it.) **Update (current tree): resolved** — the notes store now goes through `aios_storage.connect` (`bot/aios_notes_store.py:17,32`), which sets the shared PRAGMAs including `busy_timeout`.
7. `reply_queue` / `schedule_queue` are **missing `foreign_keys=ON`** (verified — they *do* set `busy_timeout`, contrary to one council claim). Add `foreign_keys=ON` for consistency with the other stores.

PRAGMAs already correct and not to be touched: WAL, `busy_timeout=5000`, `synchronous=NORMAL`, `BEGIN IMMEDIATE` on claim paths, `0700`/`0600` + `-wal`/`-shm` re-chmod (`aios_storage.py:320-333`).

---

## 7. Sequencing vs "leaks first"

The owner's stated priority this session is **close ALL secret leaks first, before routing migration**. The data-layer work slots cleanly behind that and must **not** jump ahead of leak-closure — with one nuance where a leak *depends on* a data-layer fix.

```
PRIORITY 0 — LEAKS FIRST (owner's gate; data-engine decision does NOT touch this)
   • Rotate/close any live secret leaks (the actual "leaks first" work)
   • Unix-user split ai-os → ai-os-bot / ai-os-worker        ← IS a leak-closure item:
       it is the load-bearing isolation barrier (adr:82); same-user
       0600 is cosmetic encryption. So this belongs IN priority 0,
       not after it. It is an OS/systemd change, engine-neutral.
        │
        ▼
PRIORITY 1 — ENCRYPTION (engine-neutral; lands independently of the router)
   • Add `cryptography` dep; build context_store per-field AEAD (scheme 3)
   • Encrypt free-text: unrouted_inbox.raw_message, notes body/title,
     memory entry text, chat blocks  (adr:164-176)
   • Gate behind ciphertext-at-rest test
        │
        ▼
PRIORITY 2 — ROUTER MIGRATION (the data-layer hardening lives HERE)
   • Per-chat_id lock (see S6 transport plan)   • SQLITE_BUSY retry on enqueue paths
   • Extend WAL-aware backup to tasks.sqlite3
   • Encrypt task_text + replies.text + conv_state blob (S6 transport plan prereqs)
```

**Where the engine decision sits:** it sits *under everything and blocks nothing* — by deciding "no engine change," it removes a fork from the critical path entirely. Nothing in the leak-closure work depends on a database engine.

**The one dependency to call out explicitly:** the router *creates a new leak* — enabling the catch-all writes personal notes/chat **plaintext** into shared SQLite at rest, strictly worse than today's in-RAM-only state. Therefore the encryption substrate (Priority 1) and the `task_text`/`replies.text`/`conv_state` encryption are **hard prerequisites of the router** (see the S6 transport plan). This is consistent with "leaks first": you do not enable the router until encryption + the user split are real, precisely so the router does not open a new leak. The engine decision being "stay on SQLite" makes this ordering simpler, not harder — column-level AEAD is engine-native to this plan.

---

## 8. Open questions (need the owner's decision)

1. **Unix-user split timing.** The `ai-os-bot` / `ai-os-worker` split needs a sudo step on the VPS (owner-only, `adr:266`). Approve doing it *now* as part of leaks-first, since same-user `0600` makes the planned encryption merely cosmetic? (Recommendation: yes — it is the actual barrier.)
2. **LUKS full-disk (CFO option 4).** Cheap one-time stolen-disk insurance under SQLite, complementary to app-AEAD. Adopt now or defer to backlog? (Recommendation: defer — app-AEAD + user split close the higher-probability leak first; LUKS is for the physical-theft tail.)
3. **`SQLITE_BUSY` retry budget.** Bounded retry + worker `busy_timeout` 15s is the proposal; confirm there is no hard latency ceiling on a turn that would prefer fail-fast over retry. (Engineering default if no answer: bounded retry, 15s.)
4. **Reference-catalog confirmation.** ADR marks the static reference catalog personal-data-free and out of scope (`adr:174`); owner has deferred it to backlog. Confirm the catalog carries no free-text needing encryption, so it stays engine-and-crypto out of scope.

---

### Grounding index (re-verified this session)
- Engine + access pattern: `bot/task_queue.py:43-47,97-121,139-170`; `bot/reply_queue.py:24-48,63-64`; `bot/schedule_queue.py:25-51,66-67`; `bot/aios_storage.py:37-42,83-94,154-173,208-229,299-336`; `bot/aios_notes_store.py` (**Update:** now via `aios_storage.connect`, `busy_timeout` set).
- Crypto dep at decision time: none. **Update (current tree):** `cryptography` is present in `requirements.txt`.
- Service users at decision time: transport/worker/sync all `User=ai-os`. **Update (current tree):** `aios-telegram-bot.service:12` `User=aios-bot`; `aios-integrations-worker.service:40` `User=ai-os-integrations`; `aios-claude-worker.service:14`, `aios-sync.service:13` `User=ai-os`.
- Backup is already WAL-aware but defaults to main store: `scripts/aios_backup.py:60,91-97`; `scripts/aios_restore_drill.py`.
- Locked encryption design: `docs/security/adr-encryption-notion-migration.md` (scheme 3 `:26-41`; plaintext-boundary-via-separate-users `:80-85`,`:307`; importer/migration `:160-199`; field map `:164-176`; Phase 0 user split + keygen `:143`,`:266`).
- Router transport plan: `docs/architecture/s6-thin-transport-plan.md` (queue write multiplication + lifecycle; per-`chat_id` lock not free; timers-as-writers; inbound/reply/conv_state encryption hard prereqs).
- Research basis (engine scale + Postgres-has-no-TDE → client AEAD): `sqlite.org/whentouse.html`, `sqlite.org/np1queue.html`, `sqlite.org/wal.html`; `postgresql.org/docs/current/pgcrypto.html`, `postgresql.org/docs/current/encryption-options.html`, `postgresql.org/docs/current/pgupgrade.html`.

**One correction applied to the council inputs (honesty):** the CTO/CFO claim that reply/schedule queues lack `busy_timeout` is wrong — they set it (`reply_queue.py:64`, `schedule_queue.py:67`); they lack `foreign_keys=ON`. Notes was the store then missing `busy_timeout` (since resolved — see §6 item 6). And the SRE "backups may be silently incomplete" risk is already mitigated in code (`VACUUM INTO`) except for queue-DB coverage. These corrections do not change the decision; they sharpen the hardening list in §6.