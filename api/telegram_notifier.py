"""TelegramNotifier — best-effort error/skip notifications to a Telegram chat.

Ports the n8n "Error Trigger → Send Error to Telegram" mechanism that sat on
the Moco→Brevo and Moco→Bexio workflows: whenever a sync fails (or hits an
expense skip branch), a message is posted to a Telegram group so the problem
is noticed without watching Vercel logs.

Thin transport only (urllib, no deps). The send is best-effort: a Telegram
outage must never change the HTTP response a webhook returns to Moco — the
caller is usually already inside an error path — so `notify` swallows every
error and logs a tidy warning (status + body) instead of raising or dumping a
traceback (see `feedback_soft_failure_logging`).

Auth: the Telegram Bot API carries the bot token in the URL path
(`/bot<token>/sendMessage`); the destination chat is identified by `chat_id`.

Docs: https://core.telegram.org/bots/api#sendmessage
"""

import json
import logging
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger("telegram_notifier")


class TelegramNotifier:
    HTTP_TIMEOUT_SECONDS = 10
    BASE_URL = "https://api.telegram.org"

    def __init__(self, *, bot_token: str, chat_id: str):
        self._send_url = f"{self.BASE_URL}/bot{bot_token}/sendMessage"
        self._chat_id = chat_id

    def notify(self, text: str) -> bool:
        """POST a message to the configured chat. Best-effort.

        Returns True if Telegram accepted the message, False otherwise. Never
        raises — callers are typically already handling an upstream failure and
        must not have it masked (or a successful sync turned into a 500) by a
        notification hiccup.
        """
        payload = {"chat_id": self._chat_id, "text": text}
        data = json.dumps(payload).encode()
        req = urlrequest.Request(
            self._send_url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS):
                return True
        except urlerror.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            logger.warning("telegram notify failed: %s %s", e.code, body)
            return False
        except Exception as e:  # URLError, timeout, anything — must not raise
            logger.warning("telegram notify failed: %s", e)
            return False
