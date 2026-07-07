#!/usr/bin/env python3
"""Plain-python tests (no pytest) for B-02C inert request-queue API
(bot.aios_request_queue).

SAFE: temp DBs only; SYNTHETIC payloads; NO network, NO live Notion, NO
services, NO production-DB writes, NO secret values used.  Mirrors the
scripts/test_aios_*.py style.

Covers:

  Constants + value-free dataclass (Req 1-4):
    1. ALLOWED_KINDS matches the union of WRITE_KINDS and READ_KINDS
    2. ALLOWED_STATUSES exactly matches the 6-value status set
    3. RequestRecord is frozen
    4. RequestRecord.__repr__ redacts payload contents (only key names)

  generate_correlation_id (Req 5-6):
    5. returns a non-empty str (UUID4 hex shape, 32 lowercase hex chars)
    6. successive calls produce distinct ids (rough uniqueness check)

  enqueue_request (Req 7-13):
    7. creates a pending row with attempts=0
    8. payload round-trips through get_request
    9. caller-supplied correlation_id is honoured
   10. None correlation_id is auto-generated
   11. duplicate correlation_id is IDEMPOTENT (returns same id, no
       mutation to existing row, NO exception)
   12. invalid kind -> InvalidKindError
   13. payload with secret-like key -> SecretLikePayloadError (recursive
       scan at any depth)

  ttl_deadline (Req 14):
   14. ttl_seconds=N sets ttl_deadline_ts = now + N

  get_request (Req 15-16):
   15. missing correlation_id returns None
   16. existing returns full RequestRecord with all 14 fields

  claim_next_request (Req 17-25):
   17. claims oldest pending; status -> claimed; worker_id + claimed_ts set
   18. no eligible row returns None
   19. account filter picks only matching account
   20. kinds filter respects whitelist
   21. invalid kind in kinds filter -> InvalidKindError
   22. respects next_attempt_ts (skips not-yet-due failed rows)
   23. claim_next_request requires worker_id
   24. per-account serialization: with account=X, claim picks oldest X row
       (even if another account has an older row overall)
   25. failed-with-due retry is claimable

  mark_done (Req 26-28):
   26. mark_done on claimed row -> done; result_json + resolved_ts set
   27. mark_done on already-done row -> WrongStateError
   28. mark_done with secret-like result key -> SecretLikePayloadError

  mark_failed_or_dead (Req 29-34):
   29. on claimed row with attempts+1 < max -> 'failed'; attempts++;
       next_attempt_ts set; worker_id/claimed_ts cleared (becomes
       retry-eligible)
   30. on claimed row with attempts+1 >= max -> 'dead'; resolved_ts set
   31. error_class stored as class NAME ONLY (sanitized to [A-Za-z0-9_])
   32. error_class truncated at 64 chars
   33. on non-claimed row -> WrongStateError
   34. on missing correlation_id -> ValueError

  expire_due_requests (Req 35-37):
   35. sweeps pending/claimed/failed with ttl_deadline_ts <= now ->
       expired (resolved_ts set)
   36. does NOT touch 'done' rows even if past TTL
   37. NULL ttl_deadline_ts -> never expires

  get_request_queue_stats (Req 38):
   38. returns aggregate dict (total, by_status, by_kind,
       oldest_pending_age_sec); NEVER returns correlation_ids or
       payload contents

  Hygiene + import-safety (Req 39-41):
   39. module imports cleanly with NO live Notion / Bitwarden / network
       (verified by absence of httpx import in this module)
   40. RequestRecord.__repr__ does NOT leak real account name even when
       account is populated
   41. RequestRecord.__repr__ does NOT leak payload VALUES

  Existing B-02A/B compatibility (Req 42):
   42. SCHEMA_VERSION unchanged; request_queue still present

  1st-ADDENDUM coverage (Req 43-50):
   43. all WRITE kinds past TTL -> dead (parametric)
   44. all READ kinds past TTL -> expired (parametric)
   45. per-account write lock blocks second WRITE on same account
   46. READ on same account NOT blocked by WRITE lock; NULL-account READ
       NOT blocked either
   47. 5 successive claims return 5 DISTINCT ids; 6th returns None
       (atomic BEGIN IMMEDIATE proof)
   48. recover_stale_claims flips stale claimed -> failed (StaleClaimRecovered)
   49. per-account lock applies even with account=NULL; cross-account
       writes proceed independently
   50. get_request_queue_stats reflects dead/expired buckets

  2nd-ADDENDUM coverage (Req 51-55):
   51. enqueue WRITE without ttl_seconds -> ValueError;
       bool ttl / sub-MIN ttl rejected; failed enqueue did NOT create row
   52. enqueue READ without ttl_seconds -> OK; NULL ttl_deadline stored
   53. compute_backoff: monotone, capped, validates args;
       caller-computed next_attempt_ts honoured (no implicit retry storm)
   54. check_write_ttl_over_lease: ttl >= ratio*lease OK;
       ttl < ratio*lease ValueError; bool / zero rejected
   55. lease-vs-TTL race: WRITE with ttl=120, lease=30, crash at t=10 ->
       recover at t=50 (BEFORE TTL t=120) -> failed -> reclaim -> done
       (proof: no silent loss to dead via TTL bypass)

Run:  PYTHONPATH=<repo> python3 scripts/test_aios_b02c_request_queue_api.py
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    tmp = tempfile.mkdtemp(prefix="aios_b02c_test_")
    sys.path.insert(0, str(repo_root))

    os.environ["AIOS_DATA_DB"] = str(Path(tmp) / "_fallback.sqlite3")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-stub-not-real")
    os.environ.setdefault("TELEGRAM_OWNER_ID", "12345")
    os.environ.setdefault("NOTION_API_KEY", "")

    failed: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    try:
        from bot import aios_storage as S
        from bot import aios_request_queue as RQ

        # ----------------------------------------------------------
        # Req 1-4: constants + RequestRecord shape
        # ----------------------------------------------------------
        check("Req 1: ALLOWED_KINDS matches the WRITE/READ split",
              set(RQ.ALLOWED_KINDS) == set(RQ.WRITE_KINDS) | set(RQ.READ_KINDS))
        check("Req 2: ALLOWED_STATUSES exactly matches 6-value spec set",
              set(RQ.ALLOWED_STATUSES) == {
                  "pending", "claimed", "done", "failed", "expired", "dead",
              })
        # RequestRecord frozen
        rr = RQ.RequestRecord(
            correlation_id="frozen_test", kind="store_write", account=None,
            status="pending", worker_id=None, payload=None, result=None,
            error_class=None, attempts=0, created_ts=1700000000.0,
            claimed_ts=None, resolved_ts=None, next_attempt_ts=None,
            ttl_deadline_ts=None,
        )
        frozen_ok = False
        try:
            rr.status = "done"  # type: ignore[misc]
        except Exception:
            frozen_ok = True
        check("Req 3: RequestRecord is frozen", frozen_ok)
        # __repr__ redacts payload values
        rr_with_payload = RQ.RequestRecord(
            correlation_id="repr_test", kind="store_write",
            account="AcctSecretRepr", status="pending", worker_id=None,
            payload={"field": "FieldSecret",
                     "count": 42424242,
                     "account": "AcctSecretRepr"},
            result=None, error_class=None, attempts=0,
            created_ts=1700000000.0, claimed_ts=None, resolved_ts=None,
            next_attempt_ts=None, ttl_deadline_ts=None,
        )
        rep = repr(rr_with_payload)
        check("Req 4a: __repr__ does NOT leak account name",
              "AcctSecretRepr" not in rep)
        check("Req 4b: __repr__ does NOT leak field text",
              "FieldSecret" not in rep)
        check("Req 4c: __repr__ does NOT leak amount literal",
              "42424242" not in rep)
        check("Req 4d: __repr__ shows payload_keys list",
              "payload_keys=['account', 'count', 'field']" in rep)
        check("Req 4e: __repr__ shows account_present=True",
              "account_present=True" in rep)

        # ----------------------------------------------------------
        # Req 5-6: generate_correlation_id
        # ----------------------------------------------------------
        cid_1 = RQ.generate_correlation_id()
        cid_2 = RQ.generate_correlation_id()
        check("Req 5a: returns non-empty str",
              isinstance(cid_1, str) and len(cid_1) > 0)
        check("Req 5b: UUID4 hex shape (32 lowercase hex chars)",
              bool(re.match(r"^[0-9a-f]{32}$", cid_1)))
        check("Req 6: successive calls produce distinct ids", cid_1 != cid_2)

        # ----------------------------------------------------------
        # Req 7-13: enqueue_request
        # ----------------------------------------------------------
        scen_a = str(Path(tmp) / "scen_a_enqueue.sqlite3")
        S.init_db(db=scen_a)
        cid_a = RQ.enqueue_request(
            kind="store_write", account="notes",
            payload={"field": "value", "count": 2000},
            ttl_seconds=300.0,
            correlation_id="enq_cid_001", db=scen_a,
        )
        check("Req 7: enqueue returns the correlation_id",
              cid_a == "enq_cid_001")
        rec_a = RQ.get_request("enq_cid_001", db=scen_a)
        check("Req 7b: row created with status='pending'",
              rec_a is not None and rec_a.status == "pending")
        check("Req 7c: attempts default 0", rec_a.attempts == 0)
        check("Req 8: payload round-trips through get_request",
              rec_a.payload == {"field": "value", "count": 2000})
        # Req 9-10: caller-supplied vs auto-generated
        cid_b = RQ.enqueue_request(
            kind="store_read", db=scen_a, correlation_id=None,
        )
        check("Req 9: auto-generated correlation_id is non-empty hex",
              isinstance(cid_b, str)
              and bool(re.match(r"^[0-9a-f]{32}$", cid_b)))
        cid_c = RQ.enqueue_request(
            kind="store_read", db=scen_a,
            correlation_id="caller_supplied_cid_xyz",
        )
        check("Req 10: caller-supplied correlation_id honoured",
              cid_c == "caller_supplied_cid_xyz")
        # Req 11: duplicate is idempotent (no exception, same id, no mutation)
        cid_dup = RQ.enqueue_request(
            kind="store_write", account="DiffAccount",
            payload={"field": "OTHER", "count": 99999999},
            ttl_seconds=300.0,
            correlation_id="enq_cid_001", db=scen_a,
        )
        check("Req 11a: duplicate returns the SAME correlation_id",
              cid_dup == "enq_cid_001")
        rec_a2 = RQ.get_request("enq_cid_001", db=scen_a)
        check("Req 11b: original row UNCHANGED (account preserved)",
              rec_a2.account == "notes")
        check("Req 11c: original row UNCHANGED (payload preserved)",
              rec_a2.payload == {"field": "value", "count": 2000})
        # Req 12: invalid kind
        raised_kind = False
        try:
            RQ.enqueue_request(kind="bogus_kind", db=scen_a,
                               correlation_id="enq_bad_kind")
        except RQ.InvalidKindError:
            raised_kind = True
        check("Req 12: invalid kind -> InvalidKindError", raised_kind)
        # Req 13: secret-like keys (top-level)
        secret_keys = [
            "token", "api_key", "Api-Key", "secret", "password",
            "passwd", "bearer", "auth", "private_key", "access_key",
            "refresh_token", "client_secret",
        ]
        for sk in secret_keys:
            raised_sk = False
            try:
                RQ.enqueue_request(
                    kind="store_write", account="X",
                    payload={sk: "value_doesnt_matter"},
                    ttl_seconds=300.0,
                    correlation_id="enq_sk_" + sk, db=scen_a,
                )
            except RQ.SecretLikePayloadError:
                raised_sk = True
            check("Req 13: top-level secret-like key '" + sk
                  + "' -> SecretLikePayloadError", raised_sk)
        # Req 13b: nested secret key
        raised_nested = False
        try:
            RQ.enqueue_request(
                kind="store_write", account="X",
                payload={"params": {"deeper": {"api_token": "x"}}},
                ttl_seconds=300.0,
                correlation_id="enq_nested_secret", db=scen_a,
            )
        except RQ.SecretLikePayloadError:
            raised_nested = True
        check("Req 13b: nested secret key (recursive scan) -> raise",
              raised_nested)

        # ----------------------------------------------------------
        # Req 14: ttl_deadline
        # ----------------------------------------------------------
        fixed_now = 1700001000.0
        cid_ttl = RQ.enqueue_request(
            kind="store_read", ttl_seconds=120.0,
            correlation_id="enq_ttl_1", db=scen_a,
            now_fn=lambda: fixed_now,
        )
        rec_ttl = RQ.get_request("enq_ttl_1", db=scen_a)
        check("Req 14a: ttl_deadline_ts == now + ttl_seconds",
              abs(rec_ttl.ttl_deadline_ts - (fixed_now + 120.0)) < 1e-6)
        # ttl_seconds=None -> NULL
        RQ.enqueue_request(
            kind="store_read", correlation_id="enq_no_ttl", db=scen_a,
        )
        rec_no_ttl = RQ.get_request("enq_no_ttl", db=scen_a)
        check("Req 14b: ttl_seconds=None -> ttl_deadline_ts is None",
              rec_no_ttl.ttl_deadline_ts is None)

        # ----------------------------------------------------------
        # Req 15-16: get_request
        # ----------------------------------------------------------
        check("Req 15: missing correlation_id -> None",
              RQ.get_request("does_not_exist", db=scen_a) is None)
        # All 14 fields populated
        rr_fields = {f.name for f in dataclasses.fields(RQ.RequestRecord)}
        check("Req 16: RequestRecord has 14 fields",
              len(rr_fields) == 14)

        # ----------------------------------------------------------
        # Req 17-25: claim_next_request
        # ----------------------------------------------------------
        scen_b = str(Path(tmp) / "scen_b_claim.sqlite3")
        S.init_db(db=scen_b)
        # Seed 3 pending rows with deterministic created_ts
        RQ.enqueue_request(kind="store_write", account="Acct1",
                           payload={"x": 1}, ttl_seconds=300.0,
                           correlation_id="cl_1",
                           db=scen_b, now_fn=lambda: 1700000010.0)
        RQ.enqueue_request(kind="store_write", account="Acct2",
                           payload={"x": 2}, ttl_seconds=300.0,
                           correlation_id="cl_2",
                           db=scen_b, now_fn=lambda: 1700000020.0)
        RQ.enqueue_request(kind="store_read", account=None,
                           correlation_id="cl_3",
                           db=scen_b, now_fn=lambda: 1700000030.0)
        now_claim = 1700100000.0
        claimed = RQ.claim_next_request(
            worker_id="wkr_a", now_fn=lambda: now_claim, db=scen_b,
        )
        check("Req 17a: claim returns RequestRecord",
              isinstance(claimed, RQ.RequestRecord))
        check("Req 17b: claim picks OLDEST (cl_1)",
              claimed.correlation_id == "cl_1")
        check("Req 17c: status -> claimed", claimed.status == "claimed")
        check("Req 17d: worker_id set", claimed.worker_id == "wkr_a")
        check("Req 17e: claimed_ts set ~= now",
              claimed.claimed_ts is not None
              and abs(claimed.claimed_ts - now_claim) < 1e-6)
        # Req 18: no eligible row (all claimed/done)
        scen_b_empty = str(Path(tmp) / "scen_b_empty.sqlite3")
        S.init_db(db=scen_b_empty)
        check("Req 18: no eligible row -> None",
              RQ.claim_next_request(worker_id="wkr",
                                     db=scen_b_empty) is None)
        # Req 19: account filter
        # In scen_b we already claimed cl_1; cl_2 (Acct2) and cl_3 (None)
        # remain pending.  Filter by account='Acct2' must pick cl_2.
        c_acct2 = RQ.claim_next_request(
            worker_id="wkr_b", account="Acct2",
            now_fn=lambda: now_claim + 1, db=scen_b,
        )
        check("Req 19: account filter picks Acct2 row",
              c_acct2 is not None and c_acct2.correlation_id == "cl_2")
        # Req 20: kinds filter
        # cl_3 (store_read) remains; filter by ['store_write'] returns None.
        c_kinds_none = RQ.claim_next_request(
            worker_id="wkr_c", kinds=["store_write"],
            now_fn=lambda: now_claim + 2, db=scen_b,
        )
        check("Req 20a: kinds filter excludes other kinds (returns None)",
              c_kinds_none is None)
        c_kinds_match = RQ.claim_next_request(
            worker_id="wkr_c", kinds=["store_read"],
            now_fn=lambda: now_claim + 2, db=scen_b,
        )
        check("Req 20b: kinds filter matches store_read (cl_3)",
              c_kinds_match is not None
              and c_kinds_match.correlation_id == "cl_3")
        # Req 21: invalid kind in filter
        raised_filt = False
        try:
            RQ.claim_next_request(worker_id="wkr_e",
                                   kinds=["nonexistent_kind"],
                                   db=scen_b)
        except RQ.InvalidKindError:
            raised_filt = True
        check("Req 21: invalid kind in kinds filter -> InvalidKindError",
              raised_filt)
        # Req 22: next_attempt_ts not yet due is skipped
        scen_due = str(Path(tmp) / "scen_due.sqlite3")
        S.init_db(db=scen_due)
        # Hand-craft a failed row with next_attempt_ts in the future
        with S.connect(db=scen_due) as conn:
            conn.execute(
                "INSERT INTO request_queue (correlation_id, kind,"
                " status, attempts, error_class, next_attempt_ts, created_ts)"
                " VALUES (?,?,?,?,?,?,?)",
                ("future_retry", "store_read", "failed", 1,
                 "SomeError", 1700200000.0, 1700000000.0),
            )
        check("Req 22a: claim skips failed row with next_attempt_ts in future",
              RQ.claim_next_request(worker_id="wkr_d",
                                     now_fn=lambda: 1700100000.0,
                                     db=scen_due) is None)
        # When the time is past next_attempt_ts, it becomes claimable
        c_due = RQ.claim_next_request(
            worker_id="wkr_d2", now_fn=lambda: 1700200001.0, db=scen_due,
        )
        check("Req 22b: claim picks failed row when next_attempt_ts is past",
              c_due is not None and c_due.correlation_id == "future_retry"
              and c_due.status == "claimed")
        # Req 23: worker_id required
        raised_wid = False
        try:
            RQ.claim_next_request(worker_id="", db=scen_due)
        except ValueError:
            raised_wid = True
        check("Req 23: empty worker_id -> ValueError", raised_wid)
        # Req 24: per-account serialization
        scen_pas = str(Path(tmp) / "scen_per_account.sqlite3")
        S.init_db(db=scen_pas)
        # Oldest overall is for AcctA; for AcctB, the oldest is later
        RQ.enqueue_request(kind="store_write", account="AcctA",
                           ttl_seconds=300.0, correlation_id="pas_a1",
                           db=scen_pas, now_fn=lambda: 1700000000.0)
        RQ.enqueue_request(kind="store_write", account="AcctB",
                           ttl_seconds=300.0, correlation_id="pas_b1",
                           db=scen_pas, now_fn=lambda: 1700000010.0)
        RQ.enqueue_request(kind="store_write", account="AcctA",
                           ttl_seconds=300.0, correlation_id="pas_a2",
                           db=scen_pas, now_fn=lambda: 1700000020.0)
        c_pas_b = RQ.claim_next_request(
            worker_id="wkr_p", account="AcctB",
            now_fn=lambda: 1700100000.0, db=scen_pas,
        )
        check("Req 24: per-account claim picks AcctB row (not the overall oldest)",
              c_pas_b is not None and c_pas_b.correlation_id == "pas_b1"
              and c_pas_b.account == "AcctB")
        # Req 25: failed-with-due retry is claimable (verified via Req 22b too)
        check("Req 25: failed retry status is in _CLAIMABLE_STATUSES",
              "failed" in RQ._CLAIMABLE_STATUSES)

        # ----------------------------------------------------------
        # Req 26-28: mark_done
        # ----------------------------------------------------------
        scen_md = str(Path(tmp) / "scen_md.sqlite3")
        S.init_db(db=scen_md)
        RQ.enqueue_request(kind="store_write", correlation_id="md_cid_1",
                           account="notes", payload={"field": "value"},
                           ttl_seconds=300.0, db=scen_md)
        RQ.claim_next_request(worker_id="wkr_md", db=scen_md,
                               now_fn=lambda: 1700100100.0)
        ok = RQ.mark_done("md_cid_1", result={"page_id": "p_xyz"},
                          db=scen_md, now_fn=lambda: 1700100200.0)
        check("Req 26a: mark_done on claimed row returns True", ok is True)
        rec_md = RQ.get_request("md_cid_1", db=scen_md)
        check("Req 26b: status -> done", rec_md.status == "done")
        check("Req 26c: result_json round-trips",
              rec_md.result == {"page_id": "p_xyz"})
        check("Req 26d: resolved_ts set ~= now",
              abs(rec_md.resolved_ts - 1700100200.0) < 1e-6)
        # Req 27: mark_done on done row -> WrongStateError
        raised_ws = False
        try:
            RQ.mark_done("md_cid_1", result={"page_id": "p2"},
                         db=scen_md, now_fn=lambda: 1700100300.0)
        except RQ.WrongStateError:
            raised_ws = True
        check("Req 27: mark_done on already-done -> WrongStateError",
              raised_ws)
        # Req 28: mark_done with secret-like result key
        RQ.enqueue_request(kind="store_write", correlation_id="md_cid_2",
                           ttl_seconds=300.0, db=scen_md)
        RQ.claim_next_request(worker_id="wkr_md2", db=scen_md,
                               now_fn=lambda: 1700100400.0)
        raised_skp = False
        try:
            RQ.mark_done("md_cid_2", result={"auth_token": "x"},
                         db=scen_md, now_fn=lambda: 1700100500.0)
        except RQ.SecretLikePayloadError:
            raised_skp = True
        check("Req 28: mark_done with secret-like result key -> raise",
              raised_skp)

        # ----------------------------------------------------------
        # Req 29-34: mark_failed_or_dead
        # ----------------------------------------------------------
        scen_mf = str(Path(tmp) / "scen_mf.sqlite3")
        S.init_db(db=scen_mf)
        RQ.enqueue_request(kind="store_write", correlation_id="mf_cid_1",
                           ttl_seconds=600.0, db=scen_mf)
        RQ.claim_next_request(worker_id="wkr_mf", db=scen_mf,
                               now_fn=lambda: 1700100000.0)
        new_status = RQ.mark_failed_or_dead(
            "mf_cid_1", error_class="HTTPStatusError",
            next_attempt_ts=1700200000.0, max_attempts=3,
            db=scen_mf, now_fn=lambda: 1700100100.0,
        )
        check("Req 29a: failed (attempts 1 < max 3) -> 'failed'",
              new_status == "failed")
        rec_mf = RQ.get_request("mf_cid_1", db=scen_mf)
        check("Req 29b: attempts == 1", rec_mf.attempts == 1)
        check("Req 29c: next_attempt_ts set",
              abs(rec_mf.next_attempt_ts - 1700200000.0) < 1e-6)
        check("Req 29d: worker_id cleared on failed (retry-eligible)",
              rec_mf.worker_id is None)
        check("Req 29e: claimed_ts cleared on failed", rec_mf.claimed_ts is None)
        # Re-claim, fail again -> attempts=2 still 'failed'
        RQ.claim_next_request(worker_id="wkr_mf2", db=scen_mf,
                               now_fn=lambda: 1700300000.0)
        new_status2 = RQ.mark_failed_or_dead(
            "mf_cid_1", error_class="HTTPStatusError",
            next_attempt_ts=1700400000.0, max_attempts=3,
            db=scen_mf, now_fn=lambda: 1700300100.0,
        )
        check("Req 29f: second failure (attempts 2 < max 3) -> 'failed'",
              new_status2 == "failed")
        # Re-claim, fail again -> attempts=3 == max -> 'dead'
        RQ.claim_next_request(worker_id="wkr_mf3", db=scen_mf,
                               now_fn=lambda: 1700500000.0)
        new_status3 = RQ.mark_failed_or_dead(
            "mf_cid_1", error_class="HTTPStatusError",
            max_attempts=3,
            db=scen_mf, now_fn=lambda: 1700500100.0,
        )
        check("Req 30a: third failure (attempts 3 >= max 3) -> 'dead'",
              new_status3 == "dead")
        rec_mf_dead = RQ.get_request("mf_cid_1", db=scen_mf)
        check("Req 30b: resolved_ts set on dead",
              rec_mf_dead.resolved_ts is not None)
        check("Req 30c: attempts == 3", rec_mf_dead.attempts == 3)
        # Req 31: error_class sanitization (class NAME only).
        RQ.enqueue_request(kind="store_write", correlation_id="mf_cid_san",
                           ttl_seconds=600.0, db=scen_mf)
        RQ.claim_next_request(worker_id="wkr_san", db=scen_mf,
                               now_fn=lambda: 1700600000.0)
        # Operator passes a "class with attached args" style string.
        # Sanitization MUST strip non-identifier chars.
        RQ.mark_failed_or_dead(
            "mf_cid_san",
            error_class="HTTPStatusError('https://api.notion.com/secret-url')",
            max_attempts=99, db=scen_mf,
            now_fn=lambda: 1700600100.0,
        )
        rec_san = RQ.get_request("mf_cid_san", db=scen_mf)
        check("Req 31a: error_class is [A-Za-z0-9_] only (Python identifier)",
              all(c.isalnum() or c == "_"
                  for c in (rec_san.error_class or "")))
        # Addendum-fix: sanitizer extracts the FIRST identifier prefix only,
        # so the exact class name is preserved (no leak of URL / arg text).
        check("Req 31b: error_class == 'HTTPStatusError' (first-prefix only)",
              rec_san.error_class == "HTTPStatusError")
        # Note: "https" alone would false-positive ("HTTPStatusError".lower()
        # naturally starts with "https"); check unambiguous URL fragments.
        check("Req 31c: error_class does NOT contain url fragment",
              "notion" not in (rec_san.error_class or "").lower()
              and "api." not in (rec_san.error_class or "").lower()
              and "secret" not in (rec_san.error_class or "").lower()
              and "url" not in (rec_san.error_class or "").lower())
        # Req 32: error_class length cap at 64. Use Python-identifier
        # chars (Xs) so the sanitizer DOES match a long prefix; otherwise
        # the first-prefix extract would return "" and length would be 0.
        # Fresh DB so the earlier-set-to-failed rows in scen_mf don't get
        # picked first by the oldest-first claim.
        scen_mf2 = str(Path(tmp) / "scen_mf2.sqlite3")
        S.init_db(db=scen_mf2)
        long_err = "X" * 200
        RQ.enqueue_request(kind="store_write", correlation_id="mf_cid_long",
                           ttl_seconds=600.0, db=scen_mf2)
        RQ.claim_next_request(worker_id="wkr_long", db=scen_mf2,
                               now_fn=lambda: 1700700000.0)
        RQ.mark_failed_or_dead(
            "mf_cid_long", error_class=long_err, max_attempts=99,
            db=scen_mf2, now_fn=lambda: 1700700100.0,
        )
        rec_long = RQ.get_request("mf_cid_long", db=scen_mf2)
        check("Req 32: error_class truncated at 64 chars",
              len(rec_long.error_class) == 64)
        # Req 33: WrongStateError on non-claimed row (e.g. pending).
        # Fresh DB so the row IS the pending row (no prior failed siblings).
        scen_mf3 = str(Path(tmp) / "scen_mf3.sqlite3")
        S.init_db(db=scen_mf3)
        RQ.enqueue_request(kind="store_write", correlation_id="mf_cid_pend",
                           ttl_seconds=600.0, db=scen_mf3)
        raised_ws_mf = False
        try:
            RQ.mark_failed_or_dead(
                "mf_cid_pend", error_class="X", max_attempts=3, db=scen_mf3,
            )
        except RQ.WrongStateError:
            raised_ws_mf = True
        check("Req 33: mark_failed_or_dead on pending -> WrongStateError",
              raised_ws_mf)
        # Req 34: missing correlation_id
        raised_missing = False
        try:
            RQ.mark_failed_or_dead(
                "does_not_exist", error_class="X", db=scen_mf,
            )
        except ValueError:
            raised_missing = True
        check("Req 34: mark_failed_or_dead on missing id -> ValueError",
              raised_missing)

        # ----------------------------------------------------------
        # Req 35-37: expire_due_requests (ADDENDUM: write/read split)
        # ----------------------------------------------------------
        scen_ex = str(Path(tmp) / "scen_ex.sqlite3")
        S.init_db(db=scen_ex)
        # Seed:
        #   ex_w_pend  WRITE store_write, pending, TTL past -> 'dead'
        #   ex_r_pend  READ  store_read, pending, TTL past -> 'expired'
        #   ex_w_done  WRITE store_write, done, TTL past -> UNCHANGED
        #   ex_w_no_ttl WRITE store_write, no TTL -> UNCHANGED
        RQ.enqueue_request(kind="store_write", correlation_id="ex_w_pend",
                           account="AcctEx1", ttl_seconds=10.0,
                           db=scen_ex, now_fn=lambda: 1700000000.0)
        RQ.enqueue_request(kind="store_read", correlation_id="ex_r_pend",
                           ttl_seconds=10.0,
                           db=scen_ex, now_fn=lambda: 1700000000.0)
        RQ.enqueue_request(kind="store_write", correlation_id="ex_w_done",
                           account="AcctExDone", ttl_seconds=10.0,
                           db=scen_ex, now_fn=lambda: 1700000000.0)
        RQ.claim_next_request(worker_id="wkr_ex_done",
                               account="AcctExDone",
                               kinds=["store_write"], db=scen_ex,
                               now_fn=lambda: 1700000001.0)
        RQ.mark_done("ex_w_done", result={"ok": True}, db=scen_ex,
                     now_fn=lambda: 1700000005.0)
        # Req 37 was originally a WRITE without TTL; addendum #2 forbids
        # this at enqueue time.  Use a READ kind (where NULL ttl_deadline
        # remains legal) to keep the "NULL ttl_deadline -> never expires"
        # invariant covered.
        RQ.enqueue_request(kind="store_read", correlation_id="ex_w_no_ttl",
                           db=scen_ex)
        # Sweep at a time well past TTL.
        result_ex = RQ.expire_due_requests(
            now_fn=lambda: 1701000000.0, db=scen_ex,
        )
        check("Req 35a: expire_due returns dict",
              isinstance(result_ex, dict))
        check("Req 35b: write past TTL -> 'dead' (owner-visible; addendum)",
              result_ex["dead_from_ttl"] == 1)
        check("Req 35c: read past TTL -> 'expired' (drop allowed; addendum)",
              result_ex["expired"] == 1)
        check("Req 35d: result_ex total == 2", result_ex["total"] == 2)
        rec_w_pend = RQ.get_request("ex_w_pend", db=scen_ex)
        check("Req 35e: WRITE past-TTL row status -> dead",
              rec_w_pend.status == "dead")
        check("Req 35f: WRITE past-TTL row resolved_ts set",
              rec_w_pend.resolved_ts is not None)
        rec_r_pend = RQ.get_request("ex_r_pend", db=scen_ex)
        check("Req 35g: READ past-TTL row status -> expired",
              rec_r_pend.status == "expired")
        check("Req 35h: READ past-TTL row resolved_ts set",
              rec_r_pend.resolved_ts is not None)
        rec_done = RQ.get_request("ex_w_done", db=scen_ex)
        check("Req 36: done WRITE row NOT touched by expire",
              rec_done.status == "done")
        rec_no_ttl = RQ.get_request("ex_w_no_ttl", db=scen_ex)
        check("Req 37: NULL ttl_deadline_ts -> still pending after sweep",
              rec_no_ttl.status == "pending")

        # ----------------------------------------------------------
        # Req 38: get_request_queue_stats (aggregate, value-free)
        # ----------------------------------------------------------
        stats = RQ.get_request_queue_stats(db=scen_ex)
        check("Req 38a: stats has 'total' key", "total" in stats)
        check("Req 38b: stats has 'by_status' key", "by_status" in stats)
        check("Req 38c: stats has 'by_kind' key", "by_kind" in stats)
        check("Req 38d: stats has 'oldest_pending_age_sec' key",
              "oldest_pending_age_sec" in stats)
        # Value-free: stats output must not contain correlation_id values
        stats_str = repr(stats)
        check("Req 38e: stats output contains NO correlation_id literals",
              "ex_w_pend" not in stats_str
              and "ex_r_pend" not in stats_str
              and "ex_w_done" not in stats_str
              and "ex_w_no_ttl" not in stats_str)
        check("Req 38f: by_status counts are ints",
              all(isinstance(v, int) for v in stats["by_status"].values()))
        check("Req 38g: by_kind counts are ints",
              all(isinstance(v, int) for v in stats["by_kind"].values()))

        # ----------------------------------------------------------
        # Req 39-41: hygiene + import-safety
        # ----------------------------------------------------------
        # Req 39: module does NOT import httpx (no network capacity).
        src = (repo_root / "bot" / "aios_request_queue.py").read_text(
            encoding="utf-8")
        check("Req 39a: module source does NOT import httpx",
              "import httpx" not in src and "from httpx" not in src)
        check("Req 39b: module source does NOT import requests",
              "import requests" not in src
              and "from requests" not in src)
        check("Req 39c: module source does NOT import secrets_loader",
              "secrets_loader" not in src)
        check("Req 39d: module source does NOT reference NOTION_API_KEY",
              "NOTION_API_KEY" not in src)
        check("Req 39e: module source does NOT reference Bitwarden",
              "bitwarden" not in src.lower())
        # Req 40: __repr__ with account does not leak
        rr_a = RQ.RequestRecord(
            correlation_id="leak_test", kind="store_write",
            account="AcctLeakProbe", status="pending", worker_id=None,
            payload={"x": "ValueLeakProbe"}, result=None,
            error_class=None, attempts=0, created_ts=1700000000.0,
            claimed_ts=None, resolved_ts=None, next_attempt_ts=None,
            ttl_deadline_ts=None,
        )
        rep_a = repr(rr_a)
        check("Req 40: __repr__ does NOT leak account name even when populated",
              "AcctLeakProbe" not in rep_a)
        check("Req 41: __repr__ does NOT leak payload values",
              "ValueLeakProbe" not in rep_a)

        # ----------------------------------------------------------
        # Req 42: existing B-02A invariants intact
        # ----------------------------------------------------------
        check("Req 42a: SCHEMA_VERSION is a non-empty version string",
              isinstance(S.SCHEMA_VERSION, str) and bool(S.SCHEMA_VERSION))
        with S.connect(db=scen_a) as conn:
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        check("Req 42b: request_queue table still present",
              "request_queue" in tables)

        # ================================================================
        # ADDENDUM SCENARIOS (Req 43-50): close 5 audit-findings from the
        # B-02C addendum.
        # ================================================================

        # ----- Req 43: write past TTL -> dead (NOT expired, NOT lost).
        # Already covered structurally by Req 35b/35e; pin it as an
        # explicit addendum-named scenario with a separate seed so the
        # invariant is auditable in isolation.
        scen_43 = str(Path(tmp) / "scen_43_write_ttl_dead.sqlite3")
        S.init_db(db=scen_43)
        for write_kind in RQ.WRITE_KINDS:
            RQ.enqueue_request(
                kind=write_kind, account="AcctW43_" + write_kind,
                correlation_id="w43_" + write_kind,
                ttl_seconds=5.0, db=scen_43,
                now_fn=lambda: 1700000000.0,
            )
        r43 = RQ.expire_due_requests(
            now_fn=lambda: 1701000000.0, db=scen_43,
        )
        check("Req 43a: all WRITE kinds past TTL -> dead",
              r43["dead_from_ttl"] == len(RQ.WRITE_KINDS) and r43["expired"] == 0)
        for write_kind in RQ.WRITE_KINDS:
            rec43 = RQ.get_request("w43_" + write_kind, db=scen_43)
            check("Req 43b: WRITE '" + write_kind + "' past TTL is 'dead'",
                  rec43.status == "dead")

        # ----- Req 44: read past TTL -> expired (drop allowed).
        scen_44 = str(Path(tmp) / "scen_44_read_ttl_expired.sqlite3")
        S.init_db(db=scen_44)
        for read_kind in RQ.READ_KINDS:
            RQ.enqueue_request(
                kind=read_kind, correlation_id="r44_" + read_kind,
                ttl_seconds=5.0, db=scen_44,
                now_fn=lambda: 1700000000.0,
            )
        r44 = RQ.expire_due_requests(
            now_fn=lambda: 1701000000.0, db=scen_44,
        )
        check("Req 44a: all READ kinds past TTL -> expired",
              r44["expired"] == len(RQ.READ_KINDS) and r44["dead_from_ttl"] == 0)
        for read_kind in RQ.READ_KINDS:
            rec44 = RQ.get_request("r44_" + read_kind, db=scen_44)
            check("Req 44b: READ '" + read_kind + "' past TTL is 'expired'",
                  rec44.status == "expired")

        # ----- Req 45: second WRITE on same account is NOT claimed
        # while first WRITE is in 'claimed' state.
        scen_45 = str(Path(tmp) / "scen_45_write_lock.sqlite3")
        S.init_db(db=scen_45)
        RQ.enqueue_request(kind="store_write", account="LockAcct",
                           ttl_seconds=600.0,
                           correlation_id="w45_first", db=scen_45,
                           now_fn=lambda: 1700000000.0)
        RQ.enqueue_request(kind="store_write", account="LockAcct",
                           ttl_seconds=600.0,
                           correlation_id="w45_second", db=scen_45,
                           now_fn=lambda: 1700000010.0)
        c45_first = RQ.claim_next_request(
            worker_id="wkr_45a", account="LockAcct",
            now_fn=lambda: 1700100000.0, db=scen_45,
        )
        check("Req 45a: first WRITE claimed",
              c45_first is not None
              and c45_first.correlation_id == "w45_first")
        # Now the SECOND WRITE for LockAcct MUST be blocked.
        c45_second = RQ.claim_next_request(
            worker_id="wkr_45b", account="LockAcct",
            now_fn=lambda: 1700100001.0, db=scen_45,
        )
        check("Req 45b: second WRITE on same account NOT claimable "
              "while first is in 'claimed' state (per-account lock)",
              c45_second is None)
        # When the first resolves, the second becomes claimable.
        RQ.mark_done("w45_first", result={"ok": True}, db=scen_45,
                     now_fn=lambda: 1700100050.0)
        c45_second_after = RQ.claim_next_request(
            worker_id="wkr_45c", account="LockAcct",
            now_fn=lambda: 1700100060.0, db=scen_45,
        )
        check("Req 45c: second WRITE claimable AFTER first resolved",
              c45_second_after is not None
              and c45_second_after.correlation_id == "w45_second")

        # ----- Req 46: READ is claimable while WRITE on same account locked.
        scen_46 = str(Path(tmp) / "scen_46_read_not_blocked.sqlite3")
        S.init_db(db=scen_46)
        RQ.enqueue_request(kind="store_write", account="RAcct",
                           ttl_seconds=600.0,
                           correlation_id="w46_lock", db=scen_46,
                           now_fn=lambda: 1700000000.0)
        RQ.enqueue_request(kind="store_read", account="RAcct",
                           correlation_id="r46_read", db=scen_46,
                           now_fn=lambda: 1700000010.0)
        RQ.enqueue_request(kind="todoist_read", account=None,
                           correlation_id="r46_null", db=scen_46,
                           now_fn=lambda: 1700000020.0)
        # Lock the write
        c46_w = RQ.claim_next_request(
            worker_id="wkr_46a", account="RAcct",
            kinds=["store_write"],
            now_fn=lambda: 1700100000.0, db=scen_46,
        )
        check("Req 46a: WRITE on RAcct claimed (lock acquired)",
              c46_w is not None and c46_w.correlation_id == "w46_lock")
        # READ for same account MUST still be claimable.
        c46_r = RQ.claim_next_request(
            worker_id="wkr_46b", account="RAcct",
            kinds=["store_read"],
            now_fn=lambda: 1700100001.0, db=scen_46,
        )
        check("Req 46b: READ on same account NOT blocked by WRITE lock",
              c46_r is not None and c46_r.correlation_id == "r46_read")
        # READ with account=NULL also claimable.
        c46_n = RQ.claim_next_request(
            worker_id="wkr_46c",
            kinds=["todoist_read"],
            now_fn=lambda: 1700100002.0, db=scen_46,
        )
        check("Req 46c: NULL-account READ NOT blocked by any write lock",
              c46_n is not None and c46_n.correlation_id == "r46_null")

        # ----- Req 47: two claims never return same id (atomic claim).
        # Single-owner BEGIN IMMEDIATE invariant: claim N times in a row
        # against M eligible rows -> never duplicate, total claimed = M.
        scen_47 = str(Path(tmp) / "scen_47_atomic.sqlite3")
        S.init_db(db=scen_47)
        for i in range(5):
            # Use DIFFERENT accounts so per-account write lock doesn't
            # serialize them (we want all 5 claimable in any order).
            RQ.enqueue_request(
                kind="store_write", account="Acct47_" + str(i),
                ttl_seconds=600.0,
                correlation_id="atom_" + str(i),
                db=scen_47, now_fn=lambda i=i: 1700000000.0 + i,
            )
        claimed_ids: list = []
        for i in range(5):
            c = RQ.claim_next_request(
                worker_id="wkr_47_" + str(i),
                now_fn=lambda i=i: 1700100000.0 + i, db=scen_47,
            )
            if c is not None:
                claimed_ids.append(c.correlation_id)
        # 6th claim should be None (all 5 already claimed).
        c_extra = RQ.claim_next_request(
            worker_id="wkr_47_extra",
            now_fn=lambda: 1700100100.0, db=scen_47,
        )
        check("Req 47a: 5 successive claims return 5 distinct ids",
              len(claimed_ids) == 5 and len(set(claimed_ids)) == 5)
        check("Req 47b: 6th claim returns None (no eligible row)",
              c_extra is None)

        # ----- Req 48: stale-claimed-row recovery (explicit, not silent hang).
        scen_48 = str(Path(tmp) / "scen_48_stale.sqlite3")
        S.init_db(db=scen_48)
        RQ.enqueue_request(kind="store_write", account="StaleAcct",
                           ttl_seconds=600.0,
                           correlation_id="stale_1", db=scen_48,
                           now_fn=lambda: 1700000000.0)
        RQ.claim_next_request(
            worker_id="wkr_stale_crashed",
            now_fn=lambda: 1700100000.0, db=scen_48,
        )
        # Worker "crashed": claimed_ts = 1700100000.0, never resolved.
        # Lease = 60s; now = 1700100100 (claimed_ts + 100s past lease).
        n_recovered = RQ.recover_stale_claims(
            lease_seconds=60.0, now_fn=lambda: 1700100100.0, db=scen_48,
        )
        check("Req 48a: stale claimed row recovered (1)", n_recovered == 1)
        rec48 = RQ.get_request("stale_1", db=scen_48)
        check("Req 48b: recovered row status -> failed (retry-eligible)",
              rec48.status == "failed")
        check("Req 48c: attempts incremented", rec48.attempts == 1)
        check("Req 48d: worker_id cleared", rec48.worker_id is None)
        check("Req 48e: claimed_ts cleared", rec48.claimed_ts is None)
        check("Req 48f: error_class records 'StaleClaimRecovered'",
              rec48.error_class == "StaleClaimRecovered")
        check("Req 48g: next_attempt_ts set (immediate retry)",
              rec48.next_attempt_ts is not None)
        # Recovered row is claim-eligible immediately
        c48_retry = RQ.claim_next_request(
            worker_id="wkr_stale_retry",
            now_fn=lambda: 1700100101.0, db=scen_48,
        )
        check("Req 48h: recovered row re-claimable on next call",
              c48_retry is not None and c48_retry.correlation_id == "stale_1")
        # Req 48i: a still-fresh claimed row is NOT recovered.
        scen_48b = str(Path(tmp) / "scen_48b_fresh.sqlite3")
        S.init_db(db=scen_48b)
        RQ.enqueue_request(kind="store_write", account="FreshAcct",
                           ttl_seconds=600.0,
                           correlation_id="fresh_1", db=scen_48b,
                           now_fn=lambda: 1700000000.0)
        RQ.claim_next_request(worker_id="wkr_fresh",
                               now_fn=lambda: 1700100000.0, db=scen_48b)
        # Lease = 60s; now is only 10s after claim -> fresh.
        n_fresh = RQ.recover_stale_claims(
            lease_seconds=60.0, now_fn=lambda: 1700100010.0, db=scen_48b,
        )
        check("Req 48i: fresh claimed row NOT recovered (still in-flight)",
              n_fresh == 0)
        rec48b = RQ.get_request("fresh_1", db=scen_48b)
        check("Req 48j: fresh row still in 'claimed' state",
              rec48b.status == "claimed")
        # Req 48k: invalid lease_seconds -> ValueError
        for bad in (0, -1, "60"):
            raised48 = False
            try:
                RQ.recover_stale_claims(lease_seconds=bad, db=scen_48b)
            except (ValueError, TypeError):
                raised48 = True
            check("Req 48k: invalid lease_seconds " + repr(bad) + " -> raise",
                  raised48)

        # ----- Req 49: write-lock domain for account=NULL (NULL is its
        # own lock domain; NULL-account WRITE blocks another NULL-account
        # WRITE but does NOT block a non-NULL-account WRITE).
        scen_49 = str(Path(tmp) / "scen_49_null_lock.sqlite3")
        S.init_db(db=scen_49)
        RQ.enqueue_request(kind="store_write", account=None,
                           ttl_seconds=600.0,
                           correlation_id="null_w1", db=scen_49,
                           now_fn=lambda: 1700000000.0)
        RQ.enqueue_request(kind="store_write", account=None,
                           ttl_seconds=600.0,
                           correlation_id="null_w2", db=scen_49,
                           now_fn=lambda: 1700000010.0)
        RQ.enqueue_request(kind="store_write", account="OtherAcct",
                           ttl_seconds=600.0,
                           correlation_id="other_w1", db=scen_49,
                           now_fn=lambda: 1700000020.0)
        c49_null = RQ.claim_next_request(
            worker_id="wkr_49a", now_fn=lambda: 1700100000.0, db=scen_49,
        )
        check("Req 49a: first NULL-account WRITE claimed",
              c49_null is not None and c49_null.correlation_id == "null_w1")
        # Second NULL-account WRITE blocked by first
        c49_null2 = RQ.claim_next_request(
            worker_id="wkr_49b",
            kinds=["store_write"],
            now_fn=lambda: 1700100001.0, db=scen_49,
        )
        # But OtherAcct WRITE is NOT blocked (different account domain)
        check("Req 49b: claim returns OtherAcct (skips null_w2, picks other_w1)",
              c49_null2 is not None
              and c49_null2.correlation_id == "other_w1")

        # ----- Req 50: get_request_queue_stats reflects the addendum
        # statuses (dead / expired / failed-retry) without leaking.
        # Use scen_43 which has 3 'dead' rows from TTL sweep + scen_44 which
        # has 3 'expired' rows.
        stats_43 = RQ.get_request_queue_stats(db=scen_43)
        check("Req 50a: stats by_status shows all dead rows in scen_43",
              stats_43["by_status"].get("dead", 0) == len(RQ.WRITE_KINDS))
        stats_44 = RQ.get_request_queue_stats(db=scen_44)
        check("Req 50b: stats by_status shows all expired rows in scen_44",
              stats_44["by_status"].get("expired", 0) == len(RQ.READ_KINDS))

        # ----------------------------------------------------------
        # 2nd-ADDENDUM coverage (Req 51-55)
        # ----------------------------------------------------------
        # Req 51: enqueue write WITHOUT ttl_seconds -> ValueError
        #         (addendum #2: write without TTL never reaches dead-letter)
        scen_51 = str(Path(tmp) / "scen_51.sqlite3")
        S.init_db(db=scen_51)
        for wk in RQ.WRITE_KINDS:
            raised_51 = False
            try:
                RQ.enqueue_request(
                    kind=wk, account="X51", correlation_id="w51_" + wk,
                    db=scen_51,
                )
            except ValueError as e:
                if "ttl_seconds is REQUIRED" in str(e):
                    raised_51 = True
            check("Req 51a: WRITE kind '" + wk
                  + "' without ttl_seconds -> ValueError(ttl_seconds REQUIRED)",
                  raised_51)
        # And the row was NOT inserted (transactional fail-closed).
        for wk in RQ.WRITE_KINDS:
            rec51 = RQ.get_request("w51_" + wk, db=scen_51)
            check("Req 51b: failed enqueue '" + wk
                  + "' did NOT create a row", rec51 is None)
        # Req 51c: enqueue with bool ttl_seconds -> ValueError
        raised_51c = False
        try:
            RQ.enqueue_request(
                kind="store_write", account="X51c",
                ttl_seconds=True,  # bool, not number
                correlation_id="w51c", db=scen_51,
            )
        except ValueError:
            raised_51c = True
        check("Req 51c: WRITE with bool ttl_seconds -> ValueError",
              raised_51c)
        # Req 51d: enqueue with ttl_seconds < MIN_WRITE_TTL_SECONDS -> ValueError
        raised_51d = False
        try:
            RQ.enqueue_request(
                kind="store_write", account="X51d",
                ttl_seconds=0.5,
                correlation_id="w51d", db=scen_51,
            )
        except ValueError:
            raised_51d = True
        check("Req 51d: WRITE with ttl_seconds < MIN_WRITE_TTL -> ValueError",
              raised_51d)

        # Req 52: enqueue READ kinds WITHOUT ttl_seconds -> still OK
        #         (addendum #2 specifically scopes the requirement to WRITES)
        scen_52 = str(Path(tmp) / "scen_52.sqlite3")
        S.init_db(db=scen_52)
        for rk in RQ.READ_KINDS:
            try:
                cid52 = RQ.enqueue_request(
                    kind=rk, correlation_id="r52_" + rk, db=scen_52,
                )
                check("Req 52a: READ kind '" + rk
                      + "' without ttl_seconds -> OK", cid52 == "r52_" + rk)
                rec52 = RQ.get_request(cid52, db=scen_52)
                check("Req 52b: READ kind '" + rk
                      + "' row has NULL ttl_deadline_ts",
                      rec52 is not None and rec52.ttl_deadline_ts is None)
            except ValueError:
                check("Req 52a: READ kind '" + rk
                      + "' without ttl_seconds -> OK (raised unexpectedly)",
                      False)

        # Req 53: compute_backoff helper (addendum #5).
        #         Exponential with cap; no implicit retry-storm.
        b0 = RQ.compute_backoff(attempts=0)
        b1 = RQ.compute_backoff(attempts=1)
        b2 = RQ.compute_backoff(attempts=2)
        b3 = RQ.compute_backoff(attempts=3)
        b_max = RQ.compute_backoff(attempts=100)  # well past cap
        check("Req 53a: compute_backoff(0) >= base",
              b0 >= RQ.DEFAULT_BACKOFF_BASE_SECONDS - 0.001)
        check("Req 53b: compute_backoff monotone increasing in attempts",
              b0 <= b1 <= b2 <= b3)
        check("Req 53c: compute_backoff(100) capped at DEFAULT_BACKOFF_MAX",
              abs(b_max - RQ.DEFAULT_BACKOFF_MAX_SECONDS) < 0.001)
        # Req 53d: compute_backoff with bool -> ValueError
        raised_53d = False
        try:
            RQ.compute_backoff(attempts=True)
        except ValueError:
            raised_53d = True
        check("Req 53d: compute_backoff(attempts=True) -> ValueError",
              raised_53d)
        # Req 53e: compute_backoff with negative -> ValueError
        raised_53e = False
        try:
            RQ.compute_backoff(attempts=-1)
        except ValueError:
            raised_53e = True
        check("Req 53e: compute_backoff(attempts=-1) -> ValueError",
              raised_53e)
        # Req 53f: no implicit retry storm -- mark_failed_or_dead with
        #         next_attempt_ts=None leaves row claimable, but only
        #         because the caller EXPLICITLY chose immediate retry.
        scen_53f = str(Path(tmp) / "scen_53f.sqlite3")
        S.init_db(db=scen_53f)
        RQ.enqueue_request(kind="store_write", account="BackoffAcct",
                           ttl_seconds=600.0,
                           correlation_id="bo_1", db=scen_53f,
                           now_fn=lambda: 1700000000.0)
        RQ.claim_next_request(worker_id="wkr_bo", db=scen_53f,
                               now_fn=lambda: 1700000010.0)
        # Caller computes backoff EXPLICITLY -- no implicit "retry now".
        bo_next = 1700000010.0 + RQ.compute_backoff(attempts=1)
        RQ.mark_failed_or_dead(
            "bo_1", error_class="TransientFailure",
            next_attempt_ts=bo_next, max_attempts=10, db=scen_53f,
            now_fn=lambda: 1700000010.0,
        )
        rec_bo = RQ.get_request("bo_1", db=scen_53f)
        check("Req 53f: caller-computed next_attempt_ts honoured "
              "(no implicit retry storm)",
              rec_bo is not None and rec_bo.next_attempt_ts is not None
              and abs(rec_bo.next_attempt_ts - bo_next) < 0.001)
        # Re-claim BEFORE next_attempt_ts -> not claimable
        c_bo_early = RQ.claim_next_request(
            worker_id="wkr_bo2", db=scen_53f,
            now_fn=lambda: bo_next - 1.0,
        )
        check("Req 53g: claim BEFORE next_attempt_ts -> None "
              "(backoff respected; no storm)", c_bo_early is None)

        # Req 54: check_write_ttl_over_lease (addendum #4).
        # ttl >= ratio * lease -> OK; ttl < ratio * lease -> ValueError.
        try:
            RQ.check_write_ttl_over_lease(
                ttl_seconds=120.0, lease_seconds=30.0,
            )
            check("Req 54a: ttl 120 >= 2*lease 30 -> OK", True)
        except ValueError:
            check("Req 54a: ttl 120 >= 2*lease 30 -> OK "
                  "(raised unexpectedly)", False)
        raised_54b = False
        try:
            RQ.check_write_ttl_over_lease(
                ttl_seconds=30.0, lease_seconds=30.0,
            )
        except ValueError:
            raised_54b = True
        check("Req 54b: ttl 30 < 2*lease 30 -> ValueError",
              raised_54b)
        raised_54c = False
        try:
            RQ.check_write_ttl_over_lease(
                ttl_seconds=120.0, lease_seconds=True,
            )
        except ValueError:
            raised_54c = True
        check("Req 54c: bool lease_seconds -> ValueError", raised_54c)
        raised_54d = False
        try:
            RQ.check_write_ttl_over_lease(
                ttl_seconds=0.0, lease_seconds=30.0,
            )
        except ValueError:
            raised_54d = True
        check("Req 54d: zero ttl_seconds -> ValueError", raised_54d)

        # Req 55: lease-vs-TTL race scenario.
        # If TTL > lease, a crashed WRITE claim should be recovered to
        # 'failed' BEFORE the next expire sweep would send it to 'dead'.
        scen_55 = str(Path(tmp) / "scen_55.sqlite3")
        S.init_db(db=scen_55)
        # ttl_seconds = 120 (deadline at 1700000000 + 120 = 1700000120)
        # lease_seconds = 30 (claim from 1700000010 stale at 1700000040)
        # Sweep order: recover at t=1700000050 -> claim back to 'failed';
        # then expire at t=1700000125 (past TTL) -- but row is now 'failed'
        # not 'claimed', and Req 35 invariant holds (failed past TTL ->
        # dead for WRITE).  The KEY assertion: the row didn't bypass
        # 'failed' on its way to 'dead' (no silent loss).
        RQ.enqueue_request(kind="store_write", account="RaceAcct",
                           ttl_seconds=120.0,
                           correlation_id="race_1", db=scen_55,
                           now_fn=lambda: 1700000000.0)
        RQ.claim_next_request(worker_id="wkr_race", db=scen_55,
                               now_fn=lambda: 1700000010.0)
        rec55a = RQ.get_request("race_1", db=scen_55)
        check("Req 55a: WRITE claimed (in-flight)",
              rec55a is not None and rec55a.status == "claimed")
        # Crash. Recovery runs at t=1700000050 (lease 30s elapsed).
        n_rec = RQ.recover_stale_claims(
            lease_seconds=30.0, db=scen_55,
            now_fn=lambda: 1700000050.0,
        )
        check("Req 55b: recover_stale_claims recovered 1 row "
              "(BEFORE TTL would expire it)", n_rec == 1)
        rec55b = RQ.get_request("race_1", db=scen_55)
        check("Req 55c: recovered row is now 'failed' (retry-eligible)",
              rec55b is not None and rec55b.status == "failed")
        # Now (counterfactual): if we had NOT recovered, the next claim
        # before TTL would still get it back -- and *that* is what
        # protects against the storm.  Re-claim and let it succeed.
        c55_redo = RQ.claim_next_request(
            worker_id="wkr_race2", db=scen_55,
            now_fn=lambda: 1700000060.0,
        )
        check("Req 55d: recovered row re-claimable",
              c55_redo is not None and c55_redo.correlation_id == "race_1")
        RQ.mark_done("race_1", result={"ok": True}, db=scen_55,
                     now_fn=lambda: 1700000070.0)
        rec55c = RQ.get_request("race_1", db=scen_55)
        check("Req 55e: recovered WRITE row landed as 'done' "
              "(no silent loss to 'dead')",
              rec55c is not None and rec55c.status == "done")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(str(f) for f in failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
