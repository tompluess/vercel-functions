#!/usr/bin/env python3
"""Send a one-off Telegram test message using the app's TelegramNotifier.

Verifies a bot token + chat id pair before they're configured in Vercel, by
exercising the exact code path production uses.

Usage (from the repo root):
    TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python scripts/send_test_telegram.py
    # or pass them positionally:
    python scripts/send_test_telegram.py <bot_token> <chat_id> ["custom message"]

Exits 0 if Telegram accepted the message, 1 if it was rejected (the reason —
HTTP status + body, e.g. "chat not found" / "Unauthorized" — is logged as a
warning by TelegramNotifier), 2 if the token/chat id are missing.
"""

import logging
import os
import sys
from pathlib import Path

# Allow running straight from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO)


def main() -> int:
    args = sys.argv[1:]
    token = args[0] if len(args) > 0 else os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = args[1] if len(args) > 1 else os.environ.get("TELEGRAM_CHAT_ID", "")
    message = args[2] if len(args) > 2 else (
        "✅ vercel-functions Telegram test — if you can read this, error "
        "notifications are wired up correctly."
    )

    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — pass them as env "
              "vars or as the first two CLI args.", file=sys.stderr)
        return 2

    ok = TelegramNotifier(bot_token=token, chat_id=chat_id).notify(message)
    if ok:
        print(f"Sent test message to chat {chat_id}.")
        return 0
    print("Telegram rejected the message — see the warning above for the "
          "status + body.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
