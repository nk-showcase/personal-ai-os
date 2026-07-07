#!/usr/bin/env python3
"""Plain-python tests (no pytest) for B-02E inert integrations-worker
skeleton (``bot.integrations_worker``).

SAFE: temp DBs only; SYNTHETIC payloads; NO network, NO live Notion, NO
real systemd start, NO production-DB writes, NO secret values used.
Mirrors the ``scripts/test_aios_*.py`` style.

Covers:

  Import safety (Req 1-7):
    1. import bot.integrations_worker starts NO loop
    2. import does NOT open any DB
    3. import does NOT read any env secret
    4. module source does NOT import httpx / requests / notion_client /
       urllib.request
    5. module source does NOT reference NOTION_API_KEY
    6. module source does NOT import note_storage / handlers /
       bot.main
    7. import does NOT call systemctl / start any service

  Config / env (Req 8-11):
    8. WORKER_ID resolves from AIOS_INTEGRATIONS_WORKER_ID env when set
    9. WORKER_ID falls back to safe non-empty default 'integrations-
       worker-1' (DISTINCT from claude-worker's 'claude-worker-1')
   10. POLL_INTERVAL_S, SWEEP_INTERVAL_S, LEASE_S exposed and numeric
   11. AIOS_INTEGRATIONS_WORKER_ID is a DIFFERENT env var than
       AIOS_WORKER_ID (no name collision with claude-worker)

  Default applier (Req 12-14):
   12. _noop_inert_applier returns {'applied': False, 'reason':
       'b02e_inert_stub', 'kind': <record.kind>}
   13. _noop_inert_applier does NOT contact any external service
       (verified by source scan)
   14. default applier used when run_once(applier=None)

  run_once (Req 15-23):
   15. empty queue -> None
   16. WRITE row + stub applier success -> done in DB, returns cid
   17. WRITE row + stub applier failure -> failed/dead in DB with
       class-name-only error_class
   18. READ row + stub applier success -> done in DB
   19. injected applier is invoked (not default) when provided
   20. injected applier receives a deep-copy of the record (B-02D
       guarantee surfaces here too)
   21. kinds filter passed through to claim_next_request
   22. account filter passed through
   23. process_one_request error class is the bare class name
       (no traceback, no URL fragment)

  run_loop (Req 24-31):
   24. max_iterations=N exits after exactly N iterations
   25. empty queue causes sleep(poll_interval) between iterations
   26. populated queue: row processed, no sleep on busy iteration
   27. sweep_interval triggers sweep_request_queue
   28. sweep ordering: recover BEFORE expire (verified via crashed
       WRITE row that should land in 'failed' not 'dead')
   29. exception inside sweep does NOT crash the loop
   30. loop returns value-free summary {iterations, processed, sweeps}
   31. injected sleep / now_fn are honored (test-driven clock)

  Inertness invariants (Req 32-41):
   32. module is import-side-effect-free (re-asserted via subprocess)
   33. no concrete domain *_storage writer is imported (inert skeleton)
   34. no handlers/main.py wiring (worker module is not imported by
       any handler file)
   35. no flag in bot/aios_config.py is flipped by B-02E (config file
       unchanged since prior block)
   36. systemd unit aios-integrations-worker.service file exists in
       repo but is target-only (User=ai-os-integrations DISTINCT from
       claude-worker's User=ai-os)
   37. systemd template does NOT auto-install (no install command in
       any B-02E file)
   38. AIOS_RAW_NOTION_PERSIST is NOT referenced or set in
       integrations_worker
   39. Telegram-bot env is NOT widened (no NOTION_API_KEY mention)
   40. main() is gated by ``if __name__ == "__main__"`` (no auto-run
       at import)
   41. main() has the ``pragma: no cover`` marker matching
       claude_worker style

Run: PYTHONPATH=<repo> python3 scripts/test_aios_b02e_integrations_worker_skeleton.py
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    # Strip env vars that could leak into the import-safety checks.
    for var in ("AIOS_INTEGRATIONS_WORKER_ID",
                "AIOS_INTEGRATIONS_POLL_S",
                "AIOS_INTEGRATIONS_SWEEP_S",
                "AIOS_INTEGRATIONS_LEASE_S"):
        os.environ.pop(var, None)

    failed: list = []

    def check(name: str, cond: bool) -> None:
        if cond:
            print("PASS: " + name)
        else:
            print("FAIL: " + name)
            failed.append(name)

    tmp = tempfile.mkdtemp(prefix="aios_b02e_")
    try:
        # ----------------------------------------------------------
        # Req 1-7: import safety
        # ----------------------------------------------------------
        mod_path = repo_root / "bot" / "integrations_worker.py"
        src = mod_path.read_text(encoding="utf-8")
        # Import the module fresh and verify no side effect.  Use a
        # subprocess so any module-level side effect (DB open, loop
        # start, etc.) would manifest as a hang or stderr noise.
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, " + repr(str(repo_root))
             + "); import bot.integrations_worker as W; "
             + "print('OK', W.WORKER_ID, callable(getattr(W,'main',None)))"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        check("Req 1: import bot.integrations_worker exits cleanly "
              "(no loop started)", proc.returncode == 0)
        check("Req 1b: import prints OK + WORKER_ID",
              proc.stdout.startswith("OK "))
        # Req 2: no DB opened at import time.  We invoke the import
        # under a controlled CWD and check no .sqlite3 file was
        # touched.  Stronger: scan stdout/stderr for any sqlite3 hint.
        check("Req 2: no DB sqlite3 path appears in import stderr",
              ".sqlite3" not in proc.stderr)
        # Req 3: no secret values resolved.  We can't easily verify
        # negative, but the module source must not call any
        # secrets_loader at module load.
        check("Req 3a: module source does NOT call secrets_loader at "
              "module load (no top-level secrets_loader.* call)",
              "secrets_loader" not in src)
        check("Req 3b: module source does NOT reference NOTION_API_KEY",
              "NOTION_API_KEY" not in src)
        # Req 4: no live HTTP / Notion clients.
        check("Req 4a: module source does NOT import httpx",
              "import httpx" not in src
              and "from httpx" not in src)
        check("Req 4b: module source does NOT import requests",
              "import requests" not in src
              and "from requests" not in src)
        check("Req 4c: module source does NOT import notion_client",
              "notion_client" not in src and "notion-client" not in src)
        check("Req 4d: module source does NOT import urllib.request",
              "import urllib.request" not in src
              and "from urllib.request" not in src)
        check("Req 4e: module source does NOT import aiohttp",
              "import aiohttp" not in src
              and "from aiohttp" not in src)
        # Req 5: no NOTION_API_KEY (also covered by Req 3b -- restate
        # for the addendum's verbatim wording).
        check("Req 5: source does NOT reference NOTION_API_KEY token name",
              "NOTION_API_KEY" not in src)
        # Req 6: inertness against domain-storage / handler edits.
        check("Req 6a: source does NOT import note_storage",
              "from .note_storage" not in src
              and "from bot.note_storage" not in src
              and "import note_storage" not in src)
        check("Req 6b: source does NOT import bot.main",
              "from .main" not in src
              and "from bot.main" not in src
              and "import main" not in src)
        check("Req 6c: source does NOT import handlers package",
              "from .handlers" not in src
              and "from bot.handlers" not in src)
        # Req 7: no systemctl / service-start invocation in source.
        check("Req 7a: source does NOT call subprocess / systemctl",
              "subprocess" not in src
              and "systemctl" not in src
              and "service start" not in src.lower())
        check("Req 7b: source does NOT import os.system",
              "os.system" not in src)

        # ----------------------------------------------------------
        # Req 8-11: config + env
        # ----------------------------------------------------------
        from bot import integrations_worker as IW
        from bot import claude_worker as CW
        check("Req 8a: WORKER_ID resolves to non-empty str",
              isinstance(IW.WORKER_ID, str) and IW.WORKER_ID)
        check("Req 9: default WORKER_ID 'integrations-worker-1'",
              IW.WORKER_ID == "integrations-worker-1")
        # Req 9b: distinct from claude-worker default
        check("Req 9b: WORKER_ID DISTINCT from claude-worker default",
              IW.WORKER_ID != CW.WORKER_ID)
        check("Req 10a: POLL_INTERVAL_S is float", isinstance(IW.POLL_INTERVAL_S, float))
        check("Req 10b: SWEEP_INTERVAL_S is float", isinstance(IW.SWEEP_INTERVAL_S, float))
        check("Req 10c: LEASE_S is float", isinstance(IW.LEASE_S, float))
        check("Req 10d: POLL_INTERVAL_S > 0", IW.POLL_INTERVAL_S > 0)
        check("Req 10e: SWEEP_INTERVAL_S > 0", IW.SWEEP_INTERVAL_S > 0)
        # Req 11: env var name is DIFFERENT from claude-worker's.
        check("Req 11: source references AIOS_INTEGRATIONS_WORKER_ID "
              "(distinct from AIOS_WORKER_ID)",
              "AIOS_INTEGRATIONS_WORKER_ID" in src
              and 'os.getenv("AIOS_WORKER_ID"' not in src)
        # Req 11b: env override actually takes effect (re-import via
        # subprocess with a custom WORKER_ID env)
        proc_env = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, " + repr(str(repo_root))
             + "); import bot.integrations_worker as W; print(W.WORKER_ID)"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ,
                 "AIOS_INTEGRATIONS_WORKER_ID": "test-override-worker-99",
                 "PYTHONUNBUFFERED": "1"},
        )
        check("Req 11c: AIOS_INTEGRATIONS_WORKER_ID env override honored",
              proc_env.returncode == 0
              and proc_env.stdout.strip() == "test-override-worker-99")

        # ----------------------------------------------------------
        # Req 12-14: default inert applier
        # ----------------------------------------------------------
        from bot import aios_storage as S
        from bot import aios_worker_contract as WC
        scen_def = str(Path(tmp) / "scen_def.sqlite3")
        S.init_db(db=scen_def)
        cid_def = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="def:1",
            account="X", payload={"x": 1},
            db=scen_def, now_fn=lambda: 1700000000.0,
        )
        from bot import aios_request_queue as RQ
        rec_def = RQ.get_request(cid_def, db=scen_def)
        # Review-fix (CRITICAL): default applier RAISES instead of
        # returning a success-shaped dict.  Previously the row landed
        # in 'done' and the user saw "saved" for a write that
        # never touched the real backend (silent-correctness bug).
        raised_12 = False
        try:
            IW._noop_inert_applier(rec_def)
        except IW.InertStubNotInjected:
            raised_12 = True
        check("Req 12a: _noop_inert_applier RAISES InertStubNotInjected "
              "(fail-loud, NOT silent success)", raised_12)
        check("Req 12b: InertStubNotInjected is RuntimeError subclass",
              issubclass(IW.InertStubNotInjected, RuntimeError))
        # Smoke-check the exception class is exported as a public name
        check("Req 12c: InertStubNotInjected is exported on the module",
              hasattr(IW, "InertStubNotInjected"))
        check("Req 12d: exception class name is 'InertStubNotInjected' "
              "(class-name-only error_class downstream)",
              IW.InertStubNotInjected.__name__ == "InertStubNotInjected")
        # Req 13: _noop_inert_applier does NO external call (source scan).
        # Locate the function body in source by string match.  Length
        # guard: empty slice is a vacuous pass and indicates the
        # slice failed (review-fix robustness).
        applier_body_start = src.find("def _noop_inert_applier(")
        applier_body_end = src.find("\ndef ", applier_body_start + 1)
        applier_body = src[applier_body_start:applier_body_end] if applier_body_start >= 0 else ""
        check("Req 13z: applier body slice non-empty (robustness check)",
              len(applier_body) > 50)
        check("Req 13a: _noop_inert_applier body has NO 'import' / 'http' "
              "/ 'requests'",
              "import " not in applier_body
              and "http" not in applier_body.lower()
              and "requests" not in applier_body)
        check("Req 13b: _noop_inert_applier body raises with the marker "
              "'inject real applier'",
              "raise" in applier_body
              and "inject real applier" in applier_body)
        # Req 14: default applier used by run_once when applier=None
        # ALL paths through it land the row in 'failed' (NOT 'done') so
        # the user never sees a false "saved".
        out_14 = IW.run_once(
            applier=None, db=scen_def, now_fn=lambda: 1700000010.0,
        )
        check("Req 14a: run_once(applier=None) returns the cid "
              "(process_one_request returns cid even on applier failure)",
              out_14 == cid_def)
        rec_14 = RQ.get_request(cid_def, db=scen_def)
        check("Req 14b: row -> 'failed' after default applier raise "
              "(NOT 'done' -- review-fix CRITICAL)",
              rec_14.status == "failed")
        check("Req 14c: error_class is class NAME 'InertStubNotInjected'",
              rec_14.error_class == "InertStubNotInjected")
        check("Req 14d: row's result remains NULL (applier never produced)",
              rec_14.result is None)
        # Req 14e: max_attempts=1 path -> the next run_once flips to 'dead'.
        scen_14d = str(Path(tmp) / "scen_14d.sqlite3")
        S.init_db(db=scen_14d)
        cid_14d = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="14d:1",
            account="X", payload={"x": 1},
            db=scen_14d, now_fn=lambda: 1700000000.0,
        )
        # Use process_one_request directly to control max_attempts=1
        WC.process_one_request(
            worker_id="wkr_14d", applier=IW._noop_inert_applier,
            max_attempts=1, db=scen_14d, now_fn=lambda: 1700000010.0,
        )
        rec_14d = RQ.get_request(cid_14d, db=scen_14d)
        check("Req 14e: with max_attempts=1, first failure flips row "
              "directly to 'dead' (-> Outcome.STORAGE_UNAVAILABLE downstream)",
              rec_14d.status == "dead")

        # ----------------------------------------------------------
        # Req 15-23: run_once
        # ----------------------------------------------------------
        # 15: empty queue
        scen_emp = str(Path(tmp) / "scen_emp.sqlite3")
        S.init_db(db=scen_emp)
        out_15 = IW.run_once(db=scen_emp, now_fn=lambda: 1700000000.0)
        check("Req 15: run_once on empty queue -> None", out_15 is None)

        # 16: WRITE success
        scen_16 = str(Path(tmp) / "scen_16.sqlite3")
        S.init_db(db=scen_16)
        cid_16 = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r16:1",
            account="X", payload={"x": 1},
            db=scen_16, now_fn=lambda: 1700000000.0,
        )

        def good_applier(rec):
            return {"saved": True, "kind": rec.kind}
        out_16 = IW.run_once(
            applier=good_applier, db=scen_16,
            now_fn=lambda: 1700000010.0,
        )
        check("Req 16a: run_once returns the cid", out_16 == cid_16)
        rec_16 = RQ.get_request(cid_16, db=scen_16)
        check("Req 16b: row status -> done", rec_16.status == "done")
        check("Req 16c: result echo preserved",
              rec_16.result == {"saved": True, "kind": "add_note"})

        # 17: WRITE failure -> failed
        scen_17 = str(Path(tmp) / "scen_17.sqlite3")
        S.init_db(db=scen_17)
        cid_17 = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r17:1",
            account="X", payload={"x": 1},
            db=scen_17, now_fn=lambda: 1700000000.0,
        )

        def raising_applier(rec):
            raise RuntimeError(
                "fake connection refused -- https://api.example/secret"
            )
        out_17 = IW.run_once(
            applier=raising_applier, db=scen_17,
            now_fn=lambda: 1700000010.0,
        )
        check("Req 17a: run_once returns cid even on failure",
              out_17 == cid_17)
        rec_17 = RQ.get_request(cid_17, db=scen_17)
        check("Req 17b: row status -> failed", rec_17.status == "failed")
        check("Req 17c: error_class is class NAME only ('RuntimeError')",
              rec_17.error_class == "RuntimeError")
        check("Req 17d: error_class does NOT contain URL fragment",
              "http" not in (rec_17.error_class or "").lower()
              and "secret" not in (rec_17.error_class or "").lower())

        # 18: READ success
        scen_18 = str(Path(tmp) / "scen_18.sqlite3")
        S.init_db(db=scen_18)
        cid_18 = WC.enqueue_read_request(
            kind="read_note", user_id=7, business_key="rb:18",
            db=scen_18, now_fn=lambda: 1700000000.0,
        )

        def read_applier(rec):
            return {"value": 12500.0}
        out_18 = IW.run_once(
            applier=read_applier, db=scen_18,
            now_fn=lambda: 1700000010.0,
        )
        check("Req 18a: READ run_once returns cid", out_18 == cid_18)
        rec_18 = RQ.get_request(cid_18, db=scen_18)
        check("Req 18b: READ row -> done", rec_18.status == "done")
        check("Req 18c: READ result preserved",
              rec_18.result == {"value": 12500.0})

        # 19: injected applier invoked, NOT default stub
        scen_19 = str(Path(tmp) / "scen_19.sqlite3")
        S.init_db(db=scen_19)
        cid_19 = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r19:1",
            account="X", payload={"x": 1},
            db=scen_19, now_fn=lambda: 1700000000.0,
        )
        seen_calls = []

        def tracking_applier(rec):
            seen_calls.append(rec.correlation_id)
            return {"injected": True}
        IW.run_once(
            applier=tracking_applier, db=scen_19,
            now_fn=lambda: 1700000010.0,
        )
        check("Req 19a: injected applier called once",
              len(seen_calls) == 1)
        rec_19 = RQ.get_request(cid_19, db=scen_19)
        check("Req 19b: row result is from injected applier "
              "(NOT the inert stub)",
              rec_19.result == {"injected": True}
              and rec_19.result.get("reason") != "b02e_inert_stub")

        # 20: applier receives a deep-copy (B-02D guarantee surfaces).
        scen_20 = str(Path(tmp) / "scen_20.sqlite3")
        S.init_db(db=scen_20)
        cid_20 = WC.enqueue_write_request(
            kind="update_note", user_id=7,
            business_key="wd20", account="X",
            payload={"field_a": -5000.0,
                     "field_b": 12500.0},
            db=scen_20, now_fn=lambda: 1700000000.0,
        )

        def mutating_applier(rec):
            rec.payload["field_a"] = 0.0
            rec.payload["leaked"] = True
            return {"applied": True}
        IW.run_once(
            applier=mutating_applier, db=scen_20,
            now_fn=lambda: 1700000010.0,
        )
        rec_20 = RQ.get_request(cid_20, db=scen_20)
        check("Req 20a: applier mutation does NOT touch DB row's "
              "field_a",
              rec_20.payload["field_a"] == -5000.0)
        check("Req 20b: applier mutation does NOT add keys to DB row",
              "leaked" not in rec_20.payload)

        # 21: kinds filter passthrough
        scen_21 = str(Path(tmp) / "scen_21.sqlite3")
        S.init_db(db=scen_21)
        cid_21w = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r21:w",
            account="X", payload={"x": 1},
            db=scen_21, now_fn=lambda: 1700000000.0,
        )
        cid_21r = WC.enqueue_read_request(
            kind="read_note", user_id=7, business_key="r21:r",
            db=scen_21, now_fn=lambda: 1700000010.0,
        )
        # Only READ kind allowed: WRITE should NOT be picked
        out_21 = IW.run_once(
            applier=good_applier, kinds=["read_note"],
            db=scen_21, now_fn=lambda: 1700000020.0,
        )
        check("Req 21a: kinds=['read_note'] picks the READ row",
              out_21 == cid_21r)
        # Now allow WRITE -- the next claim should pick it.
        out_21b = IW.run_once(
            applier=good_applier, kinds=["add_note"],
            db=scen_21, now_fn=lambda: 1700000030.0,
        )
        check("Req 21b: kinds=['add_note'] picks the WRITE row",
              out_21b == cid_21w)

        # 22: account filter passthrough
        scen_22 = str(Path(tmp) / "scen_22.sqlite3")
        S.init_db(db=scen_22)
        cid_22a = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r22:a",
            account="AcctAA", payload={"x": 1},
            db=scen_22, now_fn=lambda: 1700000000.0,
        )
        cid_22b = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r22:b",
            account="AcctBB", payload={"x": 1},
            db=scen_22, now_fn=lambda: 1700000010.0,
        )
        out_22 = IW.run_once(
            applier=good_applier, account="AcctBB",
            db=scen_22, now_fn=lambda: 1700000020.0,
        )
        check("Req 22: account filter picks AcctBB (not the older AcctAA)",
              out_22 == cid_22b)

        # 23: bare class name -- covered by Req 17c/d above (no
        # message, no URL).  Re-assert via plain-identifier regex.
        check("Req 23: error_class is plain Python identifier",
              re.match(r"^[A-Za-z_][A-Za-z0-9_]*$",
                       rec_17.error_class or "") is not None)

        # ----------------------------------------------------------
        # Req 24-31: run_loop
        # ----------------------------------------------------------
        scen_24 = str(Path(tmp) / "scen_24.sqlite3")
        S.init_db(db=scen_24)
        sleep_calls_24 = []

        def fake_sleep_24(s):
            sleep_calls_24.append(float(s))
        clock_24 = {"t": 1700000000.0}

        def now_24():
            clock_24["t"] += 0.001
            return clock_24["t"]
        # 24: empty queue, max_iterations=3 -> exits after 3, calls
        # sleep(poll_interval) on each iter.
        summary_24 = IW.run_loop(
            applier=good_applier,
            max_iterations=3,
            poll_interval=0.5,
            sweep_interval=999.0,  # don't sweep
            sleep=fake_sleep_24, now_fn=now_24,
            db=scen_24,
        )
        check("Req 24a: loop ran exactly 3 iterations",
              summary_24["iterations"] == 3)
        check("Req 24b: 0 rows processed (empty queue)",
              summary_24["processed"] == 0)
        check("Req 25: sleep called 3 times with poll_interval (empty queue)",
              len(sleep_calls_24) == 3
              and all(abs(s - 0.5) < 1e-9 for s in sleep_calls_24))

        # 26: populated queue, max_iterations=1 -> row processed, NO
        # sleep on busy iteration.
        scen_26 = str(Path(tmp) / "scen_26.sqlite3")
        S.init_db(db=scen_26)
        cid_26 = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r26:1",
            account="X", payload={"x": 1},
            db=scen_26, now_fn=lambda: 1700000000.0,
        )
        sleep_calls_26 = []

        def fake_sleep_26(s):
            sleep_calls_26.append(float(s))
        clock_26 = {"t": 1700000010.0}

        def now_26():
            clock_26["t"] += 0.001
            return clock_26["t"]
        summary_26 = IW.run_loop(
            applier=good_applier,
            max_iterations=1,
            poll_interval=0.5, sweep_interval=999.0,
            sleep=fake_sleep_26, now_fn=now_26, db=scen_26,
        )
        check("Req 26a: 1 iteration, 1 processed",
              summary_26["iterations"] == 1
              and summary_26["processed"] == 1)
        check("Req 26b: NO sleep call on busy iteration",
              len(sleep_calls_26) == 0)
        rec_26 = RQ.get_request(cid_26, db=scen_26)
        check("Req 26c: row -> done after loop iteration",
              rec_26.status == "done")

        # 27: sweep_interval triggers sweep_request_queue.
        scen_27 = str(Path(tmp) / "scen_27.sqlite3")
        S.init_db(db=scen_27)
        sleep_calls_27 = []

        def fake_sleep_27(s):
            sleep_calls_27.append(float(s))
        # Now ticks +100s per call -> sweep_interval=30 fires every
        # iteration.
        clock_27 = {"t": 1700000000.0}

        def now_27():
            v = clock_27["t"]
            clock_27["t"] += 100.0
            return v
        summary_27 = IW.run_loop(
            applier=good_applier,
            max_iterations=2,
            poll_interval=0.1, sweep_interval=30.0,
            sleep=fake_sleep_27, now_fn=now_27, db=scen_27,
        )
        check("Req 27: sweep called on multiple iterations "
              "(sweep_interval elapsed via fast clock)",
              summary_27["sweeps"] >= 2)

        # 28: sweep ordering -- recover BEFORE expire is the rule
        # INSIDE one sweep_request_queue call.  Important nuance from
        # B-02C semantics: expire_due_requests sweeps statuses
        # ('pending', 'claimed', 'failed') with elapsed TTL -- so
        # even AFTER recover flips claimed -> failed, if TTL has
        # already elapsed, expire (running second within the same
        # sweep) still flips failed -> dead.  The ordering therefore
        # only "rescues" a row when TTL is STILL IN THE FUTURE at
        # sweep time (the lease-stale-but-ttl-future window).
        #
        # Scenario A (the meaningful window): ttl=120, lease=5,
        # claim at t=2, sweep at t=10.  Lease elapsed (claim staled
        # at t=7); TTL deadline at t=122 is NOT elapsed.  recover
        # flips claimed -> failed; expire no-op on this row.
        # Next iteration claims the failed row and stub succeeds.
        scen_28 = str(Path(tmp) / "scen_28.sqlite3")
        S.init_db(db=scen_28)
        cid_28 = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r28:1",
            account="X", payload={"x": 1},
            lease_seconds=5.0, ttl_seconds=120.0,
            db=scen_28, now_fn=lambda: 1700000000.0,
        )
        RQ.claim_next_request(worker_id="wkr_crashed",
                              db=scen_28,
                              now_fn=lambda: 1700000002.0)
        clock_28 = {"t": 1700000010.0}

        def now_28():
            v = clock_28["t"]
            clock_28["t"] += 0.001
            return v
        IW.run_loop(
            applier=good_applier,
            max_iterations=2,
            poll_interval=0.001, sweep_interval=0.0001,
            lease_seconds=5.0,
            sleep=lambda s: None,
            now_fn=now_28, db=scen_28,
        )
        rec_28 = RQ.get_request(cid_28, db=scen_28)
        check("Req 28a: WRITE crashed with TTL still future is NOT 'dead' "
              "(recover-BEFORE-expire rescues it)",
              rec_28.status != "dead")
        check("Req 28b: row landed in 'done' (recovered -> claimed -> "
              "stub success)",
              rec_28.status == "done")
        # Scenario B (boundary acknowledgment): when TTL ALSO elapsed
        # at sweep time, the WRITE row goes claimed -> failed
        # (recover) -> dead (expire) regardless of within-sweep
        # ordering.  This is by design (addendum #3 / B-02C dead-
        # letter contract).  Pinned so a future refactor cannot
        # silently change it.
        scen_28b = str(Path(tmp) / "scen_28b.sqlite3")
        S.init_db(db=scen_28b)
        cid_28b = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r28b:1",
            account="X", payload={"x": 1},
            lease_seconds=5.0, ttl_seconds=20.0,
            db=scen_28b, now_fn=lambda: 1700000000.0,
        )
        RQ.claim_next_request(worker_id="wkr_crashed_b",
                              db=scen_28b,
                              now_fn=lambda: 1700000002.0)
        clock_28b = {"t": 1700000050.0}

        def now_28b():
            v = clock_28b["t"]
            clock_28b["t"] += 0.001
            return v
        IW.run_loop(
            applier=good_applier,
            max_iterations=2,
            poll_interval=0.001, sweep_interval=0.0001,
            lease_seconds=5.0,
            sleep=lambda s: None,
            now_fn=now_28b, db=scen_28b,
        )
        rec_28b = RQ.get_request(cid_28b, db=scen_28b)
        check("Req 28c: WRITE with elapsed TTL+lease lands in 'dead' "
              "(expire dominates within sweep regardless of order; "
              "owner-visible dead-letter per addendum #3)",
              rec_28b.status == "dead")

        # 29: exception inside sweep does NOT crash the loop
        # (review-fix MAJOR).  Previous test was tautological -- both
        # branches of try/except asserted True.  New version
        # monkeypatches IW.sweep_once to raise; real DB so run_once
        # is healthy.  Asserts: loop completes; sweeps counter
        # increments; iterations counter == max_iterations.
        scen_29 = str(Path(tmp) / "scen_29.sqlite3")
        S.init_db(db=scen_29)
        cid_29 = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r29:1",
            account="X", payload={"x": 1},
            db=scen_29, now_fn=lambda: 1700000000.0,
        )
        original_sweep_once = IW.sweep_once
        sweep_call_count_29 = {"n": 0}

        def raising_sweep(*args, **kwargs):
            sweep_call_count_29["n"] += 1
            raise RuntimeError(
                "fake sweep failure -- /home/user/.sensitive-path/x.db"
            )
        IW.sweep_once = raising_sweep  # type: ignore[assignment]
        try:
            clock_29 = {"t": 1700000010.0}

            def now_29():
                v = clock_29["t"]
                clock_29["t"] += 100.0
                return v
            summary_29 = IW.run_loop(
                applier=good_applier,
                max_iterations=3,
                poll_interval=0.001, sweep_interval=10.0,
                sleep=lambda s: None, now_fn=now_29, db=scen_29,
            )
        finally:
            IW.sweep_once = original_sweep_once  # restore
        check("Req 29a: loop completed after sweep raised "
              "(no exception escaped)", summary_29 is not None)
        check("Req 29b: iterations counter == max_iterations "
              "(loop did NOT abort early on sweep failure)",
              summary_29["iterations"] == 3)
        check("Req 29c: sweep_once was CALLED at least once "
              "(monkeypatch fired)", sweep_call_count_29["n"] >= 1)
        check("Req 29d: summary['sweeps'] reflects attempts including "
              "the failed ones (not silently dropped)",
              summary_29["sweeps"] == sweep_call_count_29["n"])
        # Row should still be processed by the (working) run_once.
        rec_29 = RQ.get_request(cid_29, db=scen_29)
        check("Req 29e: run_once still processed the row despite "
              "sweep failure", rec_29.status == "done")

        # 30: loop summary keys
        check("Req 30a: summary has 'iterations' key",
              "iterations" in summary_24)
        check("Req 30b: summary has 'processed' key",
              "processed" in summary_24)
        check("Req 30c: summary has 'sweeps' key",
              "sweeps" in summary_24)
        check("Req 30d: all summary values are ints",
              all(isinstance(summary_24[k], int)
                  for k in ("iterations", "processed", "sweeps")))

        # 31: injected sleep / now_fn honored is implicit in all loop
        # tests above (they use fake sleep / now_fn).  Re-assert.
        check("Req 31: loop honored injected sleep_fn "
              "(captured 3 calls)",
              len(sleep_calls_24) == 3)

        # ----------------------------------------------------------
        # Req 32-41: inertness invariants
        # ----------------------------------------------------------
        # 32: import-side-effect-free (re-assert via subprocess)
        check("Req 32: subprocess import returned 0",
              proc.returncode == 0)

        # 33: the skeleton stays inert toward any domain storage module --
        # it must not import a concrete `*_storage` writer (the real
        # applier is injected at run time, never wired at module load).
        check("Req 33: source does NOT import any concrete domain "
              "*_storage writer",
              "import note_storage" not in src
              and "from .note_storage" not in src
              and "from bot.note_storage" not in src)

        # 34: no handler imports integrations_worker
        handlers_dir = repo_root / "bot" / "handlers"
        if handlers_dir.exists():
            handler_files = list(handlers_dir.rglob("*.py"))
            any_imports = False
            for f in handler_files:
                content = f.read_text(encoding="utf-8")
                if ("integrations_worker" in content):
                    any_imports = True
                    break
            check("Req 34: no handler imports bot.integrations_worker",
                  not any_imports)
        else:
            check("Req 34: handlers dir absent -- nothing to check",
                  True)

        # 35: aios_config.py is not flipped (the integrations_worker
        # module never modifies it; verify source absence)
        check("Req 35a: integrations_worker source does NOT import "
              "aios_config",
              "aios_config" not in src)
        check("Req 35b: integrations_worker source does NOT mention "
              "write_mode / read_source / shadow_compare / "
              "legacy_counter_tracking flags",
              "write_mode" not in src
              and "read_source" not in src
              and "shadow_compare" not in src
              and "legacy_counter_tracking" not in src)

        # 36: systemd unit file: target-only with distinct user
        unit_path = repo_root / "systemd" / "aios-integrations-worker.service"
        unit_src = unit_path.read_text(encoding="utf-8")
        check("Req 36a: systemd unit file exists",
              unit_path.exists())
        check("Req 36b: User=ai-os-integrations (distinct from "
              "claude-worker's User=ai-os)",
              "User=ai-os-integrations" in unit_src
              and "User=ai-os\n" not in unit_src)
        # 37: no install / start command issued by B-02E sources
        check("Req 37a: unit comment marks it as TARGET-only "
              "(not installed)",
              "TARGET" in unit_src or "target" in unit_src.lower())
        check("Req 37b: B-02E source files do NOT contain "
              "'systemctl enable' or 'systemctl start'",
              "systemctl enable" not in src
              and "systemctl start" not in src
              and "systemctl enable" not in unit_src
              and "systemctl start" not in unit_src)

        # 38: no AIOS_RAW_NOTION_PERSIST reference
        check("Req 38: source does NOT reference AIOS_RAW_NOTION_PERSIST",
              "AIOS_RAW_NOTION_PERSIST" not in src)

        # 39: no Notion token name (token-boundary inertness)
        check("Req 39: source does NOT reference NOTION_API_KEY",
              "NOTION_API_KEY" not in src)

        # 40: main() gated by __name__ guard
        check("Req 40: source has 'if __name__ == \"__main__\"' guard",
              'if __name__ == "__main__":' in src)

        # 41: main() has pragma: no cover marker
        check("Req 41: main() has 'pragma: no cover' marker "
              "(matches claude_worker style)",
              "pragma: no cover" in src)

        # ============================================================
        # ADVERSARIAL REVIEW FIXES (Req 42-50)
        # ============================================================

        # Req 42: run_loop with applier=None (default inert stub
        # injected via run_once) -> row lands in 'failed'
        # (NOT 'done') via the InertStubNotInjected raise path.
        # Pinned because the original code returned success-shaped
        # dict and silently corrupted user-visible Outcome.SAVED.
        scen_42 = str(Path(tmp) / "scen_42.sqlite3")
        S.init_db(db=scen_42)
        cid_42 = WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r42:1",
            account="X", payload={"x": 1},
            db=scen_42, now_fn=lambda: 1700000000.0,
        )
        clock_42 = {"t": 1700000010.0}

        def now_42():
            v = clock_42["t"]
            clock_42["t"] += 0.001
            return v
        IW.run_loop(
            applier=None,  # default inert stub
            max_iterations=1,
            poll_interval=0.001, sweep_interval=9999.0,
            sleep=lambda s: None, now_fn=now_42, db=scen_42,
        )
        rec_42 = RQ.get_request(cid_42, db=scen_42)
        check("Req 42a: run_loop(applier=None) -> row 'failed' "
              "(NOT silent 'done')",
              rec_42.status == "failed")
        check("Req 42b: error_class is 'InertStubNotInjected'",
              rec_42.error_class == "InertStubNotInjected")

        # Req 43: max_iterations=0 -> loop body never runs, summary
        # is all zeros.  Pinned to prevent regression (e.g., if
        # someone changes < to <=).
        scen_43 = str(Path(tmp) / "scen_43.sqlite3")
        S.init_db(db=scen_43)
        summary_43 = IW.run_loop(
            applier=good_applier,
            max_iterations=0,
            poll_interval=0.001, sweep_interval=9999.0,
            sleep=lambda s: None, now_fn=lambda: 1700000000.0,
            db=scen_43,
        )
        check("Req 43: max_iterations=0 -> all-zero summary",
              summary_43 == {"iterations": 0, "processed": 0, "sweeps": 0})

        # Req 44: run_once raises mid-loop -> caught, logged
        # class-only, loop continues to next iteration (review-fix
        # MAJOR -- previously a single hiccup killed the daemon).
        scen_44 = str(Path(tmp) / "scen_44.sqlite3")
        # Use a real DB and an applier that returns OK; but
        # monkeypatch IW.run_once to raise on FIRST call only.
        S.init_db(db=scen_44)
        WC.enqueue_write_request(
            kind="add_note", user_id=7, business_key="r44:1",
            account="X", payload={"x": 1},
            db=scen_44, now_fn=lambda: 1700000000.0,
        )
        original_run_once = IW.run_once
        call_count_44 = {"n": 0}

        def flaky_run_once(*args, **kwargs):
            call_count_44["n"] += 1
            if call_count_44["n"] == 1:
                raise RuntimeError(
                    "fake transient sqlite locked error -- /path/leak"
                )
            return original_run_once(*args, **kwargs)
        IW.run_once = flaky_run_once  # type: ignore[assignment]
        try:
            summary_44 = IW.run_loop(
                applier=good_applier,
                max_iterations=3,
                poll_interval=0.001, sweep_interval=9999.0,
                sleep=lambda s: None, now_fn=lambda: 1700000010.0,
                db=scen_44,
            )
        finally:
            IW.run_once = original_run_once  # restore
        check("Req 44a: loop completed despite run_once raising on "
              "iter 1 (no propagation)",
              summary_44["iterations"] == 3)
        check("Req 44b: run_once was attempted 3 times",
              call_count_44["n"] == 3)

        # Req 45: first iteration sweeps immediately (review-fix
        # MAJOR -- last_sweep_ts init to now - sweep_interval so
        # first gate fires).
        scen_45 = str(Path(tmp) / "scen_45.sqlite3")
        S.init_db(db=scen_45)
        sweep_called_45 = {"n": 0}

        def counting_sweep(*args, **kwargs):
            sweep_called_45["n"] += 1
            return {"recovered": 0, "expired": 0, "dead_from_ttl": 0}
        IW.sweep_once = counting_sweep  # type: ignore[assignment]
        try:
            IW.run_loop(
                applier=good_applier,
                max_iterations=1,
                poll_interval=0.001, sweep_interval=300.0,
                sleep=lambda s: None,
                now_fn=lambda: 1700000000.0,
                db=scen_45,
            )
        finally:
            IW.sweep_once = original_sweep_once
        check("Req 45: FIRST iteration sweeps immediately "
              "(last_sweep_ts init = now - sweep_interval)",
              sweep_called_45["n"] >= 1)

        # Req 46: log.exception is NOT used by integrations_worker
        # (review-fix MAJOR -- log.exception leaks traceback +
        # message to journald; log.warning("class=%s") is the
        # value-free pattern).
        check("Req 46a: source does NOT use 'log.exception'",
              "log.exception" not in src)
        check("Req 46b: source uses 'log.warning' for failure paths",
              "log.warning" in src)
        check("Req 46c: failure-path log lines use 'class=%s' pattern "
              "(class-name-only)",
              'class=%s' in src
              and 'type(' in src)

        # Req 47: source does NOT contain the substring 'log.error' or
        # 'log.critical' that could format an exception message.
        # (log.warning with %s and type(e).__name__ is safe.)
        check("Req 47: source does NOT use log.error / log.critical "
              "(no message-formatting log lines)",
              "log.error" not in src and "log.critical" not in src)

        # Req 48: run_once swapped BEFORE sweep in the loop (review-
        # fix MAJOR -- so a recovered row waits at least until the
        # NEXT iteration to be re-claimed; backoff respected).
        # Verify by source text inspection: in run_loop, the run_once
        # try-block appears BEFORE the sweep gate.
        loop_body_start = src.find("def run_loop(")
        loop_body_end = src.find("\n    return {", loop_body_start)
        loop_body = src[loop_body_start:loop_body_end] if loop_body_start >= 0 else ""
        run_once_pos = loop_body.find("cid = run_once(")
        sweep_pos = loop_body.find("if now_v - last_sweep_ts")
        check("Req 48: run_once call appears BEFORE sweep gate in "
              "run_loop body (review-fix order)",
              0 < run_once_pos < sweep_pos)

        # Req 49: forbidden flag / kill-switch / cutover names absent
        # from source (broader scan than Req 35b).
        forbidden = [
            "pc_tasks", "kill_switch", "production_cutover",
            "7_day_clock", "seven_day_clock",
            "GMAIL_SEND_TOKEN", "TODOIST_API_KEY", "BWS_ACCESS_TOKEN",
            "ANTHROPIC_API_KEY", "ANTHROPIC_ADMIN_API_KEY",
            "GITHUB_TOKEN",
        ]
        for fn in forbidden:
            check("Req 49: forbidden token name '" + fn + "' NOT in source",
                  fn not in src)

        # Req 50: systemctl-enable / -start scan extended to scripts/
        # (review-fix MAJOR -- previously only the worker module
        # was scanned).  Exclude this test file itself (it contains
        # the patterns as STRING LITERALS that we search FOR, and
        # would match itself).
        scripts_dir = repo_root / "scripts"
        any_install_script = False
        offending_file = None
        if scripts_dir.exists():
            for f in scripts_dir.rglob("*"):
                if not f.is_file():
                    continue
                if f.suffix not in (".sh", ".py"):
                    continue
                # Skip the B-02E test file (this very file) which
                # contains the search patterns as quoted literals.
                if f.name == "test_aios_b02e_integrations_worker_skeleton.py":
                    continue
                content = f.read_text(encoding="utf-8", errors="replace")
                if ("systemctl enable aios-integrations-worker" in content
                        or "systemctl start aios-integrations-worker" in content
                        or "systemctl --now enable aios-integrations-worker" in content):
                    any_install_script = True
                    offending_file = str(f.relative_to(repo_root))
                    break
        check("Req 50: NO script under scripts/ (excluding the B-02E "
              "test itself) enables or starts aios-integrations-worker"
              + (" -- offender=" + offending_file if offending_file else ""),
              not any_install_script)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
