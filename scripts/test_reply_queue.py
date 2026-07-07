#!/usr/bin/env python3
"""Plain-python tests for bot/reply_queue.py (S6 0c reply channel). Offline, pure SQLite.

Covers the plan's 0c reply-loop verification: undelivered backlog is claimable; a delivered
reply is never re-sent (idempotent mark_delivered survives a simulated restart); the claim
SQL exposes ONLY id/chat_id/text (no task_text/result leak).
  python3 scripts/test_reply_queue.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["AIOS_TASK_QUEUE_DB"] = str(Path(tempfile.mkdtemp(prefix="aios_replyq_")) / "q.sqlite3")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import reply_queue as rq  # noqa: E402

_FAILS = []


def check(cond, label):
    print(("PASS: " if cond else "FAIL: ") + label)
    if not cond:
        _FAILS.append(label)


def main() -> int:
    a = rq.enqueue_reply(111, "first")
    b = rq.enqueue_reply(111, "second")
    check(isinstance(a, int) and b == a + 1, "enqueue returns increasing ids")

    u = rq.claim_undelivered()
    check([r["text"] for r in u] == ["first", "second"], "claim: both undelivered, oldest first")
    keys = set(u[0].keys())
    check({"id", "chat_id", "text"} <= keys
          and not (keys & {"task_text", "result", "result_json"}),
          "claim exposes id/chat_id/text (+typed fields), never task_text/result (no leak)")

    check(rq.mark_delivered(a) is True, "mark_delivered(first) -> True")
    u2 = rq.claim_undelivered()
    check([r["text"] for r in u2] == ["second"], "after delivery, only 'second' remains undelivered")

    # idempotent: a double poll / hard restart must NOT re-send a delivered reply
    check(rq.mark_delivered(a) is False, "mark_delivered(first) again -> False (no re-send)")
    check([r["id"] for r in rq.claim_undelivered()] == [b],
          "delivered reply never reappears in the undelivered set (restart-safe)")

    # deliver the rest -> empty backlog
    check(rq.mark_delivered(b) is True, "mark_delivered(second) -> True")
    check(rq.claim_undelivered() == [], "empty backlog when all delivered")

    # --- deliver_pending (async, injected sender) ---
    import asyncio

    c = rq.enqueue_reply(222, "hello")
    d = rq.enqueue_reply(222, "world")
    sent = []

    async def good_send(chat_id, text):
        sent.append((chat_id, text))

    n = asyncio.run(rq.deliver_pending(good_send))
    check(n == 2 and sent == [(222, "hello"), (222, "world")],
          "deliver_pending sends all undelivered, oldest first")
    check(rq.claim_undelivered() == [], "deliver_pending: backlog empty after delivery")
    check(c < d, "ids monotonic")

    # a failing send leaves that reply undelivered (retry), no crash, returns 0
    rq.enqueue_reply(333, "retry-me")

    async def bad_send(chat_id, text):
        raise RuntimeError("telegram down")

    n2 = asyncio.run(rq.deliver_pending(bad_send))
    check(n2 == 0 and [r["text"] for r in rq.claim_undelivered()] == ["retry-me"],
          "deliver_pending: failed send leaves reply undelivered (retry), returns 0")

    # next poll with a working sender delivers the retried reply
    sent2 = []

    async def good_send2(chat_id, text):
        sent2.append((chat_id, text))

    n3 = asyncio.run(rq.deliver_pending(good_send2))
    check(n3 == 1 and sent2 == [(333, "retry-me")] and rq.claim_undelivered() == [],
          "deliver_pending: retried reply delivered on next pass")

    print()
    if _FAILS:
        print(f"RESULT: {len(_FAILS)} FAIL")
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
