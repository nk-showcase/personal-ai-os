#!/usr/bin/env python3
"""Plain-python test for the raw-Notion-persistence gate.

SAFE: NO live Notion / network / Telegram / coding agent / real secrets. httpx.AsyncClient
is replaced with an in-memory fake that CAPTURES request bodies; the flag is flipped via
config.RAW_NOTION_PERSIST. Proves:
  * default/bad/missing env => raw OFF (fail-safe);
  * raw OFF: save_messages / append_memory are no-ops (NO Notion call);
  * raw OFF: create_chat / update_chat_title use a NEUTRAL title (no free user text);
  * raw ON (explicit opt-in): legacy behavior preserved (transcript + free title).

Run:  PYTHONPATH=<repo> python3 scripts/test_notion_raw_gate.py   (exit 0 = OK, 1 = FAIL)
"""
import asyncio
import json
import os
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    # Dummy env so config imports (NOT real secrets). Leave AIOS_RAW_NOTION_PERSIST unset
    # so the imported default is exercised (must be OFF).
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "000:dummy")
    os.environ.setdefault("TELEGRAM_OWNER_ID", "1")
    os.environ.pop("AIOS_RAW_NOTION_PERSIST", None)
    # ISOLATION (hard requirement): a scratch data DB, NEVER the default path. Without
    # this the store-mode flags read 'sqlite' from a live DB and every write below goes
    # through the live queue into the real store. This suite tests the NOTION branch, so
    # after isolating we force store_mode=notion for all domains in the scratch config.
    import tempfile
    os.environ["AIOS_DATA_DB"] = os.path.join(tempfile.mkdtemp(prefix="rawgate-"),
                                              "aios.sqlite3")
    # With no NOTION_API_KEY the storage layers route through the queue (keyless
    # transport) and the fake httpx below never sees the request. A dummy key keeps the
    # process "keyed" so notion_request takes the DIRECT httpx path this suite captures.
    os.environ["NOTION_API_KEY"] = "dummy-rawgate-key"

    failed = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL") + ": " + name)
        if not cond:
            failed.append(name)

    from bot import config, claude_storage
    from bot import aios_config
    for _d in aios_config.CUTOVER_DOMAINS:
        aios_config.set_store_mode(_d, "notion")

    # ---- in-memory fake Notion client (captures, never networks) ----
    import httpx
    CAPTURED = []

    class _Resp:
        def __init__(self, status=200, payload=None):
            self.status_code = status
            self._p = payload if payload is not None else {"id": "pg_fake"}
            self.text = ""

        def json(self):
            return self._p

    _SCHEMA = {"properties": {k: {} for k in (
        "Name", "Type", "Date", "Value", "Notes",
    )}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            CAPTURED.append({"m": "POST", "url": url, "json": json})
            return _Resp(200, {"id": "pg_fake"})

        async def patch(self, url, headers=None, json=None):
            CAPTURED.append({"m": "PATCH", "url": url, "json": json})
            return _Resp(200, {"id": "pg_fake"})

        async def get(self, url, headers=None):
            CAPTURED.append({"m": "GET", "url": url, "json": None})
            return _Resp(200, _SCHEMA if "/databases/" in url else {"properties": {}})

        async def request(self, method, url, headers=None, json=None, params=None):
            # The keyed-direct route (integrations_proxy._direct_call) uses .request().
            m = method.upper()
            CAPTURED.append({"m": m, "url": url, "json": json})
            if m == "GET":
                return _Resp(200, _SCHEMA if "/databases/" in url else {"properties": {}})
            return _Resp(200, {"id": "pg_fake"})

    httpx.AsyncClient = _Client  # storage modules call httpx.AsyncClient at call time
    # create_chat early-returns None unless a Chats DB id is configured; set one so the
    # gate (neutral title) is actually exercised. (Module-level name create_chat reads.)
    claude_storage.NOTION_CLAUDE_CHATS_DB = "chatsdb_test"

    def reset():
        CAPTURED.clear()

    def page_post():
        for c in reversed(CAPTURED):
            if c["m"] == "POST" and c["url"].endswith("/pages") and isinstance(c["json"], dict) and "parent" in c["json"]:
                return c["json"]
        return None

    def last_patch_props():
        for c in reversed(CAPTURED):
            if c["m"] == "PATCH" and isinstance(c["json"], dict) and "properties" in c["json"]:
                return c["json"]["properties"]
        return None

    def name_text(props):
        return "".join(x.get("text", {}).get("content", "") for x in props.get("Name", {}).get("title", []))

    # =====================================================================
    # 1. config defaults / fail-safe
    # =====================================================================
    check("_is_truthy('1') True", config._is_truthy("1") is True)
    check("_is_truthy('true'/'yes'/'on') True", all(config._is_truthy(v) for v in ("true", "YES", "On")))
    check("_is_truthy ''/'0'/None/'garbage' False",
          not any(config._is_truthy(v) for v in ("", "0", None, "garbage", "false")))
    check("default RAW_NOTION_PERSIST is OFF (env unset at import)", config.RAW_NOTION_PERSIST is False)
    check("DIALOGUE_PERSIST_MODE is a valid mode", config.DIALOGUE_PERSIST_MODE in config._VALID_PERSIST_MODES)
    config.RAW_NOTION_PERSIST = True
    check("notion_raw_allowed reflects flag (True)", config.notion_raw_allowed() is True)
    config.RAW_NOTION_PERSIST = False
    check("notion_raw_allowed reflects flag (False)", config.notion_raw_allowed() is False)

    SECRET = "SECRETZEBRA"  # distinctive marker; must never appear in any Notion body when OFF

    # =====================================================================
    # 2. claude_storage gates — raw OFF
    # =====================================================================
    config.RAW_NOTION_PERSIST = False

    reset()
    r = asyncio.run(claude_storage.save_messages("pg1", "user " + SECRET, "assistant " + SECRET))
    check("OFF save_messages returns True (no-op success)", r is True)
    check("OFF save_messages made NO Notion call", len(CAPTURED) == 0)

    reset()
    r = asyncio.run(claude_storage.append_memory("memory fact " + SECRET))
    check("OFF append_memory returns True (no-op success)", r is True)
    check("OFF append_memory made NO Notion call", len(CAPTURED) == 0)

    reset()
    asyncio.run(claude_storage.create_chat("Session: " + SECRET))
    body = page_post()
    check("OFF create_chat sent a page POST", body is not None)
    check("OFF create_chat Name is neutral (no free text)",
          body is not None and SECRET not in name_text(body["properties"]) and name_text(body["properties"]).startswith("Chat "))

    reset()
    asyncio.run(claude_storage.update_chat_title("pg1", "first user message " + SECRET))
    props = last_patch_props()
    check("OFF update_chat_title Name is neutral (no free message)",
          props is not None and SECRET not in name_text(props) and name_text(props).startswith("Chat "))

    # =====================================================================
    # 3. claude_storage — raw ON (legacy preserved)
    # =====================================================================
    config.RAW_NOTION_PERSIST = True

    reset()
    r = asyncio.run(claude_storage.save_messages("pg1", "hello " + SECRET, "hi " + SECRET))
    sent = json.dumps(CAPTURED, ensure_ascii=False)
    check("ON save_messages calls Notion with transcript", len(CAPTURED) >= 1 and SECRET in sent)

    reset()
    asyncio.run(claude_storage.create_chat("Session: " + SECRET))
    body = page_post()
    check("ON create_chat keeps the passed title (legacy)",
          body is not None and SECRET in name_text(body["properties"]))

    config.RAW_NOTION_PERSIST = False

    if failed:
        print("\nRESULT: FAIL (" + ", ".join(failed) + ")")
        return 1
    print("\nRESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
