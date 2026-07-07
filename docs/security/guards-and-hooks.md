# Technical guards

These are technical barriers, not "agreements". Whatever is forbidden must be blocked by code.

## Implemented guards

The following guards live inside the service code (worker-side Python guards) and are the mechanisms present in this reference tree.

| Guard | What it does | Where it lives |
|---|---|---|
| email-sensitive-filter | Marks OTP / banking / recovery / reset messages as sensitive and hides their body | Python guard in integrations-worker |
| confirm-before-email-send | Blocks sending without preview + explicit confirmation | Python guard in integrations-worker |
| cache-fallback-warning | When running on the last-known-good cache instead of the secret manager, warns the operator; enters degraded mode if the cache is stale | Python guard in each service |

## Design principle: intentional duplication

Critical prohibitions (pushing to the main branch, force-push, printing secret values) are meant to be enforced in two independent places, so that bypassing one layer does not open the action. The reference tree ships the worker-side Python guards above; the assistant-side and git-level layers described in the architecture docs are provisioned per deployment.

> Boundary: the assistant-level hooks (secret-scan-before-output, block-secrets-in-prompts, block-destructive-delete, block-direct-push-to-main, block-print-secrets) and the git `pre-push` hooks (block-force-push, block-direct-push-to-main) are part of the target design but are not included as runnable code in this reference tree — the hooks directory ships empty. See `docs/security/threat-model.md`.
