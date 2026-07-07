#!/usr/bin/env python3
"""Value-free secret scanner — the trust gate for skill propagation (scripts/sync-skills-to-repo.sh).

Scans the files given (as args or via --list <file>) for secret-shaped content and prints one
"<path>\t<rule[,rule]>" line PER offending file (NEVER the matched value). Exit 1 if any file
matched, 0 if clean. Runs under the Mac's system python (re + stdlib only — no repo deps).

Opt-outs for legitimate examples (a skill that documents a token shape):
  * a line containing the literal pragma  allowlist secret  is skipped, OR
  * a relpath listed in --allow <file> (one path per line, # comments ok) is skipped entirely.
"""
import os
import re
import sys

ALLOW_PRAGMA = "allowlist secret"

RULES = [
    ("private_key_header", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("age_secret_key", re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{40,}")),
    ("github_token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,}")),
    ("gitlab_pat", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{20,}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("secret_assignment", re.compile(
        r"""(?i)(?:api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token|private[_-]?key|client[_-]?secret)\s*[:=]\s*["'][^"'\s]{12,}["']""")),
]


def scan_file(path):
    hits = set()
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                if ALLOW_PRAGMA in line:
                    continue
                for name, rx in RULES:
                    if rx.search(line):
                        hits.add(name)
    except (IsADirectoryError, PermissionError, FileNotFoundError):
        pass
    return hits


def main(argv):
    allow = set()
    files = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--allow" and i + 1 < len(argv):
            af = argv[i + 1]
            i += 2
            if os.path.exists(af):
                allow = {l.strip() for l in open(af) if l.strip() and not l.lstrip().startswith("#")}
            continue
        if a == "--list" and i + 1 < len(argv):
            lf = argv[i + 1]
            i += 2
            files += [l.strip() for l in open(lf) if l.strip()]
            continue
        files.append(a)
        i += 1
    bad = 0
    for p in files:
        rel = os.path.relpath(p)
        if rel in allow or p in allow:
            continue
        hits = scan_file(p)
        if hits:
            print("%s\t%s" % (rel, ",".join(sorted(hits))))
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
