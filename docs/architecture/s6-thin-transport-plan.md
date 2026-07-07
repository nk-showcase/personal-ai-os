# Thinning the telegram-bot transport (S6) - staged plan

> Scope: move business logic OUT of `telegram-bot` so it becomes **receive → auth → enqueue → deliver-reply → heartbeat** only. Locked constraints honoured: **V2 only** (no temporary single-process shim; see `docs/architecture/target-vps-ai-os.md`) and **discipline=code** (every invariant has a test/guard/startup-check). The plan reuses the already-proven producer/consumer split (`bot/task_queue.py` + `bot/claude_worker.py`); it invents no new transport mechanism.

> **The load-bearing correction in this revision:** an earlier draft repeatedly relocated work "to the worker that owns the scheduler". **No such worker exists.** Verified: the only PTB `Application` / `JobQueue` in the repo is built in `bot/main.py` (`main`); `bot/claude_worker.py` and `bot/integrations_worker.py` are bare poll loops with neither a PTB `Application` nor a scheduler, and neither holds `TELEGRAM_BOT_TOKEN` (see `config/secrets-map.yaml`). There is no PTB persistence anywhere - which is why `post_init` rebuilds precise reminders and scheduled follow-ups on every restart. The plan now (a) builds a worker-side scheduler **before** any schedule moves, (b) treats process-local in-memory state (`context.chat_data`, the `claude_bridge._pending_task_flow` dict) as the primary source of silent feature breakage, (c) splits `config.py` and `claude_bridge.py` - the two files that actually resolve the forbidden secrets - and (d) re-anchors the enforcing test on `bot.main` (the real systemd surface), not the lazy `bot.telegram_bot` shim.

---

## 1. What the transport imports today

The transport entrypoint is `python -m bot.telegram_bot` (`systemd/aios-telegram-bot.service`). `bot/telegram_bot.py` (`main`) lazily delegates to `bot.main.main()`. **The running process is `bot.main`** (confirmed: `scripts/vps_safe_import_check.sh` imports `bot.main` directly to mirror the unit). At startup `bot.main` pulls the whole legacy monolith:

- `bot/handlers.py` statically imports every feature module (storage, the classifier, and the per-feature handler and storage modules).

So today the transport process resolves and caches the forbidden integration keys purely as an import side-effect (in `secrets_loader._mem`). **While that import chain stands, the "transport holds neither the decryption key nor plaintext" boundary is unenforceable - it is violated at process startup.** Critically, moving the *handler* modules out does NOT fix this on its own, because `config.py` (kept "for the token only") and `claude_bridge.py` (kept for the bridge interceptors) are the files that actually call `get_secret` for the forbidden keys. They must be split, not merely retained.

---

## 2. Command / job classification

`T` = true transport (keep) · `B` = business logic (move) · `S` = scheduled job (move, except heartbeat + the new deliver poller). Item → class → what it touches.

