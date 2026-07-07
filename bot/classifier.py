import json
import logging
import re
from datetime import datetime

from .secrets_loader import get_secret

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an assistant inside a task-management Telegram bot. The user sends a text message.
Determine the intent and return JSON (no markdown, no ```):

1) New task - the user wants to record something for later:
   {"intent": "new", "category": "work|personal", "content": "task text", "due_string": "date string or null", "project": "project name or null", "is_event": false, "remind_days": null}
   - work: work-related (code, design, tickets, meetings, sprints, deployments)
   - personal: personal (shopping, home, hobbies, money, errands)
   IMPORTANT: if the message contains a NUMBERED LIST ("1. ... 2. ... 3. ...") of chores/work tasks - this is an ARRAY of intent=new tasks! Each item = a separate task.
   - project: an existing project name from the list below, or null for Inbox
   - due_string: date/recurrence in ENGLISH for the task backend. Examples:
     "tomorrow", "friday", "in 1 week", "every day", "every week", "every month",
     "every monday, wednesday", "march 15". If there is no date/recurrence in the text -> null
   - is_event: true if the task is tied to a SPECIFIC DATE/TIME and cannot simply be moved:
     concert, doctor, flight, birthday, ticket, appointment, car service, meeting, interview, exam, registration, booking.
     false for an ordinary task/deadline doable any time before the date (submit a report, buy milk, set up CI).
     RULE: if there is a SPECIFIC TIME (12:30, 15:00) - almost always is_event=true (it is an appointment/meeting).
   - remind_days: array of how many days BEFORE to remind. Only if is_event=true, otherwise null.
     Examples: haircut [7,2], concert [7,1,0], doctor [3,1,0], flight [7,3,1,0], birthday [7,1,0], booking [3,1,0]

2) Closing tasks - the user says a task (or tasks) is done/complete:
   {"intent": "close", "task_numbers": [121], "comment": "comment or null", "permanent": false}
   task_numbers - ALWAYS an array. May be one number [121], several [5, 8, 12], or empty [] if no number is given.
   A range "117-120" expands to [117, 118, 119, 120].
   permanent: true if the user wants to close FULLY/FOREVER (all repetitions of a recurring task).
   Default false (closes only the current iteration of a recurring task).

3) Deleting tasks:
   {"intent": "delete", "task_numbers": [5]}
   Same as above - an array of numbers, ranges supported.

4) Show tasks - the user wants to VIEW their tasks (NOT create a new one):
   {"intent": "list", "filter": "filter description", "limit": number or null}
   filter - what to show:
   - "all" - all tasks (EXCLUDING deferred/someday)
   - "work" - work tasks only
   - "personal" - personal tasks only
   - "someday" - deferred tasks (with the someday label)
   - "kick" - tasks that need a nudge (with the kick label)
   - "event" - events with reminders (with the event label)
   - a project name - tasks from a specific project
   - "overdue" - overdue tasks
   - "today" - due today
   - free text to search by content
   limit - how many to show (null = all matching)
   IMPORTANT: any message with "show", "list", "what do I have", "which tasks", "first N" - is intent=list, NOT a new task!

5) Priority tasks - the user wants to know what is MOST important/urgent:
   {"intent": "priority", "scope": "all|work|personal|project name", "limit": number or 5}
   scope - the set of tasks to analyze (default "all")
   limit - how many to show (default 5)
   IMPORTANT: "important", "most important", "priorities", "what is on fire", "what to do first", "urgent" - is intent=priority!

6) Notes - the user wants to save a free-text note, or read a summary of saved notes:
   {"intent": "notes", "action": "save_note|get_summary", "content": "note text or null"}
   action:
   - "save_note" - store one free-text note; put the note text in "content"
   - "get_summary" - show a short summary of saved notes (count + recent previews)
   Trigger words: "note", "remember", "jot down", "save note", "my notes", "show notes", "notes summary".
   Examples:
   - "note: call the plumber on Friday" -> {"intent": "notes", "action": "save_note", "content": "call the plumber on Friday"}
   - "show my notes" -> {"intent": "notes", "action": "get_summary", "content": null}

7) Review/cleanup tasks - the user wants to find UNNEEDED/POINTLESS/DUPLICATE tasks to delete:
   {"intent": "cleanup", "scope": "all|work|personal|project name", "limit": number or 10, "criteria": "criteria description"}
   scope - the set of tasks to analyze (default "all")
   limit - how many to show (default 10)
   criteria - what to look for: "useless", "vague", "duplicate", "outdated", "all"
   IMPORTANT: "stupid tasks", "what to delete", "unneeded", "useless", "duplicates", "clean up", "junk" - is intent=cleanup!

8) Moving tasks to another project:
   {"intent": "move", "task_numbers": [61, 63, 64], "project": "project name"}
   task_numbers - numbers from the list (ALWAYS an array), ranges supported
   project - the project name to move them to
   IMPORTANT: "move", "relocate", "into project X", "move to X" - is intent=move!
   If the user writes "-- ProjectName -- move 1,2,3" - project = "ProjectName"

9) Correcting the last action - the user wants to FIX/CHANGE what the bot just recorded:
   {"intent": "correct", "value": "new value", "note": "clarification or null"}
   value - the correct value
   STRICT RULE: intent=correct ONLY if the message explicitly contains a correcting marker:
   "no,", "not that", "wrong", "incorrect", "fix", "change to", "replace with", "mistake", "it was X".
   If such markers are ABSENT - this is NOT correct, even if the message starts with an imperative verb
   ("make", "add", "create", "draw", "write", "redo") and mentions an icon/image/avatar/design/UI/code.
   A request to "make something nice/cool/proper" is NOT correct - it is an ordinary task (intent=new) or just a message to the developer.
   Criticism ("ugly", "terrible", "bad", "don't like it") without correction markers is NOT correct.

Examples:
- "buy milk" -> {"intent": "new", "category": "personal", "content": "buy milk", "due_string": null, "project": null, "is_event": false, "remind_days": null}
- "buy milk every week" -> {"intent": "new", "category": "personal", "content": "buy milk", "due_string": "every week", "project": null, "is_event": false, "remind_days": null}
- "book a doctor on Friday" -> {"intent": "new", "category": "personal", "content": "book a doctor", "due_string": "friday", "project": null, "is_event": true, "remind_days": [3, 1, 0]}
- "team call tomorrow at 15:00" -> {"intent": "new", "category": "work", "content": "team call", "due_string": "tomorrow at 15:00", "project": null, "is_event": false, "remind_days": null}
- "concert on june 15" -> {"intent": "new", "category": "personal", "content": "concert", "due_string": "june 15", "project": null, "is_event": true, "remind_days": [7, 1, 0]}
- "haircut on march 20" -> {"intent": "new", "category": "personal", "content": "haircut", "due_string": "march 20", "project": null, "is_event": true, "remind_days": [7, 2]}
- "car service on april 25" -> {"intent": "new", "category": "personal", "content": "car service", "due_string": "april 25", "project": null, "is_event": true, "remind_days": [7, 3, 1]}
- "121 done, already moved" -> {"intent": "close", "task_numbers": [121], "comment": "already moved"}
- "close 117-120" -> {"intent": "close", "task_numbers": [117, 118, 119, 120], "comment": null}
- "close 5, 8 and 12" -> {"intent": "close", "task_numbers": [5, 8, 12], "comment": null}
- "close 5 fully" -> {"intent": "close", "task_numbers": [5], "comment": null, "permanent": true}
- "delete 8" -> {"intent": "delete", "task_numbers": [8]}
- "delete 3-5" -> {"intent": "delete", "task_numbers": [3, 4, 5]}
- "set up CI/CD for the project" -> {"intent": "new", "category": "work", "content": "set up CI/CD for the project", "due_string": null, "project": null, "is_event": false, "remind_days": null}
- "submit the report on Friday" -> {"intent": "new", "category": "work", "content": "submit the report", "due_string": "friday", "project": null, "is_event": false, "remind_days": null}
- "show work tasks" -> {"intent": "list", "filter": "work", "limit": null}
- "show personal" -> {"intent": "list", "filter": "personal", "limit": null}
- "show first 10" -> {"intent": "list", "filter": "all", "limit": 10}
- "what's due today" -> {"intent": "list", "filter": "today", "limit": null}
- "overdue" -> {"intent": "list", "filter": "overdue", "limit": null}
- "list" -> {"intent": "list", "filter": "all", "limit": null}
- "show deferred" -> {"intent": "list", "filter": "someday", "limit": null}
- "show kick" -> {"intent": "list", "filter": "kick", "limit": null}
- "show reminders" -> {"intent": "list", "filter": "event", "limit": null}
- "show the most important tasks" -> {"intent": "priority", "scope": "all", "limit": 5}
- "what's on fire?" -> {"intent": "priority", "scope": "all", "limit": 5}
- "important work items" -> {"intent": "priority", "scope": "work", "limit": 5}
- "what should I do first?" -> {"intent": "priority", "scope": "all", "limit": 5}
- "priorities" -> {"intent": "priority", "scope": "all", "limit": 5}
- "show 3 most urgent" -> {"intent": "priority", "scope": "all", "limit": 3}
- "note: pick up the parcel" -> {"intent": "notes", "action": "save_note", "content": "pick up the parcel"}
- "show my notes" -> {"intent": "notes", "action": "get_summary", "content": null}
- "notes summary" -> {"intent": "notes", "action": "get_summary", "content": null}
- "show stupid tasks" -> {"intent": "cleanup", "scope": "all", "limit": 10, "criteria": "useless"}
- "what can I delete?" -> {"intent": "cleanup", "scope": "all", "limit": 10, "criteria": "all"}
- "any duplicate tasks?" -> {"intent": "cleanup", "scope": "all", "limit": 10, "criteria": "duplicate"}
- "clean up the task list" -> {"intent": "cleanup", "scope": "all", "limit": 10, "criteria": "all"}
- "which tasks are vague and unclear" -> {"intent": "cleanup", "scope": "all", "limit": 10, "criteria": "vague"}
- "move 61,63,64 to Home & Life" -> {"intent": "move", "task_numbers": [61, 63, 64], "project": "Home & Life"}
- "-- Errands -- move 5, 8 here" -> {"intent": "move", "task_numbers": [5, 8], "project": "Errands"}
- "move 12 to Work" -> {"intent": "move", "task_numbers": [12], "project": "Work"}
- "3-5 move to Inbox" -> {"intent": "move", "task_numbers": [3, 4, 5], "project": "Inbox"}
- "no, fix it, it's 73" -> {"intent": "correct", "value": "73", "note": null}
- "wrong, change to Work" -> {"intent": "correct", "value": "Work", "note": null}
- "The icon picture is ugly. Make some cool superhero" -> {"intent": "new", "category": "personal", "content": "The icon picture is ugly. Make some cool superhero", "due_string": null, "project": null, "is_event": false, "remind_days": null} (NOT correct: no correction markers; this is a request/feedback, not an edit of a record)
- "the bot avatar is dull, redo it" -> {"intent": "new", "category": "personal", "content": "redo the bot avatar", "due_string": null, "project": null, "is_event": false, "remind_days": null} (NOT correct)
- "make a proper interface" -> {"intent": "new", "category": "personal", "content": "make a proper interface", "due_string": null, "project": null, "is_event": false, "remind_days": null} (NOT correct)
- "close both the project and the subtask. Moved." -> {"intent": "close", "task_numbers": [], "comment": "Moved."}
- "Tasks for the handyman. 1. Install the guitar mount. 2. Fix the oven door. 3. Mount the drying rack in the bathroom." -> [{"intent":"new","category":"personal","content":"Install the guitar mount","due_string":null,"project":null,"is_event":false,"remind_days":null},{"intent":"new","category":"personal","content":"Fix the oven door","due_string":null,"project":null,"is_event":false,"remind_days":null},{"intent":"new","category":"personal","content":"Mount the drying rack in the bathroom","due_string":null,"project":null,"is_event":false,"remind_days":null}]


Important: if the text has a number and the context points to a task action - this is NOT a new task, but close/delete.
Expand a range N-M fully: 3-6 -> [3, 4, 5, 6].
{projects_section}
Today: {today}, weekday: {weekday}. If the user gives a date without a year - use the CURRENT year. If the date has already passed this year - still use the current year (the user means the nearest such date).
Date formats: "28.03", "28/03", "march 28" -> YYYY-MM-DD.
Return ONLY JSON."""


def _build_prompt(project_names: list[str] | None = None) -> str:
    """Build system prompt with optional project list."""
    if project_names:
        section = "Available projects: " + ", ".join(project_names) + "\nIf a task fits a project - name it. Otherwise project = null (Inbox)."
    else:
        section = ""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import os as _os
    # Local-time offset in hours from UTC (configurable). Defaults to 0 (UTC).
    try:
        _offset = int(_os.getenv("AIOS_LOCAL_UTC_OFFSET_HOURS", "0"))
    except ValueError:
        _offset = 0
    today = _dt.now(_tz(_td(hours=_offset))).date()
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    weekday = weekdays[today.weekday()]
    return SYSTEM_PROMPT.replace("{projects_section}", section).replace("{today}", today.isoformat()).replace("{weekday}", weekday)


def _get_backends() -> list[str]:
    """Return list of available backends in priority order. Anthropic-only.
    ALWAYS attempt Anthropic — when the transport has no LOCAL ANTHROPIC_API_KEY,
    _call_anthropic routes the call through the channel to the worker that holds the key.
    If neither the local key nor the channel works, the call fails and the caller falls back."""
    return ["anthropic"]


async def _call_anthropic(text: str, prompt: str, max_tokens: int = 200, model: str = "claude-opus-4-8") -> str:
    # Route through integrations_proxy — the KEYED process calls Anthropic directly, the
    # KEYLESS transport routes the call to the worker that holds ANTHROPIC_API_KEY. Raise
    # on empty so callers fall back (preserves the raise-on-failure contract).
    from .integrations_proxy import anthropic_complete
    out = await anthropic_complete(system=prompt, user=text, model=model, max_tokens=max_tokens)
    if not out:
        raise RuntimeError("anthropic_complete returned empty")
    return out


async def _call_backend(backend: str, text: str, prompt: str, max_tokens: int = 200) -> str:
    """Call a specific LLM backend. Raises on failure. Anthropic-only."""
    if backend == "anthropic":
        return await _call_anthropic(text, prompt, max_tokens=max_tokens)
    raise ValueError(f"unknown classifier backend: {backend!r}")


def _parse_llm_response(raw: str) -> dict:
    """Parse LLM response, stripping markdown fences and extracting JSON from text."""
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        clean = clean.strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        # LLM may return text before/after JSON - extract it
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start = clean.find(start_char)
            end = clean.rfind(end_char)
            if start != -1 and end > start:
                try:
                    return json.loads(clean[start:end + 1])
                except json.JSONDecodeError:
                    continue
        raise


PRIORITY_PROMPT = """\
Here is the user's task list with dates and projects. Today is {today}.
Rank by importance/urgency and pick the top {limit}.

Criteria (highest to lowest):
1. Overdue tasks (due_date < today) - critical
2. Tasks due today - high priority
3. Tasks with a near deadline (1-3 days) - high
4. Work tasks (category=work) are usually more urgent than personal
5. Tasks with no date - judge by content (concrete obligations and notes > hobbies, vague ideas)

Return JSON (no markdown, no ```):
{{"ranked": [{{"index": N, "reason": "brief explanation"}}], "summary": "overall takeaway 1-2 sentences"}}
index - the task number in the list (starting at 1).
ranked - an array of exactly {limit} items (or fewer if there are few tasks).
Return ONLY JSON."""


CLEANUP_PROMPT = """\
Here is the user's task list with dates and projects. Today is {today}.
Be a STRICT critic! Your job is to find problematic tasks. Do not be afraid to criticize.
If there are more than 20 tasks, some are definitely junk - find it.

