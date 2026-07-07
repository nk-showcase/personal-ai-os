# Two-OS-user privilege split

> The Telegram transport is the most-attacked surface. This document describes how the
> system is split across two operating-system users so that the transport holds no
> encryption keys and cannot read personal plaintext at rest, while a second, keyed user
> does all the decryption.

## The two users

| OS user | Role | Holds keys? | Reads personal plaintext? |
|---|---|---|---|
| **bot user** | receive Telegram updates, authenticate the operator, enqueue intents, deliver replies, emit a heartbeat | **no** | no |
| **keyed worker user** | pick up enqueued work, decrypt the at-rest store, run the model, render and send personal-content replies | **yes** (the at-rest identity file) | yes |

The bot user is deliberately minimal: **receive → auth → enqueue → deliver-reply →
heartbeat**. All business logic and all decryption live behind the queue, under the keyed
worker user.

## Why a second user, not just a second process

Two processes under the *same* user are one trust domain: anything that compromises the
bot process can read the worker's key file. The barrier only holds if the key file is
owned by a **different** user that the bot user has no permission to read. The at-rest
identity file is `0600`, owned by the keyed worker user; the bot user simply cannot open
it.

## The shared queue (crossing the boundary safely)

The two users communicate through a durable SQLite queue that lives outside the code
checkout. The queue must be readable and writable by **both** users, which conflicts with
the single-user `0700`/`0600` default (that default re-clamps the shared directory back to
owner-only on every connect — the original two-user-split blocker).

The fix is a shared POSIX group:

- When a group-sharing environment variable is set, the queue directory becomes
  `2770` (setgid, so new files inherit the group) owned by the shared group, and each
  database file plus its `-wal`/`-shm` siblings become `660` owned by the shared group, on
  **every** connect (so a file created by the other user gets re-grouped).
- When the variable is unset, behaviour is the unchanged single-user `0700`/`0600` default
  — the split is fully inert until explicitly turned on.

A one-time provisioning step (run as the owner or root) fixes the group of any
pre-existing files, because a non-owner cannot change the group of a file it did not
create. After that, the setgid directory and a group-friendly umask on every writer keep
new files group-shared automatically.

## Keeping plaintext off the transport (Gate 0)

Even with the key barrier in place, a naive design could still route decrypted text back
through the shared queue to the transport for delivery. To close that gap, personal-content
replies are **rendered and sent by the keyed worker itself**: the bot supplies only a
chat id and a view intent, and the worker sends the Telegram message directly. Decrypted
plaintext never returns through the shared queue.

This direct-send path is **fail-closed**. If the worker has no Telegram token, every send
raises a typed "send unavailable" error; the caller reports it and falls back to the
existing queue-transit path. Nothing is silently dropped, and behaviour does not change
until the token is granted.

## Secret partitioning

Secrets are partitioned by user, not shared:

- The bot user's environment may hold only the Telegram bot token (the one secret the
  transport legitimately needs to receive updates and deliver replies).
- The keyed worker user's environment holds the at-rest identity file reference and any
  integration secrets. The transport never resolves these.

The transport token is never logged, never included in error messages, and the API URL
that embeds it is never printed.

## Summary of barriers

1. **File ownership** — the at-rest identity is a `0600` file owned by the keyed worker
   user; the bot user cannot read it.
2. **Direct send (Gate 0)** — decrypted personal text is sent by the keyed worker, never
   returned to the transport.
3. **Fail-closed fallback** — missing the token degrades gracefully to the queue path
   rather than dropping messages.
4. **Secret partitioning** — each user holds only the secrets its role requires.
