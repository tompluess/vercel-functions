"""KVClient — thin urllib wrapper around the Upstash Redis REST API.

Used to persist the rotating Bexio OAuth refresh token + the cached access
token (see `BexioTokenProvider`). Talks the Upstash REST protocol: a command is
a JSON array POSTed to the base URL with a Bearer token, and the response is
`{"result": <value>}`. Staying on urllib keeps the service dependency-free.

Only the handful of commands the token provider needs are wrapped (GET, SET,
SET NX EX, DEL). This is not a general Redis client.
"""

import json
import logging
from urllib import request as urlrequest

logger = logging.getLogger("moco_sync")


class KVClient:
    HTTP_TIMEOUT_SECONDS = 10

    def __init__(self, *, url: str, token: str):
        self._url = url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def command(self, *args):
        """POST a single Redis command as a JSON array; return its `result`.

        Upstash echoes the command's return value under `result` (e.g. the
        string value for GET, "OK" for a successful SET, `None` for a missing
        key or an NX that didn't apply).
        """
        body = json.dumps([str(a) for a in args]).encode()
        req = urlrequest.Request(self._url, data=body, method="POST",
                                 headers=self._headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            parsed = json.loads(resp.read())
        return parsed.get("result")

    def get(self, key: str):
        return self.command("GET", key)

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        if ex is not None:
            self.command("SET", key, value, "EX", ex)
        else:
            self.command("SET", key, value)

    def set_nx(self, key: str, value: str, *, ex: int) -> bool:
        """SET key value NX EX <ex> — returns True iff the key was newly set."""
        return self.command("SET", key, value, "NX", "EX", ex) == "OK"

    def delete(self, key: str) -> None:
        self.command("DEL", key)
