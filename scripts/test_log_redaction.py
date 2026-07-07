#!/usr/bin/env python3
"""Plain-python tests (no pytest) for bot/log_redaction.py (VPS-wide redaction).

Proves fake secret strings are redacted from logs and that scan_counts is
value-free. All tokens used are SYNTHETIC (format-valid, not real).
Run: PYTHONPATH=<repo> python3 scripts/test_log_redaction.py
"""
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo))
    from bot import log_redaction as LR

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    def gone(secret, text):
        return secret not in text and "<redacted>" in text

    # SYNTHETIC fakes (NOT real).
    TG = "123456789:" + "A" * 35
    URL = "https://api.telegram.org/bot" + TG + "/getMe"
    BWS = "0." + "a" * 36 + "." + "B" * 40
    ANTH = "sk-ant-" + "C" * 40
    GH = "ghp_" + "D" * 36
    JWT = "eyJ" + "E" * 40

    # Telegram
    check("telegram URL redacted, path kept",
          "api.telegram.org/bot<redacted>" in LR.redact("GET " + URL) and TG not in LR.redact(URL))
    check("telegram raw token redacted", gone(TG, LR.redact("tok " + TG)))

    # Auth headers
    check("Authorization: Bearer redacted",
          LR.redact("Authorization: Bearer " + ANTH) == "Authorization: Bearer <redacted>")
    check("bare Bearer redacted", gone("XYZ" + "Z" * 20, LR.redact("Bearer XYZ" + "Z" * 20)))
    check("Cookie header redacted", gone("sess=abc123def456", LR.redact("Cookie: sess=abc123def456")))
    check("Set-Cookie header redacted", gone("s=zzzzzzzzzz", LR.redact("Set-Cookie: s=zzzzzzzzzz; Path=/")))

    # Named project env secrets
    for key in ("TELEGRAM_BOT_TOKEN", "BWS_ACCESS_TOKEN", "ANTHROPIC_API_KEY",
                "GITHUB_REPO_WRITE_TOKEN", "GMAIL_PERSONAL_APP_PASSWORD", "NOTION_API_KEY",
                "TODOIST_API_KEY"):
        line = key + "=supersecretvalue123456"
        r = LR.redact(line)
        check(key + "=value redacted (key kept)",
              r == key + "=<redacted>" and "supersecretvalue" not in r)

    # generic + lowercase query-ish
    check("generic *_SECRET=value redacted",
          LR.redact("MY_CUSTOM_SECRET=abcdef123456") == "MY_CUSTOM_SECRET=<redacted>")
    check("lowercase token=value redacted",
          LR.redact("token=abcdef123456") == "token=<redacted>")
    check("lowercase api_key=value redacted",
          LR.redact("api_key=abcdef123456") == "api_key=<redacted>")
    check("lowercase password=value redacted",
          LR.redact("password=hunter2hunter2") == "password=<redacted>")

    # OAuth query param
    _oauth = LR.redact("https://x/cb?code=AQABsecretcode&state=1")
    check("oauth ?code= redacted",
          "AQABsecretcode" not in _oauth and "code=<redacted>" in _oauth)

    # provider shapes
    check("bws token redacted", gone(BWS, LR.redact("BWS " + BWS)))
    check("anthropic key redacted", gone(ANTH, LR.redact("key " + ANTH)))
    check("github pat redacted", gone(GH, LR.redact("pat " + GH)))
    check("jwt redacted", gone(JWT, LR.redact("jwt " + JWT)))

    # NOT over-aggressive: realistic non-secret lines unchanged
    for plain in (
        "claude-worker starting (worker_id=wkr_1, poll=5s)",
        "integrations-worker starting (applier=DEFAULT_INERT_STUB)",
        "processed task id=42 status=done in 1.3s",
        "GET https://api.notion.com/v1/databases/abc/query 200 OK",
    ):
        check("non-secret unchanged: " + plain[:32], LR.redact(plain) == plain)

    # RedactingFilter end-to-end (msg + %-args)
    for tag, fn in (("msg", lambda lg: lg.info("HTTP Request: GET " + URL)),
                    ("args", lambda lg: lg.info("HTTP Request: GET %s", URL))):
        buf = io.StringIO()
        lg = logging.getLogger("test_lr_" + tag)
        lg.handlers[:] = []
        lg.propagate = False
        h = logging.StreamHandler(buf)
        h.addFilter(LR.RedactingFilter())
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        fn(lg)
        h.flush()
        check("RedactingFilter scrubs via " + tag, gone(TG, buf.getvalue()))

    # install_log_redaction: levels + idempotency
    for n in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(n).setLevel(logging.INFO)
    LR.install_log_redaction()
    check("httpx/httpcore/urllib3 raised to WARNING",
          all(logging.getLogger(n).level == logging.WARNING
              for n in ("httpx", "httpcore", "urllib3")))
    root = logging.getLogger()
    n1 = sum(isinstance(f, LR.RedactingFilter) for f in root.filters)
    LR.install_log_redaction()
    n2 = sum(isinstance(f, LR.RedactingFilter) for f in root.filters)
    check("install idempotent (no duplicate root filter)", n1 == n2 == 1)
    check("telegram logger NOT forced to WARNING (left usable)",
          logging.getLogger("telegram").level != logging.WARNING)

    # scan_counts: detects, value-free, empty on clean
    counts = LR.scan_counts("line1 " + URL + "\nAuthorization: Bearer " + ANTH)
    check("scan_counts detects matches", sum(counts.values()) >= 2)
    check("scan_counts returns names+ints only (no values)",
          all(isinstance(k, str) and isinstance(v, int) for k, v in counts.items()))
    check("scan_counts empty on clean text",
          LR.scan_counts("nothing secret here, id=42 poll=5s") == {})

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
