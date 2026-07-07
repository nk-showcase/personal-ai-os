"""bot/schedule_executor.py — S6 stateful substrate-3: the worker-side scan loop that fires due
scheduled jobs and RE-ARMS recurring ones (CRIT-E).

schedule_queue is one-shot (a fired job is never returned again); this is the executor its
docstring promised but never had. INERT SKELETON: NO real job kinds are registered here — the
per-kind `handlers` and the recurrence policy `next_due_fn` are INJECTED (like the S-1 router echo
runner). Pure stdlib + schedule_queue; mac-testable; imports no Telegram / secrets / conv_state.

timers-as-writers (§3.5): when a real chat_data-touching timer job is migrated, its handler will
load->mutate->persist the conv_state blob under the per-chat lock (the inbound router's
single-writer guard, substrate-1). That conv_state integration ships WITH the first such job
(flow wiring); this slice provides the loop + re-arm + dedup that the job runs on.

KNOWN LIMITATION (documented, accepted for the inert skeleton): `schedule_queue.claim_due` fires
the WHOLE due batch atomically (stamps fired_ts), so a due job whose `kind` has no registered
handler is CONSUMED (marked fired) and dropped — there is no 'park unknown kind' like reply_queue.
The live executor must register a handler for every scheduled kind; a kind-filtered claim is a
future refinement, out of this inert slice (no live jobs exist yet).
"""
from __future__ import annotations

from . import schedule_queue


async def run_due_jobs(handlers, *, next_due_fn=None, now=None) -> int:
    """Fire all due scheduled jobs once, then re-arm recurring ones. Returns the count fired.

    handlers: {kind: async handler(payload)} — a due job's handler is awaited; a kind with no
      handler is a no-op (still consumed — see KNOWN LIMITATION).
    next_due_fn: (kind, payload) -> next_due_ts | None. None => one-shot (no re-arm); a non-None
      epoch re-arms the SAME kind/payload as a fresh pending row. DEDUP is by cancel_key (cancel
      any pending row with that key BEFORE re-adding), so a recurring job has EXACTLY one pending
      row after a fire — no duplicate reminder on a restart double-rebuild (CRIT-E-2). A recurring
      job with no cancel_key is re-armed WITHOUT dedup (recurring jobs SHOULD carry a flow-scoped
      cancel_key, §4.5).
    now: injected clock (epoch) for tests; default real time.

    A handler exception does NOT block the rest: the job is already fired (one-shot), so it is
    swallowed-and-skipped, never retried into a hot loop."""
    fired = 0
    for job in schedule_queue.claim_due(now):
        kind = job["kind"]
        payload = job["payload"]
        handler = handlers.get(kind)
        if handler is not None:
            try:
                await handler(payload)
            except Exception:
                pass  # already fired (one-shot); a failed job must not block others or re-loop
        if next_due_fn is not None:
            next_due = next_due_fn(kind, payload)
            if next_due is not None:
                cancel_key = job.get("cancel_key")
                if cancel_key:
                    schedule_queue.cancel(cancel_key)  # dedup: <=1 pending row per cancel_key
                schedule_queue.add_scheduled(kind, next_due, payload, cancel_key=cancel_key)
        fired += 1
    return fired
