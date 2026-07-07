"""Claude Code bridge: /bridge command for driving claude-agent-sdk from Telegram."""
import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# S6 0b commit-6: the worker-only bridge surface (Claude executor, GitHub-token
# resolution, skill clone, commit constants) lives in bot/claude_bridge_worker.py —
# imported ONLY by the worker / pc_tasks, NOT here. This transport module no longer
# imports it: the back-compat re-export shim is removed now that every consumer points
# at claude_bridge_worker directly. Transport import graph is clean of the worker surface.

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .config import TELEGRAM_OWNER_ID
from .shell import BRIDGE_BUTTON, CANCEL_BUTTON, DONE_BUTTON
from . import task_queue
from . import bridge_queue
from . import claude_policy
from .claude_worker_core import _redact
from .bridge_constants import (  # S6 0b: shared constants (no secrets), re-exported here
    _WORK_DIR,
    _GIT_USER_NAME,
    _GIT_USER_EMAIL,
    _ASSISTANT_ALIAS,
    _SKILLS_DIR,
    _SKILLS_REPO,
    _load_projects,
    _RECENT_SESSIONS_FILE,
    _RECENT_SESSIONS_CAP,
    _load_recent_sessions,
    _save_recent_sessions,
    _record_session,
)


_pending_approvals: dict[str, asyncio.Future] = {}
_pending_task_flow: dict[int, dict] = {}
_last_session_id: dict[int, str] = {}
_last_repo_path: dict[int, str] = {}
# Telegram albums (media groups) arrive as N separate Updates sharing one media_group_id;
# only one carries the caption. Without dedupe, intercept_task_photo fires per photo and
# the user sees one "Claude is thinking..." (and one queued task) per photo. Keyed by chat_id
# (bounded: owner-only), value is the last media_group_id we've already processed.
_seen_media_groups: dict[int, str] = {}

def _sanitize_claude_project_dir(cwd: Path) -> str:
    """Claude Code sanitizes cwd → projects/ subdir name by replacing / and : with -."""
    s = str(cwd).replace("\\", "/")
    # Claude's convention: each separator → "-", leading slash → leading dash
    return "-" + s.lstrip("/").replace("/", "-").replace(":", "-")


def _scan_claude_sessions_on_disk(alias: str) -> list[dict]:
    """Read ~/.claude/projects/<sanitized-cwd>/*.jsonl for this alias.

    Returns list of {sid, ts, preview, mtime} sorted newest first.
    """
    home = Path.home()  # HOME if set (systemd unit pins it), else the user's real home — no /home/app
    # cwd depends on alias
    if alias == _ASSISTANT_ALIAS:
        cwd = _WORK_DIR / "_assistant"
    else:
        cwd = _WORK_DIR / alias
    sanitized = _sanitize_claude_project_dir(cwd)
    proj_dir = home / ".claude" / "projects" / sanitized
    if not proj_dir.exists():
        return []
    results: list[dict] = []
    for f in proj_dir.glob("*.jsonl"):
        sid = f.stem
        try:
            mtime = f.stat().st_mtime
        except Exception:
            continue
        # Extract first user message as preview
        preview = ""
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("type") == "user":
                        msg = rec.get("message", {})
                        content = msg.get("content")
                        if isinstance(content, str):
                            preview = content
                            break
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    preview = block.get("text", "")
                                    break
                            if preview:
                                break
        except Exception as e:
            logger.debug("scan: read %s failed: %s", f, e)
        preview = preview.strip().replace("\n", " ")[:60]
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")
        results.append({"sid": sid, "ts": ts, "preview": preview, "mtime": mtime})
    results.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return results


def _get_blocked_sids(chat_id: int, alias: str) -> set[str]:
    data = _load_recent_sessions()
    return set(data.get(str(chat_id), {}).get("__blocked__", {}).get(alias, []))


def _block_sid(chat_id: int, alias: str, sid: str) -> None:
    """Add a session id to blocklist so it never shows in picker again."""
    data = _load_recent_sessions()
    cid = str(chat_id)
    chat_map = data.setdefault(cid, {})
    blocked_map = chat_map.setdefault("__blocked__", {})
    lst = blocked_map.setdefault(alias, [])
    if sid not in lst:
        lst.append(sid)
    # Also remove from active history if present
    history = chat_map.get(alias, [])
    chat_map[alias] = [e for e in history if e.get("sid") != sid]
    _save_recent_sessions(data)


