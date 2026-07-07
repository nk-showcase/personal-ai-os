import logging
import re
from datetime import date, datetime

TG_MAX = 4096


def split_message(text: str) -> list[str]:
    """Split long text into chunks for Telegram (max 4096 chars per message)."""
    if len(text) <= TG_MAX:
        return [text]
    chunks = []
    while text:
        if len(text) <= TG_MAX:
            chunks.append(text)
            break
        # Split at paragraph > newline > space boundary
        split_at = text.rfind("\n\n", 0, TG_MAX)
        if split_at <= 0:
            split_at = text.rfind("\n", 0, TG_MAX)
        if split_at <= 0:
            split_at = text.rfind(" ", 0, TG_MAX)
        if split_at <= 0:
            split_at = TG_MAX
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


_MONTH_SHORT = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _fmt_due(raw: str) -> str:
    """Format ISO date/datetime to short string: '7 Mar' or '7 Mar, 12:30'."""
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
            return f"{dt.day} {_MONTH_SHORT[dt.month]}, {dt.hour}:{dt.minute:02d}"
        d = date.fromisoformat(raw)
        return f"{d.day} {_MONTH_SHORT[d.month]}"
    except (ValueError, IndexError):
        return raw


from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

from . import storage
from .classifier import analyze, analyze_image, prioritize_tasks, cleanup_tasks, search_event
from .config import TELEGRAM_OWNER_ID
from .claude_handlers import start_new_chat, continue_chat, stop_claude, is_claude_active, handle_claude_text
from .reminder_lib import arm_precise_reminder

from .shell import (
    DONE_BUTTON, APPEND_BUTTON, BRIDGE_BUTTON,
    NEW_CHAT_BUTTON, CONTINUE_BUTTON, CANCEL_BUTTON,
    PERSISTENT_KEYBOARD,
)


def _parse_remind_days(text: str) -> list[int]:
    """Parse reminder days from text like '3 weeks', '14 and 3 days', '7, 3, 1'."""
    days = []
    for m in re.finditer(r"(\d+)\s*(weeks?|wk\.?|months?|mo\.?)?", text, re.IGNORECASE):
        n = int(m.group(1))
        unit = (m.group(2) or "").lower()
        if unit.startswith("week") or unit.startswith("wk"):
            n *= 7
        elif unit.startswith("month") or unit.startswith("mo"):
            n *= 30
        days.append(n)
    return sorted(set(days), reverse=True)


# Patterns for completing tasks from free text
# "28 - done", "28 done", "done 28", "close 28"
_DONE_PATTERN = re.compile(
    r"^(?:(\d+)\s*[-:.]?\s*(?:done|complete|completed|close)"
    r"|(?:done|complete|completed|close)\s*(\d+))",
    re.IGNORECASE,
)
# "delete 5", "del 5", "remove 5"
_DEL_PATTERN = re.compile(
    r"^(?:del|delete|remove)\s+(\d+)",
    re.IGNORECASE,
)


