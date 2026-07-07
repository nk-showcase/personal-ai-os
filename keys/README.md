# Encryption keys

This directory holds **public** age recipients only. Private keys (age
identities and the wrapped key-encryption-key) must **never** be committed here
or anywhere in the repository. This file is a template describing how to
generate the keypair and where each half belongs.

At-rest encryption uses [age](https://github.com/FiloSottile/age). The code
shells out to the `age` / `age-keygen` binaries (override the path with
`AIOS_AGE_BIN`). See `docs/security/key-custody-model.md` for the full custody
model.

## What the system needs

| Purpose                     | Env var                       | Half held on the server | Half held offline |
|-----------------------------|-------------------------------|-------------------------|-------------------|
| At-rest content encryption  | `AIOS_CONTEXT_IDENTITY_FILE`  | worker-only identity (private, 0600) | recovery recipient (private, offline) |
| Router transport encryption | `AIOS_ROUTER_IDENTITY_FILE`   | router worker identity (private, 0600) | - |
| Store-write sealing         | `AIOS_STORE_SEAL`             | reuses the worker identity | - |
| Encrypted backups           | `AIOS_BACKUP_AGE_RECIPIENT`   | public recipient only    | private identity offline |

Only the **public recipient** string (`age1...`) is safe to keep in code,
config, or this directory. The private identity (`AGE-SECRET-KEY-...`) must
never leave the machine that owns it.

## 1. Generate the worker identity (on the server)

```sh
mkdir -p "${AIOS_HOME}/.ai-os/keys"
age-keygen -o "${AIOS_HOME}/.ai-os/keys/context.identity"
chmod 600 "${AIOS_HOME}/.ai-os/keys/context.identity"
```

`age-keygen` prints the matching **public recipient** to stderr, e.g.:

```
Public key: age1PLACEHOLDER0000000000000000000000000000000000000000000000000
```

Recover the public half at any time without exposing the private key:

```sh
age-keygen -y "${AIOS_HOME}/.ai-os/keys/context.identity"
```

Point the process at the identity file:

```sh
export AIOS_CONTEXT_IDENTITY_FILE="${AIOS_HOME}/.ai-os/keys/context.identity"
export AIOS_CONTEXT_ENCRYPTION=1
```

## 2. Generate an offline recovery identity (recommended)

Generate a **second** keypair on a machine that is not the server (an
air-gapped laptop or a hardware-backed environment). Content is encrypted to
**both** recipients, so either the server identity or the offline recovery
identity can decrypt it - this is what lets you recover data if the server is
lost.

```sh
age-keygen -o recovery.identity     # keep this file OFFLINE
age-keygen -y recovery.identity     # copy ONLY this public recipient into the code
```

Store the recovery private identity on offline media protected by a passphrase.
Never place it in git, a prompt, a chat message, or the secret manager. Pin its
public recipient in the source (`bot/context_key.py`, `RECOVERY_RECIPIENT`) as
an immutable, non-secret value.

## 3. Router and backup recipients

The router transport and encrypted backups each take a public recipient the
same way. Generate an identity, keep the private half 0600 (router) or offline
(backup), and expose only the `age1...` public string:

```sh
export AIOS_ROUTER_IDENTITY_FILE="${AIOS_HOME}/.ai-os/keys/router.identity"
export AIOS_BACKUP_AGE_RECIPIENT="age1PLACEHOLDER0000000000000000000000000000000000000000000000000"
```

## Do / don't

- **Do** commit `age1...` public recipients (they are non-secret).
- **Do** keep every private identity 0600 and out of git.
- **Don't** ever commit a file containing `AGE-SECRET-KEY-...`.
- **Don't** paste any private key into a prompt, log, chat, or the secret
  manager.