def _delete_session_jsonl(alias: str, sid: str) -> bool:
    """Remove ~/.claude/projects/<sanitized>/<sid>.jsonl. Returns True on success."""
    home = Path.home()  # HOME if set (systemd unit pins it), else the user's real home — no /home/app
    if alias == _ASSISTANT_ALIAS:
        cwd = _WORK_DIR / "_assistant"
    else:
        cwd = _WORK_DIR / alias
    sanitized = _sanitize_claude_project_dir(cwd)
    target = home / ".claude" / "projects" / sanitized / f"{sid}.jsonl"
    try:
        if target.exists():
            target.unlink()
            return True
    except Exception as e:
        logger.warning("delete_session_jsonl: failed for %s: %r", target, e)
    return False


def _get_recent_sessions(chat_id: int, alias: str) -> list[dict]:
    """Merge: recorded sessions (JSON file) + on-disk scan, dedup by sid, newest first.
    Filters out blocklisted sids."""
    blocked = _get_blocked_sids(chat_id, alias)
    merged: dict[str, dict] = {}
    # Disk scan first (ground truth for what actually exists)
    for entry in _scan_claude_sessions_on_disk(alias):
        if entry["sid"] in blocked:
            continue
        merged[entry["sid"]] = entry
    # Recorded entries override with our saved preview if we have one
    data = _load_recent_sessions()
    for entry in data.get(str(chat_id), {}).get(alias, []):
        sid = entry.get("sid")
        if not sid or sid in blocked:
            continue
        if sid in merged:
            # Prefer our saved preview if non-empty
            if entry.get("preview") and not merged[sid].get("preview"):
                merged[sid]["preview"] = entry["preview"]
        else:
            merged[sid] = {**entry, "mtime": 0}
    # Sort by mtime (fresh first), fall back to ts
    return sorted(merged.values(), key=lambda x: x.get("mtime", 0), reverse=True)


def _has_jsonl_for(alias: str, sid: str) -> bool:
    """True iff Claude has persisted a JSONL transcript for (alias, sid).

    Used to validate whether _last_session_id (in-memory or freshly hydrated
    from the persistent volume) can still be safely passed to --resume.
    """
    home = Path.home()  # HOME if set (systemd unit pins it), else the user's real home — no /home/app
    cwd = _WORK_DIR / ("_assistant" if alias == _ASSISTANT_ALIAS else alias)
    sanitized = _sanitize_claude_project_dir(cwd)
    return (home / ".claude" / "projects" / sanitized / f"{sid}.jsonl").exists()


def _hydrate_last_sessions() -> None:
    """Repopulate _last_session_id / _last_repo_path from the persistent
    volume so that the first message after a redeploy resumes the chat's
    most-recent session instead of forcing a trip through the picker.

    Strategy: for each chat_id known to _recent_sessions.json, find the alias
    whose freshest on-disk JSONL has the latest mtime, then cache that
    (sid, alias) pair. _sub_run's clearing logic preserves the cached sid
    when its JSONL still exists for the requested alias.
    """
    try:
        data = _load_recent_sessions()
    except Exception as e:
        logger.warning("hydrate: load recent_sessions failed: %r", e)
        return
    hydrated = 0
    for cid_str, chat_map in data.items():
        if not isinstance(chat_map, dict):
            continue
        try:
            chat_id = int(cid_str)
        except (TypeError, ValueError):
            continue
        best: tuple[str, str, float] | None = None  # (alias, sid, mtime)
        for alias in chat_map.keys():
            if alias.startswith("__"):  # skip metadata keys like __blocked__
                continue
            try:
                on_disk = _scan_claude_sessions_on_disk(alias)
            except Exception:
                continue
            if not on_disk:
                continue
            top = on_disk[0]
            sid = top.get("sid")
            mtime = float(top.get("mtime", 0.0) or 0.0)
            if not sid:
                continue
            if best is None or mtime > best[2]:
                best = (alias, sid, mtime)
        if best is None:
            continue
        alias, sid, _ = best
        _last_session_id[chat_id] = sid
        cwd = _WORK_DIR / ("_assistant" if alias == _ASSISTANT_ALIAS else alias)
        _last_repo_path[chat_id] = str(cwd)
        hydrated += 1
        logger.info(
            "hydrate: chat=%s alias=%s sid=%s (path may not exist yet — will reclone on demand)",
            chat_id, alias, sid[:8],
        )
    logger.info("hydrate: restored %d chat session(s) from persistent volume", hydrated)