def is_authorized(update: Update) -> bool:
    return (
        update.effective_user
        and update.effective_user.id == TELEGRAM_OWNER_ID
        and update.effective_chat.type == "private"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "Hi! I'm your task bot (Todoist).\n\n"
        "Just type or dictate a task.\n\n"
        "/list - open tasks\n"
        "/done <N> - complete a task by its number from /list\n"
        "/del <N> - delete a task\n",
        reply_markup=PERSISTENT_KEYBOARD,
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    tasks = await storage.list_tasks()
    if not tasks:
        await update.message.reply_text("No open tasks.")
        return

    # Group by project, skip subtasks and someday
    by_project: dict[str, list] = {}
    for t in tasks:
        if t.get("parent_id"):
            continue
        if "someday" in t.get("labels", []):
            continue
        proj = t.get("project") or "Inbox"
        by_project.setdefault(proj, []).append(t)

    task_map = {}
    lines = []
    idx = 1
    for proj_name, proj_tasks in by_project.items():
        if lines:
            lines.append("")
        lines.append(f"-- {proj_name} --")
        for t in proj_tasks:
            task_map[idx] = t["id"]
            suffix = ""
            if t.get("due_date"):
                suffix += f"  [{_fmt_due(t['due_date'])}]"
            if t.get("is_recurring"):
                suffix += " \U0001f501"  # repeat icon
            lines.append(f"{idx}. {t['content']}{suffix}")
            idx += 1

    context.chat_data["task_map"] = task_map

    # Split into multiple messages if too long for Telegram (4096 char limit)
    text = "\n".join(lines)
    for chunk in split_message(text):
        await update.message.reply_text(chunk)


def _close_line(num: int, result: dict) -> str:
    """Format one line of close reply."""
    if not result["ok"]:
        return f"#{num} - API error"
    if result["recurring"]:
        line = f"#{num} - recurring iteration closed"
        if result.get("next_due"):
            line += f", next: {result['next_due']}"
    elif result.get("was_recurring"):
        line = f"#{num} - closed for good (recurrence removed)"
    else:
        line = f"#{num} - done"
    return line


def _extract_task_numbers(result: dict) -> list[int]:
    """Extract task numbers from LLM result, supporting both old and new format."""
    # New format: task_numbers array
    nums = result.get("task_numbers")
    if isinstance(nums, list):
        return [int(n) for n in nums if n is not None]
    # Old format: single task_number
    num = result.get("task_number")
    if num is not None:
        return [int(num)]
    return []


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Give a number: /done 1")
        return
    try:
        num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("The number must be an integer.")
        return

    task_map = context.chat_data.get("task_map", {})
    task_id = task_map.get(num)
    if not task_id:
        await update.message.reply_text(
            f"Number {num} not found. Run /list first"
        )
        return

    result = await storage.complete_task(task_id)
    line = _close_line(num, result)
    await update.message.reply_text(line)


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Give a number: /del 1")
        return
    try:
        num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("The number must be an integer.")
        return

    task_map = context.chat_data.get("task_map", {})
    task_id = task_map.get(num)
    if not task_id:
        await update.message.reply_text(
            f"Number {num} not found. Run /list first"
        )
        return

    ok = await storage.delete_task(task_id)
    if ok:
        await update.message.reply_text(f"Deleted #{num}")
    else:
        await update.message.reply_text("Todoist API error.")


async def _resolve_task(num: int, context: ContextTypes.DEFAULT_TYPE):
    """Look up Todoist task ID by number from last /list."""
    task_map = context.chat_data.get("task_map", {})
    return task_map.get(num)


def _filter_tasks(tasks: list, filter_val: str) -> list:
    """Filter task list based on LLM-provided filter value."""
    if filter_val == "someday":
        return [t for t in tasks if "someday" in t.get("labels", [])]
    if filter_val == "kick":
        return [t for t in tasks if "kick" in t.get("labels", [])]
    if filter_val == "event":
        return [t for t in tasks if "event" in t.get("labels", [])]

    # Exclude someday by default (unless explicitly requested above)
    tasks = [t for t in tasks if "someday" not in t.get("labels", [])]

    if not filter_val or filter_val == "all":
        return tasks

    if filter_val == "work":
        return [t for t in tasks if t.get("category") == "work"]
    if filter_val == "personal":
        return [t for t in tasks if t.get("category") == "personal"]
    if filter_val == "today":
        today_str = date.today().isoformat()
        return [t for t in tasks if t.get("due_date") == today_str]
    if filter_val == "overdue":
        today_str = date.today().isoformat()
        return [t for t in tasks if t.get("due_date") and t["due_date"] < today_str]

    # Try matching project name (case-insensitive)
    lower = filter_val.lower()
    by_project = [t for t in tasks if (t.get("project") or "").lower() == lower]
    if by_project:
        return by_project

    # Fallback: text search in content
    return [t for t in tasks if lower in (t.get("content") or "").lower()]


async def _handle_list(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict):
    """Handle list intent: fetch, filter, format tasks."""
    filter_val = result.get("filter", "all")
    limit = result.get("limit")

    tasks = await storage.list_tasks()
    if not tasks:
        await update.message.reply_text("No open tasks.")
        return

    # Skip subtasks
    tasks = [t for t in tasks if not t.get("parent_id")]

    # Apply filter
    filtered = _filter_tasks(tasks, filter_val)
    if not filtered:
        await update.message.reply_text(f"Nothing found for filter: {filter_val}")
        return

    # Apply limit
    total = len(filtered)
    if limit and isinstance(limit, (int, float)) and limit > 0:
        filtered = filtered[:int(limit)]

    # Group by project for display
    by_project: dict[str, list] = {}
    for t in filtered:
        proj = t.get("project") or "Inbox"
        by_project.setdefault(proj, []).append(t)

    # For event filter: preload reminder schedules from comments
    schedules = {}
    if filter_val == "event":
        for t in filtered:
            try:
                comments = await storage.get_comments(t["id"])
                texts = [c.get("content", "") for c in comments]
                for text in reversed(texts):
                    if text.startswith("reminders:"):
                        parts = text.split(":", 1)[1].strip().split(",")
                        schedules[t["id"]] = [int(p.strip()) for p in parts]
                        break
            except Exception:
                pass

    task_map = {}
    lines = []
    idx = 1
    for proj_name, proj_tasks in by_project.items():
        if lines:
            lines.append("")
        lines.append(f"-- {proj_name} --")
        for t in proj_tasks:
            task_map[idx] = t["id"]
            suffix = ""
            if t.get("due_date"):
                suffix += f"  [{_fmt_due(t['due_date'])}]"
            if t.get("is_recurring"):
                suffix += " \U0001f501"
            sched = schedules.get(t["id"])
            if sched:
                days_str = ", ".join(str(d) for d in sched)
                suffix += f"  (reminder in {days_str} d.)"
            lines.append(f"{idx}. {t['content']}{suffix}")
            idx += 1

    context.chat_data["task_map"] = task_map
    context.chat_data["last_list_filter"] = filter_val

    # Show count if filtered or limited
    if filter_val != "all" or limit:
        lines.insert(0, f"Found: {total}, shown: {len(filtered)}")
        lines.insert(1, "")

    text = "\n".join(lines)
    for chunk in split_message(text):
        await update.message.reply_text(chunk)


async def _handle_priority(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict):
    """Handle priority intent: fetch tasks, ask LLM to rank by importance."""
    scope = result.get("scope", "all")
    limit = result.get("limit", 5)
    if not isinstance(limit, (int, float)) or limit <= 0:
        limit = 5
    limit = int(limit)

    tasks = await storage.list_tasks()
    if not tasks:
        await update.message.reply_text("No open tasks.")
        return

    # Skip subtasks
    tasks = [t for t in tasks if not t.get("parent_id")]

    # Apply scope filter
    filtered = _filter_tasks(tasks, scope)
    if not filtered:
        await update.message.reply_text(f"No tasks to analyze (scope: {scope})")
        return

    # Format tasks for LLM
    today_str = date.today().isoformat()
    lines = []
    for i, t in enumerate(filtered, 1):
        parts = [f"{i}. {t['content']}"]
        if t.get("project"):
            parts.append(f"[{t['project']}]")
        if t.get("due_date"):
            parts.append(f"(due {_fmt_due(t['due_date'])})")
        parts.append(f"({t.get('category', 'personal')})")
        lines.append(" ".join(parts))
    tasks_text = "\n".join(lines)

    # Ask LLM to prioritize
    ranked_result = await prioritize_tasks(tasks_text, limit=min(limit, len(filtered)), today=today_str)

    ranked = ranked_result.get("ranked", [])
    summary = ranked_result.get("summary", "")

    if not ranked:
        await update.message.reply_text("Could not determine priorities. Try again later.")
        return

    # Build response
    resp_lines = []
    task_map = {}
    map_idx = 1
    for item in ranked:
        idx = item.get("index")
        reason = item.get("reason", "")
        if not idx or idx < 1 or idx > len(filtered):
            continue
        t = filtered[idx - 1]
        task_map[map_idx] = t["id"]
        line = f"{map_idx}. {t['content']}"
        if t.get("due_date"):
            due = t["due_date"]
            due_date_str = due[:10]
            if due_date_str < today_str:
                line += f" [overdue: {_fmt_due(due)}]"
            elif due_date_str == today_str:
                line += " [today]"
            else:
                line += f" [{_fmt_due(due)}]"
        if reason:
            line += f"\n   - {reason}"
        resp_lines.append(line)
        map_idx += 1

    context.chat_data["task_map"] = task_map

    header = f"Top {len(resp_lines)} by importance"
    if scope != "all":
        header += f" ({scope})"
    header += ":"

    text = header + "\n\n" + "\n\n".join(resp_lines)
    if summary:
        text += f"\n\n{summary}"

    for chunk in split_message(text):
        await update.message.reply_text(chunk)


async def _handle_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict):
    """Handle cleanup intent: find problematic tasks to delete/fix."""
    scope = result.get("scope", "all")
    limit = result.get("limit", 10)
    criteria = result.get("criteria", "all")
    if not isinstance(limit, (int, float)) or limit <= 0:
        limit = 10
    limit = int(limit)

    tasks = await storage.list_tasks()
    if not tasks:
        await update.message.reply_text("No open tasks.")
        return

    # Skip subtasks
    tasks = [t for t in tasks if not t.get("parent_id")]

    # Apply scope filter
    filtered = _filter_tasks(tasks, scope)
    if not filtered:
        await update.message.reply_text(f"No tasks to analyze (scope: {scope})")
        return

    # Format tasks for LLM
    today_str = date.today().isoformat()
    lines = []
    for i, t in enumerate(filtered, 1):
        parts = [f"{i}. {t['content']}"]
        if t.get("project"):
            parts.append(f"[{t['project']}]")
        if t.get("due_date"):
            parts.append(f"(due {_fmt_due(t['due_date'])})")
        parts.append(f"({t.get('category', 'personal')})")
        lines.append(" ".join(parts))
    tasks_text = "\n".join(lines)

    # Ask LLM to find problems
    cleanup_result = await cleanup_tasks(tasks_text, limit=limit, today=today_str, criteria=criteria)

    problems = cleanup_result.get("problems", [])
    summary = cleanup_result.get("summary", "")
    clean_count = cleanup_result.get("clean_count", 0)

    if not problems:
        msg = "All tasks look fine, nothing to delete."
        if clean_count:
            msg += f" ({clean_count} tasks are OK)"
        await update.message.reply_text(msg)
        return

    # Build response with task_map for easy deletion
    resp_lines = []
    task_map = {}
    map_idx = 1
    action_icons = {"delete": "X", "rephrase": "?", "merge": "="}
    for item in problems:
        idx = item.get("index")
        reason = item.get("reason", "")
        action = item.get("action", "delete")
        if not idx or idx < 1 or idx > len(filtered):
            continue
        t = filtered[idx - 1]
        task_map[map_idx] = t["id"]
        icon = action_icons.get(action.split()[0] if action else "", "X")
        line = f"{map_idx}. [{icon}] {t['content']}"
        if t.get("due_date"):
            due = t["due_date"]
            if due[:10] < today_str:
                days = (date.fromisoformat(today_str) - date.fromisoformat(due)).days
                line += f" [overdue {days}d]"
        line += f"\n   {action}: {reason}"
        resp_lines.append(line)
        map_idx += 1

    context.chat_data["task_map"] = task_map

    header = f"Problem tasks ({len(resp_lines)}):"
    legend = "[X] delete  [?] rephrase  [=] merge"
    text = header + "\n" + legend + "\n\n" + "\n\n".join(resp_lines)
    if summary:
        text += f"\n\n{summary}"
    text += "\n\nTo delete, type: delete <number>"

    for chunk in split_message(text):
        await update.message.reply_text(chunk)