| Item | Class | Notes |
|---|---|---|
| `start`/`help` → `cmd_start` | T-body / B-file | body is transport, but lives in `handlers.py` whose imports drag the monolith. Body moves to a transport-only module. |
| `done`/`d` → `cmd_done` | **B + stateful** | reads the per-chat task map; a Todoist `complete_task` plus a feature-specific store write. |
| `health` → `cmd_health` | **T** | the value-free heartbeat helper - ops only. No personal data, no keys. **Keep.** |
| dead-letters command | B (clean) | reads the request queue read-only. Import chain pulls **no** forbidden personal-data module (verified). Stage 5. |
| TEXT `handle_text` | **B + stateful** | monolith router → Notion, Todoist, feature stores; **reads `_pending_task_flow` and ~15 `chat_data` keys**. The `pc_tasks` branch is transport-grade. See Risks R1/R2/R3. |
| Media `handle_media` | B + stateful | conversational scenario state in `chat_data`. |
| Callback `cb_approve` (`^bridge:` prefix) | **T** | inline Allow/Deny via queue. **Also sets `_pending_task_flow[chat_id]={"laptop_mode":True}`.** Keep - see Risk R2. |
| Callback `handle_callback_query` | B + stateful | catch-all for business inline keyboards → Notion + feature stores + Todoist; writes the `chat_data` keys read by `handle_text`. |
| post_init `ensure_skills()` | S/B | resolves a GitHub write token via `claude_bridge` - a forbidden secret in transport. → claude-worker. |
| post_init `reschedule_pending_followups` | S + restart-state | scheduled follow-ups; calls `application.job_queue.run_once`. Needs a worker scheduler + durable store. |
| post_init in-memory index loads | S/B | feature index loaded into process memory. Move with the feature. |
| post_init `run_daily(check_reminders)` at a configured time | S | reminder scan → Todoist; **delivers via `context.bot.send_message`**. |
| post_init `run_daily(_api_health_job)` | S | does a **lazy** `from .classifier import check_api_health` - fires LLM-key resolution at job time, not at startup. See Risk R6. Delivers via `send_message`. |
| post_init `run_daily(_heartbeat_job)` at a configured time | **T** | value-free "I'm alive" ping; the in-bot half of the dead-man's-switch. **Keep.** |
| post_init `run_daily(...)` feature jobs | S | Notion-backed feature reports. |
| Conversational `run_once` timers (NOT in the run_daily set) | **S (hidden)** | per-feature scenario timers with `get_jobs_by_name`/`schedule_removal` cleanup. All on the transport JobQueue, deliver via `send_message`. See Risk R8. |
| **Module-import side-effect** main.py → claude_bridge | **B (decisive, KEPT module)** | `claude_bridge._github_token`, `_prepare_repo`, `_run_claude`, `ensure_skills` live in a module transport KEEPS for the interceptors. Fixed in Stage 0b. |

**Several `run_daily` jobs + N `run_once` conversational timers are registered; only `_heartbeat_job` is genuinely transport.** Everything else reads/writes Notion, Todoist, a feature SQLite, or LLM keys and delivers via `context.bot.send_message`.

---

## 3. Target transport shape

1. **Receive update** - `Application.run_polling` (`main`). Needs `TELEGRAM_BOT_TOKEN` (the only secret transport may hold).
2. **Auth** - owner-id check.
3. **Enqueue** - the only write transport performs: `auth → enqueue an intent row → ack`. Generalizes the proven `_enqueue_bridge_producer → bridge_queue.enqueue_bridge_task → task_queue.enqueue_task` path. **The transport-only pc_tasks flow is the one exception** (it executes in-process; see Stage 5 and R2) because `pc_tasks.py` holds no secrets (imports only `asyncio/re/secrets/Path` + `claude_worker_core._redact`).
4. **Deliver reply** - a poller that forwards worker-produced replies (see Stage 0c).
5. **Health heartbeat** - `_heartbeat_job`/`cmd_health`: value-free, needs the bot token, feeds the external dead-man's-switch.

### The reply channel (the missing piece)

Today the only reply is the synchronous ack *"Task #N queued"* plus a manual status command. **The async push-back is the missing piece** - verified: `task_queue.list_updates` has **zero callers in `bot/`** (only a test references it). Nothing in transport reads it.

- The poller selects undelivered rows with **`WHERE delivered_ts IS NULL`** - the table IS the cursor. There is **no separate in-memory `since_ts`**. It calls `bot.send_message(chat_id, display_text)` and stamps `delivered_ts` using the `BEGIN IMMEDIATE` atomic-claim pattern from `task_queue.claim_next_task`, so a transport restart can neither double-send (delivered rows are excluded) nor lose a reply produced while transport was down (it sits undelivered until pickup).
- The poller reads **only** the `replies` table. It MUST NOT call `task_queue.list_updates` (which does `SELECT *` and would pull inbound `task_text` - personal plaintext - into the transport process).

### The two-directional data boundary

