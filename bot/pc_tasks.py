"""Laptop task channel: drop natural-language commands into a <YOUR_ORG> inbox
repo at _pc_tasks/<timestamp>-<rand>.md. A sync task on the receiving machine
pulls the file down; a SessionStart script detects pending tasks and emits a
system reminder so the local coding agent executes them after approval.

Memory-safety boundary: the filename and the commit message carry a NEUTRAL id
only (timestamp + random suffix), never task text. The body travels as plaintext
INSIDE the file so the offline phone->laptop handoff keeps working; encrypting the
file body is a deployment option (see docs/security/threat-model.md). Obvious secret
patterns are scrubbed out before the plaintext write regardless.

Triggers (case-insensitive prefix at start of message):
  "on laptop", "on pc", "on desktop", "claude code"

Ambiguous single words are deliberately kept out of the triggers to avoid false
positives from ordinary phrasing. Use one of the explicit prefixes above, or click
the "💻 Laptop" button in the project picker for a deterministic non-regex entry.
"""
import asyncio
import logging
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .claude_worker_core import _redact

logger = logging.getLogger(__name__)

# Match start-of-message laptop-task prefix (case-insensitive). Followed by any of
# space/comma/colon/dash so "on laptop, X" and "on laptop: X" and "on laptop - X" all work.
_PC_TRIGGER = re.compile(
    r"^\s*(on\s+(?:laptop|pc|desktop|computer)|claude\s+code)\b[\s,:.\-]*",
    re.IGNORECASE,
)


def is_pc_task(content: str) -> bool:
    """True iff the message starts with one of the PC-task triggers."""
    return bool(_PC_TRIGGER.match(content or ""))


def strip_trigger(content: str) -> str:
    """Remove the trigger prefix from content, keeping the actual task body."""
    return _PC_TRIGGER.sub("", content or "", count=1).strip()


def _new_task_id() -> str:
    """Neutral task id: UTC timestamp + short random suffix.

    The id is NOT derived from the task body, so neither the filename nor the commit
    message can leak task text. The timestamp keeps the bucket chronologically ordered;
    the random suffix avoids same-second collisions.
    Shape: ``2026-05-24T10-30-00Z-a1b2c3d4`` (exactly one '-' before the suffix).
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{ts}-{secrets.token_hex(4)}"


async def _ensure_personal_clone():
    """Make sure /tmp/claude-bridge/project-c is a usable up-to-date clone of
    the <YOUR_ORG> inbox repo. Reuses the existing clone via fetch + reset --hard
    when possible — this is ~10-50× cheaper than the wipe-and-reclone path
    that ``claude_bridge._prepare_repo`` does. Falls back to full clone on
    any failure (dir missing, .git corrupted, network blip mid-fetch).

    Returns the repo Path, or None on hard failure.
    """
    from . import claude_bridge_worker as cb

    projects = cb._load_projects()
    proj = projects.get("project-c")
    if not proj:
        logger.error("submit_pc_task: alias 'project-c' missing from BRIDGE_PROJECTS")
        return None

    branch = proj.get("branch", "main")
    target = cb._WORK_DIR / "project-c"

    # Try cheap reuse path first.
    if (target / ".git").exists():
        try:
            rc, _, err = await cb._sh(
                ["git", "-C", str(target), "fetch", "--depth", "1", "origin", branch],
            )
            if rc != 0:
                raise RuntimeError(f"fetch failed: {err.strip()}")
            rc, _, err = await cb._sh(
                ["git", "-C", str(target), "reset", "--hard", f"origin/{branch}"],
            )
            if rc != 0:
                raise RuntimeError(f"reset failed: {err.strip()}")
            await cb._sh(["git", "-C", str(target), "clean", "-fd"])
            # Re-assert author identity (lost on cold disk wipe).
            await cb._sh(
                ["git", "-C", str(target), "config", "user.name", cb._GIT_USER_NAME],
            )
            await cb._sh(
                ["git", "-C", str(target), "config", "user.email", cb._GIT_USER_EMAIL],
            )
            return target
        except Exception as e:
            logger.warning(
                "ensure_personal_clone: reuse failed (%s), falling back to full clone",
                e,
            )

    # Cold path — full clone via the existing helper (which wipes the dir first).
    try:
        return await cb._prepare_repo("project-c", proj["url"], branch)
    except Exception as e:
        logger.exception("ensure_personal_clone: full clone failed: %s", e)
        return None


async def submit_pc_task(task_text: str, chat_id: int) -> str | None:
    """Write task_text to the <YOUR_ORG> inbox repo at _pc_tasks/<timestamp>.md
    and push. Returns relative path on success, None on failure, or the
    sentinel string "TOO_LONG" if the body exceeds 8000 chars.

    Cheap path: pure git ops, no Claude invocation.
    """
    from . import claude_bridge_worker as cb

    logger.info(
        "submit_pc_task: enter (chat=%s, task_len=%d)", chat_id, len(task_text or "")
    )
    repo = await _ensure_personal_clone()
    if repo is None:
        logger.warning("submit_pc_task: _ensure_personal_clone returned None (chat=%s)", chat_id)
        return None

    body = strip_trigger(task_text)
    if not body:
        # Trigger only, no actual command — refuse so the user notices.
        logger.warning("submit_pc_task: empty body after stripping trigger")
        return None

    # Reject bodies above 8000 chars (≈ 4-page essay). A voice note that
    # transcribed into 50KB does not belong in a single task file: it bloats
    # the repo, costs full re-clone bandwidth, and almost certainly was an
    # accident. The 8000 limit is well above any reasonable typed command.
    if len(body) > 8000:
        logger.warning("submit_pc_task: body too long (%d chars), rejecting", len(body))
        return "TOO_LONG"

    task_id = _new_task_id()
    rel_path = f"_pc_tasks/{task_id}.md"
    full_path = repo / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    # The body travels as plaintext IN THE FILE so the receiving side keeps working;
    # encrypting the file body is a deployment option (see docs/security/threat-model.md).
    # Run the shared secret scrubber first, so an accidental token in a dictated task never
    # lands plaintext in Git history. The filename + commit message carry NO body text.
    safe_body = _redact(body)
    created = task_id.rsplit("-", 1)[0]  # timestamp portion of the neutral id
    md = (
        f"---\n"
        f"created: {created}\n"
        f"task_id: {task_id}\n"
        f"chat_id: {chat_id}\n"
        f"source: telegram-bot\n"
        f"---\n"
        f"\n"
        f"{safe_body}\n"
    )
    full_path.write_text(md, encoding="utf-8")

    try:
        rc, _, err = await cb._sh(["git", "-C", str(repo), "add", rel_path])
        if rc != 0:
            logger.warning("submit_pc_task: git add failed: %s", err.strip())
            return None
        rc, _, err = await cb._sh([
            # Neutral commit message — never embed task text.
            "git", "-C", str(repo), "commit", "-m", f"pc-task: queued {task_id}",
        ])
        if rc != 0:
            logger.warning("submit_pc_task: git commit failed: %s", err.strip())
            return None
        rc, _, err = await cb._sh(["git", "-C", str(repo), "push"])
        if rc != 0:
            logger.warning("submit_pc_task: git push failed: %s", err.strip())
            return None
        logger.info("submit_pc_task: pushed %s", rel_path)
        return rel_path
    except Exception as e:
        logger.exception("submit_pc_task: git ops failed: %s", e)
        return None
