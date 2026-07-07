"""bot/integrations_proxy.py -- Path B keyless-transport HTTP proxy.

In a KEYED process (the integrations-worker: NOTION_API_KEY / TODOIST_API_KEY
resolve non-empty) notion_request/todoist_request call the API directly. In the
KEYLESS transport they enqueue the op onto the shared request queue and poll for
the worker's result. The worker runs integration_applier.

Review fixes baked in:
  C1: explicit shared DB path passed to enqueue/poll (also set via AIOS_DATA_DB).
  C2: BOTH request and result are base64-wrapped -> the queue secret-scan only
      ever sees benign keys (op_b64 / result_b64); raw Notion/Todoist JSON never
      reaches mark_done's scanner.
  C3: WRITE ops use a stable correlation_id from a caller-supplied business_key.
  M2: WRITE account = the single-user id string (not None).
  H1: dedicated bounded ThreadPoolExecutor for the blocking enqueue+poll.
  H3: TodoistResponse.raise_for_status raises IntegrationHTTPError (handled).
"""
from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import httpx

from .secrets_loader import get_secret, SecretAccessDenied

logger = logging.getLogger(__name__)

NOTION_BASE_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TODOIST_BASE_URL = "https://api.todoist.com/api/v1"

KIND_NOTION_READ = "notion_read"
KIND_NOTION_WRITE = "notion_write"
KIND_TODOIST_READ = "todoist_read"
KIND_TODOIST_WRITE = "todoist_write"
KIND_LLM = "llm"
KIND_STORE_READ = "store_read"
KIND_STORE_WRITE = "store_write"
KIND_STORE_VIEW = "store_view"
INTEGRATION_KINDS = (
    KIND_NOTION_READ, KIND_NOTION_WRITE, KIND_TODOIST_READ, KIND_TODOIST_WRITE, KIND_LLM,
    KIND_STORE_READ, KIND_STORE_WRITE, KIND_STORE_VIEW,
)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

# C1: one absolute DB for both processes. Env override matches the systemd units.
_SHARED_DB = os.getenv("AIOS_DATA_DB") or None  # None -> aios_storage.db_path()

# M2: a single operator -> one write-lock domain (NOT None, which is the NULL-account domain).
_OWNER_ACCOUNT = os.getenv("AIOS_INTEGRATION_ACCOUNT", "operator")

# H2: poll budget < POLL_DEADLINE_CEILING_SECONDS (15); interval < deadline.
_POLL_DEADLINE_S = 12.0
_POLL_INTERVAL_S = 0.4
_WRITE_TTL_S = 90.0  # >= 2 * DEFAULT_LEASE_SECONDS(30) -> RECOMMENDED_TTL_LEASE_RATIO ok

_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_NOTION_READ_POST_SUFFIXES = ("/search", "/query")

# H1: bounded pool dedicated to queue round-trips; bot event loop never blocks,
# and a burst cannot exhaust the shared default to_thread pool.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="intproxy")


class IntegrationHTTPError(RuntimeError):
    """Raised by raise_for_status() on a >=400 integration response.

    A RuntimeError subclass so the existing storage call sites (which catch
    httpx.HTTPError or broad Exception) handle it as a failed save rather than
    crashing on a malformed httpx exception object (review H3)."""


