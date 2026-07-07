"""bot/telegram_bot.py - V2 telegram-bot entrypoint (transport).

Runnable module: `python -m bot.telegram_bot`. Delegates to the existing `bot.main.main()`
(the real Telegram bot run: commands, /bridge-producer enqueues tasks, etc.).

Properties:
  - Side-effect-free import: `bot.main` is imported LAZILY inside `main()`, so
    `import bot.telegram_bot` starts nothing, reads no secrets, and pulls in no telegram/agent code.
  - Requires TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_ID only on a REAL run (like bot.main).
  - Does NOT manage the coding-agent credentials and does NOT touch agent/GitHub secrets.
  - Does NOT run on import.
"""
from __future__ import annotations


def main() -> None:
    # Lazy import: a plain `import bot.telegram_bot` must do nothing
    # (config, telegram, claude_bridge are not loaded until the actual run).
    from .main import main as _bot_main
    _bot_main()


if __name__ == "__main__":
    main()