async def _handle_move(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict):
    """Handle move intent: move tasks to a different project."""
    nums = _extract_task_numbers(result)
    project_name = result.get("project", "")

    if not nums:
        await update.message.reply_text("Which tasks to move? Give numbers from /list")
        return
    if not project_name:
        await update.message.reply_text("Move to which project? Type: move 5 to ProjectName")
        return

    # Resolve project name to ID
    project_id = storage.get_project_id_by_name(project_name)
    if not project_id:
        # Try refreshing projects cache
        await storage.get_projects()
        project_id = storage.get_project_id_by_name(project_name)
    if not project_id:
        await update.message.reply_text(f"Project '{project_name}' not found. Check the name.")
        return

    lines = []
    for num in nums:
        task_id = await _resolve_task(num, context)
        if not task_id:
            lines.append(f"#{num} - not found")
            continue
        res = await storage.move_task(task_id, project_id)
        if res["ok"]:
            lines.append(f"#{num} -> {res['project']}")
        else:
            lines.append(f"#{num} - error")
    await update.message.reply_text("\n".join(lines))


async def _do_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Undo the last task action. Used by the Cancel button and text commands."""
    last = context.chat_data.get("last_undo")
    if not last:
        await update.message.reply_text("Nothing to undo.")
        return

    domain = last.get("domain", "task")

    if domain == "task":
        task_id = last.get("task_id")
        if not task_id:
            await update.message.reply_text("Nothing to undo.")
            return
        ok = await storage.delete_task(task_id)
        if ok:
            await update.message.reply_text(f"Undone: {last.get('value', '?')}")
            context.chat_data["last_redo"] = dict(last)
            context.chat_data.pop("last_undo", None)
        else:
            await update.message.reply_text("Undo error.")
    else:
        await update.message.reply_text("Nothing to undo.")


async def _do_redo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Redo: restore the last undone task action."""
    last = context.chat_data.get("last_redo")
    if not last:
        await update.message.reply_text("Nothing to restore.")
        return

    domain = last.get("domain", "task")

    if domain == "task":
        # Todoist: a deleted task cannot be restored, inform the operator.
        await update.message.reply_text("A deleted Todoist task cannot be restored.")
        context.chat_data.pop("last_redo", None)
    else:
        await update.message.reply_text("Nothing to restore.")


