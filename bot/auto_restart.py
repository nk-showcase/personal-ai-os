"""bot/auto_restart.py — service self-restart on code update.

The system is configured to auto-update on every code change: when the sync
process pulls fresh code into the working copy (git HEAD changes), the service
EXITS on its own and systemd `Restart=always` brings it back up on the new
version. No sudo and no operator involvement.

SAFETY (guard against a self-inflicted breakage): before restarting, verify that
the new code at least COMPILES (no syntax error). If it is broken, do NOT restart
— stay on the working version (the caller may warn the operator). This catches the
most common class of a bad self-edit. Boundary: a defect that only surfaces at
runtime (not a syntax error) is not caught by the compile gate; see
docs/security/threat-model.md.

Side-effect-free import: stdlib only, no secrets / telegram / network.
"""
from __future__ import annotations

import logging
import os
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("aios.auto_restart")

# Root of the working copy. bot/auto_restart.py -> parent.parent = repository.
_REPO = Path(__file__).resolve().parent.parent


def current_head() -> str | None:
    """git HEAD of the working copy (or None if git is unavailable / not a repo)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(_REPO),
            capture_output=True, text=True, timeout=10,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except Exception:  # noqa: BLE001 — no git / timeout => treat as nothing to check
        return None


def code_changed(start_head: str | None) -> bool:
    """True if the working-copy HEAD differs from the one recorded at process start."""
    if not start_head:
        return False
    h = current_head()
    return bool(h and h != start_head)


def code_compiles() -> bool:
    """True if ALL bot/*.py compile (no syntax errors).
    Pure syntax check: does NOT import the modules (so it needs no secrets/tokens
    and behaves the same in any service).

    The scratch .pyc is written to a TEMPORARY directory (cfile), not to the
    repository's __pycache__: under the unprivileged service user the repository is
    not writable, and the default py_compile would raise PermissionError -> the
    check would falsely mark ANY new code as broken and auto-restart would stall
    forever. Writing to a temp cfile avoids that."""
    ok = True
    tmp_pyc = os.path.join(tempfile.gettempdir(), f"aios-compile-check-{os.getuid()}.pyc")
    for f in sorted((_REPO / "bot").glob("*.py")):
        try:
            py_compile.compile(str(f), cfile=tmp_pyc, doraise=True)
        except py_compile.PyCompileError as e:
            log.warning("auto-restart: NOT restarting — syntax error in %s (%s)", f.name, type(e).__name__)
            ok = False
        except Exception as e:  # noqa: BLE001
            log.warning("auto-restart: compile problem in %s (%s)", f.name, type(e).__name__)
            ok = False
    try:
        os.unlink(tmp_pyc)
    except OSError:
        pass
    return ok


def imports_ok(module: str, timeout: float = 60.0) -> bool:
    """True if the new code actually IMPORTS. Catches import/early-runtime errors that
    the syntax check misses (a bad self-edit -> the bot does not load it, and stays on
    the working version). Runs `python -c "import <module>"` in a subprocess with the
    same interpreter (venv) and the same environment (so secrets are available just as
    they are to the service itself). Any nonzero exit / crash -> False."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"], cwd=str(_REPO),
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            log.warning("auto-restart: NOT restarting — new code does not import (%s)", module)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("auto-restart: smoke-import %s failed (%s) — not restarting", module, type(e).__name__)
        return False


def should_restart(start_head: str | None, *, smoke_module: str | None = None) -> bool:
    """True ONLY if the code changed, COMPILES and (if smoke_module is given) IMPORTS.
    Otherwise False (stay on the working version). smoke_module is what to import-check
    before restarting: transport -> 'bot.main', worker -> 'bot.claude_worker'."""
    if not code_changed(start_head):
        return False
    if not code_compiles():
        return False
    if smoke_module and not imports_ok(smoke_module):
        return False
    log.info("auto-restart: code changed, compiles and imports — safe to restart")
    return True