def _format_session_label(entry: dict) -> str:
    """Make a short button label for a session entry: 'DD MMM HH:MM · preview'.

    The display timezone is configurable via AIOS_DISPLAY_TZ_OFFSET_H (hours offset
    from UTC; default 0 = UTC), so the operator can render times in their own zone.
    """
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
        # local display time (offset configurable; default UTC)
        from datetime import timedelta, timezone as _tz
        try:
            offset_h = float(os.environ.get("AIOS_DISPLAY_TZ_OFFSET_H", "0"))
        except (TypeError, ValueError):
            offset_h = 0.0
        dt_local = dt.astimezone(_tz(timedelta(hours=offset_h)))
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        when = f"{dt_local.day} {months[dt_local.month-1]} {dt_local.strftime('%H:%M')}"
    except Exception:
        when = entry.get("ts", "?")[:16]
    preview = entry.get("preview", "")[:35]
    return f"{when} · {preview}" if preview else when




# S6 0b commit-2: _github_token / ensure_skills / _count_skills moved to
# claude_bridge_worker.py and re-exported via the back-compat shim near the top.


# Example project aliases for a public reference deployment. Real aliases come from
# the BRIDGE_PROJECTS env var; these dicts just decorate whatever the operator configures.
_PICKER_HIDDEN_ALIASES = {"project-a"}
_PICKER_LABELS = {"project-b": "📦 Project B", "project-c": "✨ Other"}




# S6 0b commit-2: _auth_url / _sh / _prepare_repo moved to claude_bridge_worker.py
# and re-exported via the back-compat shim near the top of this module.


# S6 0b commit-2: _AUTO_PUSH_COMMIT_MSG / _COMMIT_PUSH_COMMIT_MSG / _task_log_repr /
# _commit_push moved to claude_bridge_worker.py and re-exported via the back-compat
# shim near the top of this module. The leak-safe commit-message constants are now
# fixed strings in the worker module — no interpolation of `task`.


def _resolve(token: str, verdict: bool) -> None:
    fut = _pending_approvals.pop(token, None)
    if fut and not fut.done():
        fut.set_result(verdict)


# S6 0b commit-2: _auto_push / _run_claude moved to claude_bridge_worker.py and
# re-exported via the back-compat shim near the top. The lazy claude_agent_sdk
# import lives INSIDE _run_claude in the worker module, unchanged.


HELP = (
    "Claude Code Bridge (V2 durable queue)\n\n"
    "/bridge list - list projects\n"
    "/bridge ask <alias> <question> - read-only (queued)\n"
    "/bridge fix <alias> <task> - edit + commit + push (queued)\n"
    "/bridge continue <alias> <task> - fix + latest session context (queued)\n"
    "/bridge status <task_id> - show task status/result\n"
    "/bridge approvals - show pending tool approval (Allow/Deny)\n\n"
    "Tasks run via the durable queue (claude-worker). Projects via BRIDGE_PROJECTS env var:\n"
    '{"alias": {"url": "https://github.com/<YOUR_ORG>/repo.git", "branch": "main"}}'
)


async def cmd_bridge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != TELEGRAM_OWNER_ID:
        return
    args = context.args or []
    if not args:
        await _show_project_picker(update, context)
        return
    sub = args[0]
    rest = args[1:]
    try:
        if sub == "list":
            await _sub_list(update)
        elif sub == "ask":
            await _sub_run(update, context, rest, read_only=True)
        elif sub == "fix":
            await _sub_run(update, context, rest, read_only=False)
        elif sub == "continue":
            await _sub_continue(update, context, rest)
        elif sub == "status":
            await _sub_status(update, context, rest)
        elif sub == "approvals":
            await _sub_approvals(update, context)
        else:
            await update.message.reply_text(HELP)
    except Exception as e:
        logger.exception("bridge command failed")
        await update.message.reply_text(f"ERROR: {e}")