- **Outbound:** transport forwards one rendered string field; it never deserializes a personal-data structure and never reads `result_json`. The worker is the *only* reader of decrypted data.
- **Inbound:** transport is the *producer* - it receives the Telegram message and writes the payload (`task_queue.enqueue_task` writes `task_text`). A personal request's `task_text` is personal plaintext that transport necessarily sees on receipt. So transport must **encrypt the inbound payload to the worker's public key at enqueue time** (asymmetric: transport holds only the public half), mirroring the "encrypt to a public key" design in `docs/security/memory-safety.md`. Then transport handles inbound plaintext only transiently in RAM and never persists it readable. **This is a prerequisite for the boundary** - without it transport persists personal plaintext and the boundary is nominal on the inbound side.
- Once the business imports are gone AND `config.py`/`claude_bridge.py` are split, transport no longer resolves the forbidden keys at startup, and the inbound encryption applies. The bot is **structurally incapable of decrypting** because it never gets the key (see `secrets-map.yaml`).

---

## 3b. Staging

Each stage ships as a real cross-process slice (V2-only - no single-process shim). Each B feature flips behind a **feature flag defaulting to the legacy in-process path** (the proven `AIOS_RAW_NOTION_PERSIST`-style gate in `config.py`); reverting = flip the flag back. Reuse the existing second queue family for non-coding work - the request-queue producer + `integrations_worker.run_loop` consumer with an injected applier; the `kind` field lets every feature ride one durable channel. **Do not add a 4th service** - the Bitwarden free tier caps at 3 machine accounts (see `secrets-map.yaml`).

> **Three foundation stages (0a/0b/0c) come BEFORE any feature moves.** Without them the later stages would silently drop jobs, kill the pc_tasks flow, and leave the secret boundary nominal.

### Stage 0a - split `config.py`
- **What:** move the forbidden `get_secret` calls out of the module body into lazy call-time in worker modules; `config.py` body resolves only `TELEGRAM_BOT_TOKEN` + `TELEGRAM_OWNER_ID`.
- **Verification:** after a transport boot with dummy env, assert `secrets_loader._mem` contains ONLY `TELEGRAM_BOT_TOKEN` (+ `TELEGRAM_OWNER_ID` is a non-secret int). Existing consumers still work (lazy fetch at call time).

### Stage 0b - split `claude_bridge.py`
- **transport-bridge** (kept, imported by `main.py`): the bridge dispatch (enqueue only), the interceptors, `cb_approve`, the picker/`_pending_task_flow` state. Imports **neither** `get_secret` for GitHub **nor** `_run_claude`/`_prepare_repo`/`ensure_skills`.
- **worker-bridge** (imported only by `claude_worker.py`): `_github_token`, `_auth_url`, `_prepare_repo`, `_run_claude`, `ensure_skills`, and the dead legacy inline executor (currently unreachable but still present in the kept module - **physically delete it from the transport side**).
- **Why:** `claude_worker._claude_executor` already lazily imports `claude_bridge` and calls `_run_claude`; transport keeps it for the interceptors. Today both halves are one file, so each process drags the other's surface across the very boundary this is meant to enforce.
- **Verification:** assert the transport-bridge module's `sys.modules` footprint excludes `_run_claude`/GitHub `get_secret`/telegram-free worker helpers; assert `claude_worker` imports only worker-bridge. Add worker-bridge's module name to the FORBIDDEN set in §4.

### Stage 0c - worker scheduler + reply loop
- **What's built (reply loop):** the `replies` table + a single `JobQueue.run_repeating` poller in transport that selects `WHERE delivered_ts IS NULL`, `send_message`s each row, and stamps `delivered_ts` atomically. **Locked to the `delivered_ts` design only - the in-memory `since_ts`/`list_updates` variant is forbidden.**
- **What's built (scheduler):** a durable `due_ts` store the worker poll loop scans, so scheduled work survives a process restart (no PTB persistence exists in the repo).
- **Verification (reply loop):** (a) cold start with no cursor delivers exactly the undelivered backlog; (b) a reply produced during transport downtime is delivered after restart; (c) already-delivered rows are never re-sent (simulated hard restart → zero re-delivery); (d) the poller's SQL touches only `replies` and selects no `task_text`/`result`/`result_json` column. **Verification (scheduler):** a fake job registered on the worker fires exactly once and survives a worker process kill+restart (proves the durable `due_ts` store, not an in-memory JobQueue).