async def preflight_pc_task(update: Update, content: str) -> bool:
    """Preflight for the "send this to the laptop" flow, kept INERT and lifted out of handle_text so the
    unified-router catch-all (bot/router_transport.py, which already imports from handlers) can run the
    SAME preflight transport-side. Byte-identical today: handle_text still calls this in place,
    unconditionally, right after its auth/empty/command/strip guards. Returns True iff it HANDLED the
    message (laptop_mode OR the pc-task regex fired -> replied + caller must return); False iff it fell
    through (caller continues to the rest of handle_text).

    Two ways to trigger:
      1) Regex prefix like "laptop X" / "code X"
      2) Earlier click on the "Laptop" button in the project picker -- sets
         _pending_task_flow[chat_id] = {"laptop_mode": True}, this consumes it
    No LLM involved -- pure regex. pc_tasks + claude_bridge stay LAZY-imported in-body (do NOT hoist --
    keeps the import boundary clean; any external token resolves only at call time deep inside the
    submit path)."""
    from . import pc_tasks as _pc_tasks
    from . import claude_bridge as _cb
    _chat_state = _cb._pending_task_flow.get(update.message.chat_id) or {}
    _laptop_mode = bool(_chat_state.get("laptop_mode"))
    _regex_match = _pc_tasks.is_pc_task(content)
    logger.info(
        "handle_text preflight: chat=%s laptop_mode=%s regex_match=%s state=%s content_preview=%r",
        update.message.chat_id, _laptop_mode, _regex_match, _chat_state, content[:80],
    )
    if _laptop_mode or _regex_match:
        # Consume the one-shot laptop_mode flag BEFORE the submit await, so an in-flight failure
        # can never leave a stale flag that re-triggers on the next ordinary message.
        if _laptop_mode:
            _cb._pending_task_flow.pop(update.message.chat_id, None)
            logger.info("handle_text preflight: laptop_mode consumed (chat=%s)", update.message.chat_id)
        # Forward the FULL, unstripped content to the submit path (no LLM -- pure queue write).
        rel_path = await _pc_tasks.submit_pc_task(content, update.message.chat_id)
        if rel_path == "TOO_LONG":
            await update.message.reply_text(
                "That command is too long to queue. Please shorten it and try again."
            )
        elif rel_path:
            await update.message.reply_text(
                f"Command queued for the laptop: `{rel_path}`"
            )
        else:
            await update.message.reply_text(
                "Could not write the command to the queue. Check the platform logs."
            )
        return True
    return False


