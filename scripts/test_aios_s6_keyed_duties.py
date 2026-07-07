#!/usr/bin/env python3
"""Keyed-worker duties tests: the duties wired into bot.integrations_worker
(_keyed_setup + _run_keyed_duties) -- the missing consumer keystone.

SAFE / OFFLINE: temp sqlite DBs only (AIOS_TASK_QUEUE_DB + a temp write-queue DB); SYNTHETIC
payloads; NO network, NO real notes/tasks/Telegram send, NO systemd, NO production DB, NO secret
values. The router-echo path is exercised with dispatch ON but an UNREGISTERED callback prefix
(falls to the trivial echo -> reply_queue), so the classifier / model key is never pulled. The
schedule tick is exercised with schedule_executor.run_due_jobs monkeypatched to a counter, so no
real reminder fires.

Covers:
  T1  _keyed_setup() returns (event_loop, route_runner).
  T2  dispatch OFF (AIOS_ROUTER_DISPATCH) -> _run_keyed_duties does NOT claim a queued alias='router'
      row (row stays 'queued'); zero echo (no reply_queue row). This is the inertness guarantee.
  T3  dispatch ON + UNREGISTERED callback prefix -> row consumed (status 'done') via the echo path;
      a 'routed:' reply is enqueued. Proves the consumer claims + runs router rows when armed.
  T5  schedule cadence: below the interval -> run_due_jobs NOT called + last_sched_ts unchanged; at/
      above the interval -> run_due_jobs called once + last_sched_ts advanced.
  T6  exception isolation: run_due_jobs raising (schedule) and process_one raising (router) are both
      swallowed class-name-only -- _run_keyed_duties never propagates and still advances the clock.
  T7  run_loop(max_iterations=N) (the test path) NEVER builds or drives the keyed duties
      (_keyed_setup / _run_keyed_duties call-counts stay 0) AND still processes the write queue --
      the write loop is byte-identical.
  T8  source wiring: run_loop builds _keyed_setup() under `if max_iterations is None` and drives
      _run_keyed_duties(...) under `if _keyed_loop is not None`.

Run (VPS venv, py3.10+):  PYTHONPATH=. .venv/bin/python scripts/test_aios_s6_keyed_duties.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    tmp = tempfile.mkdtemp(prefix="aios_keyed_")
    # All queue substrate (task_queue / schedule_queue / reply_queue / conv_state) shares this file.
    queue_db = str(Path(tmp) / "queue.sqlite3")
    os.environ["AIOS_TASK_QUEUE_DB"] = queue_db
    # Keep dispatch OFF at import; individual tests flip router_dispatch.ROUTER_DISPATCH directly.
    os.environ.pop("AIOS_ROUTER_DISPATCH", None)

    failed: list = []

    def check(name: str, cond: bool) -> None:
        if cond:
            print("PASS: " + name)
        else:
            print("FAIL: " + name)
            failed.append(name)

    CHAT = 424242

    from bot import integrations_worker as IW
    from bot import router_dispatch
    from bot import task_queue
    from bot import reply_queue
    from bot import schedule_executor
    from bot import claude_worker_core

    def _enqueue_router(envelope: dict) -> int:
        return task_queue.enqueue_task(
            chat_id=CHAT, alias="router", mode="inbound",
            task_text=json.dumps(envelope, ensure_ascii=False), source="telegram",
        )

    def _reply_count() -> int:
        # count ALL replies in the shared DB (test starts empty)
        with sqlite3.connect(queue_db) as c:
            try:
                return int(c.execute("SELECT COUNT(*) FROM replies").fetchone()[0])
            except sqlite3.OperationalError:
                return 0  # replies table not created yet => zero

    def _clear_router_rows() -> None:
        # claim_next_route_task claims the GLOBALLY-oldest queued router row; clear leftovers so each
        # router sub-test is isolated (else a prior test's queued row is claimed first).
        with sqlite3.connect(queue_db) as c:
            try:
                c.execute("DELETE FROM tasks WHERE alias='router'")
            except sqlite3.OperationalError:
                pass

    # -----------------------------------------------------------------------------
    # T1: _keyed_setup returns handles
    # -----------------------------------------------------------------------------
    loop, runner = IW._keyed_setup()
    check("T1a: _keyed_setup returns an asyncio event loop",
          isinstance(loop, asyncio.AbstractEventLoop))
    check("T1b: _keyed_setup returns a callable route runner", callable(runner))

    # -----------------------------------------------------------------------------
    # T2: dispatch OFF -> router row NOT consumed (inert), no echo
    # -----------------------------------------------------------------------------
    router_dispatch.ROUTER_DISPATCH = False
    tid2 = _enqueue_router({"kind": "text", "chat_id": CHAT, "text": "hello"})
    replies_before = _reply_count()
    # last_sched_ts == now_v so the schedule gate does NOT fire -> isolates the router step
    IW._run_keyed_duties(loop, runner, now_v=1000.0, last_sched_ts=1000.0)
    row2 = task_queue.get_task(tid2)
    check("T2a: dispatch OFF -> router row stays 'queued' (NOT claimed)",
          row2 is not None and row2["status"] == "queued")
    check("T2b: dispatch OFF -> zero echo (no new reply_queue row)",
          _reply_count() == replies_before)

    # -----------------------------------------------------------------------------
    # T3: dispatch ON + unregistered callback prefix -> consumed via echo
    # -----------------------------------------------------------------------------
    _clear_router_rows()  # drop T2's still-queued row so T3 claims ITS row
    router_dispatch.ROUTER_DISPATCH = True
    try:
        env3 = {"kind": "callback", "chat_id": CHAT, "message_id": 5,
                "callback_data": "totally_unregistered_prefix_zzz"}
        tid3 = _enqueue_router(env3)
        replies_before3 = _reply_count()
        IW._run_keyed_duties(loop, runner, now_v=2000.0, last_sched_ts=2000.0)
        row3 = task_queue.get_task(tid3)
        check("T3a: dispatch ON -> unregistered-prefix router row consumed (status 'done')",
              row3 is not None and row3["status"] == "done")
        check("T3b: dispatch ON -> echo path enqueued exactly one reply",
              _reply_count() == replies_before3 + 1)
    finally:
        router_dispatch.ROUTER_DISPATCH = False

    # -----------------------------------------------------------------------------
    # T5: schedule cadence gate (run_due_jobs monkeypatched to a counter)
    # -----------------------------------------------------------------------------
    router_dispatch.ROUTER_DISPATCH = False  # isolate the schedule step
    sched_calls = {"n": 0}

    async def _fake_run_due_jobs(handlers, *, next_due_fn=None, now=None):
        sched_calls["n"] += 1
        return 0

    _orig_run_due = schedule_executor.run_due_jobs
    schedule_executor.run_due_jobs = _fake_run_due_jobs
    try:
        # below interval: last_sched_ts close to now_v -> gate NOT met
        ret_below = IW._run_keyed_duties(loop, runner, now_v=5000.0,
                                         last_sched_ts=5000.0 - (IW.SCHEDULE_TICK_INTERVAL_S - 1))
        check("T5a: below cadence -> run_due_jobs NOT called", sched_calls["n"] == 0)
        check("T5b: below cadence -> last_sched_ts returned unchanged",
              ret_below == 5000.0 - (IW.SCHEDULE_TICK_INTERVAL_S - 1))
        # at/above interval: gate met -> fire once, advance clock
        ret_at = IW._run_keyed_duties(loop, runner, now_v=6000.0,
                                      last_sched_ts=6000.0 - IW.SCHEDULE_TICK_INTERVAL_S)
        check("T5c: at cadence -> run_due_jobs called exactly once", sched_calls["n"] == 1)
        check("T5d: at cadence -> last_sched_ts advanced to now_v", ret_at == 6000.0)
    finally:
        schedule_executor.run_due_jobs = _orig_run_due

    # -----------------------------------------------------------------------------
    # T6: exception isolation (schedule raises; router raises) -> swallowed, clock advances
    # -----------------------------------------------------------------------------
    router_dispatch.ROUTER_DISPATCH = False
    async def _boom_run_due(handlers, *, next_due_fn=None, now=None):
        raise RuntimeError("fake schedule failure -- /home/user/.secret/x.db")
    schedule_executor.run_due_jobs = _boom_run_due
    try:
        ret6 = IW._run_keyed_duties(loop, runner, now_v=7000.0,
                                    last_sched_ts=7000.0 - IW.SCHEDULE_TICK_INTERVAL_S)
        check("T6a: schedule run_due_jobs raising is swallowed (no propagation)", True)
        check("T6b: last_sched_ts still advanced after schedule failure", ret6 == 7000.0)
    except Exception:  # noqa: BLE001
        check("T6a: schedule run_due_jobs raising is swallowed (no propagation)", False)
    finally:
        schedule_executor.run_due_jobs = _orig_run_due

    router_dispatch.ROUTER_DISPATCH = True
    async def _boom_process_one(worker_id, runner_fn, *, claim_fn=None):
        raise RuntimeError("fake router failure -- FAKE_PLACEHOLDER_TOKEN")
    _orig_process_one = claude_worker_core.process_one
    claude_worker_core.process_one = _boom_process_one
    try:
        # gate the schedule OFF (last_sched_ts == now_v) so only the router step runs
        IW._run_keyed_duties(loop, runner, now_v=8000.0, last_sched_ts=8000.0)
        check("T6c: router process_one raising is swallowed (no propagation)", True)
    except Exception:  # noqa: BLE001
        check("T6c: router process_one raising is swallowed (no propagation)", False)
    finally:
        claude_worker_core.process_one = _orig_process_one
        router_dispatch.ROUTER_DISPATCH = False

    # -----------------------------------------------------------------------------
    # T7: finite-max_iterations run_loop NEVER builds/drives keyed duties; write loop intact
    # -----------------------------------------------------------------------------
    from bot import aios_storage as S
    from bot import aios_worker_contract as WC
    from bot import aios_request_queue as RQ
    write_db = str(Path(tmp) / "write.sqlite3")
    S.init_db(db=write_db)
    cid7 = WC.enqueue_write_request(
        kind="store_write", user_id=7, business_key="t7:1", account="X", payload={"x": 1},
        db=write_db, now_fn=lambda: 1700000000.0,
    )

    setup_calls = {"n": 0}
    duty_calls = {"n": 0}
    _orig_setup = IW._keyed_setup
    _orig_duties = IW._run_keyed_duties
    IW._keyed_setup = lambda: (setup_calls.__setitem__("n", setup_calls["n"] + 1), (None, None))[1]
    def _count_duties(*a, **k):
        duty_calls["n"] += 1
        return k.get("last_sched_ts")
    IW._run_keyed_duties = _count_duties

    def _good_applier(rec):
        return {"saved": True}
    clock7 = {"t": 1700000010.0}
    def _now7():
        clock7["t"] += 0.001
        return clock7["t"]
    try:
        summary7 = IW.run_loop(
            applier=_good_applier, max_iterations=2,
            poll_interval=0.001, sweep_interval=9e9,
            sleep=lambda s: None, now_fn=_now7, db=write_db,
        )
    finally:
        IW._keyed_setup = _orig_setup
        IW._run_keyed_duties = _orig_duties
    check("T7a: finite run_loop did NOT call _keyed_setup (real-mode-only build)",
          setup_calls["n"] == 0)
    check("T7b: finite run_loop did NOT call _run_keyed_duties (real-mode-only drive)",
          duty_calls["n"] == 0)
    check("T7c: finite run_loop ran exactly 2 iterations", summary7["iterations"] == 2)
    rec7 = RQ.get_request(cid7, db=write_db)
    check("T7d: write-queue row still processed to 'done' (write loop intact)",
          rec7.status == "done")

    # -----------------------------------------------------------------------------
    # T8: source wiring -- build under real-mode gate, drive under _keyed_loop gate
    # -----------------------------------------------------------------------------
    src = (repo_root / "bot" / "integrations_worker.py").read_text(encoding="utf-8")
    loop_start = src.find("def run_loop(")
    loop_end = src.find("\n    return {", loop_start)
    loop_body = src[loop_start:loop_end] if loop_start >= 0 else ""
    setup_pos = loop_body.find("_keyed_setup()")
    realmode_gate = loop_body.rfind("if max_iterations is None:", 0, setup_pos)
    drive_pos = loop_body.find("_run_keyed_duties(")
    loopnone_gate = loop_body.rfind("if _keyed_loop is not None:", 0, drive_pos)
    check("T8a: run_loop calls _keyed_setup()", setup_pos > 0)
    check("T8b: _keyed_setup() build is guarded by 'if max_iterations is None:'",
          0 < realmode_gate < setup_pos)
    check("T8c: run_loop drives _run_keyed_duties(...)", drive_pos > 0)
    check("T8d: _run_keyed_duties(...) is guarded by 'if _keyed_loop is not None:'",
          0 < loopnone_gate < drive_pos)

    # cleanup
    try:
        loop.close()
    except Exception:  # noqa: BLE001
        pass

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