async def _sub_list(update: Update) -> None:
    projects = _load_projects()
    if not projects:
        await update.message.reply_text(
            "No projects. Set the BRIDGE_PROJECTS env var."
        )
        return
    lines = [f"✴️ {a}: {p.get('url')} ({p.get('branch', 'main')})" for a, p in projects.items()]
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Block 3/8: V2 transport producer + status + approvals over the durable queue.
# Pure logic lives in bot/bridge_queue.py (telegram-free, unit-tested).
# ---------------------------------------------------------------------------
async def _enqueue_bridge_producer(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   args: list[str], *, mode: str) -> None:
    """Target path: enqueue the /bridge task into the durable queue. Does NOT run real Claude."""
    if update.effective_user and update.effective_user.id != TELEGRAM_OWNER_ID:
        return
    if not args or len(args) < 2:
        await update.message.reply_text(f"Usage: /bridge {mode} <alias> <task>")
        return
    alias = args[0]
    task_text = " ".join(args[1:])
    chat_id = update.effective_chat.id
    if alias != _ASSISTANT_ALIAS and not _load_projects().get(alias):
        await update.message.reply_text(f"Unknown project: {alias}")
        return
    # Preserve the session chosen in the picker (resume_sid) for ask/fix; for continue,
    # fall back to the last local session. 'new'/empty -> None. design_mode is detected
    # inside enqueue_bridge_task from the task text (existing rule).
    state = _pending_task_flow.get(chat_id) or {}
    resume_session_id = bridge_queue.resolve_resume_sid(state, mode, _last_session_id.get(chat_id))
    try:
        task_id = bridge_queue.enqueue_bridge_task(
            chat_id=chat_id, alias=alias, mode=mode,
            task_text=task_text, resume_session_id=resume_session_id,
        )
    except Exception as e:  # noqa: BLE001 — producer must not crash the handler
        logger.warning("bridge producer: enqueue failed: %s", type(e).__name__)
        await update.message.reply_text(f"Could not enqueue the task: {type(e).__name__}")
        return
    await update.message.reply_text(
        f"Claude is thinking..."
    )


async def _sub_status(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    if update.effective_user and update.effective_user.id != TELEGRAM_OWNER_ID:
        return
    if not args:
        await update.message.reply_text("Usage: /bridge status <task_id>")
        return
    try:
        task_id = int(args[0])
    except (ValueError, TypeError):
        await update.message.reply_text("task_id must be a number.")
        return
    await update.message.reply_text(bridge_queue.format_task_status(task_queue.get_task(task_id)))


async def _sub_approvals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_user.id != TELEGRAM_OWNER_ID:
        return
    view = bridge_queue.pending_approval_view()
    if view is None:
        await update.message.reply_text("No pending approvals.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Allow", callback_data=view["allow_cb"]),
        InlineKeyboardButton("Deny", callback_data=view["deny_cb"]),
    ]])
    await update.message.reply_text(view["text"], reply_markup=kb)


async def _sub_run(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str], read_only: bool) -> None:
    # Block 3/8: target path = enqueue to the durable queue (producer). The legacy
    # inline Claude execution below is kept for reference and is NOT reached.
    await _enqueue_bridge_producer(update, context, args, mode=("ask" if read_only else "fix"))
    return


async def _sub_continue(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> None:
    # Block 3/8: target path = enqueue to the durable queue (producer). Legacy inline
    # execution below is kept for reference and is NOT reached.
    await _enqueue_bridge_producer(update, context, args, mode="continue")
    return


async def _show_project_picker(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str = "fix") -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        _last_session_id.pop(chat_id, None)
        _last_repo_path.pop(chat_id, None)
    projects = _load_projects()
    if not projects:
        await update.message.reply_text(
            "No projects. Configure BRIDGE_PROJECTS via the env var."
        )
        return
    rows = [
        [InlineKeyboardButton(
            _PICKER_LABELS.get(alias, alias),
            callback_data=f"bridge:pick:{mode}:{alias}",
        )]
        for alias in projects.keys()
        if alias not in _PICKER_HIDDEN_ALIASES
    ]
    # Laptop channel: route the next text message into the pc_task queue
    # regardless of the text-prefix regex trigger — explicit, deterministic,
    # with no false positives from ordinary phrasing.
    rows.append([InlineKeyboardButton("💻 Laptop", callback_data="bridge:laptop:-")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="bridge:cancel:-")])
    kb = InlineKeyboardMarkup(rows)
    title = {
        "fix": "Pick a project to edit:",
        "ask": "Pick a project for a read-only question:",
        "continue": "Pick a project to continue the last session:",
    }.get(mode, "Pick a project:")
    await update.message.reply_text(title, reply_markup=kb)


