"""bot/integrations_worker.py -- integrations-worker skeleton.

Runnable module: ``python -m bot.integrations_worker``. Loop: claim a
request from ``request_queue`` via ``process_one_request`` -> apply via the
injected applier -> done/failed; periodically calls ``sweep_request_queue``
(recover_stale_claims BEFORE expire_due_requests).

Properties (INERT):
  * Does NOT require TELEGRAM_BOT_TOKEN; does NOT import bot.main; does NOT
    run at import time.
  * Does NOT open the DB at import.
  * Does NOT read env secrets at import.
  * Does NOT import Notion / hosting-platform / secret-vault / Todoist
    clients at the top level or lazily (skeleton only; the real applier is
    injected at run time).
  * Default applier -- :func:`_noop_inert_applier`: raises fail-loud;
    NEVER makes network calls and NEVER reads secrets.
  * Does NOT install / enable / start the systemd unit
    ``aios-integrations-worker.service`` (target-only template).

Mirrors the shape of ``bot/claude_worker.py``:
  * ``WORKER_ID`` from env with a default.
  * ``run_once(applier, ...)`` -- one iteration.
  * ``run_loop(applier, max_iterations, poll_interval, sweep_interval,
    sleep, now_fn)`` -- consumer loop.
  * ``main()`` under ``if __name__ == "__main__":`` with
    ``pragma: no cover`` (a real run is not exercised in tests).
  * Import is side-effect-free.

Differences from ``bot/claude_worker.py``:
  * Sync, not async (pure-sync SQLite).
  * Its own ``AIOS_INTEGRATIONS_WORKER_ID`` env (distinct from
    ``AIOS_WORKER_ID`` on claude-worker).
  * Periodic sweep is built into the loop on a cadence separate from polling.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from . import aios_worker_contract as WC

log = logging.getLogger("aios.integrations_worker")


# ---- Configuration (env-driven; defaults are inert-safe) ------------------------

WORKER_ID = os.getenv("AIOS_INTEGRATIONS_WORKER_ID", "integrations-worker-1")
POLL_INTERVAL_S = float(os.getenv("AIOS_INTEGRATIONS_POLL_S", "5"))
SWEEP_INTERVAL_S = float(os.getenv("AIOS_INTEGRATIONS_SWEEP_S", "30"))
# How often the keyed worker scans for due scheduled jobs (schedule_executor).
# ~60s is fine granularity for daily reminders at a configured time. Only runs in the real service
# loop; INERT until the startup-seeder adds rows (empty schedule_queue -> 0 fired).
SCHEDULE_TICK_INTERVAL_S = float(os.getenv("AIOS_SCHEDULE_TICK_S", "60"))
LEASE_S = float(os.getenv("AIOS_INTEGRATIONS_LEASE_S",
                          str(WC.DEFAULT_LEASE_SECONDS)))
MAX_ATTEMPTS = int(os.getenv("AIOS_INTEGRATIONS_MAX_ATTEMPTS",
                              str(WC.DEFAULT_MAX_ATTEMPTS)))
BACKOFF_BASE_S = float(os.getenv("AIOS_INTEGRATIONS_BACKOFF_BASE_S",
                                  str(WC.DEFAULT_BACKOFF_BASE_SECONDS)))
BACKOFF_MAX_S = float(os.getenv("AIOS_INTEGRATIONS_BACKOFF_MAX_S",
                                 str(WC.DEFAULT_BACKOFF_MAX_SECONDS)))


# ---- Default inert applier (B-02E placeholder; B-02H will inject real) ----------

class InertStubNotInjected(RuntimeError):
    """Raised by :func:`_noop_inert_applier` when an applier was NOT
    injected and the inert stub fires.

    Fail-LOUD design: if a misconfigured deploy ever ships this module
    without wiring the real applier, the queue row goes through
    ``mark_failed_or_dead`` (class-name-only ``error_class`` pinned to
    "InertStubNotInjected") and, after ``max_attempts`` retries, lands in
    ``dead`` -- poll then surfaces ``Outcome.STORAGE_UNAVAILABLE`` ("could
    not save") rather than a fail-SILENT path that returns a success-shaped
    dict and makes the user see ``Outcome.SAVED`` ("saved") for a write
    that never landed.
    """


def _noop_inert_applier(record) -> dict:
    """Default applier (inert; fail-LOUD).

    Raises :class:`InertStubNotInjected` so the queue row goes to
    ``failed`` (and eventually ``dead`` after ``max_attempts``).  This
    prevents the silent-correctness bug where the row would otherwise
    land in ``done`` and the user would see a "saved" confirmation for
    a write that never touched the real backend.

    A real deploy REPLACES this default by injecting the real applier via
    the ``applier`` parameter on :func:`run_once` / :func:`run_loop`.
    """
    raise InertStubNotInjected(
        "default applier fired; inject real applier via run_once "
        "or run_loop (kind=" + record.kind + ")"
    )


# ---- Public API: single iteration ----------------------------------------------

def run_once(
    *,
    applier: Optional[Callable[[Any], dict]] = None,
    kinds: Optional[Any] = None,
    account: Optional[str] = None,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> Optional[str]:
    """Process one request from the queue (or no-op if empty).

    Returns the correlation_id processed, or ``None`` if the queue had
    no eligible row.

    The ``applier`` parameter is injectable; default is
    :func:`_noop_inert_applier` (B-02E inert stub).  B-02H will provide
    the real Notion-backed applier.
    """
    chosen_applier = applier if applier is not None else _noop_inert_applier
    return WC.process_one_request(
        worker_id=WORKER_ID,
        applier=chosen_applier,
        kinds=kinds,
        account=account,
        max_attempts=MAX_ATTEMPTS,
        backoff_base_seconds=BACKOFF_BASE_S,
        backoff_max_seconds=BACKOFF_MAX_S,
        now_fn=now_fn,
        db=db,
    )


def sweep_once(
    *,
    lease_seconds: float = None,  # type: ignore[assignment]
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> dict:
    """Run one sweep pass (recover_stale_claims -> expire_due_requests).

    Order is enforced by :func:`bot.aios_worker_contract.
    sweep_request_queue`: recover-before-expire so a WRITE row whose
    worker crashed mid-claim gets a retry chance BEFORE the TTL sweep
    can turn it into ``dead``.
    """
    lease_s = float(LEASE_S) if lease_seconds is None else float(lease_seconds)
    return WC.sweep_request_queue(
        lease_seconds=lease_s, now_fn=now_fn, db=db,
    )


# ---- keyed-worker duties (schedule executor + router consumer) ----
#
# The keyed worker is the ONLY process that can send Telegram messages and read Todoist + the
# local encrypted store, so the two remaining receiver-side behaviours migrate here:
#   (1) fire the daily scheduled jobs (schedule_executor.run_due_jobs) -- the consumer
#       loop that schedule_queue's docstring always promised; and
#   (2) consume alias='router' rows (the unified-router worker path).
# Both are built/driven ONLY in the real service loop (max_iterations is None). The finite-
# max_iterations test path never touches them, so the well-tested request-queue loop stays
# byte-identical.


def _keyed_setup():
    """Build the keyed-worker duty handles (real service only). Returns (event_loop, route_runner).

    Callback handlers register into router_dispatch.CALLBACK_HANDLERS at their own import site, so a
    consumed kind='callback' router row can route to the in-worker handlers. The event loop and the
    route runner are built lazily -- the router/schedule import graph is pulled ONLY in the running
    service, never at module import (keeps `import bot.integrations_worker` side-effect-free).
    """
    import asyncio
    from . import router_worker
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)  # single-threaded worker: make this the process's current loop
    return loop, router_worker.build_route_runner()


def _run_keyed_duties(loop, route_runner, *, now_v, last_sched_ts):
    """One tick of the keyed-worker duties. Returns the updated last_sched_ts.

    Each duty is isolated: any exception is logged CLASS-NAME-ONLY and swallowed -- a bad tick must
    never crash the always-on worker (same discipline as the run_once / sweep ticks).

    (1) Router consume -- GATED on router_dispatch.dispatch_enabled() (AIOS_ROUTER_DISPATCH). While
        the flag is OFF it NEVER claims a router row, so there is zero echo traffic and zero extra
        queue load; arming the flag is a single indivisible change (flag ON on the keyed worker +
        the live group-0 handler drops the prefixes the worker now owns). When ON: claim ONE
        alias='router' row (single-writer-per-chat) and run it through the echo/dispatch runner.
    (2) Schedule fire -- on a ~SCHEDULE_TICK_INTERVAL_S cadence, fire due scheduled jobs and re-arm
        the recurring ones. INERT until the startup-seeder adds rows (an empty schedule_queue fires
        0 jobs); the run_daily JobQueue path in bot/main.py is untouched until each job's cutover.
    """
    from . import router_dispatch
    # (1) router consume -- flag-gated (dormant until the money-cutover arms AIOS_ROUTER_DISPATCH)
    if router_dispatch.dispatch_enabled():
        from . import claude_worker_core, task_queue
        try:
            loop.run_until_complete(
                claude_worker_core.process_one(
                    WORKER_ID, route_runner,
                    claim_fn=task_queue.claim_next_route_task,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- a router tick MUST NOT crash the loop
            log.warning("router consume failed (class=%s; loop continues)", type(exc).__name__)
    # (2) schedule fire -- cadence-gated; empty queue is a no-op (inert until the step-6 seeder)
    if now_v - last_sched_ts >= float(SCHEDULE_TICK_INTERVAL_S):
        last_sched_ts = now_v
        from . import schedule_executor, schedule_jobs
        try:
            loop.run_until_complete(
                schedule_executor.run_due_jobs(
                    schedule_jobs.HANDLERS, next_due_fn=schedule_jobs.next_due_fn,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- a schedule tick MUST NOT crash the loop
            log.warning("schedule fire failed (class=%s; loop continues)", type(exc).__name__)
    return last_sched_ts


# ---- Public API: continuous loop -----------------------------------------------

def run_loop(
    *,
    applier: Optional[Callable[[Any], dict]] = None,
    kinds: Optional[Any] = None,
    account: Optional[str] = None,
    max_iterations: Optional[int] = None,
    poll_interval: float = POLL_INTERVAL_S,
    sweep_interval: float = SWEEP_INTERVAL_S,
    lease_seconds: float = None,  # type: ignore[assignment]
    sleep: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
    db: Optional[str] = None,
) -> dict:
    """Consume loop.  ``max_iterations=None`` -> run forever (real mode).

    Tests pass a finite ``max_iterations`` (and a fake ``sleep`` /
    ``now_fn``) to keep the loop bounded.

    The loop interleaves:
      * On each iteration: call :func:`run_once` (claim+apply+mark).
      * Periodically (every ``sweep_interval`` seconds): call
        :func:`sweep_once` (recover-then-expire).
      * On empty queue (``run_once`` returned None): sleep for
        ``poll_interval`` to avoid busy-spinning.

    Sweep cadence is decoupled from poll cadence -- a worker that is
    busy processing rows still gets periodic sweeps, and an idle worker
    still wakes briefly for sweeps.

    Returns a value-free summary dict::

        {
            'iterations': int,        # run_once calls made
            'processed':  int,        # run_once calls that returned a cid
            'sweeps':     int,        # sweep_once calls made
        }
    """
    iterations = 0
    processed = 0
    sweeps = 0
    # Review-fix: init last_sweep_ts so the FIRST iteration's gate
    # fires immediately (a worker that just started after a crash
    # must run a sweep ASAP to recover orphaned claims, not wait
    # sweep_interval).
    last_sweep_ts = float(now_fn()) - float(sweep_interval)
    # Auto-restart on code update (real mode only). Mirror claude_worker — on an
    # idle pass, if the live repo HEAD changed and the new code compiles+imports, exit so
    # systemd relaunches us on fresh code (future deploys apply without a manual restart).
    _ar = None
    _start_head = None
    if max_iterations is None:
        try:
            from . import auto_restart as _ar
            _start_head = _ar.current_head()
        except Exception:  # noqa: BLE001
            _ar = None
    # Real service only: the keyed worker ALSO fires due scheduled jobs and consumes
    # alias='router' rows. Built once here; driven each iteration by _run_keyed_duties. Tests
    # (finite max_iterations) NEVER build/run this -> the request-queue loop stays byte-identical.
    _keyed_loop = None
    _route_runner = None
    _last_sched_ts = float(now_fn())  # first schedule scan one interval after startup
    if max_iterations is None:
        try:
            _keyed_loop, _route_runner = _keyed_setup()
        except Exception as exc:  # noqa: BLE001 -- keyed-duties setup must not stop the worker
            log.warning("keyed-duties setup failed (class=%s; request loop continues)",
                        type(exc).__name__)
            _keyed_loop = None
        # Seed the pending scheduled-reminder row so the in-process executor fires it at a configured
        # time (the live run_daily 'daily_reminders' registration is removed in the same cutover).
        # Idempotent across restarts (cancel-then-add). Real service only; finite-max_iterations tests
        # never seed (they never enter this branch), so the request loop stays byte-identical. GATED on
        # _keyed_loop being live: seeding a row while the executor (which only runs when _keyed_loop is
        # not None, below) is dead would arm a reminder that never fires — the exact silent-loss this
        # cutover must avoid. A seed failure MUST NOT stop the worker.
        if _keyed_loop is not None:
            try:
                from . import schedule_seed
                schedule_seed.seed_schedules()
            except Exception as exc:  # noqa: BLE001 -- seeding must not stop the worker
                log.warning("schedule seeding failed (class=%s; loop continues)", type(exc).__name__)
            # Precise per-task reminders (one-shot, arbitrary due instant): reconcile worker-side at
            # startup — scan Todoist (keyed) and arm one value-free schedule_queue row per future
            # time-specific task. The task title is read + sent ONLY here worker-side at fire time,
            # never logged/sent by the keyless receiver. Async (Todoist read) -> drive it on the keyed
            # loop. Same _keyed_loop gate + swallow-and-continue discipline as the daily seed above.
            try:
                from . import schedule_seed
                _keyed_loop.run_until_complete(schedule_seed.seed_precise_reminders())
            except Exception as exc:  # noqa: BLE001 -- seeding must not stop the worker
                log.warning("precise reminder seeding failed (class=%s; loop continues)",
                            type(exc).__name__)
    while max_iterations is None or iterations < max_iterations:
        # Review-fix: run_once FIRST, sweep AFTER.  Order swapped so a
        # row recovered by sweep waits at least until the NEXT
        # iteration to be re-claimed -- the backoff that
        # recover_stale_claims sets (next_attempt_ts=now) plus the
        # natural inter-iteration sleep gives the system breathing
        # room.  This avoids zero-backoff retry on a recovered row.
        try:
            cid = run_once(
                applier=applier, kinds=kinds, account=account,
                now_fn=now_fn, db=db,
            )
        except Exception as exc:  # noqa: BLE001 -- run_once MUST NOT crash loop
            # Review-fix (hygiene #1): log only the CLASS NAME -- no
            # message, no traceback (the verbose-trace logger
            # variant leaks to journald).
            log.warning(
                "run_once failed (class=%s; loop continues)",
                type(exc).__name__,
            )
            cid = None
        iterations += 1
        if cid is None:
            # Empty queue OR run_once raised -- back off so we don't
            # busy-spin the DB.
            if _ar is not None and _ar.should_restart(_start_head, smoke_module="bot.integrations_worker"):
                import os as _os
                log.info("integrations-worker: fresh code -- exiting, systemd will relaunch")
                _os._exit(0)
            sleep(float(poll_interval))
        else:
            processed += 1
        # Periodic sweep AFTER run_once (review-fix: swapped order).
        now_v = float(now_fn())
        if now_v - last_sweep_ts >= float(sweep_interval):
            try:
                sweep_once(
                    lease_seconds=lease_seconds, now_fn=now_fn, db=db,
                )
            except Exception as exc:  # noqa: BLE001 -- sweep MUST NOT crash loop
                # Review-fix (hygiene #1): log only the CLASS NAME --
                # no message, no traceback.
                log.warning(
                    "sweep failed (class=%s; loop continues)",
                    type(exc).__name__,
                )
            sweeps += 1
            last_sweep_ts = now_v
        # Keyed-duties tick (real service only): router consume (flag-gated) + schedule fire
        # (cadence-gated). _keyed_loop is None in the finite-max_iterations test path, so this is
        # skipped there and the request loop above stays byte-identical.
        if _keyed_loop is not None:
            _last_sched_ts = _run_keyed_duties(
                _keyed_loop, _route_runner,
                now_v=float(now_fn()), last_sched_ts=_last_sched_ts,
            )
    return {
        "iterations": iterations,
        "processed": processed,
        "sweeps": sweeps,
    }


# ---- Entrypoint (real-run; not exercised in tests) -----------------------------

def main() -> None:  # pragma: no cover -- real run; not in tests
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    # Central secret redaction for this service's logs.
    from .log_redaction import install_log_redaction
    install_log_redaction()
    # Inject the REAL Notion/Todoist applier (replaces the inert stub) and restrict to the
    # integration kinds so this worker only claims the rows it is meant to serve.
    from .integrations_proxy import integration_applier, INTEGRATION_KINDS
    log.info(
        "integrations-worker starting (worker_id=%s, poll=%ss, sweep=%ss, "
        "lease=%ss, applier=REAL notion/todoist, kinds=%s)",
        WORKER_ID, POLL_INTERVAL_S, SWEEP_INTERVAL_S, LEASE_S, INTEGRATION_KINDS,
    )
    run_loop(applier=integration_applier, kinds=INTEGRATION_KINDS)


if __name__ == "__main__":
    main()