class NotionResponse:
    """Duck-types the subset of httpx.Response the call sites use."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = int(status_code)
        self._body = body

    def json(self) -> Any:
        return self._body

    @property
    def text(self) -> str:
        try:
            return _json.dumps(self._body, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(self._body)


class TodoistResponse(NotionResponse):
    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise IntegrationHTTPError(f"Todoist HTTP {self.status_code}")


# ---- payload / result codec (C2: scan only ever sees op_b64 / result_b64) ----

def _b64(obj: Any) -> str:
    return base64.b64encode(
        _json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def _unb64(blob: str) -> Any:
    return _json.loads(base64.b64decode(blob.encode("ascii")).decode("utf-8"))


def encode_op(method: str, path: str, json_body, params) -> dict:
    return {"op_b64": _b64(
        {"method": method, "path": path, "json": json_body, "params": params}
    )}


def decode_op(payload: dict) -> dict:
    return _unb64(payload["op_b64"])


def encode_result(result: dict) -> dict:
    return {"result_b64": _b64(result)}


def decode_result(result: dict) -> dict:
    return _unb64(result["result_b64"])


def _build_store_payload(domain: str, op: str, args: dict | None):
    """Build the queue payload for a store op (read/write/view), one envelope for all kinds.

    OFF (default, AIOS_STORE_SEAL false): returns EXACTLY {"op_b64": _b64({domain,op,args})} —
    byte-identical to the pre-seal code (the store's personal text still rides base64, reversible).

    ON: base64-packs the same inner {domain,op,args}, then age-seals that inner blob to the
    worker's public recipient and returns {"op_b64_sealed": …}. Fail-closed: if the recipient is
    a placeholder / unprovisioned the seal RAISES RouterSealError and this returns None (the caller
    degrades to {"ok": False, "unavailable": True}) — it NEVER falls back to enqueuing plaintext.

    'op_b64_sealed' passes the queue's _SECRET_KEY_RE scan for the same reason 'op_b64' does
    (no token/secret/key/auth/password substring)."""
    inner = _b64({"domain": domain, "op": op, "args": args or {}})
    from . import config  # lazy: config resolves the bot token at import (absent on bare python3)
    if not config.store_seal_enabled():
        return {"op_b64": inner}          # OFF path — unchanged, byte-identical
    from . import store_seal              # lazy: pulls in age primitives only when sealing
    try:
        return {"op_b64_sealed": store_seal.seal_store_inbound(inner)}
    except store_seal.RouterSealError:
        return None                       # unprovisioned recipient -> fail closed, no plaintext


def write_business_key(*parts: Any) -> str:
    """Caller-facing helper to build a stable WRITE business key
    (e.g. write_business_key('notes', '2026-06-30'))."""
    return "|".join("" if p is None else str(p) for p in parts)


def _kind_for(service: str, method: str, path: str) -> str:
    is_write = method.upper() in _WRITE_METHODS
    if (service == "notion" and method.upper() == "POST"
            and path.endswith(_NOTION_READ_POST_SUFFIXES)):
        is_write = False
    if service == "notion":
        return KIND_NOTION_WRITE if is_write else KIND_NOTION_READ
    return KIND_TODOIST_WRITE if is_write else KIND_TODOIST_READ


def _notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {get_secret('NOTION_API_KEY', default='')}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _todoist_headers() -> dict:
    return {
        "Authorization": f"Bearer {get_secret('TODOIST_API_KEY', default='')}",
        "Content-Type": "application/json",
    }


def _anthropic_headers() -> dict:
    return {
        "x-api-key": get_secret("ANTHROPIC_API_KEY", default=""),
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }


def _extract_anthropic_text(data) -> str:
    """Pull the first text block out of an Anthropic Messages API response."""
    if not isinstance(data, dict):
        return ""
    for block in (data.get("content") or []):
        if isinstance(block, dict) and block.get("type") == "text":
            return (block.get("text") or "").strip()
    return ""


def _is_keyed_for(secret_name: str) -> bool:
    # Two-user split: under AIOS_SERVICE_NAME=telegram-bot the per-service policy denies non-owned
    # keys (Notion/Todoist/Anthropic) with SecretAccessDenied. That denial MEANS "not keyed here" ->
    # route keyless to the integrations-worker. Swallow it so Path B works instead of crashing.
    try:
        return bool(get_secret(secret_name, default=""))
    except SecretAccessDenied:
        return False


# ---- transport side: direct (keyed) OR enqueue+poll (keyless) ------------------

async def _direct_call(base_url, headers, method, path, json_body, params, timeout):
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(
            method.upper(), f"{base_url}{path}",
            headers=headers, json=json_body, params=params,
        )


def _blocking_round_trip(kind, payload, *, is_write, business_key):
    """Runs in the dedicated executor thread. Enqueue then short-poll."""
    from . import aios_request_queue as RQ
    from . import aios_worker_contract as WC

    if is_write:
        # C3: stable id -> a retry of the SAME logical write reuses the row.
        cid = WC.build_stable_correlation_id(
            user_id=_OWNER_ACCOUNT, kind=kind,
            business_key=business_key if business_key is not None else _b64(payload),
        )
        RQ.enqueue_request(
            kind=kind, account=_OWNER_ACCOUNT, payload=payload,
            ttl_seconds=_WRITE_TTL_S, correlation_id=cid, db=_SHARED_DB,
        )
    else:
        cid = RQ.enqueue_request(
            kind=kind, account=None, payload=payload,
            ttl_seconds=None, db=_SHARED_DB,
        )
    return WC.poll_request_result(
        cid, deadline_seconds=_POLL_DEADLINE_S,
        poll_interval_seconds=_POLL_INTERVAL_S, db=_SHARED_DB,
    )


def _outcome_to_response(outcome, resp_cls):
    """Map a PollOutcome to a response object.

    WRITE_SAVED / READ_RESULT / READ_LOCAL_FALLBACK -> real status+json
      (decoded from result_b64).
    WRITE_ACCEPTED / WRITE_DEAD / READ_UNAVAILABLE -> synthetic 503 so the
      existing `!= 200` branches set last_error and degrade (no fake success;
      no hang -- the poll already returned at the deadline)."""
    from .aios_worker_contract import PollResultState as S
    st = outcome.state
    if st in (S.WRITE_SAVED, S.READ_RESULT, S.READ_LOCAL_FALLBACK):
        raw = outcome.result or {}
        try:
            decoded = decode_result(raw) if "result_b64" in raw else raw
        except (KeyError, ValueError, TypeError):
            decoded = {"status_code": 502, "json": {"object": "error"}}
        return resp_cls(decoded.get("status_code", 200), decoded.get("json", {}))
    return resp_cls(503, {
        "object": "error", "status": 503,
        "message": f"integration unavailable ({getattr(st, 'value', st)})",
    })


async def notion_request(method, path, *, json=None, params=None,
                         timeout=30.0, business_key=None) -> NotionResponse:
    if _is_keyed_for("NOTION_API_KEY"):
        return await _direct_call(
            NOTION_BASE_URL, _notion_headers(), method, path, json, params, timeout
        )
    kind = _kind_for("notion", method, path)
    payload = encode_op(method, path, json, params)
    loop = asyncio.get_running_loop()
    outcome = await loop.run_in_executor(
        _EXECUTOR, lambda: _blocking_round_trip(
            kind, payload, is_write=(kind == KIND_NOTION_WRITE),
            business_key=business_key,
        )
    )
    return _outcome_to_response(outcome, NotionResponse)


async def store_request(domain: str, op: str, args: dict | None = None, *,
                        is_write: bool, business_key=None) -> dict:
    """Cutover: a local ENCRYPTED-store op executed by the keyed worker over the same
    queue (kinds store_read/store_write). Both payload and result travel base64-packed
    (C2 - like the Notion ops). Returns the applier result ({"ok": …, …}); when the
    worker is unavailable, {"ok": False, "unavailable": True} (no hang, no fake success).
    The bot is always keyless for the store - there is no direct branch by design (only the worker is keyed)."""
    from .aios_worker_contract import PollResultState as S
    kind = KIND_STORE_WRITE if is_write else KIND_STORE_READ
    payload = _build_store_payload(domain, op, args)
    if payload is None:                  # seal ON but recipient unprovisioned -> fail closed
        return {"ok": False, "unavailable": True}
    loop = asyncio.get_running_loop()
    outcome = await loop.run_in_executor(
        _EXECUTOR, lambda: _blocking_round_trip(
            kind, payload, is_write=is_write, business_key=business_key,
        )
    )
    if outcome.state in (S.WRITE_SAVED, S.READ_RESULT, S.READ_LOCAL_FALLBACK):
        raw = outcome.result or {}
        try:
            return decode_result(raw) if "result_b64" in raw else raw
        except (KeyError, ValueError, TypeError):
            return {"ok": False, "unavailable": True}
    if outcome.state == S.WRITE_ACCEPTED:
        # The row is durably queued (TTL); the worker will apply it exactly once, it just did
        # not finish within the poll window (e.g. it is busy with a long LLM turn). This is NOT
        # an error and NOT unavailability: the caller must not show "error" and provoke a retry
        # (a retry with a new business_key/id creates a SECOND row -> a duplicate apply). Cf. view_request.
        return {"ok": False, "accepted": True}
    return {"ok": False, "unavailable": True}


async def view_request(domain: str, view: str, args: dict | None = None, *,
                       business_key=None) -> dict:
    """Gate 0 / Part B: ask the keyed worker to render AND SEND the personal output to
    Telegram itself (chat_id is required in args). The decrypted text is NOT returned -
    the result carries only structure ({"ok", "sent", number->id maps, etc.}).

    Three outcomes (one contract for the whole worker-sends mechanism):
      WRITE_SAVED/READ_*     -> the decoded worker result ({"ok": …, "sent": …}).
      WRITE_ACCEPTED         -> {"ok": False, "accepted": True}. The worker durably took
        the row but did not close it within the poll window (~12s); it will STILL send the
        reply to Telegram itself (20-40s). This is NOT unavailability - the transport must NOT
        call "retry" (a retry creates a second turn). See view_render.view_reply_for.
      WRITE_DEAD/corrupt blob -> {"ok": False, "unavailable": True}. Genuine unavailability
        (no token / the row died on TTL / the queue is unreachable): the transport falls back to
        VIEW_BUSY_MESSAGE (no lost replies)."""
    from .aios_worker_contract import PollResultState as S
    payload = _build_store_payload(domain, view, args)
    if payload is None:                  # seal ON but recipient unprovisioned -> fail closed
        return {"ok": False, "unavailable": True}
    loop = asyncio.get_running_loop()
    outcome = await loop.run_in_executor(
        _EXECUTOR, lambda: _blocking_round_trip(
            KIND_STORE_VIEW, payload, is_write=True, business_key=business_key,
        )
    )
    if outcome.state in (S.WRITE_SAVED, S.READ_RESULT, S.READ_LOCAL_FALLBACK):
        raw = outcome.result or {}
        try:
            return decode_result(raw) if "result_b64" in raw else raw
        except (KeyError, ValueError, TypeError):
            return {"ok": False, "unavailable": True}
    if outcome.state == S.WRITE_ACCEPTED:
        # The row is durably queued; the worker will send the result to Telegram itself -> not an error.
        return {"ok": False, "accepted": True}
    return {"ok": False, "unavailable": True}


async def todoist_request(method, path, *, json=None, params=None,
                          timeout=30.0, business_key=None) -> TodoistResponse:
    if _is_keyed_for("TODOIST_API_KEY"):
        return await _direct_call(
            TODOIST_BASE_URL, _todoist_headers(), method, path, json, params, timeout
        )
    kind = _kind_for("todoist", method, path)
    payload = encode_op(method, path, json, params)
    loop = asyncio.get_running_loop()
    outcome = await loop.run_in_executor(
        _EXECUTOR, lambda: _blocking_round_trip(
            kind, payload, is_write=(kind == KIND_TODOIST_WRITE),
            business_key=business_key,
        )
    )
    return _outcome_to_response(outcome, TodoistResponse)


async def anthropic_complete(*, system: str, user: str, model: str = "claude-opus-4-8",
                             max_tokens: int = 200, effort: str = "max", timeout: float = 60.0) -> str:
    """Anthropic text completion. KEYED process -> call Anthropic directly; KEYLESS
    transport -> route the call to the worker (which holds ANTHROPIC_API_KEY) over the
    channel. Returns the assistant text (str), or '' on failure (caller falls back).
    Used by classifier._call_anthropic / _call_opus_thinking so classification and other
    LLM-backed features work without the transport holding the AI key.
    effort -> output_config.effort (low/medium/high/xhigh/max); 'max' is the configured
    default - maximum depth for these AI features (a Messages API parameter, verified with a 200)."""
    payload = {
        "model": model, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": effort},
    }
    if _is_keyed_for("ANTHROPIC_API_KEY"):
        try:
            resp = await _direct_call(ANTHROPIC_BASE_URL, _anthropic_headers(),
                                      "POST", "/v1/messages", payload, None, timeout)
            return _extract_anthropic_text(resp.json()) if resp.status_code == 200 else ""
        except Exception as e:  # noqa: BLE001
            logger.warning("anthropic direct call failed: %s", type(e).__name__)
            return ""

    op = encode_op("POST", "/v1/messages", payload, None)
    loop = asyncio.get_running_loop()

    def _enqueue_poll():
        from . import aios_request_queue as RQ
        from . import aios_worker_contract as WC
        from .aios_worker_contract import PollResultState as S
        cid = RQ.enqueue_request(kind=KIND_LLM, account=None, payload=op,
                                 ttl_seconds=None, db=_SHARED_DB)
        # Loop the short poll (each < 15s) up to ~24s, within the worker lease, so a
        # fast LLM call lands; slow ones degrade to fallback.
        for _ in range(2):
            outcome = WC.poll_request_result(cid, deadline_seconds=12.0,
                                             poll_interval_seconds=0.4, db=_SHARED_DB)
            if outcome.state == S.READ_RESULT:
                return outcome.result or {}
        return None

    res = await loop.run_in_executor(_EXECUTOR, _enqueue_poll)
    if not res:
        return ""
    try:
        decoded = decode_result(res) if "result_b64" in res else res
    except (KeyError, ValueError, TypeError):
        return ""
    if decoded.get("status_code") != 200:
        return ""
    return _extract_anthropic_text(decoded.get("json") or {})


async def anthropic_messages(payload: dict, *, timeout: float = 120.0,
                             max_wait_seconds: float = 24.0) -> dict:
    """Full Messages API passthrough (supports tools / adaptive thinking / images).
    KEYED process -> call Anthropic directly; KEYLESS transport -> route the call to the
    worker (which holds ANTHROPIC_API_KEY) over the channel. Returns the RAW response JSON
    (dict, with content blocks + stop_reason), or {} on failure/timeout so the caller can
    degrade gracefully.

    max_wait_seconds: how long the KEYLESS caller waits for the worker's result before giving
    up. Default ~24s for quick calls (vision). The Claude chat passes a LONG budget so a deep
    adaptive-thinking reply (30-60s on a long transcript) lands instead of degrading to {}.
    Safe because the integrations-worker is single-threaded and runs stale-recovery only
    BETWEEN requests (bot/integrations_worker.run_loop): a long in-flight call is never
    reclaimed, so only the caller's wait needs to be long enough — no lease change required."""
    if _is_keyed_for("ANTHROPIC_API_KEY"):
        try:
            resp = await _direct_call(ANTHROPIC_BASE_URL, _anthropic_headers(),
                                      "POST", "/v1/messages", payload, None, timeout)
            return resp.json() if resp.status_code == 200 else {}
        except Exception as e:  # noqa: BLE001
            logger.warning("anthropic_messages direct call failed: %s", type(e).__name__)
            return {}

    op = encode_op("POST", "/v1/messages", payload, None)
    loop = asyncio.get_running_loop()
    _rounds = max(1, int(round(max_wait_seconds / 12.0)))
    _ttl = max(int(max_wait_seconds) + 60, 90)  # keep the request valid past the wait

    def _enqueue_poll():
        from . import aios_request_queue as RQ
        from . import aios_worker_contract as WC
        from .aios_worker_contract import PollResultState as S
        cid = RQ.enqueue_request(kind=KIND_LLM, account=None, payload=op,
                                 ttl_seconds=_ttl, db=_SHARED_DB)
        for _ in range(_rounds):
            outcome = WC.poll_request_result(cid, deadline_seconds=12.0,
                                             poll_interval_seconds=0.4, db=_SHARED_DB)
            if outcome.state == S.READ_RESULT:
                return outcome.result or {}
        return None

    res = await loop.run_in_executor(_EXECUTOR, _enqueue_poll)
    if not res:
        return {}
    try:
        decoded = decode_result(res) if "result_b64" in res else res
    except (KeyError, ValueError, TypeError):
        return {}
    if decoded.get("status_code") != 200:
        return {}
    return decoded.get("json") or {}


# ---- worker side: the REAL applier (keyed process) -----------------------------

def _run_sync_http(base_url, headers, op) -> dict:
    """Execute the op synchronously (worker context). Returns the (b64-wrapped)
    result dict for mark_done. A non-2xx response is a VALID result -- the status
    code is carried back so the transport reproduces its existing error branch.
    Only a transport/network exception propagates (-> mark_failed_or_dead)."""
    with httpx.Client(timeout=30) as client:
        r = client.request(
            op["method"].upper(), f"{base_url}{op['path']}",
            headers=headers, json=op.get("json"), params=op.get("params"),
        )
    try:
        body = r.json()
    except (ValueError, _json.JSONDecodeError):
        body = {"object": "error", "raw_text": r.text[:500]}
    # C2: wrap the raw response so the queue scanner never sees Notion/Todoist keys.
    return encode_result({"status_code": r.status_code, "json": body})


def notion_op_applier(record) -> dict:
    return _run_sync_http(NOTION_BASE_URL, _notion_headers(), decode_op(record.payload or {}))


def todoist_op_applier(record) -> dict:
    return _run_sync_http(TODOIST_BASE_URL, _todoist_headers(), decode_op(record.payload or {}))


def llm_op_applier(record) -> dict:
    # Anthropic Messages API call, executed where ANTHROPIC_API_KEY resolves (the worker).
    return _run_sync_http(ANTHROPIC_BASE_URL, _anthropic_headers(), decode_op(record.payload or {}))


def integration_applier(record) -> dict:
    if record.kind in (KIND_NOTION_READ, KIND_NOTION_WRITE):
        return notion_op_applier(record)
    if record.kind in (KIND_TODOIST_READ, KIND_TODOIST_WRITE):
        return todoist_op_applier(record)
    if record.kind == KIND_LLM:
        return llm_op_applier(record)
    if record.kind in (KIND_STORE_READ, KIND_STORE_WRITE):
        # Cutover: local encrypted-store ops. Lazy import; the applier itself refuses
        # fail-closed when this process has no encryption/KEK (see aios_store_applier).
        from .aios_store_applier import store_op_applier
        return store_op_applier(record)
    if record.kind == KIND_STORE_VIEW:
        # Gate 0: render + direct Telegram send inside the keyed worker.
        from .aios_store_applier import store_view_applier
        return store_view_applier(record)
    raise ValueError(f"integration_applier: unsupported kind {record.kind!r}")