### Stage 1 - smallest self-contained feature
- **What moves:** one feature's handler + storage (an in-memory index loaded at startup). No scheduled job, minimal coupling.
- **Replies:** request enqueued; worker owns the storage + load-on-start; reply returns via the Stage-0c loop, byte-identical text.
- **Verification:** send the request → same reply text; automated test asserts the feature's storage module is absent from `sys.modules` after importing **`bot.main`** (the real surface - see §4).

### Stage 2 - reminders (a scheduled feature)
- **What moves:** the reminder scan + its daily job.
- **Verification:** restart transport → exactly one reminder at the configured time; a precise reminder set before an actual process **kill+restart** still fires exactly once (not zero); the moved job produces a `replies` row, not a `send_message` call. Test asserts the job name is **present-and-fireable on the worker AND absent from transport** (absence-from-both must FAIL - see §4).

### Stage 3 - an integration feature with a credential-entry flow
- **Credential entry:** the credential-entry handler must NOT decrypt or persist on transport - it enqueues an integrations-style request consumed by the worker that holds the data key.
- **Verification:** the feature works unchanged; exactly one daily report from the worker (as a `replies` row); the credential never lands in transport (guard).

### Stage 4 - the largest stateful feature group
- **What moves:** the feature modules with the largest plaintext surface - the highest-value, riskiest group. Sub-stage each behind its callback-prefix routing.
- **Schedules (run_daily):** the LLM-key job and the feature report jobs; rewrite each from `send_message` to `replies.enqueue`; move + delete atomically.
- **Schedules (conversational run_once):** the per-feature `run_once` timers and their `get_jobs_by_name`/`schedule_removal` cancellation are real schedules. Move each to the worker's durable `due_ts` store and reimplement the cancellation semantics over that store. A timer left on the transport JobQueue after its module moves either never fires or fires orphaned.
- **Restart-state:** `reschedule_pending_followups` calls `application.job_queue.run_once` → convert to durable `due_ts` rows like precise reminders.
- **Conversational-state caveat (biggest correctness risk - see R3):** `handle_text` branches on ~15 `chat_data` keys written by `handle_callback_query` and read on the next text turn. Plus the cancel handler reads ALL of them at once. **Design the full chat_data state transfer (move into the durable queue keyed by chat_id, OR keep each flow end-to-end in ONE process and never split a flow mid-stage) BEFORE this stage, not ad hoc per feature.**
- **Verification:** per-flow **multi-turn** regression tests (start → mid-turn → cancel) cross-process; follow-ups fire once after a real restart; `classifier` absent from `sys.modules` after importing `bot.main`.

### Stage 5 - the remaining monolith router + task commands
- **What moves:** `cmd_list`/`cmd_done`/`cmd_delete`, the `handle_text`/`handle_photo`/`handle_media`/`handle_callback_query` business bodies + `classifier` + the dead-letters command body.
- **R2 - the pc_tasks flow (critical):** `cb_approve` (kept in transport) sets `_pending_task_flow[chat_id]={"laptop_mode":True}`; `intercept_task_text` (kept) reads it and deliberately `return`s without `ApplicationHandlerStop` so the next text falls through; `handle_text` reads/pops it. Setter and reader must end up in the **same process**. Since `pc_tasks.py` holds no secrets, **re-home the WHOLE pc_tasks flow into the transport-only module**: keep the `_pending_task_flow` laptop_mode read, the regex preflight, and `submit_pc_task` IN TRANSPORT - no worker round-trip. Do not let `handle_text` (worker) own the pc_tasks branch.
- **R4 - fall-through catch-all (high):** today a non-bridge, non-pc_tasks owner message falls through from the interceptors (which `return` without `ApplicationHandlerStop`) to the default-group `handle_text`. When `handle_text` moves to the worker, transport must register a **default-group catch-all** that AUTHs and ENQUEUES the raw message as a generic business intent - otherwise normal free-text logging hits no handler and is silently dropped. Verify the fall-through matrix explicitly: bridge-active → interceptor handles; laptop_mode → transport pc_tasks; everything else → enqueue. A test must assert no owner text path reaches a dead end.
- **R-callback (medium):** business inline buttons delivered via the reply channel produce `CallbackQuery`s that arrive at transport (it holds the token). Transport's catch-all `CallbackQueryHandler` must do **auth + opaque-enqueue of `callback_data` only** - no prefix-based business routing beyond the `^bridge:` approval prefix which stays. Test: the transport callback handler imports no business module and branches on `callback_data` content only for the bridge prefix.
- **Transport-grade extraction:** the `cmd_start` body and the `pc_tasks` branch move into the transport-only module; preserve handler group ordering / `ApplicationHandlerStop` semantics.
- **End state:** `bot.main` imports only `config` (token only, post-0a), the transport-bridge module, the deliver-reply loop, the `^bridge:` approval callback, the catch-all enqueue handlers, `log_redaction`, and the heartbeat helper.
- **Verification:** cross-process **multi-turn** sequences pass - LIST→DONE→DEL, pc_tasks-button→next-text→`submit_pc_task` (entirely in transport), start→mid-turn→cancel; every command produces byte-identical owner replies; `classifier` and `handlers` absent from `sys.modules` after importing `bot.main`.

