"""bot/sync_service.py — sync-service (auto `git pull --ff-only`).

Runnable module: ``python -m bot.sync_service``. A long-lived service
(systemd ``Type=simple``, ``Restart=always``). Periodically and safely updates the
code from GitHub: ``git fetch`` + ``git pull --ff-only``.

Design (see docs/sync/auto-sync-rules.md):
  * ff-only ONLY — never overwrites local state, never does a destructive reset. If a
    fast-forward is impossible (history diverged), it skips (and logs), it does NOT crash.
  * Polling implementation: zero inbound ports, zero webhook secrets (a webhook is a
    future optimization, not the basis of the coding flow). Polls every ``AIOS_SYNC_POLL_S`` s.
  * Fail-safe: any iteration error is logged ONLY by its class name, and the loop
    continues — the service never crashes because of a single failed sync.
  * Secret values are NEVER logged: on startup it installs ``install_log_redaction()``;
    git output is not printed verbatim, only short value-free summaries.
  * Side-effect-free import: ``import bot.sync_service`` runs nothing, reads no env
    secrets, and does not touch git.
  * When rules change (CLAUDE.md / claude/skills / claude/hooks) after a pull, it logs a
    warning and writes a sentinel file; restarting claude-worker is outside this service's
    privileges (NoNewPrivileges) and is handled by a separate mechanism.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("aios.sync")

# Repository: defaults to the package's parent (reliable regardless of cwd).
REPO_DIR = os.getenv("AIOS_REPO_DIR") or str(Path(__file__).resolve().parent.parent)
POLL_INTERVAL_S = float(os.getenv("AIOS_SYNC_POLL_S", "60"))
GIT = os.getenv("AIOS_GIT_BIN", "git")

# Paths whose change means claude-worker must restart to pick up the current rules
# (docs/sync/auto-sync-rules.md, section "After an update").
_RULE_PREFIXES = ("CLAUDE.md", "claude/skills", "claude/hooks")
_RULES_SENTINEL = os.getenv(
    "AIOS_RULES_CHANGED_SENTINEL",
    os.path.join(os.path.expanduser("~"), ".ai-os", "state", "rules-changed"),
)


def _git(args: List[str], cwd: Optional[str] = None, timeout: float = 120.0):
    """Run git. Returns (returncode, stdout, stderr).

    Does NOT raise on a non-zero return code (it returns the code); an exception is possible
    only on a timeout or an inability to start the process — the caller handles that.

    umask 0022 for the duration of the git operation: sync_once is called FROM claude-worker,
    which runs with a group-sharing umask (UMask=0007) for the shared queue. Under that mask,
    `git pull` created files as 0660 (group-only), and the transport process — which runs as a
    different user not in that group — could not read them. Code files must be world-readable
    (by policy the repository contains no secrets), so the git operation runs under 0022.
    """
    prev_umask = os.umask(0o022)
    try:
        proc = subprocess.run(
            [GIT, *args],
            cwd=cwd or REPO_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        os.umask(prev_umask)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def current_branch(cwd: Optional[str] = None) -> str:
    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return out if rc == 0 else ""


def _head_rev(cwd: Optional[str] = None) -> str:
    rc, out, _ = _git(["rev-parse", "HEAD"], cwd=cwd)
    return out if rc == 0 else ""


def _paths_touch_rules(paths: List[str]) -> bool:
    """Pure logic: does the list of paths touch rule files (CLAUDE.md/skills/hooks)?
    Extracted separately so it can be tested without git."""
    for raw in paths:
        line = raw.strip()
        if not line:
            continue
        if any(line == p or line.startswith(p + "/") for p in _RULE_PREFIXES):
            return True
    return False


def _changed_rule_files(old_rev: str, new_rev: str, cwd: Optional[str] = None) -> bool:
    if not old_rev or not new_rev or old_rev == new_rev:
        return False
    rc, out, _ = _git(["diff", "--name-only", old_rev, new_rev], cwd=cwd)
    if rc != 0 or not out:
        return False
    return _paths_touch_rules(out.splitlines())


def _write_rules_sentinel() -> None:
    try:
        p = Path(_RULES_SENTINEL)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("rules changed; claude-worker should restart\n", encoding="utf-8")
    except Exception as exc:  # never crash because of a sentinel write
        log.warning("rules sentinel write failed (class=%s)", type(exc).__name__)


def sync_once(cwd: Optional[str] = None) -> dict:
    """One iteration: fetch + ff-only pull. Returns a value-free summary.

    Never raises: any git failure (non-zero code, timeout, missing git binary) is reflected
    in the summary (``error`` = code / class name), and the loop stays alive.
    """
    summary = {"fetched": False, "pulled": False, "rules_changed": False, "error": None}
    try:
        branch = current_branch(cwd)
        if not branch or branch == "HEAD":
            # Detached HEAD / not a repository — an ff-only pull is meaningless, so skip.
            summary["error"] = "no-branch"
            return summary

        rc, _, _ = _git(["fetch", "--quiet"], cwd=cwd)
        summary["fetched"] = rc == 0
        if rc != 0:
            summary["error"] = "fetch-failed"
            return summary

        before = _head_rev(cwd)
        rc, _, _ = _git(["pull", "--ff-only", "--quiet"], cwd=cwd)
        if rc != 0:
            # ff impossible (divergence) or another reason — a safe no-op.
            summary["error"] = "pull-ff-only-skipped"
            return summary
        after = _head_rev(cwd)
        summary["pulled"] = bool(after and before != after)
        if summary["pulled"]:
            summary["rules_changed"] = _changed_rule_files(before, after, cwd)
            if summary["rules_changed"]:
                _write_rules_sentinel()
    except Exception as exc:  # truly never raise (git missing / timeout / etc.)
        summary["error"] = type(exc).__name__
    return summary


def run_loop(
    *,
    max_iterations: Optional[int] = None,
    poll_interval: float = POLL_INTERVAL_S,
    sleep: Callable[[float], None] = time.sleep,
    sync_fn: Callable[..., dict] = sync_once,
    cwd: Optional[str] = None,
) -> dict:
    """Polling loop. ``max_iterations=None`` -> forever (real mode).
    Tests pass a finite count + fake ``sleep``/``sync_fn`` so they neither loop forever nor
    invoke real git."""
    iterations = 0
    pulls = 0
    while max_iterations is None or iterations < max_iterations:
        try:
            res = sync_fn(cwd=cwd) or {}
            if res.get("pulled"):
                pulls += 1
                if res.get("rules_changed"):
                    log.warning("rules changed after pull; claude-worker restart recommended")
                else:
                    log.info("code updated (ff-only pull)")
        except Exception as exc:  # the loop must NOT die
            log.warning("sync iteration failed (class=%s; loop continues)", type(exc).__name__)
        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            sleep(float(poll_interval))
    return {"iterations": iterations, "pulls": pulls}


def main() -> None:  # pragma: no cover — real run (systemd), not in tests
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    from .log_redaction import install_log_redaction
    install_log_redaction()
    log.info("sync-service starting (repo=%s, poll=%ss, ff-only)", REPO_DIR, POLL_INTERVAL_S)
    run_loop()


if __name__ == "__main__":
    main()