def _build_session_picker_kb(mode: str, alias: str, recent: list[dict], manage: bool) -> InlineKeyboardMarkup:
    rows = []
    has_sessions = False
    for entry in recent[:_RECENT_SESSIONS_CAP]:
        sid = entry.get("sid", "")
        if not sid:
            continue
        has_sessions = True
        label = _format_session_label(entry)
        if manage:
            rows.append([
                InlineKeyboardButton(label, callback_data=f"bridge:sess:{mode}:{alias}:{sid}"),
                InlineKeyboardButton("🗑️", callback_data=f"bridge:del:{mode}:{alias}:{sid}"),
            ])
        else:
            rows.append([InlineKeyboardButton(label, callback_data=f"bridge:sess:{mode}:{alias}:{sid}")])
    last_row = [InlineKeyboardButton("🆕 New", callback_data=f"bridge:sess:{mode}:{alias}:new")]
    if has_sessions:
        if manage:
            last_row.append(InlineKeyboardButton("✅ Done", callback_data=f"bridge:manage:{mode}:{alias}:0"))
        else:
            last_row.append(InlineKeyboardButton("🗑 Manage", callback_data=f"bridge:manage:{mode}:{alias}:1"))
    last_row.append(InlineKeyboardButton("❌ Cancel", callback_data="bridge:cancel:-"))
    rows.append(last_row)
    return InlineKeyboardMarkup(rows)


