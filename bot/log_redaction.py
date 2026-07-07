"""bot/log_redaction.py -- central, VPS-wide logging redaction for secrets.

Goal: make it structurally hard for ANY AI OS service (telegram-bot,
claude-worker, integrations-worker) to write tokens/secrets to
stdout/stderr/journald.

Two layered defenses, installed by :func:`install_log_redaction` from each
service entry point's logging bootstrap:

  1. raise noisy HTTP client loggers (httpx / httpcore / urllib3) to WARNING so
     their INFO "HTTP Request: GET <url>" lines -- which can carry a token in the
     URL (the Telegram leak: ``api.telegram.org/bot<TOKEN>/getMe``) -- are never
     emitted. telegram / telegram.ext are intentionally LEFT at their level
     (they do not embed the token and give useful lifecycle logs); they still
     pass through the redaction filter below.
  2. attach a :class:`RedactingFilter` to the root logger's handlers (handlers
     see all propagated records) and to the root logger, so any record that
     still contains a secret-shaped string is scrubbed before a handler writes
     it -- covers our own code, tracebacks, headers, and future libraries.

Import-light by design: only ``re`` and ``logging``. No project imports, no
secret reads -- safe to import from any service bootstrap.

The same rule set powers :func:`scan_counts`, a value-free log scanner
(counts + pattern names only) used by ``scripts/aios_log_scan.py`` to validate a
journal AFTER a restart without ever printing a matched value.
"""
from __future__ import annotations

import logging
import re

_R = "<redacted>"

# (name, compiled_pattern, replacement). Order matters (specific first).
# Replacements PRESERVE context (keep the surrounding structure; replace only
# the secret), e.g. ``api.telegram.org/bot<redacted>/getMe``,
# ``Authorization: Bearer <redacted>``, ``TELEGRAM_BOT_TOKEN=<redacted>``.
_RULES = [
    # --- Telegram ---
    ("telegram_api_url",
     re.compile(r"(api\.telegram\.org/bot)\d{6,}:[A-Za-z0-9_-]{20,}"), r"\1" + _R),
    ("telegram_bot_path",
     re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot" + _R),
    ("telegram_raw_token",
     re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), _R),
    # --- HTTP auth headers ---
    ("authorization_bearer",
     re.compile(r"(Authorization\s*:\s*Bearer\s+)[^\s\"']+", re.IGNORECASE), r"\1" + _R),
    ("bearer_token",
     re.compile(r"(\bBearer\s+)[A-Za-z0-9._\-+/=]{8,}"), r"\1" + _R),
    ("cookie_header",
     re.compile(r"((?:Set-)?Cookie\s*:\s*)\S.*", re.IGNORECASE), r"\1" + _R),
    # --- named project secret env vars: KEY=value / KEY: value ---
    ("named_secret_env",
     re.compile(
         r"((?:TELEGRAM_BOT_TOKEN|BWS_ACCESS_TOKEN|ANTHROPIC_API_KEY|"
         r"GITHUB_REPO_WRITE_TOKEN|GITHUB_TOKEN|NOTION_API_KEY|"
         r"TODOIST_API_KEY)\s*[=:]\s*)\S+",
         re.IGNORECASE), r"\1" + _R),
    # --- generic secret-ish KEY=value (any *_TOKEN/_KEY/_SECRET/_PASSWORD) ---
    ("generic_secret_env",
     re.compile(
         r"\b([A-Za-z0-9_]*(?:TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|SECRET|"
         r"PASSWORD|PASSWD)\s*[=:]\s*)\S+", re.IGNORECASE), r"\1" + _R),
    # --- lowercase query-ish key=value (explicitly requested) ---
    ("query_kv_secret",
     re.compile(
         r"\b(password|passwd|pwd|token|api[_-]?key|secret|access[_-]?token|"
         r"client[_-]?secret|refresh[_-]?token)\s*=\s*[^&\s\"']+",
         re.IGNORECASE), r"\1=" + _R),
    # --- OAuth code / tokens carried as URL query params ---
    ("oauth_query_param",
     re.compile(
         r"([?&](?:code|access_token|id_token|refresh_token|client_secret)=)"
         r"[^&\s\"']+", re.IGNORECASE), r"\1" + _R),
    # --- provider token shapes (defense in depth) ---
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), _R),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}"), _R),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}"), _R),
    ("github_fine_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"), _R),
    ("bws_access_token",
     re.compile(r"\b0\.[0-9a-f-]{30,}\.[A-Za-z0-9+/=]{20,}"), _R),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9._-]{20,}"), _R),
]


def redact(text):
    """Return ``text`` with secret-shaped substrings replaced by ``<redacted>``,
    preserving surrounding context. Non-str input is coerced to str."""
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    for _name, pat, repl in _RULES:
        text = pat.sub(repl, text)
    return text


def scan_counts(text):
    """Value-free scan: return ``{rule_name: match_count}`` for rules that match.
    NEVER returns matched values -- counts only. Counts may overlap across rules
    (a single secret can match more than one shape); treat as a leak ALARM, not
    an exact tally."""
    if not text:
        return {}
    if not isinstance(text, str):
        text = str(text)
    out = {}
    for name, pat, _repl in _RULES:
        n = len(pat.findall(text))
        if n:
            out[name] = n
    return out


class RedactingFilter(logging.Filter):
    """logging.Filter that scrubs secret-shaped strings from each record.

    Renders the record's final message (``msg % args``); if redaction changed
    anything, replaces ``record.msg`` with the scrubbed text and clears
    ``record.args`` so later formatting cannot re-introduce the raw value. Always
    returns True (scrubs, never drops)."""

    def filter(self, record):  # noqa: A003 (logging API name)
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(getattr(record, "msg", ""))
        red = redact(msg)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


# Noisy HTTP clients that log full request URLs at INFO (token-in-URL leak).
# telegram / telegram.ext are deliberately NOT lowered here (no token in their
# messages; INFO is useful) -- they still pass through RedactingFilter.
_QUIET_HTTP_LOGGERS = (
    "httpx", "httpcore", "httpcore.http11", "httpcore.connection", "urllib3",
)


def install_log_redaction(quiet_http: bool = True, extra_quiet=()):
    """Install central redaction. Idempotent; safe to call once per service main().

    * attaches a :class:`RedactingFilter` to every root-logger handler and to the
      root logger itself;
    * (default) raises httpx/httpcore/urllib3 to WARNING;
    * ``extra_quiet`` -- optional extra logger names to raise to WARNING.
    """
    root = logging.getLogger()
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter())
    if quiet_http:
        for name in _QUIET_HTTP_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
    for name in extra_quiet:
        logging.getLogger(name).setLevel(logging.WARNING)
