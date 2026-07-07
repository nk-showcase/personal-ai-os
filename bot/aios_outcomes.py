"""bot/aios_outcomes.py -- Stage 7 no-silent-failure outcome model.

Every handler invocation MUST return an :class:`Outcome`. There is no None, no
silent return, no exception swallowing. The user-visible message is
derived deterministically from the state via :func:`user_message`; the message
map covers every :class:`OutcomeState` member (asserted by
``scripts/test_aios_outcomes.py``). Adding a new state without updating
``_USER_MESSAGES`` fails the test — which catches future "swallow this case"
regressions before they reach production.

Outcome objects may carry a short, sanitized ``tag`` identifying the failure
class (e.g. ``"todoist_io"``, ``"llm_down"``, ``"amount_out_of_range"``) for
internal logs. The tag is normalized to ``[a-z0-9_]`` and length-capped at 32
chars. Outcome NEVER carries user PII, free text, row contents, or secret
values.

The ``unrouted_inbox`` table (defined in :mod:`bot.aios_storage`, Stage 4
schema) is reserved as the eventual dead-letter store. Stage 7 keeps writes
to it OFF; :func:`unrouted_inbox_enabled` returns ``False`` unless the env
flag ``AIOS_UNROUTED_INBOX_ENABLED`` is truthy. The foundation is in place so
later stages can flip it without refactor.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum


class OutcomeState(str, Enum):
    """The closed set of handler outcomes.

    Adding a new member requires updating ``_USER_MESSAGES`` (enforced by
    :func:`_every_state_has_message` and the outcome tests).
    """
    SAVED = "saved"
    NOT_UNDERSTOOD = "not_understood"
    CLASSIFIER_UNAVAILABLE = "classifier_unavailable"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    VALIDATION_FAILED = "validation_failed"
    DEFERRED_DISABLED = "deferred_disabled"
    # B-02B (integrations-worker prep): the write request was durably queued
    # (e.g. in request_queue) and a definitive result will arrive
    # asynchronously.  DISTINCT from DEFERRED_DISABLED ("feature temporarily
    # off"): ACCEPTED means "we did our part, hold tight"; deferred
    # means "this feature is off".
    ACCEPTED = "accepted"


# Single source of truth for user-visible messages. Adding a new OutcomeState
# without updating this map will fail scripts/test_aios_outcomes.py.
_USER_MESSAGES: dict[OutcomeState, str] = {
    OutcomeState.SAVED:                   "saved",
    OutcomeState.NOT_UNDERSTOOD:          "not understood",
    OutcomeState.CLASSIFIER_UNAVAILABLE:  "feature temporarily off",
    OutcomeState.STORAGE_UNAVAILABLE:     "could not save",
    OutcomeState.VALIDATION_FAILED:       "not understood",
    OutcomeState.DEFERRED_DISABLED:       "feature temporarily off",
    OutcomeState.ACCEPTED:                "accepted for processing",
}

_TAG_RE = re.compile(r"[^a-z0-9_]")
_TAG_MAX = 32


def _norm_tag(t: str) -> str:
    """Lowercase, strip everything outside ``[a-z0-9_]``, cap at 32 chars."""
    return _TAG_RE.sub("", (t or "").lower())[:_TAG_MAX]


@dataclass(frozen=True)
class Outcome:
    """An immutable handler outcome.

    ``state`` -> :class:`OutcomeState`. ``tag`` is a short ASCII identifier for
    the failure class (e.g. ``"llm_down"``, ``"todoist_io"``,
    ``"amount_out_of_range"``). Normalized at construction to ``[a-z0-9_]``
    and length-capped at 32 chars. NEVER contains user free text, row
    contents, or secret values.
    """
    state: OutcomeState
    tag: str = ""

    def __post_init__(self) -> None:
        # frozen=True blocks normal assignment; object.__setattr__ is the
        # documented escape hatch used inside __post_init__.
        object.__setattr__(self, "tag", _norm_tag(self.tag))


# ---- Constructors (cheap, explicit) ----------------------------------------------

def saved(tag: str = "") -> Outcome:
    return Outcome(OutcomeState.SAVED, tag=tag)


def not_understood(tag: str = "") -> Outcome:
    return Outcome(OutcomeState.NOT_UNDERSTOOD, tag=tag)


def classifier_unavailable(tag: str = "") -> Outcome:
    return Outcome(OutcomeState.CLASSIFIER_UNAVAILABLE, tag=tag)


def storage_unavailable(tag: str = "") -> Outcome:
    return Outcome(OutcomeState.STORAGE_UNAVAILABLE, tag=tag)


def validation_failed(tag: str = "") -> Outcome:
    return Outcome(OutcomeState.VALIDATION_FAILED, tag=tag)


def deferred_disabled(tag: str = "") -> Outcome:
    return Outcome(OutcomeState.DEFERRED_DISABLED, tag=tag)


def accepted(tag: str = "") -> Outcome:
    """B-02B: write request durably queued; result arrives asynchronously."""
    return Outcome(OutcomeState.ACCEPTED, tag=tag)


# ---- Message + logging helpers ---------------------------------------------------

def user_message(outcome: Outcome) -> str:
    """Map an :class:`Outcome` to its user-visible message.

    Raises ``KeyError`` if the state has no mapping — intentional so a future
    "you added a state, did you map it?" surfaces at test time, not silently
    in production.
    """
    return _USER_MESSAGES[outcome.state]


def internal_log_line(outcome: Outcome) -> str:
    """Structured single-line log entry. State + sanitized tag only.

    NEVER contains user PII, free text, secret values, or row contents.
    """
    return f"outcome state={outcome.state.value} tag={outcome.tag or '-'}"


# ---- Feature flag for the eventual dead-letter store -----------------------------

UNROUTED_INBOX_FLAG = "AIOS_UNROUTED_INBOX_ENABLED"


def unrouted_inbox_enabled() -> bool:
    """Whether the ``unrouted_inbox`` dead-letter store is enabled.

    Stage 7 keeps this OFF by default. The write code path is NOT yet
    implemented in this module; later stages will add it behind this flag.
    Returns ``True`` only when the env value (case-insensitive, stripped)
    is one of ``{1, true, yes, on}``.
    """
    raw = (os.getenv(UNROUTED_INBOX_FLAG) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ---- Self-coverage assertion (importable for sanity checks) ----------------------

def _every_state_has_message() -> bool:
    """``True`` iff every :class:`OutcomeState` member is in ``_USER_MESSAGES``."""
    return all(s in _USER_MESSAGES for s in OutcomeState)