---

## 4. End-state invariant + the exact enforcing checks

**Invariant:** importing **`bot.main`** (the module the systemd unit actually runs, via `bot.telegram_bot`'s lazy delegation) with dummy env transitively imports **NONE** of the business modules (`handlers`, `classifier`, the per-feature handler/storage modules, `reminder_lib`, `storage`) or the **worker-bridge** module (Stage 0b). Therefore the transport process never resolves `TODOIST_API_KEY`/`NOTION_API_KEY`/`ANTHROPIC_API_KEY`/the classifier-backup key/`GITHUB_REPO_WRITE_TOKEN`, `secrets_loader._mem` holds only `TELEGRAM_BOT_TOKEN`, and `post_init` holds only the heartbeat job + deliver poller.

Enforce **five ways** (mirroring discipline already in-repo):

1. **Import-graph test - extend the entrypoint test, anchored on `bot.main`, NOT the lazy `bot.telegram_bot` shim.** The current test imports `bot.telegram_bot`, which is lazy and pulls nothing - so a naive FORBIDDEN-in-`sys.modules` check would PASS on today's intact monolith and certify a broken process as clean. Import `bot.main` with dummy env (as `vps_safe_import_check.sh` does), then assert the FORBIDDEN business/worker-bridge module set is disjoint from `sys.modules`, and that `secrets_loader._mem` is a subset of `{"TELEGRAM_BOT_TOKEN"}`.
2. **Static guard against lazily-imported jobs - extend `scripts/vps_spec_guard.sh`.** A startup `sys.modules` check cannot catch a job that lazily imports a forbidden module only when it fires (canonical evasion: `_api_health_job` does `from .classifier import check_api_health` inside the callback). Grep transport source (`bot/main.py` + the transport-only modules) for any `from .<forbidden>` import **anywhere** (including inside job callbacks), and fail the build. The guard's existing per-service systemd secret-hygiene check is the **primary** OS-level boundary - strengthen it to assert the telegram-bot unit's resolvable secret set is exactly `{TELEGRAM_BOT_TOKEN}`; the import test is secondary.
3. **Runtime per-service enforcement in `secrets_loader.get_secret`.** Today `get_secret` does NOT check `secrets-map.yaml` allowed_secrets - it reads env → Bitwarden → disk-cache with no per-service refusal, so the entire boundary rests on the static import test. Add fail-closed enforcement keyed on `AIOS_SERVICE_NAME` (set per systemd unit) + the service's `allowed_secrets`: raise if a process requests a secret not allowed for its service. This converts the boundary from "enforced only by what we forgot to import" to fail-closed at the resolution point.
4. **Fail-closed startup self-check in `telegram_bot.main()` - a Stage-5 deliverable, NOT earlier.** Refuse to start if any forbidden module is in `sys.modules` or any scheduled job other than the heartbeat + deliver poller is registered. **Sequencing is load-bearing:** if added before every forbidden `post_init` block is removed, it trips on the still-present feature/skills jobs and bricks the bot mid-migration. Until Stage 5, the import-graph test (non-fatal) is the discipline. Document this check as Stage-5-only.
5. **Delivery + inbound-encryption tests:** assert the reply poller passes only a string field to `send_message` and never `json.loads` a payload nor selects `task_text`/`result`; and assert transport encrypts the inbound payload to the worker's public key at enqueue, never persisting readable plaintext.

---

## 5. Requirements the design must satisfy (and how)

Each row below states a requirement the split must satisfy and the design that satisfies it. Where a requirement is a prerequisite that is **not yet built as runnable code in this reference tree**, that boundary is recorded in `docs/security/threat-model.md`.

| ID | Requirement | Design that satisfies it |
|---|---|---|
| **R1** | `/list`→`/done N`/`/del N` must survive splitting the list-producer from the done-consumer. `cmd_list` writes `context.chat_data["task_map"]`; `cmd_done`/`del` read it, and PTB `chat_data` is per-process memory, never shared. | Make the number→id map durable: the worker persists `task_map` keyed by `chat_id` in the queue DB, OR `/list` emits stable ids the owner replies with. Add a **cross-process LIST→DONE→DEL** regression test (Stage 5). |
| **R2** | The pc_tasks flow must not break. Setter `cb_approve` (transport) and reader `handle_text` (worker) communicate via the in-memory `_pending_task_flow` dict. | Re-home the WHOLE pc_tasks flow (laptop_mode read + regex preflight + `submit_pc_task`) into the transport-only module - `pc_tasks.py` holds no secrets. Test: button → next-text → `submit_pc_task` entirely in-process in transport, no worker round-trip. |
| **R3** | The ~15 conversational `chat_data` keys must not break. Producers (`handle_callback_query`) and consumer (`handle_text`) split across processes; the cancel handler reads ALL at once. | Enumerate ALL keys; design state transfer (durable queue keyed by chat_id, OR each flow end-to-end in ONE process) BEFORE Stage 4. Per-flow multi-turn (start→mid→cancel) regression tests. |
| **R4** | Fall-through text must not be dropped. Interceptors `return` without `ApplicationHandlerStop` to reach default-group `handle_text`; when that moves, transport has nothing to fall through to. | Transport registers a default-group catch-all that auths + opaque-enqueues the raw message. Test the full fall-through matrix; assert no owner text path is a dead end. |
| **R5** | A worker scheduler must exist before any schedule moves (only `main.py` builds an Application/JobQueue; both workers are bare loops). | **Stage 0c builds the scheduler (durable `due_ts` store, or worker APScheduler) BEFORE any schedule moves.** No `run_daily` may be deleted from `main.py` until the worker scheduler exists and is tested. |
| **R6** | Moved jobs must be able to deliver even though the worker has no bot token. All daily jobs call `context.bot.send_message`. | Every moved job's terminal action is rewritten from `send_message` to `replies.enqueue` as the SAME edit. Guard/test: worker code never imports/holds a Telegram Bot; no `send_message` call site in worker-side code. |
| **R7** | Restart-state rebuilders must not be dropped. `reschedule_precise_reminders` and `reschedule_pending_followups` call `application.job_queue.run_*` - a JobQueue the worker lacks. | Convert precise reminders + scheduled follow-ups to durable `due_ts` rows the worker poll loop scans (Stage 0c store), not PTB `run_once`. Test: a reminder set before an actual process kill+restart fires exactly once. |
| **R8** | Conversational `run_once` timers and their `schedule_removal` cancellation are real schedules not in the daily-job table; they must not vanish or fire orphaned when modules move. | Inventory every `run_once` timer; move each to the durable `due_ts` store with reimplemented `get_jobs_by_name`/`schedule_removal` semantics. Add to Stage-4 checklist + the "no timer in both processes" test. |
| **R9** | `config.py` must not resolve forbidden keys at module body the instant it is imported "for the token only". | **Stage 0a** moves those `get_secret` calls to lazy call-time in worker modules; `config.py` body resolves only `TELEGRAM_BOT_TOKEN` + `TELEGRAM_OWNER_ID`. |
| **R10** | `claude_bridge.py` (KEPT) must not carry GitHub-token resolution + the dead inline executor into transport. | **Stage 0b** splits into transport-bridge / worker-bridge; physically delete the dead inline executor from the transport side; add worker-bridge to the FORBIDDEN set. |
| **R11** | The §4 import test must measure the right surface - `import bot.telegram_bot` is lazy and pulls nothing. | Anchor the test on `bot.main` (the systemd ExecStart surface, mirrored by `vps_safe_import_check.sh`). |
| **R12** | `secrets_loader.get_secret` must have per-service enforcement so the import test is not the sole line of defense. | Add fail-closed runtime enforcement keyed on `AIOS_SERVICE_NAME` + `secrets-map.yaml`; make the OS-level systemd/Bitwarden scope (`vps_spec_guard.sh` per-service check) the PRIMARY boundary, import test secondary. |
| **R13** | Lazily-importing job callbacks must not evade the import-time check (`_api_health_job` does `from .classifier import` inside the callback). | Static guard greps transport source (including job callbacks) for any deferred `from .<forbidden>` import; assert the transport JobQueue contains exactly the two allowed job names. |
| **R14** | Inbound plaintext must not be persisted by transport. Transport is the producer; `task_queue.enqueue_task` writes `task_text`. | Transport encrypts the inbound payload to the worker's public key at enqueue (asymmetric; transport holds only the public half), mirroring `memory-safety.md`. Prerequisite for the boundary. |
| **R15** | No at-least-once double-send / loss on restart. An in-memory `since_ts` cursor resets on restart (mass re-send or silent loss). This is stressed when two jobs share the same configured firing time. | Lock Stage 0c to the `delivered_ts IS NULL` + `BEGIN IMMEDIATE` design only; forbid the in-memory cursor. Tests: cold start, downtime-produced reply, no re-send of delivered rows. |
| **R16** | Timezone labels must not be misinterpreted. A configured tzinfo object commented with a different zone name actually fires at the configured offset; "fixing" the label would shift jobs and can double-fire during transition. | Preserve the EXACT tzinfo object per job (copy it, do not re-interpret a comment); assert the worker job's next-run offset equals the original; fix any misleading zone comment to the real offset. |
| **R17** | `run_daily` must be idempotent. The daily calls lack the `get_jobs_by_name`+`schedule_removal` guard that the reminder module already uses; a double `post_init` stacks duplicates. | Make all `run_daily` idempotent (remove-by-name before register). Strengthen the test: each moved job must be **present-and-fireable on the worker AND absent from transport** - absence-from-both FAILS. |
| **R18** | The photo-bridge shared filesystem must be reachable by both processes. `intercept_task_photo` writes to a `_WORK_DIR` under `/tmp`, then enqueues an absolute path. If transport and worker have separate `/tmp`, the worker cannot read it. | Put `_WORK_DIR` on a volume shared between transport and worker, or write the image into a queue-owned shared dir. Verify a transport-enqueued photo is readable by the worker at the stored path. |
| **R19** | The dead-letters command split must stay boundary-clean. Registered in transport; body reads the request queue. | Assign to Stage 5 as transport-side enqueue → worker-renders → reply. Confirmed: the import chain pulls **no** forbidden personal-data module, so the read is boundary-clean. |
| **R20** | The classifier key home must be coherent under the 3-account cap. `config.py` resolves backup LLM keys that `secrets-map.yaml` assigns to no service. | Resolve before Stage 4: either add the backup keys to the owning worker's `allowed_secrets` (reconcile 3-account scopes) or collapse the classifier to a single backup key and retire the rest. Update `secrets-map.yaml` so every key `config.py` resolves has exactly one owning service. |
| **R21** | The V2-only hard rule forbids any temporary single-process shortcut. | Each stage ships as a real cross-process slice - slower but the locked constraint. No single-process shim. |
| **R22** | The Bitwarden 3-account cap must hold. | Keep services at telegram-bot / claude-worker / integrations-worker; no 4th service per feature group. |

---

### Adversarial review - applied vs rejected

**Applied (genuinely strengthen the plan):**
- **Stage 0c - worker scheduler before any schedule moves** (lost-schedule lens, CRITICAL). Verified: no worker has an Application/JobQueue; both are bare poll loops. The draft's "move to the worker that owns the scheduler" had no referent.
- **Per-job `send_message`→`replies.enqueue` rewrite** as part of each schedule move (CRITICAL). Verified all job callbacks + conversational timers deliver via `context.bot.send_message`; the worker holds no token.
- **Restart-state → durable `due_ts` store, not PTB run_once** (CRITICAL). Verified `reschedule_precise_reminders`/`reschedule_pending_followups` require a JobQueue.
- **R1 task_map cross-process fix + LIST→DONE→DEL test** (broken-feature, CRITICAL). Verified the `chat_data["task_map"]` handshake.
- **R2 keep the whole pc_tasks flow in transport** (CRITICAL). Verified the `_pending_task_flow` setter/reader split and that `pc_tasks.py` is secret-free.
- **R3 enumerate all chat_data keys + cancel handler before Stage 4** (CRITICAL). Verified ~15 keys and the all-flows cancel handler.
- **R4 default-group catch-all enqueue** so fall-through text isn't dropped (HIGH). Verified the interceptor `return`-without-stop fall-through.
- **Stage 0a split `config.py` + Stage 0b split `claude_bridge.py`** (boundary-nominal, CRITICAL ×3). Verified both files resolve forbidden secrets and are KEPT/imported in transport.
- **Re-anchor §4 test on `bot.main`, not `bot.telegram_bot`** (boundary-nominal, CRITICAL). Verified the lazy shim pulls nothing and `vps_safe_import_check.sh` already uses `bot.main`.
- **Runtime per-service enforcement in `get_secret` + make systemd scope the primary boundary** (HIGH). Verified `get_secret` has no allowlist check; verified the spec-guard per-service check exists.
- **Static guard for lazily-imported job callbacks** (HIGH). Verified `_api_health_job` lazy-imports `classifier`.
- **`delivered_ts IS NULL` only; forbid in-memory cursor** (HIGH). Verified `list_updates` does `SELECT *` and has no delivery stamp.
- **Inbound encrypt-to-worker-public-key** (HIGH). Verified transport is the producer that writes `task_text`.
- **Idempotent `run_daily` + present-and-fireable-on-worker test** (HIGH). Verified the daily calls lack the remove-by-name guard the reminder module has.
- **Conversational run_once timers added to the schedule inventory** (HIGH); **timezone tzinfo-preservation** (MEDIUM); **photo `_WORK_DIR` shared FS** (LOW); **dead-letters → Stage 5** (MEDIUM); **callback opaque-enqueue** (MEDIUM); **classifier key-home reconciliation** (MEDIUM) - all verified and applied.
- **Fail-closed startup self-check sequenced to Stage 5 only** (MEDIUM). Verified the same `post_init` holds jobs that would trip an early check.

**Rejected / down-scoped (with reason):**
- *"Add APScheduler as the worker scheduler"* - offered as one of two shapes only. **Preferred shape is a durable `due_ts` column** the existing poll loop scans, because it survives a process kill (no PTB persistence exists in the repo, which is precisely why restart-state rebuilders exist). APScheduler in-memory would reintroduce the same restart-loss class. Recorded as an acceptable fallback, not the default.
- *"telegram-bot keeps all run_daily timers as enqueue-only producers"* - **rejected as the primary path.** A transport-side timer firing on a wall clock still needs to enqueue an intent the worker renders/delivers; that is functionally Stage 0c's worker-side `due_ts` store reached the long way, and it leaves scheduling logic (cron expressions, tz, dedup) in transport. Kept only as a stop-gap if Stage 0c's durable store slips.
- No adversarial item was rejected as wrong - every "breaks the plan" claim was reproduced against the repo. The only modifications are scoping (which of two valid shapes to prefer) and sequencing (which stage owns each fix).
