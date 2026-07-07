"""bot/secrets_loader.py — safe secret-loading layer.

Resolution order (first hit wins):
  1. in-process in-memory cache
  2. environment variable (primary source for local/dev/test)
  3. Bitwarden Secrets Manager via the `bws` CLI (production; SAFE no-op if `bws`
     is not installed or BWS_ACCESS_TOKEN is unset)
  4. on-disk last-known-good cache in ~/.ai-os/secrets-cache (availability fallback,
     only for NON-critical runtime secrets; per-secret; chmod 600)

NEVER logs or prints secret VALUES — only names and status.
Cache files live OUTSIDE the repo and are not committed (see .gitignore + secrets-management.md).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("aios.secrets")

# Backend: "env" (default — dev/test) | "bitwarden" (production runtime fetch).
BACKEND = os.getenv("AIOS_SECRETS_BACKEND", "env").strip().lower()

# On-disk cache directory (see docs/security/secrets-management.md). Owner-only (700/600).
CACHE_DIR = Path(
    os.getenv("AIOS_SECRETS_CACHE_DIR", str(Path.home() / ".ai-os" / "secrets-cache"))
)

# These secrets are NEVER written to disk (forbidden_everywhere + critical).
NO_DISK_CACHE = {
    "MASTER_PASSWORDS",
    "RECOVERY_CODES",
    "BANKING_OTP",
    "PERSONAL_PASSWORDS",
    "PREVIOUS_HOSTING_MASTER_TOKEN",
    "CLAUDE_OAUTH",
    # Claude Code login (subscription OAuth token) — crown-jewel; never touch disk.
    "CLAUDE_CODE_OAUTH_TOKEN",
    # Context encryption master key (KEK identity) — NEVER on disk through this layer.
    # It lives as a worker-only age-identity FILE, not via get_secret/Bitwarden (see context_key.py).
    "CONTEXT_STORE_IDENTITY",
    "CONTEXT_KDB_MASTER_KEY",
}

_mem: dict[str, str] = {}


# --- Per-service access policy (least privilege; mirrors config/secrets-map.yaml) ---
# A process declares its service via AIOS_SERVICE_NAME (set per-systemd-unit). When set,
# get_secret() DENIES any secret not allowed to that service (fail-closed). When unset
# (dev/test/not-yet-provisioned) the check is DISABLED, i.e. behaviour is unchanged and
# nothing breaks. This map MUST match services.*.allowed_secrets in config/secrets-map.yaml
# — drift is caught by scripts/test_secret_policy.py.
SERVICE_ALLOWED: dict[str, set[str]] = {
    "telegram-bot": {"TELEGRAM_BOT_TOKEN"},
    "claude-worker": {"GITHUB_REPO_WRITE_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"},
    "integrations-worker": {
        "NOTION_API_KEY",
        "TODOIST_API_KEY",
        # Part B: the keyed worker sends personal-content views to Telegram ITSELF, so
        # decrypted text never returns through the shared queue to the transport.
        "TELEGRAM_BOT_TOKEN",
        # LLM ops run in THIS worker (they call anthropic_messages -> get_secret). Without
        # this, enabling the AIOS_SERVICE_NAME lock would deny the key and crash every LLM call.
        "ANTHROPIC_API_KEY",
    },
}

# Never handed to ANY service (mirrors forbidden_everywhere in secrets-map.yaml).
FORBIDDEN_EVERYWHERE: set[str] = {
    "RECOVERY_CODES",
    "MASTER_PASSWORDS",
    "BANKING_OTP",
    "PERSONAL_PASSWORDS",
    "PREVIOUS_HOSTING_MASTER_TOKEN",
}

# The universe of names the policy governs. Names OUTSIDE this set — non-secret config /
# identifiers — are not checked and resolve as before.
_MANAGED: set[str] = set().union(*SERVICE_ALLOWED.values()) | FORBIDDEN_EVERYWHERE


class SecretAccessDenied(RuntimeError):
    """A process requested a secret not allowed to its service. The message contains only
    the secret NAME and the service name, NEVER the value."""


def _current_service() -> str:
    return (os.getenv("AIOS_SERVICE_NAME") or "").strip().lower()


def _enforce_policy(name: str) -> None:
    """Fail-closed per-service access check. No-op until AIOS_SERVICE_NAME is set."""
    service = _current_service()
    if not service:
        return  # check is off until the unit sets AIOS_SERVICE_NAME
    if name in FORBIDDEN_EVERYWHERE:
        raise SecretAccessDenied(f"Secret '{name}' is forbidden for every service")
    if name in _MANAGED:
        allowed = SERVICE_ALLOWED.get(service)
        if allowed is None:
            raise SecretAccessDenied(
                f"Unknown service '{service}' may not resolve managed secret '{name}'"
            )
        if name not in allowed:
            raise SecretAccessDenied(f"Service '{service}' is not allowed secret '{name}'")


class MissingSecretError(RuntimeError):
    """A required secret was not found. The message contains only the NAME, never the value."""


def _from_env(name: str):
    v = os.getenv(name)
    return v if v not in (None, "") else None


def _bws_ready() -> bool:
    return shutil.which("bws") is not None and bool(os.getenv("BWS_ACCESS_TOKEN"))


def _from_bitwarden(name: str):
    """Fetch via bws. SAFE: return None (no crash) if bws/token are absent or the output
    format is unexpected."""
    if BACKEND != "bitwarden" or not _bws_ready():
        return None
    try:
        proc = subprocess.run(
            ["bws", "secret", "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            log.warning("secrets: bws list rc=%s for %s", proc.returncode, name)
            return None
        for item in json.loads(proc.stdout or "[]"):
            if isinstance(item, dict) and item.get("key") == name:
                return item.get("value")
        return None
    except Exception as e:  # noqa: BLE001 — any failure = safe None
        log.warning("secrets: bws error for %s: %s", name, type(e).__name__)
        return None


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{name}.secret"


def _from_disk_cache(name: str):
    if name in NO_DISK_CACHE:
        return None
    p = _cache_path(name)
    try:
        if p.is_file():
            v = p.read_text(encoding="utf-8").rstrip("\n")
            return v or None
    except Exception as e:  # noqa: BLE001
        log.warning("secrets: cache read error for %s: %s", name, type(e).__name__)
    return None


def _write_disk_cache(name: str, value: str) -> None:
    if name in NO_DISK_CACHE or not value:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(CACHE_DIR, 0o700)
        except OSError:
            pass
        p = _cache_path(name)
        p.write_text(value, encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        log.info("secrets: cached %s on disk (value not logged)", name)
    except Exception as e:  # noqa: BLE001
        log.warning("secrets: cache write error for %s: %s", name, type(e).__name__)


def get_secret(name: str, *, required: bool = False, default=None, cache_to_disk: bool = False):
    """Return a secret value by name. NEVER logs the value.

    required=True and not found → MissingSecretError("Missing required secret: <NAME>").
    Before any resolution — a fail-closed per-service access check (_enforce_policy).
    """
    _enforce_policy(name)
    if name in _mem:
        return _mem[name]

    val = _from_env(name)
    src = "env"
    if val is None:
        val = _from_bitwarden(name)
        src = "bitwarden"
        if val is not None and cache_to_disk:
            _write_disk_cache(name, val)
    if val is None:
        val = _from_disk_cache(name)
        src = "disk-cache"

    if val is None:
        if required:
            raise MissingSecretError(f"Missing required secret: {name}")
        log.info("secrets: %s not set (src=none) -> default", name)
        return default

    _mem[name] = val
    log.info("secrets: loaded %s (src=%s)", name, src)
    return val


def cache_dir_is_outside_repo(repo_root: str | None = None) -> bool:
    """Test helper: the cache dir is NOT inside the repo (so it will not be committed)."""
    repo = Path(repo_root or Path(__file__).resolve().parent.parent).resolve()
    try:
        CACHE_DIR.resolve().relative_to(repo)
        return False  # inside the repo — bad
    except ValueError:
        return True  # outside the repo — good
