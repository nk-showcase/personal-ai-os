# Memory Safety — neutralizing raw context in Git (living document)

> Goal: do not deposit raw task/dialogue text into places that are hard to scrub
> (Git history, log viewers). Done in stages. This file records what is already in
> place and where the current boundary sits. The full attack-surface map lives in the
> Block -1 audit bundles.

## Block -1A (done) — neutral Git metadata (intermediate measure)

A cheap measure that lands BEFORE the encrypted channel. The task body is **not removed** —
only the way it travels changes (file name, commit message, log). The body itself stays
plaintext inside the file so the offline "phone -> PC" flow keeps working. This is the
current boundary, documented explicitly in `docs/security/threat-model.md`.

### task producer (`bot/pc_tasks.py`)
- **Neutral file name:** `_pc_tasks/<timestamp>-<random>.md` (UTC time + random suffix from
  `secrets.token_hex`). The name is **not derived from the body** — previously a slug of the
  first characters of the task ended up in the name.
- **Neutral commit message:** `pc-task: queued <id>`. Previously `pc-task: <body[:60]>`.
- **Body is stored** in the file body (after the frontmatter) — the PC side reads it as before.
  The body still lives plaintext in Git until the encrypted format arrives (see below). Scrubbing
  old history is a separate block, out of scope here.
- **Secret scrub before write:** the body is passed through the shared redactor
  `claude_worker_core._redact` (patterns `sk-ant-…`, `ghp_…`, telegram token, etc.) so an
  accidentally dictated secret does not land plaintext in Git.
- The frontmatter now carries a neutral `task_id` (plus the existing `created` / `chat_id` /
  `source`). No keys were removed — the PC classifier does not break.

### bridge commits and log (`bot/claude_bridge.py`)
- `_auto_push` and `_commit_push`: commit messages are **fixed string constants**
  (`bridge: auto-commit` / `claude: bridge commit`), with no interpolation of task text.
  Previously `bridge: <task[:60]>` / `claude: <task[:72]>`.
- The startup log line: task text goes through `_task_log_repr`, which **scrubs secrets first**
  (`_redact`), then collapses newlines and truncates length. Raw text never reaches the log.

### guard (`scripts/vps_spec_guard.sh`)
- check 11: fails if `pc_tasks.py` again derives the file name from the body (`_slug(body…)`)
  or interpolates the body into the commit message.
- check 12: fails if `claude_bridge.py` returns task interpolation in a commit
  (`f"claude: {…}"` / `f"bridge: {…}"`).
- The checks are narrow (they catch exactly the old leak forms), with no broad grep rules — a
  neutral `{task_id}` does not trip them.

### test
- `scripts/test_pc_tasks_metadata.py` (git/network mocked): file name and commit carry no body
  words, id in name == id in commit, body stored in file, a dictated secret scrubbed; on a venv
  it additionally checks the bridge constants are neutral and the log representation is scrubbed.

### effect on the PC side (no PC-code change in this block)
The PC classifier builds a "processed" commit from the file NAME. Because the name is now neutral,
that PC-side commit also stops carrying a fragment of the body — but the PC script itself is **not
changed** here (editing PC hooks is a separate block).

## Block -1C (done) — raw free-text into the notes store is OFF by default

The notes store stops accepting raw free-text by default; structural trackers work as before.
The split is: STRUCTURE (numbers/enums/tags — written) versus FREE TEXT (transcripts, notes —
by default NOT written).

### switch (`bot/config.py`)
- `AIOS_RAW_PERSIST` — default **OFF**. Raw text is stored only on EXPLICIT enable
  (`=1`/`true`/`yes`/`on`). Any other value — empty, invalid, false-like — is treated as OFF
  (fail-safe).
- `AIOS_DIALOGUE_PERSIST_MODE` — default `structured_or_summary` (informational, reserved for a
  future summarizer). On its own it does NOT open raw writes — precedence: `AIOS_RAW_PERSIST=0`
  always wins, regardless of mode.
- The decision is read at call time via `config.raw_allowed()`.

### gate in the storage layer (backstop — catches every call)
- `bot/claude_storage.py`: when OFF, `save_messages` and `append_memory` are a **no-op success**
  (the store is not called, no raw text leaves; chat history lives in memory); `create_chat` and
  `update_chat_title` set a **neutral** title `Chat <date>` instead of free text (the first
  message).
- `bot/aios_notes_store.py`: when OFF, `add_record` writes only STRUCTURE (Type, Value, enum fields,
  numbers, checkboxes), **drops** free-text `Notes`, and uses a **neutral** Name (`<type> <date>`,
  no value). `append_page_notes` when OFF — **no-op success**.
- **Block -1C.1 (UPDATE paths):** the same gate stands on record edits. When OFF, `update_record`
  **does not write** `Notes` and **does not derive** Name/title from a (possibly free-text)
  `value` — but `Value` as tracker content remains (as in `add_record`); structural
  `Date`/numbers/checkboxes update normally. `append_note` when OFF — **no-op success** (no read,
  no write). The edit rests on call-site inspection: `value=` in `update_record` may be free text,
  so Name is not built from it; `append_note` has no callers.

### call-site edits (where free text went into Value/Trigger/Behavior)
The storage layer does not distinguish an enum from free text in Value/Trigger/Behavior, so two
calls are edited pointwise:
- **task-status entry**: when OFF the task does NOT go into title/value/trigger (neutral label,
  trigger empty); structure is preserved.
- **note entry**: when OFF the task/problem does NOT go into trigger/behavior (a neutral constant);
  the transcript and notes are suppressed by the gate itself.
Enum/number-only entries need no edit — their Value/Trigger/Behavior are already enums/numbers.

### summary reads
When OFF, a structured summary reads Value + Behavior (not Notes), so it **does not break**.

### what was NOT done here
- Old records are NOT migrated and NOT deleted (a separate block).
- The encrypted store was NOT built. Raw free text will later move into an encrypted store; for
  now it is simply not written.
- The operator can restore the legacy behavior only deliberately: `AIOS_RAW_PERSIST=1`
  (not recommended).
- The guard is extended with a structural check (check 13): the `raw_allowed` gate is present in
  config + claude_storage + aios_notes_store, default OFF. Test `scripts/test_notion_raw_gate.py`
  (store client mocked, no live store).

## Block -1B (later) — encrypted channel phone -> PC

Full closure of body confidentiality (design, NOT implemented here):
- the bot encrypts the body to the **PC public key** (age / libsodium sealed box); the bot holds
  only the public key — it can write, but not read back;
- only **metadata + an encrypted blob** travel through Git; the name and commit are already
  neutral (Block -1A);
- the private key stays **only on the PC** (OS keyring / file mode 0600); the PC runner decrypts
  locally, shows the text for **confirmation**, and executes only after Approve;
- decryption is fail-closed: on error, do not execute, do not delete, quarantine + notify the
  operator.

## Out of scope (explicitly)
- Migration/scrubbing of old plaintext `_pc_tasks` and old jsonl — a separate block.
- Rewriting Git history, building the encrypted store, changing store persistence — not here.
