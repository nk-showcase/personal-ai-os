"""bot/router_worker.py — S6 S-1 worker-router skeleton (echo). Import-side-effect-free:
imports only stdlib + reply_queue. NO Telegram / Claude / secrets / classifier.

build_route_runner() returns a runner(task) for claude_worker_core.process_one: it handles ONLY
alias='router' rows (raises NotImplementedError on anything else, so nothing real fires by
accident), parses the structural envelope from task_text, runs a TRIVIAL echo handler, and
enqueues the reply via reply_queue. Real feature/classifier dispatch is deferred to S-2+.
See docs/architecture/s6-router-s1-skeleton.md §3.
"""
from __future__ import annotations

import json

from . import reply_queue, router_dispatch, router_seal  # router_dispatch is import-light (no telegram)
from .claude_worker_core import _redact   # single source of truth for _SECRET_PATTERNS

_MAX_REPLY = 4096   # Telegram hard message limit; tighter than _MAX_RESULT (8000)


def _redact_reply(text) -> str:
    """Mirror _coerce_result (claude_worker_core.py): scrub secret-shaped tokens,
    then cap. Applied to every reply BEFORE reply_queue.enqueue_reply (which has no
    redaction/cap of its own).

    BOUNDARY: this scrubs secret-shaped tokens (_SECRET_PATTERNS) and caps length. It is a
    secret-leak guard, not a content-encryption layer: reply rows in the reply queue are
    protected at rest by directory permissions and local-only placement, not by the context
    cipher. Content-at-rest encryption of reply rows is a documented future hardening (see
    docs/security/threat-model.md), not a property this function provides."""
    return _redact(str(text or ""))[:_MAX_REPLY]


def _echo_handler(envelope: dict) -> str:
    """TRIVIAL S-1 pass-through. NO real feature logic — proves the pipe only."""
    return "routed: " + (envelope.get("text") or "")


def build_route_runner(handler=_echo_handler):
    """runner(task) for process_one. alias='router' only; parse envelope -> handler -> reply.
    mode='inbound-enc' (S-2 ON) -> open_inbound the sealed task_text first (fail-closed if the
    worker identity is unset/missing/!0600); mode='inbound' (OFF) -> plain json.loads (S-1)."""

    async def runner(task):
        if task.get("alias") != "router":
            raise NotImplementedError(f"router_worker: non-route alias {task.get('alias')!r}")
        if task.get("mode") == "inbound-enc":
            raw = router_seal.open_inbound(task["task_text"])   # fail-closed if identity missing
        else:
            raw = task["task_text"]                             # S-1 plain path, unchanged
        envelope = json.loads(raw)

        # --- S6 dispatch (flag-gated; default OFF => byte-identical to S-1/S-2) ---
        if router_dispatch.dispatch_enabled():
            if envelope.get("kind") == "callback":
                # button tap: route by callback_data prefix (no classifier). INERT until a callback
                # handler is registered (CALLBACK_HANDLERS empty -> run_callback False -> echo).
                owner_id = int(envelope["chat_id"])
                if await router_dispatch.run_callback(envelope, owner_id=owner_id):
                    return {"routed": True, "dispatched": "callback", "kind": "callback"}
            else:
                from . import classifier                 # LAZY: telegram + Anthropic key stay worker-side
                result = await classifier.analyze(envelope.get("text") or "")
                # gate: a dict result (NOT the multi-intent list) AND a dispatchable intent
                if isinstance(result, dict) and result.get("intent") in router_dispatch.DISPATCH_INTENTS:
                    merged = {**envelope, **result}          # carries result["intent"], result["query"], ...
                    owner_id = int(envelope["chat_id"])      # dispatched read intents never read update.effective_user.id
                    dispatched = await router_dispatch.run_intent(merged, owner_id=owner_id)
                    if dispatched:
                        return {"routed": True, "dispatched": result["intent"], "kind": envelope.get("kind")}
        # --- end dispatch; everything else is the existing echo path, UNCHANGED ---

        reply_text = _redact_reply(handler(envelope))
        reply_id = reply_queue.enqueue_reply(task["chat_id"], reply_text)
        return {"routed": True, "reply_id": reply_id, "kind": envelope.get("kind")}

    return runner
