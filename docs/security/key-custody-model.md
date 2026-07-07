# Key custody model (template)

> How the at-rest encryption key is held, protected, and recovered.
> This is a reference template. Substitute your own recipients, paths, and devices —
> no real key locations, devices, or fingerprints appear here.

## Goals

1. The personal free-text stored by the system (chat transcripts, long-term memory,
   free-text notes) is encrypted at rest under a single master key.
2. The most-attacked process (the Telegram transport) can **never** read that key and
   **never** sees decrypted plaintext.
3. A single operator can recover the data if the working key is lost, using an offline
   copy they alone control — with the fewest terminal steps possible.

## The two-layer envelope

Encryption uses envelope encryption so the master key is touched as rarely as possible.

| Layer | What it protects | Primitive |
|---|---|---|
| **Data key (DEK)** | one per row/field | ChaCha20-Poly1305 (AEAD), fresh 256-bit key per row |
| **Key-encryption key (KEK)** | wraps every DEK | one long-lived 256-bit master key |

A fresh DEK per row gives a one-row blast radius for any nonce mistake or key
compromise, and makes per-row crypto-erase (delete the wrapped DEK) trivial.

The KEK never exists on disk in the clear. It lives only as an `age`-encrypted blob
(referred to below as the *sealed KEK*), wrapped to two independent recipients so that
either one alone can recover it:

```
age -r <working_recipient> -r <recovery_recipient>
```

## The two recipients

| Recipient | Type | Where it lives | Who can read it |
|---|---|---|---|
| **Working identity** | age X25519 private key | a `0600` file on the server, owned by the keyed worker OS user (never the bot user) | the keyed worker process only |
| **Recovery identity** | age X25519 private key | air-gapped, in the operator's sole custody, at least two copies (e.g. paper + offline USB), never on the server or in any cloud | the operator only, on an offline machine |

Only the two **public** recipients ever appear in the codebase or on the server. The
recovery **private** key never touches the server, git, chat, or logs.

### Pinned recovery recipient

The recovery **public** recipient is committed as a git-tracked constant, so the
"the sealed KEK is wrapped to exactly these two recipients" assertion has an immutable
oracle that an attacker or a bug cannot silently rewrite alongside the sealed KEK.

In the reference code this constant is a placeholder:

```python
RECOVERY_RECIPIENT = "age1PLACEHOLDER..."   # replace with your own recovery public recipient
```

Replace it with your own recovery public recipient before enabling encryption.

### Pinned key fingerprint

A one-way fingerprint (SHA-256) of the provisioned KEK bytes is also pinned as a
git-tracked constant. Every time the KEK is unwrapped it is verified against this pin,
**fail-closed**: a swapped or attacker-supplied sealed KEK may decrypt cleanly, but a KEK
that does not match the pin is never *used*, so no ciphertext is ever produced under a
foreign key.

In the reference code this constant is intentionally empty:

```python
KEK_SHA256_PIN = ""   # pin your own KEK fingerprint here
```

Pin your own fingerprint before enabling encryption.

## Provisioning (one-time, operator-gated)

1. Generate the working identity on the server (`age-keygen`), stored `0600`, owned by
   the keyed worker OS user.
2. Generate the recovery identity on the operator's **own offline machine**. The private
   half never leaves that machine; only its public recipient is copied to the server and
   pinned in git.
3. Generate the KEK as 32 CSPRNG bytes in the worker's memory, wrap it to both public
   recipients to produce the sealed KEK, then wipe the plaintext KEK from memory.
4. Record the KEK fingerprint pin and confirm the sealed KEK decodes to exactly the two
   intended recipients.
5. Perform **one live offline decrypt** of the sealed KEK with the recovery identity, to
   prove recovery works before any data is written under this key.

Until the working identity file exists, encryption stays OFF and the system runs
unaffected — provisioning is a deliberate, separate operator step.

## Recovery

If the server or the working identity is lost:

1. On the offline machine, use the recovery identity to decrypt the sealed KEK back to the
   32-byte master key.
2. Provision a fresh working identity on a new server and re-wrap the KEK to the new
   working recipient plus the (unchanged) recovery recipient.
3. Verify the fingerprint pin still matches, and confirm every held identity decrypts the
   re-wrapped blob to identical bytes before putting the new server into service.

Because the KEK bytes are unchanged, the existing ciphertext remains readable — recovery
restores access without re-encrypting the data.

## Rotation and resealing

Resealing (adding or removing a recipient, e.g. rotating the working identity) is a
distinct operation from generating a new key. Generating a new key would orphan all
existing ciphertext, so the two must never be conflated:

- **Reseal** keeps the same KEK bytes and re-wraps them to a new recipient set. It is
  backup-first: a timestamped copy of the current sealed KEK and a confirmed off-server
  copy are made *before* resealing, and every held identity is verified to decrypt the new
  blob to identical bytes before the atomic replace.
- **Generate** mints a brand-new KEK and is only ever used for first-time provisioning.

## What the transport never gets

The Telegram transport process runs as a **separate OS user** that cannot read the
working identity file, is never given the KEK or any DEK, and never receives decrypted
personal plaintext. This is a hard barrier, not a policy: see
[two-user-split-plan.md](two-user-split-plan.md).
