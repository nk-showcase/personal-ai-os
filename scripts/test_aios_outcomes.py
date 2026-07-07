#!/usr/bin/env python3
"""Plain-python tests (no pytest) for the Stage 7 no-silent-failure outcome model.

SAFE: pure functions; no IO, no network, no DB, no services.

Covers:
  1. every OutcomeState has a user-visible message (no silent unmapped state);
  2. each state -> exact expected user-facing message;
  3. Outcome is frozen (immutable);
  4. tag is normalized to [a-z0-9_] and length-capped at 32 chars;
  5. internal_log_line is single-line, no PII, structured;
  6. unrouted_inbox_enabled() is False by default;
  7. unrouted_inbox_enabled() respects truthy/falsey env values;
  8. user_message raises KeyError for a state not in the map.

Run:  PYTHONPATH=<repo> python3 scripts/test_aios_outcomes.py    (exit 0 = OK, 1 = FAIL)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from bot import aios_outcomes as O

    failed: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    # ---- Req 1: coverage of every state ------------------------------------------
    check("_every_state_has_message() is True", O._every_state_has_message() is True)
    check("every OutcomeState member has a message",
          all(s in O._USER_MESSAGES for s in O.OutcomeState))

    # ---- Req 2: exact messages ---------------------------------------------------
    cases = [
        (O.saved(),                    "saved"),
        (O.not_understood(),           "not understood"),
        (O.validation_failed(),        "not understood"),
        (O.classifier_unavailable(),   "feature temporarily off"),
        (O.storage_unavailable(),      "could not save"),
        (O.deferred_disabled(),        "feature temporarily off"),
        # write request durably queued -> definitive result arrives async.
        (O.accepted(),                 "accepted for processing"),
    ]
    for outcome, expected in cases:
        check(
            f"user_message({outcome.state.value}) == '{expected}'",
            O.user_message(outcome) == expected,
        )

    # ---- ACCEPTED is a distinct state with a distinct message --------
    check("OutcomeState.ACCEPTED exists",
          hasattr(O.OutcomeState, "ACCEPTED")
          and O.OutcomeState.ACCEPTED.value == "accepted")
    check("accepted() helper returns ACCEPTED state",
          O.accepted().state == O.OutcomeState.ACCEPTED)
    check("accepted message is 'accepted for processing'",
          O.user_message(O.accepted()) == "accepted for processing")
    check("accepted message is non-empty",
          len(O.user_message(O.accepted())) > 0)
    check("accepted DISTINCT from deferred_disabled (different state)",
          O.accepted().state != O.deferred_disabled().state)
    check("accepted DISTINCT from deferred_disabled (different message)",
          O.user_message(O.accepted()) != O.user_message(O.deferred_disabled()))
    check("accepted tag normalization preserved (alnum+_ lowercased)",
          O.accepted(tag="My Tag!").tag == "mytag")
    # ACCEPTED also distinct from CLASSIFIER_UNAVAILABLE (which shares the
    # "feature off" message with DEFERRED_DISABLED).  ACCEPTED is its own
    # category and MUST NOT collapse into either.
    check("accepted DISTINCT from classifier_unavailable (state)",
          O.accepted().state != O.classifier_unavailable().state)
    check("accepted DISTINCT from classifier_unavailable (message)",
          O.user_message(O.accepted()) != O.user_message(O.classifier_unavailable()))
    check("accepted message NOT in {'feature off' family}",
          O.user_message(O.accepted()) not in {
              O.user_message(O.deferred_disabled()),
              O.user_message(O.classifier_unavailable()),
          })
    # ACCEPTED Outcome is frozen (inherits Outcome dataclass invariant).
    out_acc = O.accepted()
    mutated_acc = False
    try:
        out_acc.state = O.OutcomeState.NOT_UNDERSTOOD  # type: ignore[misc]
        mutated_acc = True
    except Exception:
        mutated_acc = False
    check("accepted Outcome is frozen (cannot mutate state)",
          mutated_acc is False)
    # internal_log_line(accepted) uses state.value + tag; MUST NOT leak
    # the user-facing message.
    log_acc = O.internal_log_line(O.accepted(tag="queued"))
    check("internal_log_line(accepted) includes state=accepted",
          "state=accepted" in log_acc)
    check("internal_log_line(accepted) does NOT leak user message",
          "accepted for processing" not in log_acc)

    # ---- Req 3: Outcome is frozen -----------------------------------------------
    out = O.saved()
    mutated = False
    try:
        out.state = O.OutcomeState.NOT_UNDERSTOOD   # type: ignore[misc]
        mutated = True
    except Exception:
        mutated = False
    check("Outcome is frozen (cannot mutate state)", mutated is False)

    # ---- Req 4: tag normalization -----------------------------------------------
    check("'LLM Down!' -> 'llmdown' (lower + strip non-[a-z0-9_])",
          O.not_understood(tag="LLM Down!").tag == "llmdown")
    check("tag length capped at 32",
          len(O.storage_unavailable(tag="x" * 100).tag) == 32)
    check("empty tag stays empty",
          O.validation_failed(tag="").tag == "")
    check("alnum + underscore preserved (lowercased)",
          O.validation_failed(tag="Hello_World_123").tag == "hello_world_123")
    check("special chars stripped, underscores kept",
          O.validation_failed(tag="foo bar-baz_qux!").tag == "foobarbaz_qux")

    # ---- Req 5: internal_log_line shape -----------------------------------------
    log = O.internal_log_line(O.classifier_unavailable(tag="llm_down"))
    check("internal_log_line includes state",
          "state=classifier_unavailable" in log)
    check("internal_log_line includes tag", "tag=llm_down" in log)
    check("internal_log_line is single line", "\n" not in log)
    check("empty tag rendered as '-'",
          "tag=-" in O.internal_log_line(O.saved()))

    # ---- Req 6 + 7: unrouted_inbox feature flag --------------------------------
    os.environ.pop(O.UNROUTED_INBOX_FLAG, None)
    check("unrouted_inbox_enabled() False with no env",
          O.unrouted_inbox_enabled() is False)
    for v in ("0", "false", "no", "off", "", "anything", "  ", "FALSE", "OFF"):
        os.environ[O.UNROUTED_INBOX_FLAG] = v
        check(f"unrouted_inbox_enabled() False for {v!r}",
              O.unrouted_inbox_enabled() is False)
    for v in ("1", "true", "True", "YES", "on", " on ", "ON"):
        os.environ[O.UNROUTED_INBOX_FLAG] = v
        check(f"unrouted_inbox_enabled() True for {v!r}",
              O.unrouted_inbox_enabled() is True)
    os.environ.pop(O.UNROUTED_INBOX_FLAG, None)

    # ---- Req 8: user_message raises on bogus state ------------------------------
    class FakeOutcome:
        state = "NOT_A_STATE"
        tag = ""
    raised_keyerror = False
    try:
        O.user_message(FakeOutcome())   # type: ignore[arg-type]
    except KeyError:
        raised_keyerror = True
    except Exception:
        pass
    check("user_message raises KeyError on unmapped state", raised_keyerror is True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
