# Migration story: from a hosted document store to a local encrypted store

> One narrative of two coupled moves that were carried out together:
> (1) moving the operator's personal free-text out of a third-party hosted document store
> and into a local, encrypted-at-rest store, and (2) thinning the Telegram transport down
> to a pure message pipe so it never holds keys or plaintext. High level, no code cites.

## Where it started

The system began with the operator's most sensitive free-text — chat transcripts,
long-term memory facts, per-domain notes — living as plaintext in a third-party hosted
document store. The Telegram bot was a monolith: a single process that received messages,
resolved every integration secret at import time, read and wrote the hosted store
directly, and rendered every reply. That process was both the most-attacked surface and
the one holding all the keys and all the plaintext. Those two facts are the whole reason
for the migration.

## The two targets

**Target 1 — a local encrypted store.** Replace the hosted document store with a local
SQLite store whose personal free-text columns are encrypted at rest under envelope
encryption (a per-row data key, all wrapped under one master key). The master key is held
only by a keyed worker; see [key custody](../security/key-custody-model.md).

**Target 2 — a thin transport.** Reduce the bot to *receive → authenticate → enqueue →
deliver-reply → heartbeat*. All business logic, all decryption, and all personal-content
rendering move behind a durable queue, into a keyed worker running as a
[separate OS user](../security/two-user-split-plan.md).

The two targets are coupled: the store cannot be encrypted-only until the process that
reads it is the keyed worker, and the transport cannot be thinned until there is a store
and a queue for the worker to talk to.

## How the data moved (non-destructive, verified)

The data migration was designed to be safe to re-run and impossible to corrupt silently:

1. **Read-only source.** The migration engine only *reads* the hosted store. It never
   deletes, archives, or mutates the source during migration.
2. **Encrypt on save.** Each record is read, encrypted through the single encryption
   chokepoint, and written to the local store keyed on the record's stable source id.
   Because writes upsert on that id, re-running the migration rewrites the same rows rather
   than duplicating them.
3. **Fresh-connection verify.** After each save, the row is read back on a fresh
   connection, decrypted, and byte-compared against the source. Structured fields are
   compared numerically; free-text fields byte-exact. A mismatch is counted and the id
   recorded, and the source row is left completely untouched.
4. **Encryption required.** The engine refuses to run while encryption is off — migrating
   into cleartext would defeat the purpose.

The hosted store stayed the system of record throughout migration. Only after a domain's
full read surface was built and verified did the system flip that domain over.

## How the cutover was gated

The read side and the write side of each domain were flipped **together**, per domain, via
one combined switch — there was never a window where the system read locally but still
wrote to the hosted store, or vice versa. Before any flip:

- The local read surface for that domain (queries, filters, ordering, decrypt-scan search)
  was built and validated by a dual-write-and-compare check, not by a live stale read.
- Deletions were made representable locally (an archive flag plus archive/restore keyed on
  the source id), so nothing became unexpressible after cutover.
- A final catch-up ran under an explicit write freeze, reconciling the local id set against
  the full source id set and reporting the counts.

Only once the local store held and re-verified every record was the hosted store archived
(behind a separate, recovery-gated step) and taken out of the write path.

## How the transport got thin

The transport was reduced to a pipe by moving work behind a durable queue:

- **Enqueue, don't execute.** The only write the transport performs is to authenticate the
  operator and enqueue an intent row; it acknowledges and stops there.
- **Reply by polling.** A poller forwards worker-produced replies back to the operator. It
  reads only the replies table (never the inbound task text, which is personal plaintext),
  selects undelivered rows, sends them, and stamps them delivered under an atomic claim — so
  a transport restart can neither double-send a reply nor lose one produced while the
  transport was down.
- **Direct send for personal content.** For personal-content views the keyed worker renders
  and sends the Telegram message itself; decrypted text never returns through the shared
  queue. Only value-free structure (sent / not-sent plus a summary) crosses back.
- **Heartbeat stays.** A value-free "I'm alive" ping remains in the transport as the in-bot
  half of a dead-man's switch.

The load-bearing correction discovered mid-migration: the secrets were resolved as an
*import side-effect* of the monolith, so simply moving the handler modules out did not
remove the keys from the transport. The files that actually resolved the forbidden secrets
had to be split out of the transport's import chain, not merely retained.

## Where it ended

After the migration the personal free-text lives in a local store, encrypted at rest under
a master key that only the keyed worker can read. The transport is a thin pipe that holds
one token, no encryption keys, and no personal plaintext. The keyed worker, running as a
separate OS user, does all decryption and sends all personal-content replies directly. The
two moves together turn "the most-attacked process holds everything" into "the
most-attacked process holds almost nothing."