Criteria (apply AGGRESSIVELY):
1. Duplicates/near-duplicates - tasks with the same essence, even if worded differently
2. Vague - no concrete action: "think about", "figure out", "look into", "study"
3. Outdated - overdue by weeks/months and clearly forgotten
4. Procrastination/dreams - "someday" tasks: "learn X", "start Y", abstract goals without a plan
5. Trivial - not worth recording: "google", "check", "look at"
6. Eternal - habit-tasks that hang here but will never be closed

{criteria_hint}

For each problematic task explain briefly and to the point:
- "delete" - definitely not needed
- "rephrase" - the idea is fine but the wording is useless
- "merge with N" - a duplicate or very similar

Return JSON (no markdown, no ```):
{{"problems": [{{"index": N, "reason": "why problematic", "action": "delete|rephrase|merge with N"}}], "clean_count": number of clean tasks, "summary": "overall takeaway 1-2 sentences"}}
index - the task number in the list (starting at 1).
problems - an array of up to {limit} of the most problematic tasks, worst to best.
If all tasks are fine - problems is an empty array.
Return ONLY JSON."""

_CRITERIA_HINTS = {
    "useless": "Focus: look for USELESS and HARMFUL tasks that waste time.",
    "vague": "Focus: look for VAGUE tasks with no clear action or outcome.",
    "duplicate": "Focus: look for DUPLICATES and very similar tasks that can be merged.",
    "outdated": "Focus: look for OUTDATED tasks that are long overdue or no longer relevant.",
    "all": "Look for all kinds of problems: duplicates, vague, outdated, useless.",
}


async def cleanup_tasks(tasks_text: str, limit: int = 10, today: str = "", criteria: str = "all") -> dict:
    """Ask LLM to find problematic tasks to delete/fix. Returns parsed JSON."""
    hint = _CRITERIA_HINTS.get(criteria, _CRITERIA_HINTS["all"])
    prompt = CLEANUP_PROMPT.format(limit=limit, today=today, criteria_hint=hint)

    backends = _get_backends()
    for backend in backends:
        try:
            raw = await _call_backend(backend, tasks_text, prompt, max_tokens=1000)
            result = _parse_llm_response(raw)
            logger.info(f"[{backend}] cleanup: {len(result.get('problems', []))} problems found")
            return result
        except Exception as e:
            logger.warning(f"[{backend}] cleanup failed: {e}")
            continue

    return {"problems": [], "clean_count": 0, "summary": "Could not analyze the tasks."}


async def prioritize_tasks(tasks_text: str, limit: int = 5, today: str = "") -> dict:
    """Ask LLM to rank tasks by importance. Returns parsed JSON."""
    prompt = PRIORITY_PROMPT.format(limit=limit, today=today)

    backends = _get_backends()
    for backend in backends:
        try:
            raw = await _call_backend(backend, tasks_text, prompt, max_tokens=600)
            result = _parse_llm_response(raw)
            logger.info(f"[{backend}] prioritize: {len(result.get('ranked', []))} items")
            return result
        except Exception as e:
            logger.warning(f"[{backend}] prioritize failed: {e}")
            continue

    return {"ranked": [], "summary": "Could not analyze priorities."}


IMAGE_PROMPT = """\
The user sent a photo to a task-management Telegram bot. Determine what is in the photo and return JSON (no markdown, no ```).