async def _handle_notes(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict):
    """Demo notes domain: hand a note off to the keyed worker's encrypted-at-rest notes store.

    Reference-architecture example of the "keyless receiver -> keyed worker" split: the note text is
    forwarded to the worker view op ("notes","save_note"); the worker writes it to the encrypted store
    and renders/sends any confirmation. The receiver only reacts via view_reply_for (busy / accepted /
    done). No personal fields -- this is a plain freeform note demo."""
    from .integrations_proxy import view_request
    from . import view_render
    action = result.get("action", "save_note")
    text = (result.get("text") or result.get("content") or "").strip()

    if action == "get_summary":
        res = await view_request("notes", "get_summary",
                                 {"chat_id": update.effective_chat.id})
        reply = view_render.view_reply_for(res)
        if reply:
            await update.message.reply_text(reply)
        return

    if not text:
        await update.message.reply_text("What should I note down? Type: note <text>")
        return

    res = await view_request("notes", "save_note",
                             {"chat_id": update.effective_chat.id, "text": text})
    reply = view_render.view_reply_for(res)
    if reply:
        await update.message.reply_text(reply)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    text = update.message.text
    if not text or text.startswith("/"):
        return

    content = text.strip()
    if not content:
        return

    # Laptop-task preflight: lifted inert into preflight_pc_task so the unified-router catch-all can
    # run the SAME preflight transport-side. Called unconditionally (the preflight log line fires for
    # every message); returns True iff it handled (replied).
    if await preflight_pc_task(update, content):
        return

    # Cancel button - exit any active chat/scenario, then undo the last task action.
    if content == CANCEL_BUTTON:
        # Claude chat - just exit, no undo
        if await stop_claude(context):
            await update.message.reply_text("Cancelled: Claude chat")
            return
        # Check if any active scenario is running - if so, only cancel the scenario, don't undo data
        _has_active_state = any(context.chat_data.get(k) for k in (
            "pending_note", "remind_edit_task_id",
        ))
        # Undo last task action ONLY if no active scenario
        last_undo = context.chat_data.get("last_undo")
        if last_undo and not _has_active_state:
            await _do_undo(update, context)
        # Clear any active states
        cancelled = []
        if context.chat_data.pop("pending_note", None):
            cancelled.append("note")
        if context.chat_data.pop("remind_edit_task_id", None):
            cancelled.append("reminder")
        if cancelled:
            await update.message.reply_text(f"Cancelled: {', '.join(cancelled)}")
        elif not last_undo:
            await update.message.reply_text("Nothing to undo.")
        return

    # Claude chat buttons
    if content == NEW_CHAT_BUTTON:
        await start_new_chat(update, context)
        return
    if content == CONTINUE_BUTTON:
        await continue_chat(update, context)
        return

    # Any menu button (except New/Continue) exits Claude chat automatically
    _MENU_BUTTONS = {DONE_BUTTON, APPEND_BUTTON, BRIDGE_BUTTON}
    if content in _MENU_BUTTONS and is_claude_active(context):
        await stop_claude(context)
        await update.message.reply_text("(chat ended)")

    # Add Note button
    if content == APPEND_BUTTON:
        await _handle_add_note_button(update, context)
        return

    # Pending note: user is typing a note for the last action
    pending = context.chat_data.get("pending_note")
    if pending:
        if content.lower() == "cancel":
            context.chat_data.pop("pending_note", None)
            await update.message.reply_text("Note cancelled.")
            return
        await _handle_pending_note(update, context, content, pending)
        return

    # Chat-first routing: when a Claude chat is ACTIVE, free text is a CHAT MESSAGE, not a command --
    # route it to the chat before the task fast-paths below can misread it. Everything that
    # legitimately outranks a chat message already ran and returned above: Cancel/New/Continue, the
    # menu buttons (which call stop_claude), and the state-guarded note handler. So only genuine free
    # chat text reaches here. If handle_claude_text does not consume it, we fall through unchanged.
    if is_claude_active(context):
        consumed = await handle_claude_text(update, context)
        if consumed:
            return

    # Reminder edit mode: user is typing new schedule after the "Edit" button
    remind_edit_id = context.chat_data.get("remind_edit_task_id")
    if remind_edit_id:
        days = _parse_remind_days(content)
        if days:
            schedule_str = ",".join(str(d) for d in days)
            await storage.add_comment(remind_edit_id, f"reminders: {schedule_str}")
            days_str = ", ".join(str(d) for d in days)
            await update.message.reply_text(f"Reminder in: {days_str} d.")
            context.chat_data.pop("remind_edit_task_id", None)
            return
        # Can't parse - clear state, fall through to normal processing
        context.chat_data.pop("remind_edit_task_id", None)

    # Fast path: simple regex patterns
    m = _DONE_PATTERN.match(content)
    if m:
        num = int(m.group(1) or m.group(2))
        task_id = await _resolve_task(num, context)
        if task_id:
            result = await storage.complete_task(task_id)
            line = _close_line(num, result)
            await update.message.reply_text(line)
        else:
            await update.message.reply_text(f"#{num} not found. Run /list first")
        return

    m = _DEL_PATTERN.match(content)
    if m:
        num = int(m.group(1))
        task_id = await _resolve_task(num, context)
        if task_id:
            ok = await storage.delete_task(task_id)
            await update.message.reply_text(
                f"Deleted #{num}" if ok else "Todoist API error."
            )
        else:
            await update.message.reply_text(f"#{num} not found. Run /list first")
        return

    # Fast path: undo last task action
    if content.lower() in ("undo", "cancel"):
        await _do_undo(update, context)
        return

    # Fast path: redo (restore after accidental undo)
    if content.lower() in ("redo", "restore"):
        await _do_redo(update, context)
        return

    # Fast path: bare number after a task list -> ask what to do
    if content.isdigit():
        num = int(content)
        task_map = context.chat_data.get("task_map", {})
        if task_map and task_map.get(num):
            buttons = [
                InlineKeyboardButton("Done", callback_data=f"task_close_{num}"),
                InlineKeyboardButton("Delete", callback_data=f"task_delete_{num}"),
            ]
            # Context-dependent: add "Edit" for event tasks
            last_filter = context.chat_data.get("last_list_filter")
            if last_filter == "event":
                buttons.append(InlineKeyboardButton("Edit", callback_data=f"remind_edit_{num}"))
            keyboard = InlineKeyboardMarkup([buttons])
            await update.message.reply_text(f"#{num} - what to do?", reply_markup=keyboard)
            return

    # Fast path: edit reminder schedule
    # "remind in 14 and 3 days", "2 - add reminder 3 weeks", "2 remind in 21 days"
    m_remind = re.match(
        r"^(?:(\d+)\s*[-:.]?\s*)?(?:add\s+reminder|change\s+reminder|remind|reminder)\s+(?:in|for)?\s*(.+)$",
        content, re.IGNORECASE,
    )
    if m_remind:
        num_str = m_remind.group(1)
        raw_days_text = m_remind.group(2)

        # Determine target task
        target_task_id = None
        if num_str:
            num = int(num_str)
            task_map = context.chat_data.get("task_map", {})
            target_task_id = task_map.get(num)
            if not target_task_id:
                # Fallback: re-fetch event tasks by position
                logger.info(f"remind: task_map miss for #{num}, keys={list(task_map.keys())}, fetching events")
                all_tasks = await storage.list_tasks()
                events = [t for t in all_tasks
                          if "event" in t.get("labels", []) and not t.get("parent_id")]
                if num <= len(events):
                    target_task_id = events[num - 1]["id"]
            if not target_task_id:
                await update.message.reply_text(f"#{num} not found. Show reminders first")
                return
        else:
            target_task_id = context.chat_data.get("last_event_task_id")
            if not target_task_id:
                await update.message.reply_text("No event task to edit.")
                return

        new_days = _parse_remind_days(raw_days_text)
        if not new_days:
            await update.message.reply_text("Could not parse days. Example: remind in 14 and 3 days")
            return

        # "add" = merge with existing schedule, otherwise replace
        is_add = "add" in content.lower()
        if is_add:
            comments = await storage.get_comments(target_task_id)
            texts = [c.get("content", "") for c in comments]
            existing = []
            for t in reversed(texts):
                if t.startswith("reminders:"):
                    parts = t.split(":", 1)[1].strip().split(",")
                    try:
                        existing = [int(p.strip()) for p in parts]
                    except ValueError:
                        pass
                    break
            merged = sorted(set(existing + new_days), reverse=True)
        else:
            merged = new_days

        schedule_str = ",".join(str(d) for d in merged)
        await storage.add_comment(target_task_id, f"reminders: {schedule_str}")
        # Ensure event label (merge with existing)
        task_data = await storage.get_task(target_task_id)
        if task_data:
            labels = list(set(task_data.get("labels", []) + ["event"]))
            await storage.update_task_labels(target_task_id, labels)
        days_str = ", ".join(str(d) for d in merged)
        await update.message.reply_text(f"Reminder in: {days_str} d.")
        return

    # LLM intent analysis (pass project names for smart assignment)
    try:
        projects = await storage.get_projects()
        project_names = list(projects.keys()) if projects else None
    except Exception as e:
        logger.warning(f"get_projects failed: {e}")
        project_names = None

    try:
        result = await analyze(content, project_names=project_names)
    except Exception as e:
        logger.exception("LLM analysis failed")
        await update.message.reply_text(f"Error: {type(e).__name__}: {e}")
        return

    # Multi-intent: the LLM may return an array for mixed messages
    if isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            item_intent = item.get("intent", "new")
            if item_intent == "notes":
                await _handle_notes(update, context, item)
            else:
                await _add_task(update, context, item, content)
        return

    intent = result.get("intent", "new")

    if intent == "_llm_down":
        await update.message.reply_text(
            "All LLM backends are unavailable, can't parse the message. Try again later."
        )
        return

    if intent == "close":
        nums = _extract_task_numbers(result)
        comment = result.get("comment")
        permanent = result.get("permanent", False)
        if not nums:
            await update.message.reply_text(
                "Which task to close? Give a number from /list"
            )
            return
        lines = []
        for num in nums:
            task_id = await _resolve_task(num, context)
            if not task_id:
                lines.append(f"#{num} - not found")
                continue
            close_result = await storage.complete_task(task_id, comment=comment, permanent=permanent)
            lines.append(_close_line(num, close_result))
        if comment:
            lines.append(f"Comment: {comment}")
        await update.message.reply_text("\n".join(lines))

    elif intent == "delete":
        nums = _extract_task_numbers(result)
        if not nums:
            await update.message.reply_text(
                "Which task to delete? Give a number from /list"
            )
            return
        lines = []
        for num in nums:
            task_id = await _resolve_task(num, context)
            if not task_id:
                lines.append(f"#{num} - not found")
                continue
            ok = await storage.delete_task(task_id)
            lines.append(f"#{num} - deleted" if ok else f"#{num} - API error")
        await update.message.reply_text("\n".join(lines))

    elif intent == "list":
        await _handle_list(update, context, result)

    elif intent == "priority":
        await _handle_priority(update, context, result)

    elif intent == "cleanup":
        await _handle_cleanup(update, context, result)

    elif intent == "move":
        await _handle_move(update, context, result)

    elif intent == "notes":
        await _handle_notes(update, context, result)

    else:  # intent == "new"
        await _add_task(update, context, result, content)


