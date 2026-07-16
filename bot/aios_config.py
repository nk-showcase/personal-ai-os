"""bot/aios_config.py — typed app_config helpers for cutover-cycle flags.

INERT BY DEFAULT. This module does NOT flip any flag implicitly. It only exposes
typed read/write helpers around :func:`bot.aios_storage.get_config` /
:func:`bot.aios_storage.set_config` with:

* a **safe default per flag** that preserves the *current* bot behaviour;
* an **allowed-value whitelist** per flag (any other stored or candidate value is
  rejected); reads fall back to the safe default, writes raise
  :class:`InvalidFlagValue`;
* a **fail-safe read** that does NOT lazily create the production DB: if the
  AI OS data DB does not exist on disk, every getter returns its safe default
  without any I/O side effect (this matters because cutover code must NOT change
  observable behaviour until the operator explicitly flips a flag).

Defining a setter here is NOT a flip. Only an explicit caller flips a flag, and
the only callers allowed to do that are approved migration scripts.
"""
from __future__ import annotations


class InvalidFlagValue(ValueError):
    """Raised by a setter when the candidate value is not in the flag's allowed set.

    Carries the FLAG NAME and the ALLOWED SET — never a secret or row content.
    """


# ---- Internal helpers ------------------------------------------------------------

def _safe_get(key: str, allowed: tuple, default: str) -> str:
    """Read ``app_config[key]`` with fail-safe semantics.

    Returns ``default`` if any of the following hold:

    * the AI OS data DB does not exist on disk (no lazy create);
    * the row is absent;
    * the stored value is not in ``allowed``;
    * any exception is raised during read.

    This preserves the invariant that the inert default code path is observably
    identical to the pre-cutover behaviour even when the flag layer is invoked.
    """
    try:
        from . import aios_storage as S  # local import: keeps top-level cheap
        if not S.db_path().exists():
            return default
        val = S.get_config(key, default=default)
    except Exception:
        return default
    if val not in allowed:
        return default
    return val


def _validated_set(key: str, value: str, allowed: tuple) -> None:
    """Write ``app_config[key]=value`` after validating ``value in allowed``.

    The setter is an EXPLICIT flip and will lazily create the DB via
    :func:`bot.aios_storage.connect` (intentional — flipping a flag without a
    DB to record it would be a silent failure).
    """
    if value not in allowed:
        raise InvalidFlagValue(
            f"{key}: value not in allowed set; got non-allowed value"
        )
    from . import aios_storage as S
    S.set_config(key, value)


# ---- Cutover combined per-domain store flags -------------------------------------
# ONE flag per domain switches READ and WRITE together. A staged read-first flip would
# corrupt data (read-your-own-writes breaks inside single handler runs), so the forbidden
# interim state (read=sqlite while write=notion) is structurally IMPOSSIBLE here: there is
# simply no separate read flag to diverge. Default 'notion' = current behaviour; the code-level
# default flips to 'sqlite' per domain in the same deploy as that domain's cutover, and the
# Notion-archive step gates on the default already being 'sqlite'.
CUTOVER_DOMAINS = ("chat", "memory", "notes")
STORE_MODE_VALUES = ("notion", "sqlite")
# Per-domain code default: flips to 'sqlite' in the same deploy as the domain's cutover, so a
# missing app_config row can never silently fall a flipped domain back to Notion.
STORE_MODE_DEFAULTS = {"chat": "sqlite", "memory": "sqlite", "notes": "sqlite"}
STORE_MODE_DEFAULT = "notion"  # fallback for any domain missing from the dict


def get_store_mode(domain: str) -> str:
    """Combined read+write source for a cutover domain: 'notion' (legacy) or 'sqlite' (flipped)."""
    if domain not in CUTOVER_DOMAINS:
        raise InvalidFlagValue(f"store_mode: unknown domain {domain!r}")
    return _safe_get(f"store_mode.{domain}", STORE_MODE_VALUES,
                     STORE_MODE_DEFAULTS.get(domain, STORE_MODE_DEFAULT))


def set_store_mode(domain: str, value: str) -> None:
    """Explicit operator-gated flip. Raises InvalidFlagValue on unknown domain/value."""
    if domain not in CUTOVER_DOMAINS:
        raise InvalidFlagValue(f"store_mode: unknown domain {domain!r}")
    _validated_set(f"store_mode.{domain}", value, STORE_MODE_VALUES)


# ---- Claude chat: durable "current chat" pointer -----------------------------------------
# A NON-personal pointer to the active Claude-chat page: just a page id
# (e.g. 'local-<uuid>') — no title, no message text. It lives in the non-encrypted app_config
# because it carries zero personal content. Written ONLY by the keyed worker (which owns the
# DB + key) whenever it appends a chat turn or creates a new chat; read by the keyless receiver
# through the store proxy (structure-only: page_id + a message count). This is the durable
# source of truth that "Continue" resumes, replacing the RAM-only guard that the Gate-0 worker
# migration left permanently false (the receiver no longer holds the transcript in state).
KEY_CURRENT_CHAT = "chat.current_page_id"


def get_current_chat_page_id() -> str | None:
    """Return the current Claude-chat page id, or None when unset/empty. Fail-safe: a missing DB
    (no lazy create) or absent row reads as None."""
    from . import aios_storage as S
    if not S.db_path().exists():
        return None
    try:
        return S.get_config(KEY_CURRENT_CHAT, default=None) or None
    except Exception:
        return None


def set_current_chat_page_id(pid) -> None:
    """Stamp the current-chat pointer (worker-only). Empty/None clears it to ''."""
    from . import aios_storage as S
    S.set_config(KEY_CURRENT_CHAT, pid or "")