async def cb_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if update.effective_user and update.effective_user.id != TELEGRAM_OWNER_ID:
        await q.answer("denied")
        return
    data = q.data or ""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != "bridge":
        await q.answer()
        return
    verb = parts[1]

    if verb in ("allow", "deny"):
        if len(parts) != 3:
            await q.answer()
            return
        verdict = verb == "allow"
        token = parts[2]
        _resolve(token, verdict)
        await q.answer("ok")
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    if verb == "approve":
        # Block 3/8: V2 queue-backed approval — bridge:approve:<approval_id>:<allow|deny>.
        # Resolves the verdict in the durable queue (no in-process Future across processes).
        res = bridge_queue.resolve_approval_callback(data)
        await q.answer(res.get("toast", "ok"))
        try:
            await q.edit_message_text(res["message"])
        except Exception:
            pass
        return

    if verb == "cancel":
        chat_id = q.message.chat_id if q.message else None
        if chat_id is not None:
            _pending_task_flow.pop(chat_id, None)
            _last_session_id.pop(chat_id, None)
            _last_repo_path.pop(chat_id, None)
        await q.answer("cancelled")
        try:
            await q.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if verb == "laptop":
        chat_id = q.message.chat_id if q.message else None
        if chat_id is not None:
            _pending_task_flow[chat_id] = {"laptop_mode": True}
            logger.info("cb_approve laptop: set laptop_mode (chat=%s)", chat_id)
        await q.answer("ok")
        try:
            await q.edit_message_text(
                "💻 Laptop — what should I do?\n"
                "The next message is queued for the coding agent on the receiving machine. "
                "It runs the next time that machine picks up the queue."
            )
        except Exception:
            pass
        return

    if verb == "pick":
        if len(parts) != 4:
            await q.answer()
            return
        mode = parts[2]
        alias = parts[3]
        chat_id = q.message.chat_id if q.message else q.from_user.id

        # For "continue" mode we keep old behavior (resume last session silently)
        # For fix/ask — show a session picker: new or resume from recent
        if mode in ("fix", "ask") and alias != _ASSISTANT_ALIAS:
            recent = _get_recent_sessions(chat_id, alias)
            kb = _build_session_picker_kb(mode, alias, recent, manage=False)
            label_ru = {"fix": "edit", "ask": "question"}.get(mode, "task")
            await q.answer()
            try:
                await q.edit_message_text(
                    f"{alias} ({label_ru}) — pick a session:",
                    reply_markup=kb,
                )
            except Exception:
                pass
            return

        # continue mode or assistant — skip session picker
        _pending_task_flow[chat_id] = {"alias": alias, "mode": mode}
        await q.answer()
        label = {"fix": "edit", "ask": "question", "continue": "continue"}.get(mode, "task")
        try:
            await q.edit_message_text(
                f"{alias} ({label}) — describe the task in the next message."
            )
        except Exception:
            pass
        return

    if verb == "manage":
        # bridge:manage:<mode>:<alias>:<0|1>
        if len(parts) != 5:
            await q.answer()
            return
        mode = parts[2]
        alias = parts[3]
        flag = parts[4] == "1"
        chat_id = q.message.chat_id if q.message else q.from_user.id
        recent = _get_recent_sessions(chat_id, alias)
        kb = _build_session_picker_kb(mode, alias, recent, manage=flag)
        await q.answer()
        # Edit only the inline keyboard — text does not change, and editing the
        # full message text causes iOS Telegram to repaint the chat composer,
        # which makes the persistent reply keyboard flicker.
        try:
            await q.edit_message_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    if verb == "del":
        # bridge:del:<mode>:<alias>:<sid>
        if len(parts) != 5:
            await q.answer()
            return
        mode = parts[2]
        alias = parts[3]
        sid = parts[4]
        chat_id = q.message.chat_id if q.message else q.from_user.id
        _block_sid(chat_id, alias, sid)
        _delete_session_jsonl(alias, sid)
        if _last_session_id.get(chat_id) == sid:
            _last_session_id.pop(chat_id, None)
        # Re-render picker — stay in manage mode if anything is left to delete
        recent = _get_recent_sessions(chat_id, alias)
        has_any = any(e.get("sid") for e in recent[:_RECENT_SESSIONS_CAP])
        kb = _build_session_picker_kb(mode, alias, recent, manage=has_any)
        # Silent answer — text in q.answer() raises a toast on iOS that yanks
        # focus away from the chat and triggers the reply-keyboard flicker.
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    if verb == "sess":
        # bridge:sess:<mode>:<alias>:<sid-or-new>
        if len(parts) != 5:
            await q.answer()
            return
        mode = parts[2]
        alias = parts[3]
        sid = parts[4]
        chat_id = q.message.chat_id if q.message else q.from_user.id
        state: dict = {"alias": alias, "mode": mode}
        if sid != "new":
            state["resume_sid"] = sid
            # Also preload _last_session_id so existing code path resumes this one
            _last_session_id[chat_id] = sid
        else:
            _last_session_id.pop(chat_id, None)
        _pending_task_flow[chat_id] = state
        await q.answer()
        label = {"fix": "edit", "ask": "question"}.get(mode, "task")
        suffix = " (new session)" if sid == "new" else f" (↺ resume {sid[:8]})"
        try:
            await q.edit_message_text(
                f"{alias} ({label}){suffix} — describe the task in the next message."
            )
        except Exception:
            pass
        return

    await q.answer()


async def intercept_credentials_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """HARD-DISABLED by design. Uploading Claude credentials via a Telegram document is DISABLED.

    Security rationale (solved by design): credentials must never travel through chat. The Claude
    Code login is a local-only file on the VPS (~/.claude/.credentials.json), installed OUTSIDE the
    bot, NOT via Telegram and NOT via env. This function downloads NOTHING and writes NOTHING.
    """
    from telegram.ext import ApplicationHandlerStop

    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return
    doc = update.message.document
    if not doc:
        return
    name = (doc.file_name or "").lower()
    if "credentials" not in name or not name.endswith(".json"):
        return  # not a credential document — let other handlers process it
    # Intentionally download NOTHING and write NOTHING. Reply to the operator and stop the chain.
    logger.warning("bridge: credentials-document upload is hard-disabled (V2); refused")
    await update.message.reply_text(
        "Uploading Claude credentials via Telegram is disabled. The login lives as a local "
        "file on the VPS (~/.claude/.credentials.json) and is installed outside the bot."
    )
    raise ApplicationHandlerStop


