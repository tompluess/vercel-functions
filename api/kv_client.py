"""KVClient — a minimal Redis client over the RESP protocol (stdlib only).

Used to persist the rotating Bexio OAuth refresh token + the cached access
token (see `BexioTokenProvider`). The Vercel Marketplace Redis integration
exposes a single native connection string (`REDIS_URL`, e.g.
`rediss://default:<token>@host:6379`) rather than an HTTP REST endpoint, so we
speak RESP over a TCP (optionally TLS) socket. Only the handful of commands the
token provider needs are wrapped (GET, SET, SET NX EX, DEL); this is not a
general Redis client.

Staying on the stdlib (`socket`/`ssl`) keeps the service dependency-free. A
fresh connection is opened per command — the common path is a single GET per
webhook (the access token is usually cached), and a refresh (~once/hour) issues
a few commands; at this volume the per-command connect overhead is negligible
and it sidesteps stale-connection / FD-leak concerns in warm serverless
instances.

Transport failures are raised as `KVError`, a `urllib` `URLError` subclass, so
the endpoint's existing `except URLError` arm maps a Redis blip to a 502 retry
(no Telegram) — same treatment as any other transient upstream. A Redis-level
error reply (e.g. bad AUTH) raises `RedisCommandError`, which surfaces as an
application error (Telegram + ok=false) since a retry won't fix it.
"""

import logging
import socket
import ssl
from urllib import error as urlerror
from urllib import parse as urlparse

logger = logging.getLogger("kv_client")


class KVError(urlerror.URLError):
    """Transient Redis transport failure → mapped to a 502 retry upstream."""


class RedisCommandError(Exception):
    """Redis replied with an error (`-ERR …`) — a non-retryable problem."""


class KVClient:
    TIMEOUT_SECONDS = 10

    def __init__(self, *, url: str):
        parsed = urlparse.urlparse(url)
        self._host = parsed.hostname
        self._port = parsed.port or 6379
        self._username = parsed.username or ""
        self._password = parsed.password or ""
        self._use_tls = parsed.scheme == "rediss"

    def command(self, *args):
        """Send a single command and return its (decoded) reply."""
        try:
            sock = self._connect()
            try:
                reader = sock.makefile("rb")
                if self._password:
                    self._send(sock, self._auth_args())
                    self._read_reply(reader)  # consume AUTH's +OK
                self._send(sock, args)
                return self._read_reply(reader)
            finally:
                sock.close()
        except OSError as e:
            raise KVError(f"redis transport error: {e}") from e

    def get(self, key: str):
        return self.command("GET", key)

    def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        if ex is not None:
            self.command("SET", key, value, "EX", ex)
        else:
            self.command("SET", key, value)

    def set_nx(self, key: str, value: str, *, ex: int) -> bool:
        """SET key value NX EX <ex> — returns True iff the key was newly set.

        Redis replies `+OK` when it set the key and a null bulk string when NX
        prevented it, so a non-None reply means we won the lock.
        """
        return self.command("SET", key, value, "NX", "EX", ex) is not None

    def delete(self, key: str) -> None:
        self.command("DEL", key)

    # --- transport -----------------------------------------------------------

    def _connect(self):
        sock = socket.create_connection((self._host, self._port),
                                        timeout=self.TIMEOUT_SECONDS)
        if self._use_tls:
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=self._host)
        return sock

    def _auth_args(self) -> tuple:
        # Redis 6 ACL AUTH takes username + password (REDIS_URL from the
        # integration carries "default"); legacy servers take a password only.
        if self._username:
            return ("AUTH", self._username, self._password)
        return ("AUTH", self._password)

    @staticmethod
    def _send(sock, args) -> None:
        parts = [f"*{len(args)}\r\n".encode()]
        for arg in args:
            raw = str(arg).encode()
            parts.append(b"$%d\r\n%s\r\n" % (len(raw), raw))
        sock.sendall(b"".join(parts))

    @staticmethod
    def _read_reply(reader):
        line = reader.readline()
        if not line:
            raise KVError("redis connection closed mid-reply")
        prefix, body = line[:1], line[1:].rstrip(b"\r\n")
        if prefix == b"+":
            return body.decode()
        if prefix == b"-":
            raise RedisCommandError(body.decode())
        if prefix == b":":
            return int(body)
        if prefix == b"$":
            length = int(body)
            if length == -1:
                return None
            data = reader.read(length)
            reader.read(2)  # trailing CRLF
            return data.decode()
        if prefix == b"*":
            count = int(body)
            if count == -1:
                return None
            return [KVClient._read_reply(reader) for _ in range(count)]
        raise RedisCommandError(f"unexpected redis reply: {line!r}")
