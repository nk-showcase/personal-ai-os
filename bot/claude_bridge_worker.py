"""bot/claude_bridge_worker.py — worker-only bridge surface.

Extracted from bot/claude_bridge.py so the TRANSPORT process (claude_bridge.py, which imports
telegram) no longer pulls in the worker-only code that resolves the GitHub secret
(`_github_token` -> get_secret) and runs the Claude executor (`_run_claude` / `_prepare_repo`
/ `ensure_skills`). Imported only by claude-worker (`bot/claude_worker.py`) and the
laptop-side flow (`bot/pc_tasks.py`).

HARD CONSTRAINTS:
  - NO telegram import here (this module lives on the worker side of the seam).
  - Resolves secrets ONLY by NAME via get_secret (never logs/echoes a value).
  - The claude_agent_sdk import stays LAZY inside _run_claude.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .bridge_constants import (
    _WORK_DIR,
    _GIT_USER_NAME,
    _GIT_USER_EMAIL,
    _ASSISTANT_ALIAS,
    _SKILLS_DIR,
    _SKILLS_REPO,
    _load_projects,
)
from .secrets_loader import get_secret
from .claude_worker_core import _redact
from . import claude_policy

logger = logging.getLogger(__name__)


def _setup_claude_auth() -> None:
    """Explicit no-op. The Claude Code login is a local-only file (~/.claude/.credentials.json)
    provisioned OUTSIDE the application: not from env, not from a secret manager, not via Telegram.

    By design this function does NOT read any credentials env var, does NOT write a credentials
    file, and does NOT change ANTHROPIC_API_KEY. It is kept as an explicit no-op so that calls
    from claude-worker do not fail.
    """
    return None


# Claude login setup is NOT performed as an import side effect. The telegram-bot transport
# imports claude_bridge only for the Telegram glue and must not touch ~/.claude/.credentials.json
# or change ANTHROPIC_API_KEY. The claude-worker entrypoint calls setup_claude_auth() EXPLICITLY
# before starting Claude.
setup_claude_auth = _setup_claude_auth  # explicit public call; only from claude-worker


# Default to the "opus" alias so the bridge always runs the LATEST Opus snapshot
# (on the Anthropic API this currently resolves to Opus 4.8) without pinning a dated
# version. Set the BRIDGE_MODEL env var to pin a specific model if ever needed.
# Docs: https://code.claude.com/docs/en/model-config#model-aliases
_BRIDGE_MODEL = os.environ.get("BRIDGE_MODEL", "opus")
_BRIDGE_EFFORT = os.environ.get("BRIDGE_EFFORT", "max")
_BRIDGE_THINKING = os.environ.get("BRIDGE_THINKING", "adaptive")
_BRIDGE_MAX_TURNS = int(os.environ.get("BRIDGE_MAX_TURNS", "0"))


def _build_thinking_config() -> dict | None:
    """Build a ClaudeAgentOptions.thinking value from BRIDGE_THINKING env."""
    v = (_BRIDGE_THINKING or "").strip().lower()
    if v in ("off", "disabled", "false", "0", "no", ""):
        return {"type": "disabled"}
    if v in ("adaptive", "on", "true", "1", "yes"):
        return {"type": "adaptive"}
    try:
        budget = int(v)
        return {"type": "enabled", "budget_tokens": budget}
    except ValueError:
        return {"type": "adaptive"}


_OWNER_PLAIN_REPLY_HINT = (
    "\n\nADDRESSING THE OPERATOR: the person reading your final reply is NON-TECHNICAL and reads "
    "it in a Telegram chat. Write your FINAL message in the SAME language the operator is writing "
    "to you, and follow them if they switch. "
    "BE TERSE AND CONCRETE — bloated replies are unwelcome. Rules:\n"
    "- Lead with the outcome in one sentence: what now works / what you found.\n"
    "- Then ONLY the facts that change what the operator does next, as short bullets. No preamble, "
    "no restating the request, no process narration, no 'I could also…' filler, no closing summary.\n"
    "- A question or needed decision: one direct sentence at the end.\n"
    "- Never include file paths, line numbers, function/variable names, git/commit/branch/GitHub "
    "terms, or code blocks — describe changes by what the operator SEES, in everyday language.\n"
    "- Target: the whole reply readable in ~10 seconds. Cut anything that does not serve that."
    "\n\nIMPORTANT: ONLY your LAST message is delivered to the operator — intermediate narration "
    "is hidden. Put the entire human summary into the final message; never leave tool-by-tool "
    "narration (\"Now editing…\", \"Verifying…\") as your final output."
    "\n\nPROGRESS LINES (long tasks): after completing each meaningful step, emit ONE short "
    "standalone text message in the operator's language in the shape "
    "'Done: <what is ready>. Next: <what remains>.' Maximum 2 sentences, same no-tech-jargon "
    "rules (no file names, no code terms). These lines are relayed LIVE as progress updates while "
    "you keep working — write them for the operator, not for yourself."
)

# Leading trigger words that switch on design mode. Configurable via the AIOS_DESIGN_TRIGGERS
# env var (comma-separated); defaults to just "design".
_DESIGN_TRIGGERS = tuple(
    t.strip().lower() for t in (os.getenv("AIOS_DESIGN_TRIGGERS", "design")).split(",") if t.strip()
)
_DESIGN_SYSTEM_PROMPT = (
    "You are running in DESIGN MODE. Before making any code changes:\n"
    "1. Read all relevant code thoroughly using Read/Grep/Glob.\n"
    "2. Think deeply about architecture, edge cases, failure modes, "
    "and how the change interacts with existing code.\n"
    "3. Write a clear step-by-step plan of what you will change and why, "
    "as your first response back to the user.\n"
    "4. Then execute the plan methodically, verifying each step.\n"
    "Prefer correctness, clarity, and long-term maintainability over speed. "
    "Do not cut corners. If unsure, read more code before acting."
)


def _github_token() -> str:
    """GitHub token for the coding executor's git operations (clone/pull/push).

    Resolved through the safe secret loader (env -> secret manager -> disk-cache), NOT from a
    bare os.environ read: in production secrets live in the secret manager, not in env, so a
    direct env read returned empty and auto-push silently did nothing. The name is the one
    granted to claude-worker in config/secrets-map.yaml: GITHUB_REPO_WRITE_TOKEN; it falls back
    to the legacy GITHUB_TOKEN (local/dev/env). The value is NEVER logged.
    """
    from .secrets_loader import get_secret
    return (get_secret("GITHUB_REPO_WRITE_TOKEN", default="")
            or get_secret("GITHUB_TOKEN", default=""))


async def ensure_skills() -> int:
    """Clone or pull the claude-skills repo into SKILLS_DIR using GITHUB_TOKEN.

    Returns the number of skill directories available after the operation.
    Idempotent: first call clones, subsequent calls pull for fresh content.
    """
    # LANDMINE GUARD (two-way sync): on the VPS skills are delivered by the
    # ~/.claude/skills -> repo/claude/skills SYMLINK (aios-sync 60s ff-pull; the agent reads
    # them fresh per task — verified). Cloning/wiping a SEPARATE repo over that symlink would
    # SEVER the live link and strand the agent on a stale standalone clone. So if the target is
    # a symlink (the live architecture), do NOTHING but count — never clone/pull/rmtree it.
    # The clone path below survives only for a legacy standalone-clone host (non-symlink dir).
    if _SKILLS_DIR.is_symlink():
        logger.info("ensure_skills: skills dir is a symlink (repo-delivered) — no clone, count only")
        return _count_skills()

    token = _github_token()
    if not token:
        logger.warning("ensure_skills: GITHUB_TOKEN not set, skipping clone")
        return _count_skills()

    _SKILLS_DIR.parent.mkdir(parents=True, exist_ok=True)
    git_dir = _SKILLS_DIR / ".git"

    if git_dir.exists():
        rc, out, err = await _sh(
            ["git", *_git_cred_args(), "-C", str(_SKILLS_DIR), "pull", "--quiet"], env=_git_env())
        if rc != 0:
            logger.warning("ensure_skills: pull failed: %s", err.strip())
        else:
            logger.info("ensure_skills: pulled latest")
    else:
        if _SKILLS_DIR.exists():
            try:
                shutil.rmtree(_SKILLS_DIR)
            except Exception as e:
                logger.warning("ensure_skills: cannot wipe existing dir: %s", e)
        rc, out, err = await _sh(
            ["git", *_git_cred_args(), "clone", "--depth", "1", _SKILLS_REPO, str(_SKILLS_DIR)],
            env=_git_env())
        if rc != 0:
            logger.error("ensure_skills: clone failed: %s", err.strip())
            return _count_skills()
        logger.info("ensure_skills: initial clone done")

    return _count_skills()


def _count_skills() -> int:
    if not _SKILLS_DIR.exists():
        return 0
    try:
        return sum(
            1 for p in _SKILLS_DIR.iterdir()
            if p.is_dir() and p.name != ".git" and (p / "SKILL.md").exists()
        )
    except Exception:
        return 0


def _git_cred_args() -> list[str]:
    """Args inserted right after 'git' so auth uses the token from env (AIOS_GIT_TOKEN) via an
    inline credential helper. The token is NEVER embedded in a remote URL on disk (.git/config)
    nor in a command's argv (process list) — only the helper stub is on argv, the value comes
    from env at runtime. Empty list when no token is configured."""
    if not _github_token():
        return []
    helper = ("!f() { test \"$1\" = get && "
              "printf 'username=x-access-token\\npassword=%s\\n' \"$AIOS_GIT_TOKEN\"; }; f")
    # clear any inherited helper first, then set ours (per-command, not persisted).
    return ["-c", "credential.helper=", "-c", "credential.helper=" + helper]


def _git_env() -> dict:
    """Env for git subprocesses carrying the token by NAME only (read by the inline helper).
    No token in argv, no token on disk."""
    token = _github_token()
    if not token:
        return dict(os.environ)
    return {**os.environ, "AIOS_GIT_TOKEN": token, "GIT_TERMINAL_PROMPT": "0"}


async def _sh(cmd: list[str], cwd: str | None = None, env: dict | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def _prepare_repo(alias: str, github_url: str, branch: str) -> Path:
    _WORK_DIR.mkdir(parents=True, exist_ok=True)
    target = _WORK_DIR / alias
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    rc, _, err = await _sh(
        ["git", *_git_cred_args(), "clone", "--depth", "1", "--branch", branch, github_url, str(target)],
        env=_git_env(),
    )
    if rc != 0:
        raise RuntimeError(f"clone failed: {err.strip()}")
    await _sh(["git", "config", "user.name", _GIT_USER_NAME], cwd=str(target))
    await _sh(["git", "config", "user.email", _GIT_USER_EMAIL], cwd=str(target))
    return target


# --- Leak-safe commit messages + run-log repr --------------------------------
# Commit messages for bot-driven pushes carry NO task text. The repo diff is the
# operator's own project code; the dictated task prose (which can contain anything)
# must never land in commit history or the log viewer. These are fixed string
# constants — no interpolation of `task` — enforced by a spec guard.
_AUTO_PUSH_COMMIT_MSG = "bridge: auto-commit"
_COMMIT_PUSH_COMMIT_MSG = "claude: bridge commit"


def _task_log_repr(task: str, *, limit: int = 2000) -> str:
    """Single-line, SECRET-REDACTED representation of a task for the run log.

    Block -1A: the shared `_redact` scrubber runs BEFORE truncation so a dictated
    token can't slip through inside the first `limit` chars; newlines collapse to
    a ↵ marker so the entry stays one line in the log viewer. This is the
    only path that puts task content into a log — it is never the raw text.
    """
    redacted = _redact(task or "").replace("\n", " ↵ ")
    if len(redacted) > limit:
        redacted = redacted[:limit] + f"...[truncated, total {len(task or '')} chars]"
    return redacted


async def _commit_push(repo: Path, branch: str, task: str) -> tuple[bool, str]:
    rc, out, _ = await _sh(["git", "status", "--porcelain"], cwd=str(repo))
    if not out.strip():
        return False, "clean"
    # Block -1A: neutral commit message — `task` is intentionally not embedded.
    msg = _COMMIT_PUSH_COMMIT_MSG
    for args in (
        ["git", "add", "-A"],
        ["git", "commit", "-m", msg],
        ["git", *_git_cred_args(), "push", "origin", branch],
    ):
        rc, _, err = await _sh(args, cwd=str(repo), env=_git_env())
        if rc != 0:
            raise RuntimeError(f"{' '.join(args)}: {err.strip()}")
    return True, msg


# Mandatory verification after each implementation: compile all of bot/*.py plus a few fast
# test suites (no secrets / network / production DB — each test builds its own temporary one).
# On FAILURE the changes are NOT published and the operator gets an honest "checks did not pass"
# instead of "Done". This catches broken/regressed code; a NEW feature's behavior is still
# proven by a dedicated feature test.
_VERIFY_TESTS = (
    "scripts/test_secret_policy.py",
    "scripts/test_cutover_guards.py",
    "scripts/test_router_dispatch.py",
)
_VERIFY_TEST_TIMEOUT_S = 240.0


async def _run_verification(repo) -> dict:
    """Compile gate + fast tests in the bridge clone. Returns {ok, lines, fail_tail} —
    the lines are chat-safe (no secret values)."""
    import glob as _glob
    import sys as _sys
    lines: list[str] = []
    ok = True
    fail_tail = ""
    py_files = sorted(_glob.glob(str(Path(repo) / "bot" / "*.py")))
    rc, _o, err = await _sh([_sys.executable, "-m", "py_compile", *py_files], cwd=str(repo))
    if rc == 0:
        lines.append("code compilation: OK")
    else:
        ok = False
        lines.append("code compilation: ERROR")
        fail_tail = _redact(err.strip())[-600:]
    env = dict(os.environ, PYTHONPATH=".", PYTHONDONTWRITEBYTECODE="1",
               TELEGRAM_BOT_TOKEN="dummy:verify", TELEGRAM_OWNER_ID="1")
    for k in ("AIOS_DATA_DB", "AIOS_DATA_DB_GROUP", "AIOS_CONTEXT_ENCRYPTION"):
        env.pop(k, None)  # tests never touch the production DB / encryption
    for t in _VERIFY_TESTS:
        if not (Path(repo) / t).exists():
            continue
        name = Path(t).stem.replace("test_", "")
        try:
            rc, out, err = await asyncio.wait_for(
                _sh([_sys.executable, t], cwd=str(repo), env=env),
                timeout=_VERIFY_TEST_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            ok = False
            lines.append(f"tests {name}: TIMEOUT")
            continue
        if rc == 0:
            lines.append(f"tests {name}: OK")
        else:
            ok = False
            lines.append(f"tests {name}: FAILED")
            if not fail_tail:
                fail_tail = _redact(((out or "") + "\n" + (err or "")).strip())[-600:]
    return {"ok": ok, "lines": lines, "fail_tail": fail_tail}


async def probe_claude_limit() -> str | None:
    """On a "bare" task error (exit code 1 with no stderr — the SDK swallows the CLI output),
    find the real cause: an instant one-word CLI call with DIRECT capture of stdout/stderr.
    If the account has hit its usage-window limit, return the text (with the reset time),
    otherwise None. The cost when limited is zero (it fails immediately)."""
    try:
        import importlib.util
        import sys as _sys
        spec = importlib.util.find_spec("claude_agent_sdk")
        if not spec or not spec.origin:
            return None
        cli = Path(spec.origin).parent / "_bundled" / "claude"
        if not cli.exists():
            return None
        tok = (get_secret("CLAUDE_CODE_OAUTH_TOKEN", default="") or "").strip()
        if not tok:
            return None
        env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=tok)
        env.pop("ANTHROPIC_API_KEY", None)
        rc, out, err = await asyncio.wait_for(
            _sh([str(cli), "-p", "ok", "--model", "claude-haiku-4-5"], env=env),
            timeout=60,
        )
        if rc == 0:
            return None  # the account works — the task failure had another cause
        blob = ((out or "") + "\n" + (err or "")).strip()
        if any(m in blob.lower() for m in ("limit", "usage", "resets", "hit your")):
            return _redact(blob)[:300]
        return None
    except Exception as e:  # noqa: BLE001 — the probe must not mask the original error
        logger.warning("probe_claude_limit failed: %s", type(e).__name__)
        return None


# Auto-push is gated to a configured GitHub org so the bridge never pushes to an unexpected
# remote. Set AIOS_PUSH_ORG to your org; auto-push only runs when the origin URL contains it.
_PUSH_ORG_GUARD = os.environ.get("AIOS_PUSH_ORG", "<YOUR_ORG>")
_GIT_BOT_EMAIL = os.environ.get("AIOS_GIT_BOT_EMAIL", "bot@example.com")
_GIT_BOT_NAME = os.environ.get("AIOS_GIT_BOT_NAME", "AI OS bridge")


async def _auto_push(repo, task: str) -> tuple[bool, str]:
    """Auto-commit + push the bridge working repo after a successful Claude run.

    Safety: only push if the remote URL contains the configured org (AIOS_PUSH_ORG).
    Errors are logged, never raised — the operator's task already succeeded.
    Returns (pushed, msg): pushed=True only if a commit was created and pushed.
    """
    msg = ""
    try:
        rc, out, _ = await _sh(["git", "remote", "get-url", "origin"], cwd=str(repo))
        if rc != 0 or _PUSH_ORG_GUARD not in out:
            return False, ""
        rc, out, _ = await _sh(["git", "status", "--porcelain"], cwd=str(repo))
        if rc != 0 or not out.strip():
            return False, ""
        rc, _o, err = await _sh(
            ["git", *_git_cred_args(), "pull", "--rebase", "--autostash"], cwd=str(repo), env=_git_env())
        if rc != 0:
            logger.warning("auto-push: pull --rebase failed: %s", err.strip())
            await _sh(["git", "rebase", "--abort"], cwd=str(repo))
            return False, ""
        await _sh(["git", "add", "-A"], cwd=str(repo))
        # Block -1A: neutral commit message — `task` is intentionally not embedded.
        msg = _AUTO_PUSH_COMMIT_MSG
        rc, _o, err = await _sh([
            "git", "-c", f"user.email={_GIT_BOT_EMAIL}", "-c", f"user.name={_GIT_BOT_NAME}",
            "commit", "-m", msg,
        ], cwd=str(repo))
        if rc != 0:
            if "nothing to commit" not in err.lower():
                logger.warning("auto-push: commit failed: %s", err.strip())
            return False, ""
        rc, _o, err = await _sh(["git", *_git_cred_args(), "push"], cwd=str(repo), env=_git_env())
        if rc != 0:
            logger.warning("auto-push: push failed: %s", err.strip())
            # Undo the local commit so the caller's _commit_push sees the dirty
            # tree and surfaces the push failure (otherwise the tree is clean,
            # _commit_push returns "clean", and the user gets "No changes."
            # despite Claude actually editing files).
            rb_rc, _, rb_err = await _sh(["git", "reset", "HEAD~1"], cwd=str(repo))
            if rb_rc != 0:
                logger.warning("auto-push: rollback after push failure also failed: %s", rb_err.strip())
            return False, ""
        logger.info("auto-push: pushed bridge changes (cwd=%s)", repo)
        return True, msg
    except Exception as e:
        logger.warning("auto-push: unexpected error: %r", e)
        return False, ""


def make_gated_can_use_tool(approve):
    """The SDK's can_use_tool callback on top of the durable approval queue (human-in-the-loop).

    approve — async (tool_name, tool_input) -> bool (make_queue_approver). Routine actions are
    allowed instantly; a dangerous action (claude_policy.approval_reason) waits for the operator's
    Allow/Deny, and a queue timeout is a safe "no". An error inside the policy itself -> deny
    (fail-closed, never open-fail). Module-level so it is testable without launching a real
    coding-agent session."""
    async def _gated_can_use_tool(tool_name, tool_input, context):  # type: ignore[no-untyped-def]
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        try:
            reason = claude_policy.approval_reason(tool_name, tool_input or {})
        except Exception as _pe:  # noqa: BLE001 — a broken policy must not open-fail
            logger.warning("approval policy error (%s) -> deny %s", type(_pe).__name__, tool_name)
            return PermissionResultDeny(
                message="The approval gate could not evaluate this action — it was denied.")
        if reason is None:
            return PermissionResultAllow()
        logger.info("approval gate: %s requires operator approval (%s)", tool_name, reason)
        ti = dict(tool_input or {})
        ti["_reason"] = reason  # reason for the operator's card (make_approval_notifier pops it)
        ok = await approve(tool_name, ti)
        if ok:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=(f"The operator denied this action (or did not answer within 5 minutes): {reason}. "
                     "The action was NOT executed. Continue the task without it; if it is truly "
                     "required, finish up and explain to the operator what needs manual approval."),
            interrupt=False,
        )

    return _gated_can_use_tool


async def _run_claude(
    repo: Path,
    task: str,
    bot,
    chat_id: int,
    read_only: bool,
    design_mode: bool = False,
    resume_session_id: str | None = None,
    progress_sink: list | None = None,
    approve=None,
) -> tuple[str, list[str], str | None, list[str]]:
    """Run a Claude Code session via claude-agent-sdk.

    Returns (assistant_text, stderr_lines, session_id, edited_files). On SDK
    failure, raises with stderr_lines attached as e.bridge_stderr for the caller
    to surface. session_id may be None if the SDK did not emit one. edited_files
    is the list of file paths Write/Edit/MultiEdit/NotebookEdit touched during
    the run — captured via PostToolUse hook so the bridge can surface "saved
    locally" info even when no git commit happened.

    approve — async (tool_name, tool_input) -> bool from make_queue_approver: the operator's
    durable approval queue. When claude_policy.tool_approvals_enabled() and approve is given,
    the can_use_tool gate (human-in-the-loop) is wired in: DANGEROUS actions
    (claude_policy.approval_reason) wait for the operator's Allow/Deny on the phone; routine
    work is allowed by the callback instantly. A queue timeout is a safe "no".
    """
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher
    except ImportError as e:
        raise RuntimeError(f"claude-agent-sdk not installed: {e}")

    if read_only:
        allowed_tools = ["Read", "Grep", "Glob"]
    else:
        allowed_tools = None

    stderr_lines: list[str] = []
    edited_files: set[str] = set()
    _EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

    def _stderr_cb(line: str) -> None:
        stderr_lines.append(line)
        logger.info("claude-cli stderr: %s", line.rstrip())

    async def _track_edits(input_data, tool_use_id, hook_context):  # type: ignore[no-untyped-def]
        try:
            if input_data.get("tool_name") in _EDIT_TOOLS:
                ti = input_data.get("tool_input") or {}
                path = (
                    ti.get("file_path")
                    or ti.get("path")
                    or ti.get("notebook_path")
                )
                if path:
                    edited_files.add(str(path))
        except Exception as _he:
            logger.warning("track_edits hook error: %r", _he)
        return {}

    thinking_cfg = _build_thinking_config()

    append_text = _OWNER_PLAIN_REPLY_HINT
    if design_mode:
        append_text = _DESIGN_SYSTEM_PROMPT + append_text
    system_prompt_cfg = {
        "type": "preset",
        "preset": "claude_code",
        "append": append_text,
    }

    # Human-in-the-loop: the can_use_tool gate for DANGEROUS actions. Routine work (code
    # edits, ordinary commands) is allowed by the callback instantly — the "coding from a
    # phone without endless Allow/Deny" policy is preserved; only the list in
    # docs/security/approval-policy.md prompts. Kill-switch: AIOS_TOOL_APPROVALS=0.
    _gate_active = claude_policy.tool_approvals_enabled() and approve is not None
    _gated_can_use_tool = make_gated_can_use_tool(approve) if _gate_active else None

    opts_kwargs: dict = dict(
        cwd=str(repo),
        # With the gate enabled, permission_mode='default' — permission requests go to
        # _gated_can_use_tool; with the gate disabled — the old no-confirm bypassPermissions
        # policy (see bot/claude_policy.py and docs/security/approval-policy.md).
        permission_mode=claude_policy.resolve_claude_permission_mode(),
        model=_BRIDGE_MODEL,
        effort=_BRIDGE_EFFORT,
        thinking=thinking_cfg,
        system_prompt=system_prompt_cfg,
        setting_sources=["user", "project"],
        # Enable every skill discovered via setting_sources. On the VPS the SDK's "user" source is
        # ~/.claude/skills (a symlink to this repo's claude/skills/, delivered by the 60s aios-sync
        # pull). Without skills=, discovered skills are present-but-not-activated. Field verified in
        # claude_agent_sdk 0.1.64; docs: https://code.claude.com/docs/en/agent-sdk/python . An older
        # SDK without the field is handled by the TypeError fallback below (skills stripped).
        skills="all",
        stderr=_stderr_cb,
        # CAUTION (human-in-the-loop): the presence of hooks (or MCP servers) is what makes
        # SDK 0.1.64 keep stdin open for the whole run (wait_for_result_and_end_input), which
        # is what lets can_use_tool travel out for the operator's Allow/Deny. If this
        # PostToolUse hook is ever removed, do NOT silently keep the gate: without open stdin
        # the streaming input closes after the first message and approvals stop firing. Keep
        # the hook (or another source of the bidirectional protocol).
        hooks={"PostToolUse": [HookMatcher(hooks=[_track_edits])]},
    )
    if resume_session_id:
        opts_kwargs["resume"] = resume_session_id
    if _BRIDGE_MAX_TURNS > 0:
        opts_kwargs["max_turns"] = _BRIDGE_MAX_TURNS
    if allowed_tools is not None:
        opts_kwargs["allowed_tools"] = allowed_tools
    if _gate_active:
        opts_kwargs["can_use_tool"] = _gated_can_use_tool

    # Block -1A: log a SECRET-REDACTED, truncated task for diagnostics — never the
    # raw text. `_task_log_repr` runs the shared scrubber before truncating, so a
    # dictated token cannot reach the log viewer.
    _task_for_log = _task_log_repr(task)
    logger.info(
        "bridge: run claude model=%s effort=%s thinking=%s system_prompt=preset:claude_code design=%s read_only=%s task=%r",
        _BRIDGE_MODEL, _BRIDGE_EFFORT, thinking_cfg, design_mode, read_only, _task_for_log,
    )

    try:
        options = ClaudeAgentOptions(**opts_kwargs)
    except TypeError as e:
        logger.warning("ClaudeAgentOptions rejected fields, stripping: %s", e)
        if "can_use_tool" in opts_kwargs:
            # The approval gate is impossible on this SDK — do NOT run without the
            # human-in-the-loop silently: a loud refusal instead of a quiet open pass
            # (fail-closed).
            raise RuntimeError(
                "claude-agent-sdk without can_use_tool support — the approval gate "
                "cannot run; upgrade the SDK or set AIOS_TOOL_APPROVALS=0"
            ) from e
        for key in ("skills", "setting_sources", "stderr", "thinking", "effort", "system_prompt", "max_turns", "hooks"):
            opts_kwargs.pop(key, None)
        options = ClaudeAgentOptions(**opts_kwargs)

    if _gate_active:
        # can_use_tool requires STREAMING input (SDK 0.1.64 raises ValueError on a str prompt).
        # Yield exactly the same message the SDK itself writes for a string prompt
        # (_internal/client.py: type=user + message{role,content}) — behaviour is identical.
        async def _prompt_stream():
            yield {
                "type": "user",
                "session_id": "",
                "message": {"role": "user", "content": task},
                "parent_tool_use_id": None,
            }

        prompt_input = _prompt_stream()
    else:
        prompt_input = task

    # Resolve the owner's Claude login (Max-subscription OAuth token) from the secret
    # manager (Bitwarden) BY NAME and place it in env so the CLI subprocess authenticates
    # as the subscription, NOT the paid API. .strip() guards against stray whitespace/newline
    # (a malformed token breaks the auth header). The value is NEVER logged. If absent
    # (dev/test), env is left untouched and auth falls back to a local login file.
    _oauth = (get_secret("CLAUDE_CODE_OAUTH_TOKEN", default="") or "").strip()
    if _oauth:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = _oauth

    # Pop ANTHROPIC_API_KEY just before spawning CLI subprocess so it uses
    # CLAUDE_CODE_OAUTH_TOKEN (Max plan, free) not API key (paid). bot/config.py
    # calls load_dotenv() which keeps re-adding the key; setup_claude_auth() is now
    # called explicitly by the claude-worker entrypoint (no longer at module import).
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") and "ANTHROPIC_API_KEY" in os.environ:
        os.environ.pop("ANTHROPIC_API_KEY", None)

    final_texts: list[str] = []
    session_id: str | None = None
    try:
        async for msg in query(prompt=prompt_input, options=options):
            name = type(msg).__name__
            if name in ("SystemMessage", "ResultMessage"):
                sid = getattr(msg, "session_id", None)
                if sid:
                    session_id = sid
            if name == "AssistantMessage":
                sid = getattr(msg, "session_id", None)
                if sid:
                    session_id = sid
                # Operator-facing reply = ONLY the coder's FINAL turn. The intermediate
                # "Let me…/Now editing X/Verifying…" narration between tool calls must NOT
                # leak to the non-technical operator, so reset per assistant message — the last
                # message (the clean summary) wins.
                msg_texts = [
                    getattr(b, "text", "")
                    for b in (getattr(msg, "content", None) or [])
                    if type(b).__name__ == "TextBlock" and getattr(b, "text", "")
                ]
                if msg_texts:
                    final_texts = msg_texts
                    # Live progress for the operator: accumulate the agent's intermediate
                    # replies — a heartbeat in claude_worker relays the MOST RECENT one (per the
                    # prompt, the agent writes them as "Done: … Next: …" in the operator's language).
                    if progress_sink is not None:
                        progress_sink.append("\n".join(msg_texts))
    except Exception as e:
        try:
            e.bridge_stderr = stderr_lines  # type: ignore[attr-defined]
        except Exception:
            pass
        raise

    logger.info(
        "bridge: claude finished, %d final text block(s), %d stderr lines, session_id=%s, edited_files=%d",
        len(final_texts), len(stderr_lines), session_id, len(edited_files),
    )
    return "\n".join(final_texts).strip(), stderr_lines, session_id, sorted(edited_files)
