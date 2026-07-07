#!/usr/bin/env python3
"""Safe log scanner — counts secret-shaped matches; NEVER prints matched values.

Reads log text from STDIN, applies the central redaction rule set
(bot/log_redaction.scan_counts), and prints ONLY: per-rule match counts, the
total, and an overall verdict. Matched secret values are never emitted.

Usage (owner, on the VPS — pipe a journal into it; this script never runs
journalctl or bws itself):
    journalctl -u aios-telegram-bot --no-pager | python3 scripts/aios_log_scan.py --service aios-telegram-bot
    journalctl -u aios-claude-worker --no-pager  | python3 scripts/aios_log_scan.py --service aios-claude-worker

Exit code: 0 if CLEAN (no secret-shaped matches), 1 if any match (review needed).
Note: counts may overlap across rules (one secret can match >1 shape); treat as
a leak ALARM, not an exact tally.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot.log_redaction import scan_counts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Value-free secret-shape log scanner")
    ap.add_argument("--service", default="(stdin)", help="label only; for the report line")
    args = ap.parse_args()

    data = sys.stdin.read()
    lines = data.count("\n") + (1 if data else 0)
    counts = scan_counts(data)
    total = sum(counts.values())

    print("service=" + args.service + " lines=" + str(lines))
    if counts:
        for name in sorted(counts):
            print("  " + name + ": " + str(counts[name]))
    else:
        print("  (no secret-shaped patterns found)")
    print("TOTAL_SECRET_LIKE_MATCHES: " + str(total) + "  (values NOT shown)")
    print("VERDICT: " + ("CLEAN" if total == 0 else "REVIEW — secret-shaped strings present"))
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