async def _add_task(update: Update, context: ContextTypes.DEFAULT_TYPE, result: dict, content: str):
    """Create a Todoist task from a parsed intent (shared by single + multi-intent paths)."""
    category = result.get("category", "personal")
    task_content = result.get("content", content)
    due_string = result.get("due_string")
    project_name = result.get("project")
    project_id = storage.get_project_id_by_name(project_name) if project_name else None
    task = await storage.add_task(task_content, category, due_string=due_string, project_id=project_id)
    cat_icon = "[W] " if category == "work" else ""
    parts = [f"{cat_icon}{task['content']}"]
    if task.get("project"):
        parts.append(f"({task['project']})")
    if task.get("due_date"):
        date_info = _fmt_due(task["due_date"])
        if task.get("is_recurring"):
            date_info += " \U0001f501"
        parts.append(f"[{date_info}]")

    # Event reminder setup (only if task has a date)
    is_event = result.get("is_event", False)
    remind_days = result.get("remind_days")
    if is_event and not remind_days:
        remind_days = [7, 1, 0]  # default schedule
    if is_event and task.get("id") and remind_days and task.get("due_date"):
        await storage.update_task_labels(task["id"], [category, "event"])
        schedule_str = ",".join(str(d) for d in remind_days)
        await storage.add_comment(task["id"], f"reminders: {schedule_str}")
        days_str = ", ".join(str(d) for d in remind_days)
        parts.append(f"\nReminder in: {days_str} d.")
        context.chat_data["last_event_task_id"] = task["id"]
        context.chat_data["last_event_labels"] = [category, "event"]

    if task.get("id") and task.get("due_date") and "T" in task["due_date"]:
        if arm_precise_reminder(
            task["id"], task["due_date"], update.effective_chat.id,
        ):
            when = _fmt_due(task["due_date"]).replace(", ", " at ")
            parts.append(f"\n⏰ Reminder {when}.")

    # Track for undo
    if task.get("id"):
        context.chat_data["last_undo"] = {
            "domain": "task", "task_id": task["id"], "value": task["content"],
        }

    await update.message.reply_text(" ".join(parts))