async def intercept_task_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.ext import ApplicationHandlerStop

    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return
    chat_id = update.effective_chat.id
    state = _pending_task_flow.get(chat_id)
    if not state:
        return

    photos = update.message.photo or []
    if not photos:
        return

    # Telegram albums deliver each photo as a separate Update sharing one media_group_id;
    # process the first arrival, drop the rest so the owner sees ONE "Claude is thinking..."
    # per album (and we enqueue ONE task, not N).
    media_group_id = update.message.media_group_id
    if media_group_id and _seen_media_groups.get(chat_id) == media_group_id:
        raise ApplicationHandlerStop
    if media_group_id:
        _seen_media_groups[chat_id] = media_group_id

    _WORK_DIR.mkdir(parents=True, exist_ok=True)
    image_path = _WORK_DIR / f"_bridge_photo_{chat_id}.jpg"

    try:
        tg_file = await context.bot.get_file(photos[-1].file_id)
        await tg_file.download_to_drive(str(image_path))
    except Exception as e:
        logger.exception("bridge: failed to download photo")
        await update.message.reply_text(f"Could not download the image: {e}")
        _pending_task_flow.pop(chat_id, None)
        raise ApplicationHandlerStop

    caption = (update.message.caption or "").strip()
    alias = state.get("alias")
    mode = state.get("mode", "fix")

    if caption:
        task = (
            f"{caption}\n\n"
            f"[A screenshot is attached at absolute path {image_path}. "
            f"Use the Read tool on this path to see the image and use it as visual context.]"
        )
    else:
        task = (
            f"[A screenshot is attached at absolute path {image_path}. "
            f"Use the Read tool on this path to see the image. "
            f"Infer the task from its contents and execute it on the current project.]"
        )

    logger.info("bridge: photo intercept alias=%s mode=%s caption_len=%d", alias, mode, len(caption))

    fake_args = [alias, task]
    try:
        if mode == "ask":
            await _sub_run(update, context, fake_args, read_only=True)
        elif mode == "continue":
            await _sub_continue(update, context, fake_args)
        else:
            await _sub_run(update, context, fake_args, read_only=False)
    finally:
        raise ApplicationHandlerStop


async def intercept_task_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram.ext import ApplicationHandlerStop

    if not update.message or not update.effective_user:
        return
    if update.effective_user.id != TELEGRAM_OWNER_ID:
        return
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not text:
        return

    if text == BRIDGE_BUTTON:
        logger.info("intercept_task_text: BRIDGE_BUTTON → showing picker (chat=%s)", chat_id)
        await _show_project_picker(update, context)
        raise ApplicationHandlerStop

    state = _pending_task_flow.get(chat_id)

    if state and text == CANCEL_BUTTON:
        logger.info("intercept_task_text: CANCEL_BUTTON pressed (chat=%s, state=%s)", chat_id, state)
        _pending_task_flow.pop(chat_id, None)
        _last_session_id.pop(chat_id, None)
        _last_repo_path.pop(chat_id, None)
        await update.message.reply_text("Bridge session cancelled.")
        raise ApplicationHandlerStop

    if state and text == DONE_BUTTON:
        logger.info("intercept_task_text: DONE_BUTTON pressed (chat=%s, state=%s)", chat_id, state)
        _pending_task_flow.pop(chat_id, None)
        _last_session_id.pop(chat_id, None)
        _last_repo_path.pop(chat_id, None)
        await update.message.reply_text("Bridge session closed.")
        raise ApplicationHandlerStop

    # Laptop mode is for the pc_task channel — not a /bridge alias dispatch.
    # Fall through so handlers.handle_text can route the next message via
    # pc_tasks.submit_pc_task. This was the cause of the "Unknown project:
    # None" bug: previously intercept_task_text would read state.get("alias")
    # (which is None for laptop_mode), pass that to _sub_run, and produce
    # the misleading error.
    if state and state.get("laptop_mode"):
        logger.info("intercept_task_text: laptop_mode active (chat=%s) → defer to handle_text", chat_id)
        return

    if not state:
        return
    if text.startswith("/"):
        return

    alias = state.get("alias")
    mode = state.get("mode", "fix")
    logger.info(
        "intercept_task_text: dispatch /bridge (chat=%s, alias=%r, mode=%r, state=%s)",
        chat_id, alias, mode, state,
    )

    fake_args = [alias, text]
    try:
        if mode == "ask":
            await _sub_run(update, context, fake_args, read_only=True)
        elif mode == "continue":
            await _sub_continue(update, context, fake_args)
        else:
            await _sub_run(update, context, fake_args, read_only=False)
    finally:
        raise ApplicationHandlerStop
