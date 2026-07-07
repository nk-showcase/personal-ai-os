"""ChatBot shell: unified menu, routing, and feature module registry."""

from telegram import ReplyKeyboardMarkup

# Button constants
DONE_BUTTON = "✅ Done"
APPEND_BUTTON = "✨ Add note"
NEW_CHAT_BUTTON = "\U0001f47e New Chat"
CONTINUE_BUTTON = "〰️ Continue"
CANCEL_BUTTON = "❌ Cancel"
BRIDGE_BUTTON = "✴️ Code"

# Keyboard layout
PERSISTENT_KEYBOARD = ReplyKeyboardMarkup(
    [
        [NEW_CHAT_BUTTON, CONTINUE_BUTTON, BRIDGE_BUTTON],
        [DONE_BUTTON, APPEND_BUTTON, CANCEL_BUTTON],
    ],
    resize_keyboard=True,
)
