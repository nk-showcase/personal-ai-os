#!/usr/bin/env python3
"""Plain-python tests (no pytest) for B-02D inert worker contract
(``bot.aios_worker_contract``).

SAFE: temp DBs only; SYNTHETIC payloads; NO network, NO live Notion, NO
services, NO production-DB writes, NO secret values used.  Mirrors the
``scripts/test_aios_*.py`` style.

Covers:

  Constants + import safety (Req 1-4):
    1. Module imports cleanly with NO httpx / requests / live clients
    2. POLL_DEADLINE_CEILING_SECONDS == 15
       DEFAULT_POLL_DEADLINE_SECONDS in [8, 12] and < ceiling
    3. DEFAULT_WRITE_TTL_SECONDS >> 2 * DEFAULT_LEASE_SECONDS
    4. LOCAL_FALLBACK_NOTE == "from the local copy" exact match

  build_stable_correlation_id (Req 5-9):
    5. deterministic: same inputs -> identical output
    6. 64-char lowercase hex
    7. different business_key -> different id
    8. different attempt_window -> different id
    9. invalid kind -> InvalidKindError

  Idempotent retry contract (Req 10-12):
   10. user-action retry reuses correlation_id; B-02C dedupes; only 1 row
   11. enqueue_write_request 2nd call returns SAME cid + SAME row state
   12. duplicate enqueue does NOT double the queue count

  WRITE-kind gate + verbatim payload storage (Req 13-17):
   13. a READ kind passed to enqueue_write_request -> ValueError
   14. an unknown kind passed to enqueue_write_request -> ValueError
   15. a valid WRITE kind with None payload -> row created (no
       mandatory-field gate at the generic layer)
   16. a valid WRITE kind with arbitrary payload -> stored verbatim
   17. a valid WRITE kind with numeric payload fields -> stored verbatim

  TTL / lease (Req 18-20):
   18. WRITE enqueue ALWAYS has ttl_deadline_ts set
   19. WRITE ttl_seconds < ratio*lease_seconds -> ValueError
   20. READ enqueue defaults ttl_seconds so row gets a ttl_deadline_ts

  Poll: WRITE (Req 21-25):
   21. WRITE done within window -> WRITE_SAVED + result
   22. WRITE pending at deadline -> WRITE_ACCEPTED + result=None
   23. WRITE dead -> WRITE_DEAD + result=None
   24. WRITE expired (impossible per B-02C; would only happen if upstream
       bug) -> WRITE_DEAD (defensive: any terminal-failure status maps
       same way for writes)
   25. mapping to user Outcome: SAVED / ACCEPTED / STORAGE_UNAVAILABLE

  Poll: READ never ACCEPTED (Req 26-31):
   26. READ done within window -> READ_RESULT + result
   27. READ pending at deadline + NO fallback -> READ_UNAVAILABLE
       (NEVER WRITE_ACCEPTED)
   28. READ pending at deadline + fallback_fn -> READ_LOCAL_FALLBACK
       with note=LOCAL_FALLBACK_NOTE
   29. READ terminal dead/expired + NO fallback -> READ_UNAVAILABLE
   30. READ terminal dead/expired + fallback_fn -> READ_LOCAL_FALLBACK
   31. READ_UNAVAILABLE result is None (NO fake empty list / empty dict)

  Poll deadline guard (Req 32-34):
   32. deadline_seconds >= 15 -> PollDeadlineError
   33. deadline_seconds <= 0 -> PollDeadlineError
   34. deadline_seconds bool -> PollDeadlineError

  Worker contract (Req 35-39):
   35. apply_claimed_request_with_stub returns applier dict
   36. applier returning non-dict -> ValueError
   37. process_one_request success: stub OK -> mark_done, status=done
   38. process_one_request failure: stub raises -> mark_failed_or_dead
       with class-NAME error_class (never message) and computed
       next_attempt_ts (NEVER None)
   39. process_one_request with no eligible row returns None

  Backoff invariant (Req 40-41):
   40. mark_failed_or_dead writes next_attempt_ts > now (backoff
       respected; no implicit immediate retry)
   41. error_class stored as the exception class name only (no traceback,
       no message, no URL fragment)

  Sweep ordering (Req 42-43):
   42. sweep_request_queue calls recover FIRST then expire
       (verified by scenario: crashed WRITE past lease but BEFORE TTL
       -> recovered to 'failed', not dead)
   43. sweep returns dict with 'recovered', 'expired', 'dead_from_ttl'

  Outcome boundary (Req 44-47):
   44. WRITE_ACCEPTED maps to Outcome.ACCEPTED ("processing")
   45. READ never maps to Outcome.ACCEPTED
   46. READ_LOCAL_FALLBACK maps to Outcome.SAVED with tag containing
       "local"; note carries LOCAL_FALLBACK_NOTE
   47. WRITE_DEAD maps to Outcome.STORAGE_UNAVAILABLE

  Hygiene: value-free repr / stats (Req 48-49):
   48. PollOutcome.__repr__ shows result_keys (sorted list of names)
       only -- never values, never amounts, never account names
   49. Module source does NOT import httpx / requests / notion_client;
       no NOTION_API_KEY token name; no secret-vault import

  Inertness (Req 50):
   50. module source DOES NOT import any cut-domain storage module
       (the queue core stays generic; it depends only on the two
       generic queue-core modules aios_outcomes + aios_request_queue)

  Adversarial-review fixes (Req 51-67):
   51. '|'-injection no longer collides (JSON canonical encoding)
   52. None vs literal 'None', int vs str: type-distinct cids
   53. NaN / Inf deadline_seconds -> PollDeadlineError
   54. poll_interval >= deadline -> PollDeadlineError (wall-time guard)
   55. enqueue_read_request rejects ttl_seconds None / non-positive
       / bool (B-02D layer fail-closes even though B-02C permits)
   56. applier returning None -> ValueError (no silent {} coercion)
   57. process_one_request(applier=None) -> ValueError BEFORE claim
       (row stays pending, no stale claim)
   58. local_fallback_fn raising -> READ_UNAVAILABLE with class-
       name-only marker note (no propagation, no message leak)
   59. applier mutating record.payload does NOT corrupt DB row
       (defensive deepcopy in apply_claimed_request_with_stub)
   60. compose_user_reply_text for READ_LOCAL_FALLBACK includes
       'from the local copy' (addendum #5 label preserved)
   61. compose_user_reply_text for non-fallback states returns
       base user_message ONLY (no spurious note)
   62. PollOutcome.__repr__ defensive on list / scalar / empty
       result -- no crash, no value leak
   63. Every PollResultState -> Outcome that is NEVER
       DEFERRED_DISABLED / CLASSIFIER_UNAVAILABLE / NOT_UNDERSTOOD
       / VALIDATION_FAILED (boundary pinned)
   64. Clock-jump past deadline on first iteration -> WRITE_ACCEPTED
       with zero sleep_fn calls
   65. Multi-iteration poll loop with deterministic ticking clock
       exercises actual loop body (not just the deadline-past branch)
   66. enqueue_write_request retry with different payload silently
       keeps ORIGINAL payload (idempotency pinned)
   67. enqueue_write_request enforces ttl >= ratio*lease invariant

Run:  PYTHONPATH=<repo> python3 scripts/test_aios_b02d_worker_contract.py
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    from bot import aios_outcomes as O
    from bot import aios_request_queue as RQ
    from bot import aios_worker_contract as WC
    from bot import aios_storage as S

    failed: list = []

    def check(name: str, cond: bool) -> None:
        if cond:
            print("PASS: " + name)
        else:
            print("FAIL: " + name)
            failed.append(name)

    tmp = tempfile.mkdtemp(prefix="aios_b02d_")
    try:
        # ----------------------------------------------------------
        # Req 1-4: constants + import safety
        # ----------------------------------------------------------
        mod_path = repo_root / "bot" / "aios_worker_contract.py"
        src = mod_path.read_text(encoding="utf-8")
        check("Req 1a: module source does NOT import httpx",
              "import httpx" not in src and "from httpx" not in src)
        check("Req 1b: module source does NOT import requests",
              "import requests" not in src
              and "from requests" not in src)
        check("Req 1c: module source does NOT import notion_client",
              "notion_client" not in src and "notion-client" not in src)
        check("Req 1d: module source does NOT import urllib.request",
              "import urllib.request" not in src
              and "from urllib.request" not in src)
        check("Req 2a: POLL_DEADLINE_CEILING_SECONDS == 15",
              WC.POLL_DEADLINE_CEILING_SECONDS == 15)
        check("Req 2b: DEFAULT_POLL_DEADLINE_SECONDS < ceiling",
              WC.DEFAULT_POLL_DEADLINE_SECONDS
              < WC.POLL_DEADLINE_CEILING_SECONDS)
        check("Req 2c: DEFAULT_POLL_DEADLINE_SECONDS in [8, 12]",
              8 <= WC.DEFAULT_POLL_DEADLINE_SECONDS <= 12)
        check("Req 3: DEFAULT_WRITE_TTL >> 2 * DEFAULT_LEASE",
              WC.DEFAULT_WRITE_TTL_SECONDS
              >= 2 * WC.DEFAULT_LEASE_SECONDS)
        check("Req 4: LOCAL_FALLBACK_NOTE == 'from the local copy'",
              WC.LOCAL_FALLBACK_NOTE == "from the local copy")

        # ----------------------------------------------------------
        # Req 5-9: build_stable_correlation_id
        # ----------------------------------------------------------
        cid_a = WC.build_stable_correlation_id(
            user_id=42, kind="store_write", business_key="note_2026-06-07",
        )
        cid_a_again = WC.build_stable_correlation_id(
            user_id=42, kind="store_write", business_key="note_2026-06-07",
        )
        check("Req 5: deterministic same-input -> same id",
              cid_a == cid_a_again)
        check("Req 6a: 64-char lowercase hex",
              len(cid_a) == 64
              and re.match(r"^[0-9a-f]{64}$", cid_a) is not None)
        cid_b = WC.build_stable_correlation_id(
            user_id=42, kind="store_write",
            business_key="OTHER_BUSINESS_KEY",
        )
        check("Req 7: different business_key -> different id",
              cid_a != cid_b)
        cid_c = WC.build_stable_correlation_id(
            user_id=42, kind="store_write", business_key="note_2026-06-07",
            attempt_window="2026-06-07",
        )
        check("Req 8: different attempt_window -> different id",
              cid_a != cid_c)
        raised_kind = False
        try:
            WC.build_stable_correlation_id(
                user_id=42, kind="bogus_kind", business_key="x",
            )
        except RQ.InvalidKindError:
            raised_kind = True
        check("Req 9: invalid kind -> InvalidKindError", raised_kind)

        # ----------------------------------------------------------
        # Req 10-12: idempotent retry contract
        # ----------------------------------------------------------
        scen_idem = str(Path(tmp) / "scen_idem.sqlite3")
        S.init_db(db=scen_idem)
        cid_idem1 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="note:daily:2026-06-07",
            account="Notes", payload={"field": "value", "amount": 2000},
            db=scen_idem, now_fn=lambda: 1700000000.0,
        )
        cid_idem2 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="note:daily:2026-06-07",
            account="Notes", payload={"field": "value", "amount": 2000},
            db=scen_idem, now_fn=lambda: 1700000050.0,
        )
        check("Req 10a: 2nd enqueue returns SAME correlation_id (retry idempotent)",
              cid_idem1 == cid_idem2)
        # Count rows directly via sqlite to prove no double-insert.
        with S.connect(db=scen_idem) as conn:
            n_rows = int(conn.execute(
                "SELECT COUNT(*) AS n FROM request_queue"
            ).fetchone()["n"])
        check("Req 11: only 1 row in queue after duplicate enqueue", n_rows == 1)
        # And the original row's account preserved (no overwrite).
        rec_idem = RQ.get_request(cid_idem1, db=scen_idem)
        check("Req 12a: original row account preserved",
              rec_idem.account == "Notes")
        check("Req 12b: original row payload preserved",
              rec_idem.payload == {"field": "value", "amount": 2000})

        # ----------------------------------------------------------
        # Req 13-17: WRITE-kind gate + verbatim payload storage.
        # enqueue_write_request rejects a non-WRITE kind, and stores an
        # accepted WRITE payload verbatim.
        # ----------------------------------------------------------
        scen_wd = str(Path(tmp) / "scen_wd.sqlite3")
        S.init_db(db=scen_wd)
        # 13: a READ kind passed to enqueue_write_request -> ValueError.
        raised_13 = False
        try:
            WC.enqueue_write_request(
                kind="store_read", user_id=7,
                business_key="wr:1", account="Notes",
                payload={"field": "value"}, db=scen_wd,
            )
        except ValueError:
            raised_13 = True
        check("Req 13: READ kind to enqueue_write_request -> ValueError",
              raised_13)
        # 14: an unknown kind -> ValueError (not a WRITE kind).
        raised_14 = False
        try:
            WC.enqueue_write_request(
                kind="bogus_kind", user_id=7,
                business_key="wr:2", account="Notes",
                payload={"field": "value"}, db=scen_wd,
            )
        except ValueError:
            raised_14 = True
        check("Req 14: unknown kind to enqueue_write_request -> ValueError",
              raised_14)
        # 15: None payload on a valid WRITE kind is accepted (no
        # mandatory-field gate at the generic layer).
        cid_15 = WC.enqueue_write_request(
            kind="store_write", user_id=7,
            business_key="wr:3", account="Notes",
            payload=None, db=scen_wd, now_fn=lambda: 1700000000.0,
        )
        rec_15 = RQ.get_request(cid_15, db=scen_wd)
        check("Req 15: WRITE kind with None payload -> row created",
              rec_15 is not None and rec_15.kind == "store_write")
        # 16: an arbitrary payload is accepted verbatim (no field gate).
        cid_16 = WC.enqueue_write_request(
            kind="store_write", user_id=7,
            business_key="wr:4", account="Notes",
            payload={"reason": "note"}, db=scen_wd,
            now_fn=lambda: 1700000000.0,
        )
        rec_16 = RQ.get_request(cid_16, db=scen_wd)
        check("Req 16: WRITE kind with arbitrary payload -> row created",
              rec_16 is not None
              and rec_16.payload == {"reason": "note"})
        # 17: numeric payload fields stored verbatim.
        cid_17 = WC.enqueue_write_request(
            kind="store_write", user_id=7,
            business_key="wr:5", account="Notes",
            payload={"delta": -3000.0,
                     "snapshot_value": 12500.0,
                     "snapshot_ts": 1700000000.0},
            db=scen_wd, now_fn=lambda: 1700000000.0,
        )
        rec_17 = RQ.get_request(cid_17, db=scen_wd)
        check("Req 17a: WRITE with numeric payload -> row created",
              rec_17 is not None and rec_17.kind == "store_write")
        check("Req 17b: numeric field preserved verbatim in payload",
              rec_17.payload.get("delta") == -3000.0)
        check("Req 17c: snapshot_value preserved",
              rec_17.payload.get("snapshot_value") == 12500.0)

        # ----------------------------------------------------------
        # Req 18-20: TTL / lease
        # ----------------------------------------------------------
        scen_ttl = str(Path(tmp) / "scen_ttl.sqlite3")
        S.init_db(db=scen_ttl)
        cid_18 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="ttl:1",
            account="X", payload={"x": 1},
            db=scen_ttl, now_fn=lambda: 1700000000.0,
        )
        rec_18 = RQ.get_request(cid_18, db=scen_ttl)
        check("Req 18: WRITE enqueue ALWAYS has ttl_deadline_ts set",
              rec_18.ttl_deadline_ts is not None
              and rec_18.ttl_deadline_ts
                  == 1700000000.0 + WC.DEFAULT_WRITE_TTL_SECONDS)
        # Req 19: ttl < ratio * lease -> ValueError
        raised_19 = False
        try:
            WC.enqueue_write_request(
                kind="store_write", user_id=7, business_key="ttl:bad",
                account="X", payload={"x": 1},
                lease_seconds=30.0, ttl_seconds=30.0,  # ratio 2.0 -> require >=60
                db=scen_ttl,
            )
        except ValueError:
            raised_19 = True
        check("Req 19: ttl_seconds < ratio*lease_seconds -> ValueError",
              raised_19)
        # Req 20: READ enqueue defaults set ttl_deadline
        cid_20 = WC.enqueue_read_request(
            kind="store_read", user_id=7, business_key="rb:1",
            db=scen_ttl, now_fn=lambda: 1700000000.0,
        )
        rec_20 = RQ.get_request(cid_20, db=scen_ttl)
        check("Req 20: READ enqueue with default sets ttl_deadline_ts",
              rec_20.ttl_deadline_ts is not None
              and rec_20.ttl_deadline_ts
                  == 1700000000.0 + WC.DEFAULT_READ_TTL_SECONDS)

        # ----------------------------------------------------------
        # Req 21-25: Poll WRITE
        # ----------------------------------------------------------
        scen_pw = str(Path(tmp) / "scen_pw.sqlite3")
        S.init_db(db=scen_pw)
        # 21: WRITE done within window -> WRITE_SAVED
        cid_21 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="pw:21",
            account="X", payload={"a": 1},
            db=scen_pw, now_fn=lambda: 1700000000.0,
        )
        # Simulate: worker claims + marks done.
        rec_claim = RQ.claim_next_request(
            worker_id="wkr_pw1", db=scen_pw,
            now_fn=lambda: 1700000005.0,
        )
        RQ.mark_done(cid_21, result={"saved": True}, db=scen_pw,
                     now_fn=lambda: 1700000006.0)
        # Poll with very short window; first read should see done.
        po_21 = WC.poll_request_result(
            cid_21, deadline_seconds=1.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=lambda: 1700000007.0,
            db=scen_pw,
        )
        check("Req 21a: WRITE done within window -> WRITE_SAVED",
              po_21.state == WC.PollResultState.WRITE_SAVED)
        check("Req 21b: result returned verbatim",
              po_21.result == {"saved": True})
        check("Req 21c: source_status == 'done'",
              po_21.source_status == "done")

        # 22: WRITE pending at deadline -> WRITE_ACCEPTED
        cid_22 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="pw:22",
            account="Y", payload={"a": 2},
            db=scen_pw, now_fn=lambda: 1700001000.0,
        )
        # Use a counter so now_fn advances past the deadline on the 2nd call.
        clock_22 = {"t": 1700001005.0}

        def now22():
            v = clock_22["t"]
            clock_22["t"] += 5.0
            return v
        po_22 = WC.poll_request_result(
            cid_22, deadline_seconds=2.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=now22, db=scen_pw,
        )
        check("Req 22a: WRITE pending at deadline -> WRITE_ACCEPTED",
              po_22.state == WC.PollResultState.WRITE_ACCEPTED)
        check("Req 22b: WRITE_ACCEPTED result is None",
              po_22.result is None)
        check("Req 22c: source_status == 'pending' (or 'claimed')",
              po_22.source_status in ("pending", "claimed"))

        # 23: WRITE dead -> WRITE_DEAD.
        cid_23 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="pw:23",
            account="Z", payload={"a": 3},
            db=scen_pw, now_fn=lambda: 1700002000.0,
        )
        # Claim + max_attempts=1 -> immediately dead.
        RQ.claim_next_request(worker_id="wkr_pw23", account="Z",
                              kinds=["store_write"], db=scen_pw,
                              now_fn=lambda: 1700002005.0)
        RQ.mark_failed_or_dead(
            cid_23, error_class="StubFailure", max_attempts=1,
            next_attempt_ts=1700002999.0, db=scen_pw,
            now_fn=lambda: 1700002006.0,
        )
        po_23 = WC.poll_request_result(
            cid_23, deadline_seconds=1.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=lambda: 1700002007.0,
            db=scen_pw,
        )
        check("Req 23a: WRITE dead -> WRITE_DEAD",
              po_23.state == WC.PollResultState.WRITE_DEAD)
        check("Req 23b: WRITE_DEAD result is None",
              po_23.result is None)

        # 24: defensive WRITE expired -> still WRITE_DEAD (terminal-failure
        # bucket).  Simulate by directly UPDATEing the row to 'expired'.
        cid_24 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="pw:24",
            account="Q", payload={"a": 4},
            db=scen_pw, now_fn=lambda: 1700003000.0,
        )
        with S.connect(db=scen_pw) as conn:
            conn.execute(
                "UPDATE request_queue SET status='expired',"
                " resolved_ts=1700003001.0 WHERE correlation_id=?",
                (cid_24,),
            )
        po_24 = WC.poll_request_result(
            cid_24, deadline_seconds=1.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=lambda: 1700003005.0,
            db=scen_pw,
        )
        check("Req 24: WRITE expired (defensive) -> WRITE_DEAD",
              po_24.state == WC.PollResultState.WRITE_DEAD)

        # 25: mapping to user Outcome
        oc_21 = WC.poll_outcome_to_user_outcome(po_21)
        check("Req 25a: WRITE_SAVED -> Outcome.SAVED",
              oc_21.state == O.OutcomeState.SAVED)
        oc_22 = WC.poll_outcome_to_user_outcome(po_22)
        check("Req 25b: WRITE_ACCEPTED -> Outcome.ACCEPTED",
              oc_22.state == O.OutcomeState.ACCEPTED)
        oc_23 = WC.poll_outcome_to_user_outcome(po_23)
        check("Req 25c: WRITE_DEAD -> Outcome.STORAGE_UNAVAILABLE",
              oc_23.state == O.OutcomeState.STORAGE_UNAVAILABLE)

        # ----------------------------------------------------------
        # Req 26-31: Poll READ (never ACCEPTED)
        # ----------------------------------------------------------
        scen_pr = str(Path(tmp) / "scen_pr.sqlite3")
        S.init_db(db=scen_pr)
        # 26: READ done -> READ_RESULT.
        cid_26 = WC.enqueue_read_request(
            kind="store_read", user_id=7, business_key="pr:26",
            db=scen_pr, now_fn=lambda: 1700000000.0,
        )
        RQ.claim_next_request(worker_id="wkr_pr1", db=scen_pr,
                              now_fn=lambda: 1700000001.0)
        RQ.mark_done(cid_26, result={"count": 12500.0}, db=scen_pr,
                     now_fn=lambda: 1700000002.0)
        po_26 = WC.poll_request_result(
            cid_26, deadline_seconds=1.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=lambda: 1700000003.0,
            db=scen_pr,
        )
        check("Req 26a: READ done -> READ_RESULT",
              po_26.state == WC.PollResultState.READ_RESULT)
        check("Req 26b: result {'count': 12500.0} preserved",
              po_26.result == {"count": 12500.0})

        # 27: READ pending at deadline + NO fallback -> READ_UNAVAILABLE
        cid_27 = WC.enqueue_read_request(
            kind="store_read", user_id=7, business_key="pr:27",
            db=scen_pr, now_fn=lambda: 1700001000.0,
        )
        clock_27 = {"t": 1700001005.0}

        def now27():
            v = clock_27["t"]
            clock_27["t"] += 5.0
            return v
        po_27 = WC.poll_request_result(
            cid_27, deadline_seconds=2.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=now27,
            local_fallback_fn=None, db=scen_pr,
        )
        check("Req 27a: READ pending at deadline + no fallback -> READ_UNAVAILABLE",
              po_27.state == WC.PollResultState.READ_UNAVAILABLE)
        check("Req 27b: READ result is None (NEVER ACCEPTED)",
              po_27.result is None
              and po_27.state != WC.PollResultState.WRITE_ACCEPTED)

        # 28: READ pending at deadline + fallback_fn -> READ_LOCAL_FALLBACK
        cid_28 = WC.enqueue_read_request(
            kind="store_read", user_id=7, business_key="pr:28",
            db=scen_pr, now_fn=lambda: 1700002000.0,
        )
        clock_28 = {"t": 1700002005.0}

        def now28():
            v = clock_28["t"]
            clock_28["t"] += 5.0
            return v
        fb_called = {"n": 0}

        def fallback28(rec):
            fb_called["n"] += 1
            return {"local_count": 999.0, "stale": True}
        po_28 = WC.poll_request_result(
            cid_28, deadline_seconds=2.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=now28,
            local_fallback_fn=fallback28, db=scen_pr,
        )
        check("Req 28a: READ pending + fallback -> READ_LOCAL_FALLBACK",
              po_28.state == WC.PollResultState.READ_LOCAL_FALLBACK)
        check("Req 28b: fallback_fn was called once", fb_called["n"] == 1)
        check("Req 28c: note carries LOCAL_FALLBACK_NOTE",
              po_28.note == WC.LOCAL_FALLBACK_NOTE
              and po_28.note == "from the local copy")
        check("Req 28d: fallback result preserved",
              po_28.result == {"local_count": 999.0, "stale": True})

        # 29: READ terminal expired + NO fallback -> READ_UNAVAILABLE
        cid_29 = WC.enqueue_read_request(
            kind="store_read", user_id=7, business_key="pr:29",
            db=scen_pr, now_fn=lambda: 1700003000.0,
        )
        with S.connect(db=scen_pr) as conn:
            conn.execute(
                "UPDATE request_queue SET status='expired',"
                " resolved_ts=1700003001.0 WHERE correlation_id=?",
                (cid_29,),
            )
        po_29 = WC.poll_request_result(
            cid_29, deadline_seconds=1.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=lambda: 1700003005.0,
            db=scen_pr,
        )
        check("Req 29: READ expired + no fallback -> READ_UNAVAILABLE",
              po_29.state == WC.PollResultState.READ_UNAVAILABLE)

        # 30: READ terminal dead + fallback -> READ_LOCAL_FALLBACK
        cid_30 = WC.enqueue_read_request(
            kind="store_read", user_id=7, business_key="pr:30",
            db=scen_pr, now_fn=lambda: 1700004000.0,
        )
        with S.connect(db=scen_pr) as conn:
            conn.execute(
                "UPDATE request_queue SET status='dead',"
                " resolved_ts=1700004001.0 WHERE correlation_id=?",
                (cid_30,),
            )

        def fallback30(rec):
            return {"local_count": 0.0, "stale": True}
        po_30 = WC.poll_request_result(
            cid_30, deadline_seconds=1.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=lambda: 1700004005.0,
            local_fallback_fn=fallback30, db=scen_pr,
        )
        check("Req 30a: READ terminal + fallback -> READ_LOCAL_FALLBACK",
              po_30.state == WC.PollResultState.READ_LOCAL_FALLBACK)
        check("Req 30b: note carries LOCAL_FALLBACK_NOTE",
              po_30.note == WC.LOCAL_FALLBACK_NOTE)

        # 31: READ_UNAVAILABLE result is None (no fake empty list/dict)
        check("Req 31a: READ_UNAVAILABLE result is None (not [])",
              po_27.result is None and po_27.result != []
              and po_27.result != {})
        check("Req 31b: READ_UNAVAILABLE state != WRITE_ACCEPTED",
              po_27.state != WC.PollResultState.WRITE_ACCEPTED)

        # ----------------------------------------------------------
        # Req 32-34: poll deadline guard
        # ----------------------------------------------------------
        raised_32 = False
        try:
            WC.poll_request_result(
                "irrelevant", deadline_seconds=15.0, db=scen_pr,
            )
        except WC.PollDeadlineError:
            raised_32 = True
        check("Req 32: deadline_seconds >= 15 -> PollDeadlineError",
              raised_32)
        raised_33 = False
        try:
            WC.poll_request_result(
                "irrelevant", deadline_seconds=0.0, db=scen_pr,
            )
        except WC.PollDeadlineError:
            raised_33 = True
        check("Req 33: deadline_seconds 0 -> PollDeadlineError", raised_33)
        raised_34 = False
        try:
            WC.poll_request_result(
                "irrelevant", deadline_seconds=True, db=scen_pr,
            )
        except WC.PollDeadlineError:
            raised_34 = True
        check("Req 34: deadline_seconds bool -> PollDeadlineError",
              raised_34)

        # ----------------------------------------------------------
        # Req 35-39: worker contract
        # ----------------------------------------------------------
        scen_w = str(Path(tmp) / "scen_w.sqlite3")
        S.init_db(db=scen_w)
        # 35: apply_claimed_request_with_stub returns applier dict
        cid_35 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="w:35",
            account="A", payload={"a": 1},
            db=scen_w, now_fn=lambda: 1700000000.0,
        )
        # We don't need to claim for unit-test of apply.
        rec_dummy = RQ.get_request(cid_35, db=scen_w)

        def good_applier(rec):
            return {"ok": True, "echoed_kind": rec.kind}
        result_35 = WC.apply_claimed_request_with_stub(
            rec_dummy, applier=good_applier,
        )
        check("Req 35a: applier dict returned",
              result_35 == {"ok": True, "echoed_kind": "store_write"})

        # 36: applier returning non-dict -> ValueError
        def bad_applier(rec):
            return "not a dict"
        raised_36 = False
        try:
            WC.apply_claimed_request_with_stub(
                rec_dummy, applier=bad_applier,
            )
        except ValueError:
            raised_36 = True
        check("Req 36: applier returning non-dict -> ValueError", raised_36)

        # 37: process_one_request success -> mark_done
        scen_p1 = str(Path(tmp) / "scen_p1.sqlite3")
        S.init_db(db=scen_p1)
        cid_37 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="w:37",
            account="A", payload={"a": 1},
            db=scen_p1, now_fn=lambda: 1700000000.0,
        )
        applied_37 = WC.process_one_request(
            worker_id="wkr_p1", applier=good_applier,
            db=scen_p1, now_fn=lambda: 1700000010.0,
        )
        check("Req 37a: process_one returns the cid", applied_37 == cid_37)
        rec_37 = RQ.get_request(cid_37, db=scen_p1)
        check("Req 37b: status -> done after process_one success",
              rec_37.status == "done")
        check("Req 37c: result_json round-trip",
              rec_37.result == {"ok": True, "echoed_kind": "store_write"})

        # 38: process_one_request failure -> mark_failed_or_dead with class-name only
        cid_38 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="w:38",
            account="B", payload={"a": 2},
            db=scen_p1, now_fn=lambda: 1700000050.0,
        )

        def raising_applier(rec):
            raise RuntimeError("connection to https://api.notion.com/secret-url refused")
        applied_38 = WC.process_one_request(
            worker_id="wkr_p1b", applier=raising_applier,
            kinds=["store_write"], account="B",
            backoff_base_seconds=2.0, backoff_max_seconds=300.0,
            max_attempts=10,
            db=scen_p1, now_fn=lambda: 1700000100.0,
        )
        check("Req 38a: process_one returns the cid even on failure",
              applied_38 == cid_38)
        rec_38 = RQ.get_request(cid_38, db=scen_p1)
        check("Req 38b: status -> failed after applier raise",
              rec_38.status == "failed")
        check("Req 38c: error_class is the class NAME only (RuntimeError)",
              rec_38.error_class == "RuntimeError")
        check("Req 38d: error_class does NOT contain URL fragment",
              "https" not in (rec_38.error_class or "").lower()
              and "notion" not in (rec_38.error_class or "").lower()
              and "api." not in (rec_38.error_class or "").lower()
              and "secret" not in (rec_38.error_class or "").lower())
        check("Req 38e: next_attempt_ts is set (NOT None; no implicit storm)",
              rec_38.next_attempt_ts is not None
              and rec_38.next_attempt_ts > 1700000100.0)

        # 39: process_one_request with no eligible row -> None
        scen_p2 = str(Path(tmp) / "scen_p2.sqlite3")
        S.init_db(db=scen_p2)
        applied_39 = WC.process_one_request(
            worker_id="wkr_p2", applier=good_applier,
            db=scen_p2, now_fn=lambda: 1700000000.0,
        )
        check("Req 39: process_one on empty queue -> None",
              applied_39 is None)

        # ----------------------------------------------------------
        # Req 40-41: backoff invariant
        # ----------------------------------------------------------
        # 40: next_attempt_ts > now (no immediate retry storm)
        delta_40 = rec_38.next_attempt_ts - 1700000100.0
        check("Req 40a: next_attempt_ts strictly > now (backoff respected)",
              delta_40 > 0)
        # Review-fix: process_one_request now passes rec.attempts (not
        # +1) so the helper's `attempts` parameter is the number of
        # PREVIOUSLY-failed attempts (0 for the first failure ->
        # base_seconds delay).  Expected: base 2.0 * 2 ** 0 = 2.0
        expected_40 = RQ.compute_backoff(
            attempts=0, base_seconds=2.0, max_seconds=300.0,
        )
        check("Req 40b: backoff delta matches compute_backoff(attempts=0, base=2.0)",
              abs(delta_40 - expected_40) < 0.01)
        # 41: error_class is plain identifier
        check("Req 41: error_class is plain Python identifier",
              re.match(r"^[A-Za-z_][A-Za-z0-9_]*$",
                       rec_38.error_class or "") is not None)

        # ----------------------------------------------------------
        # Req 42-43: sweep ordering
        # ----------------------------------------------------------
        scen_sw = str(Path(tmp) / "scen_sw.sqlite3")
        S.init_db(db=scen_sw)
        # WRITE with ttl_seconds=120, lease=30; claim at t=10, simulate
        # crash; sweep at t=50 (lease elapsed, TTL not yet).  Recover
        # MUST flip claimed -> failed BEFORE expire would flip it to dead.
        cid_42 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="sw:42",
            account="X", payload={"x": 1},
            lease_seconds=30.0, ttl_seconds=120.0,
            db=scen_sw, now_fn=lambda: 1700000000.0,
        )
        RQ.claim_next_request(worker_id="wkr_sw", db=scen_sw,
                              now_fn=lambda: 1700000010.0)
        # Sweep at t=50 (claim stale since t=40; TTL deadline is t=120).
        sweep_42 = WC.sweep_request_queue(
            lease_seconds=30.0, db=scen_sw,
            now_fn=lambda: 1700000050.0,
        )
        check("Req 42a: sweep returns dict with 'recovered' key",
              "recovered" in sweep_42)
        check("Req 42b: recover ran FIRST (1 row recovered, 0 dead from TTL)",
              sweep_42["recovered"] == 1 and sweep_42["dead_from_ttl"] == 0)
        rec_42 = RQ.get_request(cid_42, db=scen_sw)
        check("Req 42c: recovered row is now 'failed' (NOT 'dead')",
              rec_42.status == "failed")

        # 43: sweep returns dict with all 3 keys
        check("Req 43a: sweep dict has 'expired'",
              "expired" in sweep_42)
        check("Req 43b: sweep dict has 'dead_from_ttl'",
              "dead_from_ttl" in sweep_42)
        check("Req 43c: sweep dict has 'recovered'",
              "recovered" in sweep_42)

        # ----------------------------------------------------------
        # Req 44-47: outcome boundary
        # ----------------------------------------------------------
        # 44: WRITE_ACCEPTED maps to Outcome.ACCEPTED
        po_44 = WC.PollOutcome(
            state=WC.PollResultState.WRITE_ACCEPTED,
            correlation_id="x", result=None, note=None,
            source_status="pending",
        )
        oc_44 = WC.poll_outcome_to_user_outcome(po_44)
        check("Req 44a: WRITE_ACCEPTED maps to Outcome.ACCEPTED",
              oc_44.state == O.OutcomeState.ACCEPTED)
        check("Req 44b: user_message is 'accepted for processing'",
              O.user_message(oc_44) == "accepted for processing")

        # 45: READ never maps to Outcome.ACCEPTED (no PollResultState READ_*
        # has a mapping to ACCEPTED)
        for s in (WC.PollResultState.READ_RESULT,
                  WC.PollResultState.READ_LOCAL_FALLBACK,
                  WC.PollResultState.READ_UNAVAILABLE):
            po_s = WC.PollOutcome(state=s, correlation_id="x",
                                   result=None, note=None,
                                   source_status="pending")
            oc_s = WC.poll_outcome_to_user_outcome(po_s)
            check("Req 45: READ state '" + s.value
                  + "' maps to NON-ACCEPTED Outcome",
                  oc_s.state != O.OutcomeState.ACCEPTED)

        # 46: READ_LOCAL_FALLBACK -> SAVED with tag containing 'local';
        # note carries LOCAL_FALLBACK_NOTE
        po_46 = WC.PollOutcome(
            state=WC.PollResultState.READ_LOCAL_FALLBACK,
            correlation_id="x", result={"local_count": 0.0},
            note=WC.LOCAL_FALLBACK_NOTE, source_status="expired",
        )
        oc_46 = WC.poll_outcome_to_user_outcome(po_46)
        check("Req 46a: READ_LOCAL_FALLBACK maps to Outcome.SAVED",
              oc_46.state == O.OutcomeState.SAVED)
        check("Req 46b: SAVED tag contains 'local'",
              "local" in oc_46.tag)
        check("Req 46c: note literal is 'from the local copy'",
              po_46.note == "from the local copy")

        # 47: WRITE_DEAD -> Outcome.STORAGE_UNAVAILABLE
        po_47 = WC.PollOutcome(
            state=WC.PollResultState.WRITE_DEAD,
            correlation_id="x", result=None, note=None,
            source_status="dead",
        )
        oc_47 = WC.poll_outcome_to_user_outcome(po_47)
        check("Req 47a: WRITE_DEAD maps to Outcome.STORAGE_UNAVAILABLE",
              oc_47.state == O.OutcomeState.STORAGE_UNAVAILABLE)
        check("Req 47b: user_message is 'could not save'",
              O.user_message(oc_47) == "could not save")

        # ----------------------------------------------------------
        # Req 48-49: hygiene / value-free
        # ----------------------------------------------------------
        po_48 = WC.PollOutcome(
            state=WC.PollResultState.WRITE_SAVED,
            correlation_id="cid_test_48",
            result={"saved": True, "amount": 9999, "account": "SECRET"},
            note=None, source_status="done",
        )
        repr_48 = repr(po_48)
        check("Req 48a: __repr__ contains 'PollOutcome('",
              "PollOutcome(" in repr_48)
        # Review-fix: __repr__ now uses a defensive _summarize_result_value_free
        # helper.  For dict the format is "result=dict(keys=[...])".
        check("Req 48b: __repr__ shows result keys (sorted list of names)",
              "result=dict(keys=" in repr_48
              and "['account', 'amount', 'saved']" in repr_48)
        check("Req 48c: __repr__ does NOT leak amount literal 9999",
              "9999" not in repr_48)
        check("Req 48d: __repr__ does NOT leak account value 'SECRET'",
              "SECRET" not in repr_48)
        # Req 49 partial (additional to Req 1*)
        check("Req 49a: module source does NOT reference NOTION_API_KEY",
              "NOTION_API_KEY" not in src)
        check("Req 49b: module source does NOT reference 'Bitwarden'",
              "Bitwarden" not in src and "bitwarden" not in src.lower())

        # ----------------------------------------------------------
        # Req 50: inertness -- the contract does NOT import a cut
        # domain module (the queue core stays generic; no dependency
        # on any removed feature-specific storage module).
        # ----------------------------------------------------------
        cut_modules = (
            "domain_a_storage", "domain_b_storage", "domain_c_storage",
            "domain_d_storage", "domain_e_storage", "domain_f_storage",
        )
        no_cut_import = True
        for m in cut_modules:
            if ("import " + m) in src or ("." + m) in src:
                no_cut_import = False
        check("Req 50a: module source does NOT import any cut-domain "
              "storage module",
              no_cut_import)
        # And the contract only depends on the two generic queue-core
        # modules (aios_outcomes + aios_request_queue); it must NOT
        # reach into any domain-specific storage.
        check("Req 50b: module imports ONLY the generic queue-core deps",
              "from . import aios_outcomes as O" in src
              and "from . import aios_request_queue as RQ" in src)

        # ============================================================
        # ADVERSARIAL REVIEW FIXES (Req 51-67) -- from B-02D review
        # ============================================================

        # Req 51: business_key "|"-collision (CRITICAL).  After
        # switching to JSON canonical encoding, business_key='a|b' and
        # (business_key='a', attempt_window='b') MUST hash to
        # different cids.
        cid_51_a = WC.build_stable_correlation_id(
            user_id=42, kind="store_write", business_key="a|b",
        )
        cid_51_b = WC.build_stable_correlation_id(
            user_id=42, kind="store_write", business_key="a",
            attempt_window="b",
        )
        check("Req 51: '|'-injection no longer collides "
              "(business_key='a|b' vs business_key='a', window='b')",
              cid_51_a != cid_51_b)

        # Req 52: None vs literal "None" conflation (MAJOR).
        cid_52_a = WC.build_stable_correlation_id(
            user_id=None, kind="store_write", business_key="x",
        )
        cid_52_b = WC.build_stable_correlation_id(
            user_id="None", kind="store_write", business_key="x",
        )
        check("Req 52a: user_id=None != user_id='None' (no conflation)",
              cid_52_a != cid_52_b)
        cid_52_c = WC.build_stable_correlation_id(
            user_id=42, kind="store_write", business_key=None,
        )
        cid_52_d = WC.build_stable_correlation_id(
            user_id=42, kind="store_write", business_key="None",
        )
        check("Req 52b: business_key=None != business_key='None' "
              "(no conflation)",
              cid_52_c != cid_52_d)
        # Also: int 42 vs str "42" must differ (JSON encoding pins type)
        cid_52_e = WC.build_stable_correlation_id(
            user_id=42, kind="store_write", business_key="x",
        )
        cid_52_f = WC.build_stable_correlation_id(
            user_id="42", kind="store_write", business_key="x",
        )
        check("Req 52c: user_id=42 (int) != user_id='42' (str) "
              "(type preserved by JSON encoding)",
              cid_52_e != cid_52_f)

        # Req 53: NaN deadline_seconds -> PollDeadlineError.
        raised_53a = False
        try:
            WC.poll_request_result(
                "irrelevant", deadline_seconds=float("nan"),
                db=scen_pr,
            )
        except WC.PollDeadlineError:
            raised_53a = True
        check("Req 53a: NaN deadline_seconds -> PollDeadlineError",
              raised_53a)
        raised_53b = False
        try:
            WC.poll_request_result(
                "irrelevant", deadline_seconds=float("inf"),
                db=scen_pr,
            )
        except WC.PollDeadlineError:
            raised_53b = True
        check("Req 53b: Inf deadline_seconds -> PollDeadlineError",
              raised_53b)

        # Req 54: poll_interval >= deadline -> PollDeadlineError
        # (wall-time guard so loop does not bust caller's budget).
        raised_54a = False
        try:
            WC.poll_request_result(
                "irrelevant", deadline_seconds=1.0,
                poll_interval_seconds=2.0, db=scen_pr,
            )
        except WC.PollDeadlineError:
            raised_54a = True
        check("Req 54a: poll_interval (2.0) >= deadline (1.0) -> "
              "PollDeadlineError",
              raised_54a)
        raised_54b = False
        try:
            WC.poll_request_result(
                "irrelevant", deadline_seconds=1.0,
                poll_interval_seconds=1.0, db=scen_pr,
            )
        except WC.PollDeadlineError:
            raised_54b = True
        check("Req 54b: poll_interval == deadline -> PollDeadlineError",
              raised_54b)

        # Req 55: enqueue_read_request rejects ttl_seconds=None
        # (B-02D layer fail-closes even though B-02C allows it).
        scen_55 = str(Path(tmp) / "scen_55.sqlite3")
        S.init_db(db=scen_55)
        raised_55a = False
        try:
            WC.enqueue_read_request(
                kind="store_read", user_id=7,
                business_key="r55:1", ttl_seconds=None,
                db=scen_55,
            )
        except ValueError:
            raised_55a = True
        check("Req 55a: enqueue_read_request(ttl_seconds=None) -> ValueError",
              raised_55a)
        raised_55b = False
        try:
            WC.enqueue_read_request(
                kind="store_read", user_id=7,
                business_key="r55:2", ttl_seconds=0,
                db=scen_55,
            )
        except ValueError:
            raised_55b = True
        check("Req 55b: enqueue_read_request(ttl_seconds=0) -> ValueError",
              raised_55b)
        raised_55c = False
        try:
            WC.enqueue_read_request(
                kind="store_read", user_id=7,
                business_key="r55:3", ttl_seconds=-5.0,
                db=scen_55,
            )
        except ValueError:
            raised_55c = True
        check("Req 55c: enqueue_read_request(ttl_seconds=-5) -> ValueError",
              raised_55c)
        raised_55d = False
        try:
            WC.enqueue_read_request(
                kind="store_read", user_id=7,
                business_key="r55:4", ttl_seconds=True,
                db=scen_55,
            )
        except ValueError:
            raised_55d = True
        check("Req 55d: enqueue_read_request(ttl_seconds=True) -> ValueError",
              raised_55d)

        # Req 56: apply_claimed_request_with_stub rejects applier
        # returning None (no silent {} coercion).
        scen_56 = str(Path(tmp) / "scen_56.sqlite3")
        S.init_db(db=scen_56)
        cid_56 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="a56",
            account="X", payload={"a": 1},
            db=scen_56, now_fn=lambda: 1700000000.0,
        )
        rec_56 = RQ.get_request(cid_56, db=scen_56)

        def none_applier(rec):
            return None
        raised_56 = False
        try:
            WC.apply_claimed_request_with_stub(
                rec_56, applier=none_applier,
            )
        except ValueError:
            raised_56 = True
        check("Req 56: applier returning None -> ValueError "
              "(no silent {} coercion)",
              raised_56)

        # Req 57: process_one_request rejects applier=None BEFORE
        # claim (no row gets stuck in 'claimed' for a
        # misconfigured worker).
        scen_57 = str(Path(tmp) / "scen_57.sqlite3")
        S.init_db(db=scen_57)
        cid_57 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="p57",
            account="X", payload={"a": 1},
            db=scen_57, now_fn=lambda: 1700000000.0,
        )
        raised_57 = False
        try:
            WC.process_one_request(
                worker_id="wkr_57", applier=None, db=scen_57,
            )
        except ValueError:
            raised_57 = True
        check("Req 57a: process_one_request(applier=None) -> ValueError",
              raised_57)
        rec_57 = RQ.get_request(cid_57, db=scen_57)
        check("Req 57b: row remains 'pending' after rejected call "
              "(no stale claim)",
              rec_57.status == "pending"
              and rec_57.worker_id is None)

        # Req 58: local_fallback_fn raising is swallowed and the poll
        # returns READ_UNAVAILABLE (closed-set contract preserved).
        scen_58 = str(Path(tmp) / "scen_58.sqlite3")
        S.init_db(db=scen_58)
        cid_58 = WC.enqueue_read_request(
            kind="store_read", user_id=7, business_key="r58",
            db=scen_58, now_fn=lambda: 1700000000.0,
        )
        # Force terminal expired so fallback path triggers.
        with S.connect(db=scen_58) as conn:
            conn.execute(
                "UPDATE request_queue SET status='expired',"
                " resolved_ts=1700000001.0 WHERE correlation_id=?",
                (cid_58,),
            )

        def raising_fallback(rec):
            raise RuntimeError(
                "local sqlite disk-error /home/user/.ai-os/cache.db"
            )
        po_58 = WC.poll_request_result(
            cid_58, deadline_seconds=1.0, poll_interval_seconds=0.1,
            sleep_fn=lambda s: None, now_fn=lambda: 1700000005.0,
            local_fallback_fn=raising_fallback, db=scen_58,
        )
        check("Req 58a: fallback raises -> READ_UNAVAILABLE "
              "(no propagation)",
              po_58.state == WC.PollResultState.READ_UNAVAILABLE)
        check("Req 58b: note carries class-name-only marker, NEVER message",
              po_58.note is not None
              and "fallback_raised:RuntimeError" in po_58.note
              and "disk-error" not in po_58.note
              and "/home" not in po_58.note)

        # Req 59: applier mutating record.payload does NOT poison the
        # original / DB row (defensive deepcopy in
        # apply_claimed_request_with_stub).
        scen_59 = str(Path(tmp) / "scen_59.sqlite3")
        S.init_db(db=scen_59)
        cid_59 = WC.enqueue_write_request(
            kind="store_write", user_id=7,
            business_key="wr59", account="X",
            payload={"delta": -5000.0,
                     "snapshot_value": 12500.0},
            db=scen_59, now_fn=lambda: 1700000000.0,
        )
        rec_59_pre = RQ.get_request(cid_59, db=scen_59)

        def mutating_applier(rec):
            # Try to corrupt the stored payload.
            rec.payload["delta"] = 0.0
            rec.payload["pwned"] = True
            return {"applied": True}
        result_59 = WC.apply_claimed_request_with_stub(
            rec_59_pre, applier=mutating_applier,
        )
        check("Req 59a: applier returned its result",
              result_59 == {"applied": True})
        # The ORIGINAL record in the test scope MIGHT be mutated
        # (deepcopy is on the COPY handed to applier), so we re-read
        # from the DB to verify the row payload is intact.
        rec_59_post = RQ.get_request(cid_59, db=scen_59)
        check("Req 59b: DB row's delta UNCHANGED after applier "
              "mutation (deepcopy guard)",
              rec_59_post.payload["delta"] == -5000.0)
        check("Req 59c: DB row's payload does NOT contain 'pwned' key",
              "pwned" not in rec_59_post.payload)

        # Req 60: compose_user_reply_text appends LOCAL_FALLBACK_NOTE
        # for READ_LOCAL_FALLBACK so the user sees the label.
        po_60 = WC.PollOutcome(
            state=WC.PollResultState.READ_LOCAL_FALLBACK,
            correlation_id="x", result={"local_count": 0.0},
            note=WC.LOCAL_FALLBACK_NOTE, source_status="expired",
        )
        text_60 = WC.compose_user_reply_text(po_60)
        check("Req 60a: compose_user_reply_text for READ_LOCAL_FALLBACK "
              "includes 'from the local copy'",
              "from the local copy" in text_60)
        check("Req 60b: text starts with the SAVED user_message base",
              text_60.startswith("saved"))

        # Req 61: compose_user_reply_text for non-fallback states
        # returns the base user_message ONLY (no spurious note).
        po_61_a = WC.PollOutcome(
            state=WC.PollResultState.WRITE_SAVED,
            correlation_id="x", result=None, note=None,
            source_status="done",
        )
        text_61_a = WC.compose_user_reply_text(po_61_a)
        check("Req 61a: WRITE_SAVED reply text == 'saved' "
              "(no fallback note)",
              text_61_a == "saved")
        po_61_b = WC.PollOutcome(
            state=WC.PollResultState.WRITE_ACCEPTED,
            correlation_id="x", result=None, note=None,
            source_status="pending",
        )
        text_61_b = WC.compose_user_reply_text(po_61_b)
        check("Req 61b: WRITE_ACCEPTED reply text == 'accepted for processing'",
              text_61_b == "accepted for processing")
        po_61_c = WC.PollOutcome(
            state=WC.PollResultState.READ_UNAVAILABLE,
            correlation_id="x", result=None, note=None,
            source_status="expired",
        )
        text_61_c = WC.compose_user_reply_text(po_61_c)
        check("Req 61c: READ_UNAVAILABLE reply text == "
              "'could not save'",
              text_61_c == "could not save")

        # Req 62: PollOutcome.__repr__ with LIST result -- no crash,
        # no leaked values.
        po_62_list = WC.PollOutcome(
            state=WC.PollResultState.READ_RESULT,
            correlation_id="cid_test_62",
            result=[{"amount": 5000, "account": "REAL_ACCOUNT_NAME"},
                    {"amount": 7777, "account": "ANOTHER_SECRET"}],
            note=None, source_status="done",
        )
        repr_62 = repr(po_62_list)
        check("Req 62a: repr on list-of-dicts result does NOT crash",
              "PollOutcome(" in repr_62)
        check("Req 62b: repr on list shows 'list(len=2,item_keys=...'",
              "list(len=2,item_keys=" in repr_62)
        check("Req 62c: repr does NOT leak amount 5000",
              "5000" not in repr_62 and "7777" not in repr_62)
        check("Req 62d: repr does NOT leak account names",
              "REAL_ACCOUNT_NAME" not in repr_62
              and "ANOTHER_SECRET" not in repr_62)
        # Scalar result
        po_62_scalar = WC.PollOutcome(
            state=WC.PollResultState.WRITE_SAVED,
            correlation_id="x", result=42, note=None,
            source_status="done",
        )
        repr_62b = repr(po_62_scalar)
        check("Req 62e: repr on scalar (int) result -> '<int>'",
              "<int>" in repr_62b
              and "42" not in repr_62b)
        # Empty list
        po_62_empty = WC.PollOutcome(
            state=WC.PollResultState.READ_RESULT,
            correlation_id="x", result=[], note=None,
            source_status="done",
        )
        repr_62c = repr(po_62_empty)
        check("Req 62f: repr on empty list -> 'list(empty)'",
              "list(empty)" in repr_62c)

        # Req 63: every PollResultState maps to an Outcome that is
        # NEVER DEFERRED_DISABLED (pinned so a future regression
        # cannot conflate async-in-progress with feature-off).
        for st in WC.PollResultState:
            po_63 = WC.PollOutcome(
                state=st, correlation_id="x", result=None,
                note=None, source_status="pending",
            )
            oc_63 = WC.poll_outcome_to_user_outcome(po_63)
            check("Req 63: PollResultState '" + st.value
                  + "' does NOT map to OutcomeState.DEFERRED_DISABLED",
                  oc_63.state != O.OutcomeState.DEFERRED_DISABLED)
            check("Req 63: PollResultState '" + st.value
                  + "' does NOT map to "
                  + "OutcomeState.CLASSIFIER_UNAVAILABLE / "
                  + "NOT_UNDERSTOOD / VALIDATION_FAILED",
                  oc_63.state not in (
                      O.OutcomeState.CLASSIFIER_UNAVAILABLE,
                      O.OutcomeState.NOT_UNDERSTOOD,
                      O.OutcomeState.VALIDATION_FAILED,
                  ))

        # Req 64: poll_request_result with the first now_fn() reading
        # already PAST deadline -> WRITE_ACCEPTED with sleep_fn never
        # invoked (defensive against clock jumps / NTP corrections).
        scen_64 = str(Path(tmp) / "scen_64.sqlite3")
        S.init_db(db=scen_64)
        cid_64 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="po64",
            account="X", payload={"a": 1},
            db=scen_64, now_fn=lambda: 1700000000.0,
        )
        sleep_calls = {"n": 0}

        def counting_sleep(s):
            sleep_calls["n"] += 1
        now_calls = {"n": 0}

        def jumping_now():
            # First call (start) = 1000.0; second (deadline check) = 9999.0
            # (well past 1000.0 + 1.0 = 1001.0).
            now_calls["n"] += 1
            return 1000.0 if now_calls["n"] == 1 else 9999.0
        po_64 = WC.poll_request_result(
            cid_64, deadline_seconds=1.0, poll_interval_seconds=0.1,
            sleep_fn=counting_sleep, now_fn=jumping_now,
            db=scen_64,
        )
        check("Req 64a: clock-jump past deadline on 2nd now() -> "
              "WRITE_ACCEPTED",
              po_64.state == WC.PollResultState.WRITE_ACCEPTED)
        check("Req 64b: sleep_fn never called when deadline already past "
              "on first iteration",
              sleep_calls["n"] == 0)

        # Req 65: multi-iteration poll loop (deterministic ticking
        # clock) exercises actual loop body, not just the
        # deadline-already-past branch.
        scen_65 = str(Path(tmp) / "scen_65.sqlite3")
        S.init_db(db=scen_65)
        cid_65 = WC.enqueue_write_request(
            kind="store_write", user_id=7, business_key="po65",
            account="X", payload={"a": 1},
            db=scen_65, now_fn=lambda: 1700000000.0,
        )
        # Simulate: row gets marked done on the 3rd poll iteration.
        # Clock ticks 0.1 per call; deadline is 1.0.
        ticks_65 = {"n": 0}
        STEP_65 = 0.1
        START_65 = 1700000010.0

        def ticking_now_65():
            v = START_65 + STEP_65 * ticks_65["n"]
            ticks_65["n"] += 1
            return v
        sleep_count_65 = {"n": 0}

        def sleep_then_mark_done(s):
            sleep_count_65["n"] += 1
            if sleep_count_65["n"] == 2:
                # On the second sleep, transition the row to done.
                RQ.claim_next_request(
                    worker_id="wkr_65", db=scen_65,
                    now_fn=lambda: START_65 + STEP_65 * 5,
                )
                RQ.mark_done(
                    cid_65, result={"saved": True}, db=scen_65,
                    now_fn=lambda: START_65 + STEP_65 * 5,
                )
        po_65 = WC.poll_request_result(
            cid_65, deadline_seconds=1.0, poll_interval_seconds=STEP_65,
            sleep_fn=sleep_then_mark_done, now_fn=ticking_now_65,
            db=scen_65,
        )
        check("Req 65a: multi-iteration loop returned WRITE_SAVED after "
              "row transitioned mid-poll",
              po_65.state == WC.PollResultState.WRITE_SAVED)
        check("Req 65b: sleep_fn called at least once "
              "(actual loop iteration happened)",
              sleep_count_65["n"] >= 1)

        # Req 66: enqueue_write_request retry with a DIFFERENT
        # payload silently keeps the ORIGINAL payload
        # (documented intent: idempotent retry; user's first
        # snapshot wins).  We pin the behavior so a future refactor
        # cannot accidentally allow the second payload to overwrite.
        scen_66 = str(Path(tmp) / "scen_66.sqlite3")
        S.init_db(db=scen_66)
        cid_66_1 = WC.enqueue_write_request(
            kind="store_write", user_id=7,
            business_key="wr66", account="X",
            payload={"delta": -1000.0,
                     "snapshot_value": 10000.0},
            db=scen_66, now_fn=lambda: 1700000000.0,
        )
        cid_66_2 = WC.enqueue_write_request(
            kind="store_write", user_id=7,
            business_key="wr66", account="X",
            payload={"delta": -9999.0,
                     "snapshot_value": 99999.0},
            db=scen_66, now_fn=lambda: 1700000010.0,
        )
        check("Req 66a: retry returns SAME cid (idempotent)",
              cid_66_1 == cid_66_2)
        rec_66 = RQ.get_request(cid_66_1, db=scen_66)
        check("Req 66b: ORIGINAL delta preserved on retry "
              "(second payload silently ignored)",
              rec_66.payload["delta"] == -1000.0)
        check("Req 66c: ORIGINAL snapshot_value preserved",
              rec_66.payload["snapshot_value"] == 10000.0)

        # Req 67: enqueue_write_request rejects ttl_seconds that
        # violates ttl >= ratio*lease invariant (the underlying B-02C
        # check) at the B-02D layer too.
        scen_67 = str(Path(tmp) / "scen_67.sqlite3")
        S.init_db(db=scen_67)
        raised_67 = False
        try:
            WC.enqueue_write_request(
                kind="store_write", user_id=7, business_key="bad67",
                account="X", payload={"x": 1},
                lease_seconds=30.0, ttl_seconds=30.0,
                db=scen_67,
            )
        except ValueError:
            raised_67 = True
        check("Req 67: ttl_seconds < ratio*lease at WRITE enqueue "
              "-> ValueError",
              raised_67)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
