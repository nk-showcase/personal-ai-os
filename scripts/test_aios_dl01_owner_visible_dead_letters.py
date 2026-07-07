#!/usr/bin/env python3
"""Plain-python tests (no pytest) for DL-01 owner-visible dead-letter
path (``bot.aios_dead_letters`` + ``bot.aios_dead_letters_handler``).

SAFE: temp DBs only.  SYNTHETIC payloads.  NO live Telegram (the
handler is exercised with a hand-rolled fake update object whose
reply_text is a recording AsyncMock-equivalent).  NO live Notion.
NO production-DB writes.  NO secret values used.

Covers (49 Req):

  Helper: summarize_dead_letters (Req 1-15):
    1. empty queue -> all zeros
    2. seeded with dead WRITE row -> dead_total >= 1
    3. seeded with multiple dead WRITE rows of different kinds ->
       by_kind breakdown matches
    4. expired WRITE row counted in expired_total (defensive bucket)
    5. dead READ row is NOT counted (write-only summary)
    6. NULL error_class -> bucket _NO_ERROR_CLASS_TAG
    7. by_error_class breakdown matches
    8. by_age_bucket: lt_1h / lt_24h / older buckets correct
    9. attempts_max / attempts_total correct
   10. summary keys are exactly the documented set
   11. summary values are ints (not floats / not None)
   12. helper does NOT mutate any row (count before == count after)
   13. helper does NOT include payload values in any field
   14. helper does NOT include correlation_id values in any field
   15. helper does NOT include account names / activity strings

  Helper: format_summary_owner_text (Req 16-22):
   16. empty summary -> the documented empty-queue message
   17. non-empty summary -> string contains "dead:" / "expired:" /
       a per-kind breakdown header
   18. format output does NOT contain payload values
   19. format output does NOT contain correlation_id values
   20. format output does NOT contain account names / activity
       strings
   21. format output does NOT contain raw Notion ids / source_notion_id
   22. format output does NOT contain numeric payload values

  Helper: check_dead_letters_on_startup (Req 23-26):
   23. empty queue -> returns 0
   24. seeded with dead WRITE rows -> returns positive int
   25. does NOT mutate any row
   26. does NOT call any external service (verified by source scan
       at Req 39-44)

  Handler: cmd_aios_dead_letters (Req 27-34):
   27. owner -> reply_text called exactly once
   28. owner -> reply_text payload contains "Dead-letter" (one of
       the summary markers)
   29. non-owner -> reply_text NOT called (silent no-op)
   30. missing effective_user -> reply_text NOT called
   31. missing update.message -> handler does not raise (early exit)
   32. handler does NOT mutate queue rows (count before == after)
   33. handler imports only stdlib + telegram + bot.config + bot.
       aios_dead_letters (source scan)
   34. OWNER_COMMAND_NAME == "aios_dead_letters" (matches what
       main.py registers)

  Source safety / inertness (Req 35-46):
   35. bot/aios_dead_letters.py source does NOT contain NOTION_API_KEY
   36. does NOT import httpx / requests / notion_client
   37. does NOT import any cut-domain store (e.g. bot.domain_d_storage)
       nor any domain_a*/domain_d* module
   38. does NOT call subprocess / systemctl
   39. does NOT mention forbidden flag flips (write_mode /
       read_source / shadow_compare / legacy_counter_tracking /
       worker_route)
   40. bot/aios_dead_letters_handler.py source does NOT contain
       NOTION_API_KEY
   41. handler source does NOT import any cut-domain store
       (e.g. bot.domain_d_storage) nor any domain_a*/domain_d* module
   42. handler source does NOT call subprocess / systemctl
   43. handler source does NOT mutate (no mark_done / mark_failed_
       or_dead / mark_dead / DELETE / UPDATE / set_*)
   44. helper source does NOT output payload-shape strings
       (no "payload_json" / "result_json" in user-facing output)
   45. bot/main.py registers the DL-01 command (source scan)
   46. bot/main.py uses OWNER_COMMAND_NAME (not a hard-coded
       string that could drift from the helper)

  Inertness invariants (Req 47-49):
   47. the DL-01 helper is read-only over app config -- its source
       never imports or writes app_config (no routing/feature flip)
   48. integrations-worker is NOT started by importing the helper
       or handler (source scan)
   49. test does NOT contact any external service (the test
       module itself only imports stdlib + bot modules)

Run: PYTHONPATH=<repo> python3 scripts/test_aios_dl01_owner_visible_dead_letters.py
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))

    # Dummy env so transitive bot.config import (via handler module)
    # succeeds without a real token.
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "d")
    os.environ.setdefault("TELEGRAM_OWNER_ID", "12345")
    os.environ.setdefault("NOTION_API_KEY", "d")

    failed: list = []

    def check(name: str, cond: bool) -> None:
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    tmp = tempfile.mkdtemp(prefix="aios_dl01_")
    try:
        from bot import aios_dead_letters as DL
        from bot import aios_dead_letters_handler as DH
        from bot import aios_request_queue as RQ
        from bot import aios_storage as S

        # Helper to seed a queue row at a chosen status.
        def _seed(db, *, kind, status, error_class=None,
                  attempts=0, resolved_offset=0, created_offset=0,
                  cid="seed_default", account="X"):
            now = 1700000000.0
            with S.connect(db=db) as conn:
                conn.execute(
                    "INSERT INTO request_queue "
                    "(correlation_id, kind, account, status, "
                    " payload_json, attempts, created_ts, "
                    " resolved_ts, error_class) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (cid, kind, account, status,
                     '{"x":1}', attempts,
                     now + created_offset,
                     now + resolved_offset,
                     error_class),
                )

        # ----------------------------------------------------------
        # Req 1: empty queue -> all zeros
        # ----------------------------------------------------------
        scen_1 = str(Path(tmp) / "scen_1.sqlite3")
        S.init_db(db=scen_1)
        sum_1 = DL.summarize_dead_letters(
            now_fn=lambda: 1700000000.0, db=scen_1,
        )
        check("Req 1a: empty queue -> dead_total=0",
              sum_1["dead_total"] == 0)
        check("Req 1b: empty queue -> expired_total=0",
              sum_1["expired_total"] == 0)
        check("Req 1c: empty queue -> by_kind={}",
              sum_1["by_kind"] == {})
        check("Req 1d: empty queue -> by_error_class={}",
              sum_1["by_error_class"] == {})
        check("Req 1e: empty queue -> attempts_max=0",
              sum_1["attempts_max"] == 0)
        check("Req 1f: empty queue -> all age buckets 0",
              sum_1["by_age_bucket"] == {"lt_1h": 0, "lt_24h": 0,
                                         "older": 0})

        # ----------------------------------------------------------
        # Req 2: seeded dead WRITE row -> dead_total >= 1
        # ----------------------------------------------------------
        scen_2 = str(Path(tmp) / "scen_2.sqlite3")
        S.init_db(db=scen_2)
        _seed(scen_2, kind="store_write", status="dead",
              error_class="HTTPStatusError", attempts=3,
              resolved_offset=10, cid="dead_1")
        sum_2 = DL.summarize_dead_letters(
            now_fn=lambda: 1700000100.0, db=scen_2,
        )
        check("Req 2: dead WRITE -> dead_total >= 1",
              sum_2["dead_total"] >= 1)

        # ----------------------------------------------------------
        # Req 3: multiple kinds breakdown
        # ----------------------------------------------------------
        scen_3 = str(Path(tmp) / "scen_3.sqlite3")
        S.init_db(db=scen_3)
        _seed(scen_3, kind="store_write", status="dead",
              cid="d_3a", attempts=10)
        _seed(scen_3, kind="todoist_write", status="dead",
              cid="d_3b", attempts=3)
        _seed(scen_3, kind="store_view", status="dead",
              cid="d_3c", attempts=5)
        _seed(scen_3, kind="store_write", status="dead",
              cid="d_3d", attempts=2)
        sum_3 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_3,
        )
        check("Req 3a: by_kind store_write == 2",
              sum_3["by_kind"].get("store_write") == 2)
        check("Req 3b: by_kind todoist_write == 1",
              sum_3["by_kind"].get("todoist_write") == 1)
        check("Req 3c: by_kind store_view == 1",
              sum_3["by_kind"].get("store_view") == 1)
        check("Req 3d: dead_total == 4",
              sum_3["dead_total"] == 4)

        # ----------------------------------------------------------
        # Req 4: expired WRITE row in expired_total (defensive)
        # ----------------------------------------------------------
        scen_4 = str(Path(tmp) / "scen_4.sqlite3")
        S.init_db(db=scen_4)
        _seed(scen_4, kind="store_write", status="expired",
              cid="exp_4", attempts=1)
        sum_4 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_4,
        )
        check("Req 4: expired WRITE counted in expired_total",
              sum_4["expired_total"] == 1)

        # ----------------------------------------------------------
        # Req 5: dead READ row NOT counted (write-only)
        # ----------------------------------------------------------
        scen_5 = str(Path(tmp) / "scen_5.sqlite3")
        S.init_db(db=scen_5)
        _seed(scen_5, kind="store_read", status="dead",
              cid="read_d_5", attempts=1)
        _seed(scen_5, kind="store_write", status="dead",
              cid="wr_d_5", attempts=1)
        sum_5 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_5,
        )
        check("Req 5a: dead READ row NOT counted (dead_total only WRITE)",
              sum_5["dead_total"] == 1)
        check("Req 5b: by_kind does NOT contain READ kind",
              "store_read" not in sum_5["by_kind"])

        # ----------------------------------------------------------
        # Req 6: NULL error_class -> _NO_ERROR_CLASS_TAG
        # ----------------------------------------------------------
        scen_6 = str(Path(tmp) / "scen_6.sqlite3")
        S.init_db(db=scen_6)
        _seed(scen_6, kind="store_write", status="dead",
              error_class=None, cid="null_err_6")
        sum_6 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_6,
        )
        check("Req 6: NULL error_class -> _NO_ERROR_CLASS_TAG bucket",
              DL._NO_ERROR_CLASS_TAG in sum_6["by_error_class"])

        # ----------------------------------------------------------
        # Req 7: by_error_class breakdown
        # ----------------------------------------------------------
        scen_7 = str(Path(tmp) / "scen_7.sqlite3")
        S.init_db(db=scen_7)
        for i, ec in enumerate(("HTTPStatusError", "HTTPStatusError",
                                "ConnectionError")):
            _seed(scen_7, kind="store_write", status="dead",
                  error_class=ec, cid="ec_" + str(i))
        sum_7 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_7,
        )
        check("Req 7a: HTTPStatusError == 2",
              sum_7["by_error_class"].get("HTTPStatusError") == 2)
        check("Req 7b: ConnectionError == 1",
              sum_7["by_error_class"].get("ConnectionError") == 1)

        # ----------------------------------------------------------
        # Req 8: by_age_bucket
        # ----------------------------------------------------------
        scen_8 = str(Path(tmp) / "scen_8.sqlite3")
        S.init_db(db=scen_8)
        # Three rows: resolved at offsets -100s (<1h), -3700s
        # (<24h), -90000s (>24h).
        # We seed with explicit resolved_offset (relative to seeded
        # now=1700000000.0).
        _seed(scen_8, kind="store_write", status="dead",
              resolved_offset=0, cid="age_lt1h", error_class="X")
        _seed(scen_8, kind="store_write", status="dead",
              resolved_offset=-3700, cid="age_lt24h", error_class="X")
        _seed(scen_8, kind="store_write", status="dead",
              resolved_offset=-90000, cid="age_older", error_class="X")
        sum_8 = DL.summarize_dead_letters(
            now_fn=lambda: 1700000000.0 + 100, db=scen_8,
        )
        check("Req 8a: lt_1h bucket >= 1",
              sum_8["by_age_bucket"]["lt_1h"] >= 1)
        check("Req 8b: lt_24h bucket >= 1",
              sum_8["by_age_bucket"]["lt_24h"] >= 1)
        check("Req 8c: older bucket >= 1",
              sum_8["by_age_bucket"]["older"] >= 1)

        # ----------------------------------------------------------
        # Req 9: attempts_max / attempts_total
        # ----------------------------------------------------------
        scen_9 = str(Path(tmp) / "scen_9.sqlite3")
        S.init_db(db=scen_9)
        for i, a in enumerate((1, 5, 10)):
            _seed(scen_9, kind="store_write", status="dead",
                  attempts=a, cid="att_" + str(i),
                  error_class="X")
        sum_9 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_9,
        )
        check("Req 9a: attempts_max == 10",
              sum_9["attempts_max"] == 10)
        check("Req 9b: attempts_total == 16 (1+5+10)",
              sum_9["attempts_total"] == 16)

        # ----------------------------------------------------------
        # Req 10: summary keys are exactly the documented set
        # ----------------------------------------------------------
        expected_keys = {"dead_total", "expired_total", "by_kind",
                         "by_error_class", "by_age_bucket",
                         "attempts_max", "attempts_total"}
        check("Req 10: summary keys match documented set",
              set(sum_9.keys()) == expected_keys)

        # ----------------------------------------------------------
        # Req 11: summary values are ints (not floats)
        # ----------------------------------------------------------
        for k in ("dead_total", "expired_total", "attempts_max",
                  "attempts_total"):
            check("Req 11a: " + k + " is int",
                  isinstance(sum_9[k], int))
        for k in ("lt_1h", "lt_24h", "older"):
            check("Req 11b: by_age_bucket." + k + " is int",
                  isinstance(sum_9["by_age_bucket"][k], int))

        # ----------------------------------------------------------
        # Req 12: helper does NOT mutate (count before == after)
        # ----------------------------------------------------------
        scen_12 = str(Path(tmp) / "scen_12.sqlite3")
        S.init_db(db=scen_12)
        _seed(scen_12, kind="store_write", status="dead",
              cid="nomutate_12", attempts=3,
              error_class="HTTPStatusError")
        with S.connect(db=scen_12) as conn:
            n_before = int(conn.execute(
                "SELECT COUNT(*) FROM request_queue"
            ).fetchone()[0])
        # Call helper twice.
        DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_12,
        )
        DL.summarize_dead_letters(
            now_fn=lambda: 1700100100.0, db=scen_12,
        )
        with S.connect(db=scen_12) as conn:
            n_after = int(conn.execute(
                "SELECT COUNT(*) FROM request_queue"
            ).fetchone()[0])
        check("Req 12: helper does NOT mutate row count",
              n_before == n_after)

        # ----------------------------------------------------------
        # Req 13-15: helper output does NOT include payload/cid/
        # account/activity values.
        # ----------------------------------------------------------
        scen_13 = str(Path(tmp) / "scen_13.sqlite3")
        S.init_db(db=scen_13)
        # Use very distinctive synthetic values that would be easy
        # to spot in output: cid "DISTINCTIVE_CORRELATION_XXX",
        # account "DISTINCTIVE_ACCOUNT_YYY", activity in payload
        # "DISTINCTIVE_ACTIVITY_ZZZ".
        with S.connect(db=scen_13) as conn:
            conn.execute(
                "INSERT INTO request_queue "
                "(correlation_id, kind, account, status, "
                " payload_json, attempts, created_ts, resolved_ts, "
                " error_class) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("DISTINCTIVE_CORRELATION_XXX", "store_write",
                 "DISTINCTIVE_ACCOUNT_YYY", "dead",
                 '{"activity":"DISTINCTIVE_ACTIVITY_ZZZ","amount":99999}',
                 5, 1700000000.0, 1700000050.0, "HTTPStatusError"),
            )
        sum_13 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_13,
        )
        rendered_13 = repr(sum_13)
        check("Req 13: summary repr does NOT contain payload value "
              "(99999)",
              "99999" not in rendered_13)
        check("Req 14: summary repr does NOT contain correlation_id "
              "value",
              "DISTINCTIVE_CORRELATION_XXX" not in rendered_13)
        check("Req 15a: summary repr does NOT contain account name",
              "DISTINCTIVE_ACCOUNT_YYY" not in rendered_13)
        check("Req 15b: summary repr does NOT contain activity text",
              "DISTINCTIVE_ACTIVITY_ZZZ" not in rendered_13)

        # ----------------------------------------------------------
        # Req 16-22: format_summary_owner_text
        # ----------------------------------------------------------
        text_empty = DL.format_summary_owner_text(sum_1)
        # Empty-queue message: assert on the stable ASCII marker + that it is a short
        # single-line notice, without pinning the exact prose (language-independent).
        check("Req 16: empty summary -> short 'Dead-letter' empty-queue notice",
              "Dead-letter" in text_empty and "dead:" not in text_empty
              and "\n" not in text_empty.strip())
        text_13 = DL.format_summary_owner_text(sum_13)
        check("Req 17a: non-empty text contains 'dead:'",
              "dead:" in text_13)
        check("Req 17b: non-empty text contains 'expired:'",
              "expired:" in text_13)
        # The per-kind breakdown lists the kind name; assert the kind appears (the header
        # prose is language-dependent, the kind identifier is not).
        check("Req 17c: non-empty text contains the per-kind breakdown",
              "store_write" in text_13)
        check("Req 18: format output does NOT contain payload value "
              "99999", "99999" not in text_13)
        check("Req 19: format output does NOT contain correlation_id "
              "value",
              "DISTINCTIVE_CORRELATION_XXX" not in text_13)
        check("Req 20a: format output does NOT contain account name",
              "DISTINCTIVE_ACCOUNT_YYY" not in text_13)
        check("Req 20b: format output does NOT contain activity text",
              "DISTINCTIVE_ACTIVITY_ZZZ" not in text_13)
        # Req 21: source_notion_id absence -- we did NOT seed one;
        # verify the literal string doesn't appear.
        check("Req 21: format output does NOT contain 'source_notion_id'",
              "source_notion_id" not in text_13)
        # Req 22: a numeric payload value never reaches the owner-facing text.
        scen_22 = str(Path(tmp) / "scen_22.sqlite3")
        S.init_db(db=scen_22)
        with S.connect(db=scen_22) as conn:
            conn.execute(
                "INSERT INTO request_queue "
                "(correlation_id, kind, account, status, "
                " payload_json, attempts, created_ts, resolved_ts, "
                " error_class) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("wd_22", "store_write", "AcctW", "dead",
                 '{"payload_value":-77777,"snapshot_value":77777}',
                 5, 1700000000.0, 1700000050.0, "HTTPStatusError"),
            )
        sum_22 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_22,
        )
        text_22 = DL.format_summary_owner_text(sum_22)
        check("Req 22: format output does NOT contain payload value 77777",
              "77777" not in text_22)

        # ----------------------------------------------------------
        # Req 23-26: check_dead_letters_on_startup
        # ----------------------------------------------------------
        check("Req 23: empty queue -> startup count 0",
              DL.check_dead_letters_on_startup(
                  db=scen_1, now_fn=lambda: 1700000000.0) == 0)
        check("Req 24: seeded -> startup count > 0",
              DL.check_dead_letters_on_startup(
                  db=scen_3, now_fn=lambda: 1700100000.0) > 0)
        # Req 25: does NOT mutate
        with S.connect(db=scen_3) as conn:
            n_25_before = int(conn.execute(
                "SELECT COUNT(*) FROM request_queue"
            ).fetchone()[0])
        DL.check_dead_letters_on_startup(
            db=scen_3, now_fn=lambda: 1700100100.0,
        )
        with S.connect(db=scen_3) as conn:
            n_25_after = int(conn.execute(
                "SELECT COUNT(*) FROM request_queue"
            ).fetchone()[0])
        check("Req 25: check_dead_letters_on_startup does NOT mutate",
              n_25_before == n_25_after)

        # ----------------------------------------------------------
        # Req 27-34: handler cmd_aios_dead_letters
        # ----------------------------------------------------------
        # Build fake update/context with recording reply_text.
        class _FakeUser:
            def __init__(self, uid):
                self.id = uid

        class _FakeMessage:
            def __init__(self):
                self.replies = []

            async def reply_text(self, text, *args, **kwargs):
                self.replies.append(text)

        class _FakeUpdate:
            def __init__(self, user_id=None, has_message=True):
                self.effective_user = (_FakeUser(user_id)
                                       if user_id is not None
                                       else None)
                self.message = (_FakeMessage()
                                if has_message else None)

        class _FakeContext:
            pass

        # Monkeypatch handler's DL helper to use a temp DB so the
        # call doesn't touch prod.  The handler currently calls
        # summarize_dead_letters() with no db= override; we
        # temporarily swap the symbol on the DL module.
        original_summarize = DL.summarize_dead_letters

        def _patched_summarize(**kw):
            # Review-fix: setdefault honours an explicit db= kwarg
            # if a future handler edit starts passing one (the old
            # lambda would have raised "got multiple values for
            # keyword argument db").  Defaults to scen_2 so the
            # current handler (zero kwargs) hits our temp DB.
            kw.setdefault("db", scen_2)
            return original_summarize(**kw)
        DL.summarize_dead_letters = _patched_summarize
        try:
            # Owner case
            from bot import aios_dead_letters_handler as H
            owner_id = int(os.environ["TELEGRAM_OWNER_ID"])
            upd_owner = _FakeUpdate(user_id=owner_id, has_message=True)
            asyncio.run(H.cmd_aios_dead_letters(upd_owner, _FakeContext()))
            check("Req 27: owner -> reply_text called exactly once",
                  len(upd_owner.message.replies) == 1)
            check("Req 28: owner reply contains 'Dead-letter'",
                  "Dead-letter" in upd_owner.message.replies[0])

            # Non-owner case
            upd_other = _FakeUpdate(user_id=99999, has_message=True)
            asyncio.run(H.cmd_aios_dead_letters(upd_other, _FakeContext()))
            check("Req 29: non-owner -> reply_text NOT called "
                  "(silent no-op)",
                  len(upd_other.message.replies) == 0)

            # Missing effective_user
            upd_no_user = _FakeUpdate(user_id=None, has_message=True)
            asyncio.run(H.cmd_aios_dead_letters(upd_no_user, _FakeContext()))
            check("Req 30: missing effective_user -> reply_text NOT called",
                  len(upd_no_user.message.replies) == 0)

            # Missing message (defensive)
            upd_no_msg = _FakeUpdate(user_id=owner_id, has_message=False)
            raised_31 = False
            try:
                asyncio.run(H.cmd_aios_dead_letters(
                    upd_no_msg, _FakeContext()))
            except Exception:
                raised_31 = True
            check("Req 31: missing message -> handler does NOT raise "
                  "(graceful early exit)", not raised_31)

            # Req 32: handler does NOT mutate scen_2 rows
            with S.connect(db=scen_2) as conn:
                n_32_before = int(conn.execute(
                    "SELECT COUNT(*) FROM request_queue"
                ).fetchone()[0])
            upd_32 = _FakeUpdate(user_id=owner_id, has_message=True)
            asyncio.run(H.cmd_aios_dead_letters(upd_32, _FakeContext()))
            with S.connect(db=scen_2) as conn:
                n_32_after = int(conn.execute(
                    "SELECT COUNT(*) FROM request_queue"
                ).fetchone()[0])
            check("Req 32: handler does NOT mutate row count "
                  "(view-only)", n_32_before == n_32_after)
        finally:
            DL.summarize_dead_letters = original_summarize  # restore

        # Req 33: handler imports only stdlib + telegram + bot.config
        # + bot.aios_dead_letters
        h_src = (repo_root / "bot"
                 / "aios_dead_letters_handler.py").read_text(
                     encoding="utf-8")
        allowed_imports = (
            "from .config import TELEGRAM_OWNER_ID",
            "from . import aios_dead_letters as DL",
            "from telegram import Update",
            "from telegram.ext import ContextTypes",
            "import logging",
        )
        forbidden_imports = (
            "import httpx", "from httpx",
            "import requests", "from requests",
            "notion_client",
            "from . import domain_d_storage",
            "import subprocess",
            "systemctl",
        )
        check("Req 33: handler source contains expected imports",
              all(s in h_src for s in allowed_imports))
        check("Req 33b: handler source does NOT contain forbidden imports",
              not any(s in h_src for s in forbidden_imports))

        # Req 34: OWNER_COMMAND_NAME matches what main.py registers
        check("Req 34: OWNER_COMMAND_NAME == 'aios_dead_letters'",
              DH.OWNER_COMMAND_NAME == "aios_dead_letters"
              and DL.OWNER_COMMAND_NAME == "aios_dead_letters")

        # ----------------------------------------------------------
        # Req 35-46: source safety / inertness
        # ----------------------------------------------------------
        dl_src = (repo_root / "bot"
                  / "aios_dead_letters.py").read_text(encoding="utf-8")
        check("Req 35: helper source does NOT reference NOTION_API_KEY",
              "NOTION_API_KEY" not in dl_src)
        check("Req 36a: helper source does NOT import httpx",
              "import httpx" not in dl_src
              and "from httpx" not in dl_src)
        check("Req 36b: helper source does NOT import requests",
              "import requests" not in dl_src
              and "from requests" not in dl_src)
        check("Req 36c: helper source does NOT import notion_client",
              "notion_client" not in dl_src)
        check("Req 37: helper source does NOT import any cut-domain store "
              "(no domain_a*/domain_d* module)",
              "from . import domain_d_storage" not in dl_src
              and "from .domain_d_storage" not in dl_src
              and re.search(r"import\s+\w*(domain_a|domain_d)\w*", dl_src) is None)
        check("Req 38a: helper source does NOT contain 'subprocess'",
              "subprocess" not in dl_src)
        check("Req 38b: helper source does NOT contain 'systemctl'",
              "systemctl" not in dl_src)
        check("Req 39: helper source does NOT mention forbidden flag "
              "names",
              "write_mode" not in dl_src
              and "read_source" not in dl_src
              and "shadow_compare" not in dl_src
              and "legacy_counter_tracking" not in dl_src
              and "worker_route" not in dl_src)
        check("Req 40: handler source does NOT reference NOTION_API_KEY",
              "NOTION_API_KEY" not in h_src)
        check("Req 41: handler source does NOT import any cut-domain store "
              "(no domain_a*/domain_d* module)",
              "from . import domain_d_storage" not in h_src
              and "from .domain_d_storage" not in h_src
              and re.search(r"import\s+\w*(domain_a|domain_d)\w*", h_src) is None)
        check("Req 42a: handler source does NOT contain 'subprocess'",
              "subprocess" not in h_src)
        check("Req 42b: handler source does NOT contain 'systemctl'",
              "systemctl" not in h_src)
        # Req 43: handler does NOT call mutation functions
        check("Req 43a: handler source does NOT call mark_done",
              "mark_done" not in h_src
              and "mark_done" not in dl_src)
        check("Req 43b: handler source does NOT call mark_failed_or_dead",
              "mark_failed_or_dead" not in h_src
              and "mark_failed_or_dead" not in dl_src)
        check("Req 43c: helper source does NOT issue UPDATE / DELETE",
              " UPDATE " not in dl_src.upper()
              and "UPDATE request_queue" not in dl_src
              and " DELETE " not in dl_src.upper()
              and "DELETE FROM request_queue" not in dl_src)
        # Req 44: format output does NOT contain payload-shape strings
        check("Req 44: helper source does NOT output 'payload_json' / "
              "'result_json' in user-facing text",
              "payload_json" not in dl_src
              and "result_json" not in dl_src)

        # Req 45: main.py registers the DL-01 command
        main_src = (repo_root / "bot" / "main.py").read_text(
            encoding="utf-8")
        check("Req 45a: main.py imports aios_dead_letters_handler",
              "aios_dead_letters_handler" in main_src)
        check("Req 45b: main.py registers CommandHandler for the "
              "DL-01 command",
              "CommandHandler" in main_src
              and "OWNER_COMMAND_NAME" in main_src
              and "cmd_aios_dead_letters" in main_src)
        # Req 46: main.py uses OWNER_COMMAND_NAME constant (not
        # hard-coded string)
        check("Req 46: main.py uses OWNER_COMMAND_NAME (no drift risk)",
              "_dl_handler.OWNER_COMMAND_NAME" in main_src
              or "OWNER_COMMAND_NAME" in main_src.replace(
                  '"aios_dead_letters"', ""))

        # ----------------------------------------------------------
        # Req 47-49: inertness invariants
        # ----------------------------------------------------------
        # Req 47: the DL-01 helper is read-only over app config -- it
        # never flips a routing / feature switch.  (Retargeted from a
        # cut-domain router check to a surviving generic: the helper
        # source must not import or write app_config at all.)
        check("Req 47: helper source does NOT reach into app_config "
              "(read-only over request_queue, no config writes)",
              "app_config" not in dl_src
              and "set_config" not in dl_src
              and "aios_config" not in dl_src)
        # Req 48: integrations-worker NOT auto-started (the helper
        # source does NOT call integrations_worker.run_loop /
        # run_once)
        check("Req 48a: helper source does NOT reference "
              "integrations_worker.run_loop",
              "integrations_worker.run_loop" not in dl_src
              and "integrations_worker.run_once" not in dl_src)
        check("Req 48b: handler source does NOT reference "
              "integrations_worker.run_loop",
              "integrations_worker.run_loop" not in h_src
              and "integrations_worker.run_once" not in h_src)
        # Req 49: test module itself only imports stdlib + bot
        # modules (no live HTTP clients).  Scan IMPORT-STATEMENT
        # lines only (not the literal-strings the test uses to
        # scan OTHER files for forbidden imports).  Anchor regex
        # at start-of-line so substrings inside tuples /
        # forbidden_imports lists are excluded.
        test_src = Path(__file__).read_text(encoding="utf-8")
        bad_imports_re = re.compile(
            r"^\s*(import|from)\s+(httpx|requests|notion_client|"
            r"aiohttp|urllib\.request)\b",
            re.MULTILINE,
        )
        check("Req 49: test source has NO actual import of "
              "httpx/requests/notion_client/aiohttp/urllib.request",
              bad_imports_re.search(test_src) is None)

        # ============================================================
        # ADVERSARIAL REVIEW FIXES (Req 50-55)
        # ============================================================

        # Req 50: empty WRITE_KINDS guard -> zero-summary, no raise.
        scen_50 = str(Path(tmp) / "scen_50.sqlite3")
        S.init_db(db=scen_50)
        # Seed a dead WRITE so the SQL would normally find a row;
        # if the guard fires, the row is ignored and summary is zero.
        _seed(scen_50, kind="store_write", status="dead",
              error_class="HTTPStatusError", cid="empty_kinds")
        original_write_kinds = RQ.WRITE_KINDS
        RQ.WRITE_KINDS = ()  # type: ignore[assignment]
        try:
            sum_50 = DL.summarize_dead_letters(
                now_fn=lambda: 1700100000.0, db=scen_50,
            )
            check("Req 50a: empty WRITE_KINDS -> zero-summary "
                  "(no SQL syntax error)",
                  sum_50["dead_total"] == 0
                  and sum_50["expired_total"] == 0
                  and sum_50["by_kind"] == {}
                  and sum_50["by_error_class"] == {})
            check("Req 50b: empty WRITE_KINDS -> attempts zero",
                  sum_50["attempts_max"] == 0
                  and sum_50["attempts_total"] == 0)
            check("Req 50c: empty WRITE_KINDS -> all age buckets zero",
                  sum_50["by_age_bucket"] == {"lt_1h": 0,
                                              "lt_24h": 0,
                                              "older": 0})
        finally:
            RQ.WRITE_KINDS = original_write_kinds  # type: ignore[assignment]

        # Req 51: error_class sanitization -> non-identifier value
        # replaced with _NON_IDENTIFIER_ERROR_CLASS_TAG.  If a future
        # B-02C contributor stores str(e) (e.g. "HTTPStatusError: 401
        # account ACCT-XYZ not found"), DL-01 must NOT surface that
        # string to the owner.
        scen_51 = str(Path(tmp) / "scen_51.sqlite3")
        S.init_db(db=scen_51)
        _seed(scen_51, kind="store_write", status="dead",
              error_class="HTTPStatusError: 401 account ACCT-XYZ "
                          "not found",
              cid="rawmsg_51")
        sum_51 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_51,
        )
        text_51 = DL.format_summary_owner_text(sum_51)
        check("Req 51a: raw-message error_class replaced with "
              "_NON_IDENTIFIER_ERROR_CLASS_TAG",
              DL._NON_IDENTIFIER_ERROR_CLASS_TAG in sum_51["by_error_class"])
        check("Req 51b: format output does NOT leak 'ACCT-XYZ' from "
              "raw-message error_class",
              "ACCT-XYZ" not in text_51)
        check("Req 51c: format output does NOT leak '401' from "
              "raw-message error_class",
              "401" not in text_51)
        check("Req 51d: format output does NOT leak 'not found' "
              "from raw-message error_class",
              "not found" not in text_51)

        # Req 52: top-N cap on by_error_class breakdown so rendered
        # text stays under Telegram's 4096-char limit.
        scen_52 = str(Path(tmp) / "scen_52.sqlite3")
        S.init_db(db=scen_52)
        # Seed 30 distinct error_class identifiers (more than the
        # _ERROR_CLASS_TOP_N=20 cap).
        for i in range(30):
            _seed(scen_52, kind="store_write", status="dead",
                  error_class="ErrorClass" + str(i),
                  cid="topn_" + str(i))
        sum_52 = DL.summarize_dead_letters(
            now_fn=lambda: 1700100000.0, db=scen_52,
        )
        text_52 = DL.format_summary_owner_text(sum_52)
        check("Req 52a: by_error_class collects all 30 classes "
              "internally",
              len(sum_52["by_error_class"]) == 30)
        check("Req 52b: format output contains '(other ' marker "
              "(remaining classes rolled up)",
              "(other " in text_52)
        # Count the per-class lines in output -- must be <= TOP_N.
        per_class_lines = sum(1 for ln in text_52.split("\n")
                              if ln.startswith("  ErrorClass"))
        check("Req 52c: rendered output shows at most _ERROR_CLASS_"
              "TOP_N=20 distinct ErrorClass lines",
              per_class_lines <= DL._ERROR_CLASS_TOP_N)

        # Req 53: handler try/except -- if DL.summarize_dead_letters
        # raises, owner gets a value-free fallback reply (NOT a
        # silent no-reply).
        class _FakeUser53:
            def __init__(self, uid): self.id = uid

        class _FakeMessage53:
            def __init__(self): self.replies = []
            async def reply_text(self, text, *a, **k):
                self.replies.append(text)

        class _FakeUpdate53:
            def __init__(self):
                self.effective_user = _FakeUser53(
                    int(os.environ["TELEGRAM_OWNER_ID"]))
                self.message = _FakeMessage53()
        original_summarize_53 = DL.summarize_dead_letters

        def _raising(**kw):
            raise RuntimeError("synthetic DB lock for Req 53 test")
        DL.summarize_dead_letters = _raising
        try:
            from bot import aios_dead_letters_handler as H53
            upd_53 = _FakeUpdate53()

            class _Ctx53:
                pass
            try:
                asyncio.run(H53.cmd_aios_dead_letters(upd_53, _Ctx53()))
                no_raise_53 = True
            except Exception:
                no_raise_53 = False
            check("Req 53a: handler does NOT propagate exception when "
                  "summarize raises (try/except guards)",
                  no_raise_53)
            check("Req 53b: handler sent ONE fallback reply",
                  len(upd_53.message.replies) == 1)
            check("Req 53c: fallback text is a short value-free notice "
                  "(mentions Dead-letter, no numbers/markers)",
                  "Dead-letter" in upd_53.message.replies[0]
                  and "dead:" not in upd_53.message.replies[0])
            check("Req 53d: fallback text does NOT leak the exception "
                  "message 'synthetic DB lock'",
                  "synthetic" not in upd_53.message.replies[0]
                  and "DB lock" not in upd_53.message.replies[0])
        finally:
            DL.summarize_dead_letters = original_summarize_53

        # Req 54: caller-contract docstring exists on
        # check_dead_letters_on_startup.
        import inspect
        doc_54 = inspect.getdoc(DL.check_dead_letters_on_startup) or ""
        check("Req 54: startup helper docstring mentions CALLER "
              "CONTRACT + TELEGRAM_OWNER_ID gate",
              "CALLER CONTRACT" in doc_54
              and "TELEGRAM_OWNER_ID" in doc_54)

        # Req 55: handler source contains the value-free fallback
        # reply text AND uses log.warning class=%s pattern.
        h_src_55 = (repo_root / "bot"
                    / "aios_dead_letters_handler.py").read_text(
                        encoding="utf-8")
        check("Req 55a: handler source has a fallback reply_text path "
              "(value-free notice on summarize failure)",
              "reply_text" in h_src_55 and "except" in h_src_55)
        check("Req 55b: handler source uses log.warning class=%s "
              "(no traceback leak)",
              "log.warning" in h_src_55
              and "class=%s" in h_src_55
              and "type(" in h_src_55)
        check("Req 55c: handler source does NOT use log.exception "
              "(would leak traceback + message)",
              "log.exception" not in h_src_55)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