If the photo has SEVERAL separate items, return a JSON array of several objects.
If the photo has ONE item, return ONE JSON object (not an array).

Possible cases:

1) Ticket/booking/screenshot for an EVENT (concert, match, festival, show),
   OR a venue website page with an "Upcoming shows" / "Season" section where a specific date is visible:
   {"intent": "new", "category": "personal", "content": "event name", "due_string": "date and time in ENGLISH for the task backend", "is_event": true, "remind_days": [7, 1, 0], "venue": "short venue name", "search_query": "search query"}
   IMPORTANT for tickets/bookings:
   - content: ONLY the event/performer name! Do NOT put section, row, seat, ticket type, or address here. If the name is NOT visible - write "event"
   - NEVER use as the event name:
     * The name of the VENUE itself (an arena, a theatre, a hall)
     * The UMBRELLA name of a season/festival that covers hundreds of different shows
       (for example "Opera Festival", "Season 2027", "Playbill"). That is NOT a specific event - it is an umbrella label for a whole season's programme.
     If you see ONLY such a label + a date (no artist, opera, or show name) - set content="event"
     and leave the specific name to the search.
   - AD vs TICKET - CRITICALLY IMPORTANT! A ticket page ALWAYS has promotional banners!
     ALGORITHM (follow STRICTLY step by step):
     1) Find the ORDER DETAILS: status ("Confirmed"), date, time, row/seat, QR code
     2) Remember the DATE from the order details - this is the date of the REAL event
     3) Find ALL event names on the page
     4) Bright banners with artist PHOTOS and DIFFERENT dates are ADS, IGNORE them!
     5) The name of the REAL event is modest text next to the order details, WITHOUT an artist photo, tied to the order date
   - ALWAYS extract the DATE and TIME from the ticket! The date may be in any format.
   - due_string: translate the date into ENGLISH for the task backend (e.g. "june 28 at 17:00")
   - venue: the name of the VENUE/THEATRE/HALL. NOT a hall number, but the REAL place name. If only an address and a hall number are visible - write the street address.
   - search_query: ALWAYS for ALL tickets, even if you are sure of the name! Build the most informative query:
     * Include the site domain if visible
     * Include the street address if visible on the ticket
     * Include the city
     * Include the date
     * NEVER set null for tickets!
   - remind_days: [7, 1, 0] for concerts/matches, [3, 1, 0] for services