async def _handle_add_note_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add Note: attach a comment to the last task action."""
    last = context.chat_data.get("last_undo", {})
    domain = last.get("domain", "")

    if domain == "task":
        task_id = last.get("task_id")
        task_name = last.get("value", "task")
        context.chat_data["pending_note"] = {"domain": "task", "task_id": task_id}
        await update.message.reply_text(f"Add to task: {task_name}\nType a comment:")
        return

    await update.message.reply_text("No recent task to add a note to.")


async def _handle_pending_note(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, pending: dict
):
    """Process the note text after the operator responded to the Add Note prompt."""
    domain = pending.get("domain", "")
    context.chat_data.pop("pending_note", None)

    if domain == "task":
        task_id = pending.get("task_id")
        if task_id:
            ok = await storage.add_comment(task_id, text)
            if ok:
                await update.message.reply_text("\U0001f4dd Comment added")
            else:
                await update.message.reply_text("Error adding comment.")
        return

    await update.message.reply_text("Could not determine the context for the note.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    # If Claude chat is active, forward photo as text description
    if is_claude_active(context):
        caption = update.message.caption or ""
        msg = f"[The operator sent a photo]{': ' + caption if caption else ''}"
        update.message.text = msg
        consumed = await handle_claude_text(update, context)
        if consumed:
            return

    photo = update.message.photo[-1]  # largest size
    file = await context.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    caption = update.message.caption or None
    msg_id = update.message.message_id

    # Immediate ack so the operator sees the bot heard them even if Vision is slow.
    try:
        await context.bot.send_chat_action(update.effective_chat.id, "typing")
    except Exception:
        pass

    def _warm_error() -> str:
        quoted = ""
        if caption:
            short = caption.strip()
            if len(short) > 100:
                short = short[:100] + "…"
            quoted = f"\n\nYour caption: {short}"
        return "Couldn't read the photo. Send it again or type it out." + quoted

    replied = False
    logger.info("[photo] start msg_id=%s caption_len=%d", msg_id, len(caption or ""))
    try:
        try:
            raw_result = await analyze_image(bytes(image_bytes), "image/jpeg", caption)
        except Exception as e:
            logger.exception("[photo] analyze_image failed msg_id=%s: %s", msg_id, e)
            await update.message.reply_text(_warm_error())
            replied = True
            return

        # Vision may return a list when the photo contains multiple items
        items = raw_result if isinstance(raw_result, list) else [raw_result]
        valid_items = [r for r in items if isinstance(r, dict)]
        logger.info("[photo] vision msg_id=%s items=%d valid=%d", msg_id, len(items), len(valid_items))
        if not valid_items:
            await update.message.reply_text(_warm_error())
            replied = True
            return

        for result in valid_items:
            category = result.get("category", "personal")
            task_content = result.get("content", caption or "task from photo")
            due_string = result.get("due_string")
            is_event = result.get("is_event", False)
            remind_days = result.get("remind_days")
            search_query = result.get("search_query")
            venue = result.get("venue")

            # Auto-detect event: venue + due_string = event ticket
            if not is_event and venue and due_string:
                is_event = True
                logger.info("Auto-detected event: venue=%s, due=%s", venue, due_string)

            # Web search to find event name and venue (always for events)
            if is_event and not search_query and venue:
                # Build search query from venue + date if vision didn't provide one
                search_query = f"{venue} {due_string or ''} playbill".strip()
                logger.info("Auto-built search_query: %s", search_query)
            if is_event and search_query:
                search_result = await search_event(search_query)
                found_event = search_result.get("event")
                found_venue = search_result.get("venue")
                # Use search event name if vision returned a generic/seating fallback
                _generic_events = ("event", "ticket")
                _seating_words = ("stand", "sector", "stalls", "box", "balcony", "amphitheatre",
                                  "dress circle", "dance floor", "fan zone", "vip", "general admission")
                # Season/festival umbrella keywords: the venue's whole season, NOT a specific show.
                # Vision often mistakes a season banner for a specific event; that produces titles
                # like "Opera Festival @ Some Arena" -- a placeholder disguised as a real title.
                # Detect and fall back to a clearly generic label so it does not masquerade as the
                # concrete event name.
                _umbrella_words = (
                    "festival", "cartellone", "stagione", "season",
                    "programma", "programme", "playbill",
                    "concert series", "opera festival", "summer season",
                )
                content_lower = task_content.lower()
                is_generic = content_lower in _generic_events
                is_seating = any(w in content_lower for w in _seating_words)

                def _looks_like_umbrella(text: str, venue_hint: str | None) -> bool:
                    if not text:
                        return False
                    t = text.lower().strip()
                    if not any(w in t for w in _umbrella_words):
                        return False
                    tokens = [w for w in re.split(r"\W+", t) if w]
                    if len(tokens) > 5:
                        return False
                    stop = {"the", "of", "di", "de", "la", "el", "das", "am"}
                    if venue_hint:
                        v_tokens = {w for w in re.split(r"\W+", venue_hint.lower()) if w} - stop
                        if v_tokens & (set(tokens) - stop):
                            return True
                    return True

                is_umbrella = _looks_like_umbrella(task_content, venue)
                found_umbrella = _looks_like_umbrella(found_event or "", venue or found_venue)
                if is_umbrella:
                    if found_event and not found_umbrella:
                        task_content = found_event
                        logger.info("Umbrella '%s' replaced with search event '%s'",
                                    content_lower, found_event)
                    else:
                        logger.info("Umbrella '%s' - search also umbrella, falling back to generic",
                                    content_lower)
                        task_content = "Event"
                elif found_event and (is_generic or is_seating):
                    task_content = found_event
                elif found_event and found_event.lower() != task_content.lower():
                    # search_event is HIGH-CONFIDENCE real web search (not snippets), while vision
                    # often grabs a cast member / section header off the page. Prefer the web-confirmed
                    # event name.
                    logger.info("Web search event '%s' overrides vision '%s'", found_event, task_content)
                    task_content = found_event
                # Use search venue if vision returned a generic/unclear venue
                generic_venues = ("hall", "room")
                if found_venue and venue:
                    vision_venue_lower = venue.lower().strip()
                    # Only replace if vision venue is EXACTLY a generic word
                    is_generic_venue = vision_venue_lower in generic_venues
                    if is_generic_venue:
                        venue = found_venue
                        logger.info("Replaced generic venue '%s' with search venue '%s'", vision_venue_lower, found_venue)
                elif found_venue and not venue:
                    venue = found_venue

            # Add short venue name (without full address)
            if venue and venue not in task_content:
                short_venue = venue.split(",")[0].strip()
                task_content = f"{task_content} @ {short_venue}"

            project_name = result.get("project")
            project_id = storage.get_project_id_by_name(project_name) if project_name else None
            task = await storage.add_task(task_content, category, due_string=due_string, project_id=project_id)
            cat_icon = "[W] " if category == "work" else ""
            parts = [f"{cat_icon}{task['content']}"]
            if task.get("project"):
                parts.append(f"({task['project']})")
            if task.get("due_date"):
                date_info = _fmt_due(task["due_date"])
                if task.get("is_recurring"):
                    date_info += " \U0001f501"
                parts.append(f"[{date_info}]")

            # Event reminder setup (same as text handler)
            if is_event and not remind_days:
                remind_days = [7, 1, 0]  # default schedule
            if is_event and task.get("id") and remind_days:
                await storage.update_task_labels(task["id"], [category, "event"])
                schedule_str = ",".join(str(d) for d in remind_days)
                await storage.add_comment(task["id"], f"reminders: {schedule_str}")
                days_str = ", ".join(str(d) for d in remind_days)
                parts.append(f"\nReminder in: {days_str} d.")
                context.chat_data["last_event_task_id"] = task["id"]
            # Honest instead of a silent placeholder: if the title was never confirmed by vision or
            # web search, say so directly.
            if is_event and task_content.split(" @ ")[0].strip().lower() in ("event",):
                parts.append("\n⚠️ Couldn't confirm the exact event name online -- "
                             "it stayed generic in the task, edit it if you like.")

            if task.get("id") and task.get("due_date") and "T" in task["due_date"]:
                if arm_precise_reminder(
                    task["id"], task["due_date"], update.effective_chat.id,
                ):
                    when = _fmt_due(task["due_date"]).replace(", ", " at ")
                    parts.append(f"\n⏰ Reminder {when}.")

            # Track for undo
            if task.get("id"):
                context.chat_data["last_undo"] = {
                    "domain": "task", "task_id": task["id"], "value": task["content"],
                }

            await update.message.reply_text(" ".join(parts))
            replied = True
    except Exception as e:
        logger.exception("[photo] unexpected error msg_id=%s: %s", msg_id, e)
    finally:
        if not replied:
            try:
                await update.message.reply_text(_warm_error())
            except Exception:
                logger.exception("[photo] failed to send safety-net reply msg_id=%s", msg_id)
        logger.info("[photo] done msg_id=%s replied=%s", msg_id, replied)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks for task actions."""
    query = update.callback_query
    await query.answer()

    if not is_authorized(update):
        return

    data = query.data
    logger.info(f"[callback] data={data}")

    # Task action callbacks (close/delete from inline buttons)
    if data.startswith("task_close_") or data.startswith("task_delete_"):
        action, num_str = data.rsplit("_", 1)
        num = int(num_str)
        task_map = context.chat_data.get("task_map", {})
        task_id = task_map.get(num)
        if not task_id:
            await query.edit_message_text(f"#{num} - not found")
            return
        if "close" in action:
            result = await storage.complete_task(task_id)
            line = _close_line(num, result)
            await query.edit_message_text(line)
        else:
            ok = await storage.delete_task(task_id)
            await query.edit_message_text(
                f"#{num} - deleted" if ok else f"#{num} - delete error"
            )
        return

    # Remind edit callback: show current schedule, enter edit mode
    if data.startswith("remind_edit_"):
        num = int(data.split("_")[-1])
        task_map = context.chat_data.get("task_map", {})
        task_id = task_map.get(num)
        if not task_id:
            await query.edit_message_text(f"#{num} - not found")
            return
        comments = await storage.get_comments(task_id)
        texts = [c.get("content", "") for c in comments]
        current = []
        for t in reversed(texts):
            if t.startswith("reminders:"):
                parts = t.split(":", 1)[1].strip().split(",")
                try:
                    current = [int(p.strip()) for p in parts]
                except ValueError:
                    pass
                break
        current_str = ", ".join(str(d) for d in current) if current else "not set"
        context.chat_data["remind_edit_task_id"] = task_id
        await query.edit_message_text(
            f"Currently: {current_str} d.\nType a new schedule, e.g.: 21, 7, 1, 0"
        )
        return
