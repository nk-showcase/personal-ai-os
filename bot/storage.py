import time

import logging

# Path B: route Todoist through the integrations-worker when keyless (transport).
# In a keyed process this calls Todoist directly, so behavior is unchanged there.
from .integrations_proxy import todoist_request

logger = logging.getLogger(__name__)

BASE_URL = "https://api.todoist.com/api/v1"

# Cache project names: id -> name (refreshes every hour)
_projects_cache: dict[str, str] = {}
_projects_cache_ts: float = 0
_CACHE_TTL = 3600  # seconds


async def _load_projects():
    global _projects_cache, _projects_cache_ts
    if _projects_cache and (time.monotonic() - _projects_cache_ts) < _CACHE_TTL:
        return
    fresh: dict[str, str] = {}
    cursor = None
    while True:
        params = {"cursor": cursor} if cursor else None
        resp = await todoist_request("GET", "/projects", params=params)
        resp.raise_for_status()
        data = resp.json()
        for p in data.get("results", []):
            fresh[p["id"]] = p["name"]
        cursor = data.get("next_cursor")
        if not cursor:
            break
    _projects_cache = fresh
    _projects_cache_ts = time.monotonic()


async def get_projects() -> dict[str, str]:
    """Return {name: id} mapping of all projects."""
    await _load_projects()
    return {name: pid for pid, name in _projects_cache.items()}


def get_project_id_by_name(name: str) -> str | None:
    """Find project ID by name (case-insensitive)."""
    lower = name.lower()
    for pid, pname in _projects_cache.items():
        if pname.lower() == lower:
            return pid
    return None


async def add_task(content: str, category: str = "personal", due_string: str | None = None, project_id: str | None = None, description: str | None = None) -> dict:
    payload = {"content": content, "labels": [category]}
    if due_string:
        payload["due_string"] = due_string
    if description:
        payload["description"] = description
    if project_id:
        payload["project_id"] = project_id
    resp = await todoist_request("POST", "/tasks", json=payload)
    resp.raise_for_status()
    data = resp.json()
    await _load_projects()
    project_name = _projects_cache.get(data.get("project_id"), "")
    due = data.get("due")
    return {
        "id": data["id"],
        "content": data["content"],
        "category": category,
        "project": project_name,
        "due_date": due.get("date") if due else None,
        "is_recurring": due.get("is_recurring", False) if due else False,
        "due_string": due.get("string") if due else None,
    }


async def list_tasks() -> list:
    await _load_projects()

    all_tasks = []
    cursor = None

    while True:
        params = {"cursor": cursor} if cursor else None
        resp = await todoist_request("GET", "/tasks", params=params)
        resp.raise_for_status()
        data = resp.json()

        for t in data.get("results", []):
            if t.get("checked") or t.get("is_deleted"):
                continue
            labels = t.get("labels", [])
            category = "work" if "work" in labels else "personal"
            project_name = _projects_cache.get(
                t.get("project_id"), "?"
            )
            due = t.get("due")
            all_tasks.append({
                "id": t["id"],
                "content": t["content"],
                "description": t.get("description", ""),
                "category": category,
                "labels": labels,
                "project": project_name,
                "parent_id": t.get("parent_id"),
                "due_date": due.get("date") if due else None,
                "is_recurring": due.get("is_recurring", False) if due else False,
                "due_string": due.get("string") if due else None,
                "created_at": t.get("added_at", ""),
            })

        cursor = data.get("next_cursor")
        if not cursor:
            break

    return all_tasks


async def add_comment(task_id: str, text: str) -> bool:
    resp = await todoist_request(
        "POST", "/comments", json={"task_id": task_id, "content": text}
    )
    return resp.status_code in (200, 201)


async def get_task(task_id: str) -> dict | None:
    """Fetch a single task by ID."""
    resp = await todoist_request("GET", f"/tasks/{task_id}")
    if resp.status_code == 200:
        return resp.json()
    return None


async def complete_task(task_id: str, comment: str | None = None, permanent: bool = False) -> dict:
    """Close task. Returns {"ok": bool, "recurring": bool, "next_due": str|None}.
    If permanent=True and task is recurring, removes recurrence first then closes.
    """
    # Check if recurring before closing
    resp = await todoist_request("GET", f"/tasks/{task_id}")
    is_recurring = False
    if resp.status_code == 200:
        due = resp.json().get("due")
        is_recurring = due.get("is_recurring", False) if due else False

    was_recurring = False
    # For permanent close of recurring: remove recurrence first
    if permanent and is_recurring:
        await todoist_request(
            "POST", f"/tasks/{task_id}", json={"due_string": "no date"}
        )
        was_recurring = True
        is_recurring = False  # now it's a regular task

    if comment:
        await todoist_request(
            "POST", "/comments", json={"task_id": task_id, "content": comment}
        )
    resp = await todoist_request("POST", f"/tasks/{task_id}/close")
    result = {"ok": resp.status_code == 204, "recurring": is_recurring, "next_due": None, "was_recurring": was_recurring}

    # For recurring tasks, fetch updated due date
    if is_recurring and result["ok"]:
        resp2 = await todoist_request("GET", f"/tasks/{task_id}")
        if resp2.status_code == 200:
            due = resp2.json().get("due")
            result["next_due"] = due.get("date") if due else None

    return result


async def move_task(task_id: str, project_id: str) -> dict:
    """Move task to a different project. Returns {"ok": bool, "project": name}."""
    resp = await todoist_request(
        "POST", f"/tasks/{task_id}", json={"project_id": project_id}
    )
    if resp.status_code == 200:
        await _load_projects()
        project_name = _projects_cache.get(project_id, "?")
        return {"ok": True, "project": project_name}
    else:
        logger.warning(f"move_task failed: {resp.status_code} {resp.text[:200]}")
        return {"ok": False, "project": ""}


async def delete_task(task_id: str) -> bool:
    resp = await todoist_request("DELETE", f"/tasks/{task_id}")
    return resp.status_code == 204


async def get_comments(task_id: str) -> list[dict]:
    """Fetch comments for a task."""
    all_comments = []
    cursor = None
    while True:
        params = {"task_id": task_id}
        if cursor:
            params["cursor"] = cursor
        resp = await todoist_request("GET", "/comments", params=params)
        if resp.status_code != 200:
            return all_comments
        data = resp.json()
        all_comments.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return all_comments


async def update_task_labels(task_id: str, labels: list[str]) -> bool:
    """Update labels on a task (full replacement)."""
    resp = await todoist_request(
        "POST", f"/tasks/{task_id}", json={"labels": labels}
    )
    return resp.status_code == 200
