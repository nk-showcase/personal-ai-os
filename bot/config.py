import os

from dotenv import load_dotenv

from .secrets_loader import get_secret, SecretAccessDenied  # safe loader: env -> bitwarden -> disk-cache

load_dotenv()

# --- Secrets (via the safe loader; values are never logged) ---
# config is imported by EVERY service. Under the least-privilege lock (AIOS_SERVICE_NAME), a
# service that is NOT granted the token (e.g. claude-worker, which does not send to Telegram)
# gets SecretAccessDenied right at import. We catch it -> "" (that service does not use the
# token anyway). A permitted service (transport) gets the real value, and if it is genuinely
# absent, required=True raises a loud MissingSecretError, as before.
try:
    TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", required=True)
except SecretAccessDenied:
    TELEGRAM_BOT_TOKEN = ""
TELEGRAM_OWNER_ID = int(get_secret("TELEGRAM_OWNER_ID", default="0") or "0")
# Integration / LLM / GitHub secrets are NOT resolved at import. Importing config
# (the telegram-bot needs only the token above) must not pull those keys into
# secrets_loader._mem. Consumers fetch them lazily at call time:
# get_secret("<NAME>", default="") in the worker-side module that uses them.

# --- NOT secrets: Notion database identifiers (plain config, not via the secret loader) ---
# Read only from the environment, with empty defaults. Set these in your own deployment; see
# docs and .env.example. An empty value means the corresponding feature is disabled at runtime.
NOTION_CLAUDE_CHATS_DB = os.getenv("NOTION_CLAUDE_CHATS_DB", "")
NOTION_CLAUDE_MEMORY_PAGE = os.getenv("NOTION_CLAUDE_MEMORY_PAGE", "")

# TELEGRAM_BOT_TOKEN was already validated by the loader (required=True -> MissingSecretError).
if not TELEGRAM_OWNER_ID:
    raise ValueError("TELEGRAM_OWNER_ID is required")


# --- Memory Safety Block -1C: gate RAW free-text persistence to Notion ---
# Default OFF is the safest behavior: NO raw transcripts / free-text notes / memory
# facts / free titles go to Notion. Structured tracker fields are unaffected. Writing
# raw free text to Notion requires an EXPLICIT opt-in (AIOS_RAW_NOTION_PERSIST=1).
# Anything else — unset, empty, invalid, or false-like — is treated as OFF (fail-safe).
def _is_truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


_VALID_PERSIST_MODES = {"off", "structured", "structured_or_summary", "raw_local", "raw"}

# Explicit opt-in flag. Default OFF. Read once here; the live value is what
# notion_raw_allowed() returns (tests/ops can flip the module attribute).
RAW_NOTION_PERSIST = _is_truthy(os.getenv("AIOS_RAW_NOTION_PERSIST"))

# Informational mode (reserved for the future summarizer/KDB). Bad/missing -> safe default.
# It NEVER widens the Notion raw gate on its own — only the flag above does (see precedence).
_dialogue_mode = (os.getenv("AIOS_DIALOGUE_PERSIST_MODE") or "structured_or_summary").strip().lower()
DIALOGUE_PERSIST_MODE = _dialogue_mode if _dialogue_mode in _VALID_PERSIST_MODES else "structured_or_summary"


def notion_raw_allowed() -> bool:
    """True only when the owner has EXPLICITLY opted into raw free-text Notion writes.

    Precedence (Block -1C): AIOS_RAW_NOTION_PERSIST=0 HARD-WINS regardless of mode —
    raw free text / transcripts reach Notion only on an explicit truthy flag. Read at
    call time so tests/ops can flip ``config.RAW_NOTION_PERSIST`` without re-importing.
    Fail-safe: any non-truthy value (unset/empty/invalid) => False (structured-only).
    """
    return RAW_NOTION_PERSIST


# --- S6 S-1: unified-router feature flag (default OFF) ---
# OFF => the transport catch-all is NOT registered; handle_text/handle_photo run the legacy
# in-process path exactly as today (bot byte-identical). Opt-in is an EXPLICIT truthy env value.
# NOTE: even at ON, S-1 registers the catch-all at group=1 (structurally shadowed by group-0
# handle_text); live interception is deferred to S-5. See docs/architecture/s6-thin-transport-plan.md.
UNIFIED_ROUTER = _is_truthy(os.getenv("AIOS_UNIFIED_ROUTER"))  # default OFF


def unified_router_enabled() -> bool:
    """True only on explicit owner opt-in (AIOS_UNIFIED_ROUTER in {1,true,yes,on}).
    Read at call time so tests/ops flip config.UNIFIED_ROUTER without re-import.
    Fail-safe: unset/empty/invalid/false-like => False (legacy path, zero change)."""
    return UNIFIED_ROUTER


# --- S6 S-2: unified-router INBOUND-SEAL flag (default OFF) ---
# OFF => the route pipe stores the plain JSON envelope exactly as S-1 (byte-identical).
# ON  => router_transport seals the envelope to the WORKER age recipient before enqueue;
# router_worker decrypts with its 0600 identity FILE. INDEPENDENT of AIOS_UNIFIED_ROUTER.
#
# SCOPE: when ON, this flag encrypts only the INBOUND leg (task_text). Reply content is
# stored as plaintext at rest (subject to secret redaction and a length cap); reply-at-rest
# encryption is a separate symmetric mechanism tracked in docs/security/threat-model.md.
# Do not read this flag as end-to-end.
ROUTER_ENCRYPT = _is_truthy(os.getenv("AIOS_ROUTER_ENCRYPT"))  # default OFF


def router_encrypt_enabled() -> bool:
    """True only on explicit owner opt-in (AIOS_ROUTER_ENCRYPT in {1,true,yes,on}).
    Read at call time so tests/ops flip config.ROUTER_ENCRYPT without re-import.
    Fail-safe: unset/empty/invalid/false-like => False (plain S-1 path, zero change)."""
    return ROUTER_ENCRYPT


# --- STORE inbound-seal flag (default OFF) — asymmetric age seal of the store-op envelope ---
# OFF => integrations_proxy.store_request/view_request pack the plain {"op_b64": …} envelope
# exactly as today (byte-identical). ON  => the envelope is age-sealed to the WORKER's public
# recipient before enqueue; the keyed worker dual-reads (op_b64_sealed -> open, else op_b64).
# INDEPENDENT of AIOS_UNIFIED_ROUTER / AIOS_ROUTER_ENCRYPT. Fails CLOSED while the real worker
# recipient is a placeholder (seal raises -> caller degrades to unavailable, NEVER plaintext).
STORE_SEAL = _is_truthy(os.getenv("AIOS_STORE_SEAL"))  # default OFF


def store_seal_enabled() -> bool:
    """True only on explicit owner opt-in (AIOS_STORE_SEAL in {1,true,yes,on}).
    Read at call time so tests/ops flip config.STORE_SEAL without re-import.
    Fail-safe: unset/empty/invalid/false-like => False (plain {"op_b64": …} path, zero change)."""
    return STORE_SEAL

# NOTE: the S6 dispatch slice-1 flag (AIOS_ROUTER_DISPATCH) lives in bot/router_dispatch.py, NOT
# here — config imports python-dotenv and resolves the bot token at import, which is absent on the
# dev mac's bare python3 where the offline router/seal tests run. Keeping the worker's dispatch
# gate in the import-light router_dispatch module keeps router_worker mac-testable.