1.5) A messenger screenshot with an APPOINTMENT (salon, hairdresser, manicure, doctor, dentist, repair):
   Same format as case 1, but:
   - content: "Appointment" (e.g. "Hair treatment", "Manicure", "Toning")
   - venue: the salon/clinic/specialist name
   - search_query: null (not needed, all info is on the screenshot)
   - remind_days: [3, 1, 0] for services
   IMPORTANT: if the screenshot has SEVERAL appointments (two messages with different dates/services) - return a JSON ARRAY!
2) Work (code, ticket, bug, document) -> {"intent": "new", "category": "work", "content": "description", "due_string": null}
3) Personal (shopping, receipt, place, item) -> {"intent": "new", "category": "personal", "content": "description", "due_string": null}

If there is a caption to the photo - use it for context.
Today: {today}. If a ticket date has no year - use the current year.
Return ONLY JSON."""


async def analyze_image(image_bytes: bytes, media_type: str, caption: str | None = None) -> dict:
    """Analyze image with Claude Vision. Returns dict with intent.

    Route the vision call through integrations_proxy.anthropic_messages — keyless-safe
    (KEYED -> direct; KEYLESS transport -> worker holds the key). The keyless telegram-bot has
    no ANTHROPIC_API_KEY, so a direct client would crash on an empty key; routing works keyless.
    On empty/timeout -> a generic default so a photo still becomes a task (soft-fail, not a crash)."""
    from datetime import date
    import base64
    from .integrations_proxy import anthropic_messages

    _default = {"intent": "new", "category": "personal",
                "content": caption or "task from photo", "due_string": None}

    b64 = base64.b64encode(image_bytes).decode()
    user_content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
    ]
    if caption:
        user_content.append({"type": "text", "text": caption})

    system_prompt = IMAGE_PROMPT.replace("{today}", date.today().isoformat())

    # Opus for vision — best photo recognition (events/tickets). timeout=120 so the keyed worker
    # doesn't cut a slow vision call short; the keyless side keeps a shorter budget.
    payload = {
        "model": "claude-opus-4-8",
        "max_tokens": 500,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }
    try:
        resp = await anthropic_messages(payload, timeout=120)
        raw = ""
        for block in resp.get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                raw = block["text"].strip()
                break
        if not raw:
            logger.warning("[vision] empty response from channel -> default")
            return _default
        result = _parse_llm_response(raw)
        logger.info(f"[vision] opus: {result}")
        return result
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[vision] failed: {e}")
        return _default


def _split_numbered_list(text: str) -> tuple[str, list[str]] | None:
    """Split a numbered task list into (header, [items]).
    Returns None if not a numbered list (fewer than 2 items)."""
    parts = re.split(r'(?<!\d)\d+[.)]\s+', text)
    if len(parts) < 3:
        return None
    header = parts[0].strip()
    items = [p.strip().rstrip('.').strip() for p in parts[1:] if len(p.strip()) >= 5]
    if len(items) < 2:
        return None
    return header, items


async def analyze(text: str, project_names: list[str] | None = None) -> dict:
    """Analyze user message intent. Cascades through available LLM backends."""
    # Pre-check: numbered list (2+ items) → always multiple new tasks
    split = _split_numbered_list(text)
    if split:
        header, items = split
        logger.info(f"[pre-check] numbered list: {len(items)} items")

        # Call LLM for each item individually
        backends = _get_backends()
        prompt = _build_prompt(project_names) if backends else None
        tasks = []
        for item in items:
            category = _keyword_category(header + ' ' + item)
            project = None
            if backends and prompt:
                try:
                    raw = await _call_backend(backends[0], item, prompt)
                    ref = _parse_llm_response(raw)
                    if isinstance(ref, dict) and ref.get('intent') == 'new':
                        category = ref.get('category', category)
                        project = ref.get('project')
                except Exception as e:
                    logger.warning(f"[pre-check] LLM classify '{item[:30]}' failed: {e}")
            tasks.append({
                "intent": "new", "category": category, "content": item,
                "due_string": None, "project": project, "is_event": False, "remind_days": None,
            })
        logger.info(f"[pre-check] numbered list classified: {[(t['content'][:20], t['project']) for t in tasks]}")
        return tasks

    backends = _get_backends()

    if not backends:
        logger.info("No LLM keys, using regex/keywords")
        regex_result = _regex_intent(text)
        if regex_result:
            return regex_result
        return {"intent": "new", "category": _keyword_category(text), "content": text}

    prompt = _build_prompt(project_names)

    # Try each backend in priority order (Anthropic only; regex fallback below)
    for backend in backends:
        try:
            raw = await _call_backend(backend, text, prompt)
            result = _parse_llm_response(raw)
            logger.info(f"[{backend}] analyze: {result}")
            return result
        except Exception as e:
            logger.warning(f"[{backend}] failed: {e}, raw={locals().get('raw', '?')}")
            continue

    # All LLMs failed - fall back to regex for close/delete
    logger.warning("All LLM backends failed, using regex/keywords")
    regex_result = _regex_intent(text)
    if regex_result:
        return regex_result
    # Signal to caller that LLM is down (don't silently create tasks)
    return {"intent": "_llm_down", "content": text}


_CLOSE_RE = re.compile(
    r"(?:(\d+)\s*[-:.]?\s*(?:done|complete|closed|close)"
    r"|(?:done|complete|closed|close)\s*(\d+))",
    re.IGNORECASE,
)
_DEL_RE = re.compile(
    r"(?:delete|del|remove)\s+(\d+)", re.IGNORECASE
)


def _regex_intent(text: str) -> dict | None:
    """Try to detect intent from simple patterns. Returns None if no match."""
    m = _DEL_RE.search(text)
    if m:
        return {"intent": "delete", "task_number": int(m.group(1))}

    m = _CLOSE_RE.search(text)
    if m:
        num = int(m.group(1) or m.group(2))
        # Extract comment: everything after the matched pattern
        remainder = text[m.end():].strip().lstrip(".,;:-").strip()
        return {
            "intent": "close",
            "task_number": num,
            "comment": remainder if remainder else None,
        }

    # "close"/"done" without a number
    lower = text.lower()
    for kw in ("close", "closed", "done", "complete"):
        if kw in lower:
            return {"intent": "close", "task_number": None, "comment": text.strip()}

    return None


WORK_KEYWORDS = [
    "jira", "ticket", "sprint", "meeting", "call", "review", "deploy", "bug",
    "design", "config", "blueprint", "milestone", "use case", "uc-", "ucs-",
]


def _keyword_category(text: str) -> str:
    lower = text.lower()
    for kw in WORK_KEYWORDS:
        if kw in lower:
            return "work"
    return "personal"


# Demo/operational switch for looking up an event on the web. False = the bot does NOT search
# for the event name by venue and date (it creates a task with a generic name and says so honestly).
EVENT_SEARCH_ENABLED = True


async def search_event(query: str) -> dict:
    """Find the SPECIFIC event (show/opera/concert name) via REAL web search — the server-side
    web_search tool on the Messages API, routed through the keyed worker channel
    (anthropic_messages passes the tools array through; keyless transport works unchanged).

    Contract: {"event": ..., "venue": ...}; event is set ONLY when the model reports HIGH
    confidence from actual search results. No fallback guessing — None means "not found".
    """
    if not EVENT_SEARCH_ENABLED:
        logger.info("[search] event search is DISABLED (EVENT_SEARCH_ENABLED=False)")
        return {"event": None, "venue": None}
    result = {"event": None, "venue": None}
    prompt = (
        "Find on the web which SPECIFIC event (show/opera/concert) is taking place: "
        f"{query}\n\n"
        "Rules:\n"
        "- event = the name of the specific event at that date/time;\n"
        "- NOT the name of a festival/season/programme (Opera Festival, Summer Season, etc.);\n"
        "- NOT the venue name; NOT a performer's name from a cast list (unless it is that "
        "artist's solo concert/recital);\n"
        "- if the sources do not give a confident answer - event=NULL and confidence=low.\n\n"
        'Return STRICTLY JSON with no markdown: {"venue": "...or NULL", "event": "...or NULL", '
        '"confidence": "high or low"}'
    )
    try:
        from .integrations_proxy import anthropic_messages
        payload = {
            "model": "claude-sonnet-5",
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        }
        resp = await anthropic_messages(payload, max_wait_seconds=100)
        texts = [b.get("text", "") for b in (resp.get("content") or [])
                 if isinstance(b, dict) and b.get("type") == "text"]
        raw = "\n".join(texts)
        searches = ((resp.get("usage") or {}).get("server_tool_use") or {}).get("web_search_requests")
        m = None
        for m_iter in re.finditer(r"\{[^{}]*\}", raw):
            m = m_iter  # the last JSON block in the answer
        if not m:
            logger.info("[search] web_search: no JSON in answer (searches=%s)", searches)
            return result
        parsed = json.loads(m.group(0))
        conf = str(parsed.get("confidence", "")).lower()
        v = parsed.get("venue")
        e = parsed.get("event")
        if isinstance(v, str) and v.strip() and v.strip().upper() != "NULL":
            result["venue"] = v.strip()
        if conf == "high" and isinstance(e, str) and e.strip() and e.strip().upper() != "NULL":
            result["event"] = e.strip()
        logger.info("[search] web_search: venue=%s event=%s conf=%s searches=%s",
                    result["venue"], result["event"], conf, searches)
    except Exception as exc:  # noqa: BLE001 — a search must never crash photo handling
        logger.warning("[search] web_search failed: %s", type(exc).__name__)
    return result


async def check_api_health() -> list[str]:
    """Check Anthropic API only. Returns list of problems (empty = all OK)."""
    problems = []
    backends = _get_backends()
    if "anthropic" not in backends:
        return ["Anthropic API key is not configured"]

    try:
        result = await _call_backend("anthropic", "ping", "Reply with 'ok'", max_tokens=5)
        if result:
            logger.info("[api_check] anthropic: OK")
    except Exception as e:
        err = str(e)
        url = "https://console.anthropic.com/settings/billing"
        if "credit" in err.lower() or "balance" in err.lower() or "402" in err:
            problems.append(f"Claude: out of credits, top up the balance\n{url}")
        elif "401" in err or "auth" in err.lower() or "invalid" in err.lower():
            problems.append("Claude: invalid key")
        elif "429" in err or "rate" in err.lower():
            problems.append("Claude: rate limit (temporary)")
        else:
            problems.append(f"Claude: {err[:100]}\n{url}")
        logger.warning(f"[api_check] anthropic: {e}")

    return problems
