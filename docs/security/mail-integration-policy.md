# Mail integration - safety policy (target design)

> **Status: no mail code ships in this repository.** This page records the policy a mail
> integration must satisfy *before* it is added. It exists so the rules are decided ahead
> of the code, not retrofitted after an incident.

## Phased rollout

1. **Phase 1 - read-only.** The integrations worker may read mailbox headers and bodies.
   Sending is technically absent: no send token is provisioned, no send code path exists.
2. **Phase 2 - guarded send.** Sending may be enabled only after a preview + explicit
   confirm mechanism is implemented and tested: the operator sees the exact outgoing
   message and must explicitly approve it (CONFIRM_SEND) before anything leaves. Until
   that mechanism exists in code, the send token stays out of every allowlist.

## Sensitive-message rules (apply from Phase 1)

- **One-time codes, banking codes, recovery codes, password-reset links, and temporary
  passwords are never shown in chat and never forwarded.** The assistant may report only
  sender / subject / date plus "open this one yourself".
- Message bodies never appear in logs; only value-free metadata may be logged.
- Deleting mail is forbidden to the integration under all phases.
- Mailbox credentials follow the standard secret rules: fetched by name from the secret
  manager, scoped to the integrations service only, never in code, git, logs, or prompts.

## Least privilege

- The mailbox account used is a dedicated one, not the operator's primary account,
  wherever the provider allows it.
- The integrations worker gets a read-only credential in Phase 1; the send credential
  (Phase 2) is a separate secret with its own allowlist entry, so read access never
  implies send access.
