"""BexioTokenProvider — resolves a short-lived Bexio OAuth2 access token.

Bexio (Keycloak at auth.bexio.com) rotates the refresh token on every refresh,
so the token state lives in a durable KV blob (`bexio:oauth`) rather than an env
var. This provider returns a cached access token while one is still fresh, and
otherwise refreshes under a short KV lock — serializing concurrent refreshes so
Keycloak's reuse-detection can't revoke the whole offline session. On refresh it
persists the NEW refresh token (rotation) alongside the new access token.

Token-endpoint failures propagate as urllib errors so the endpoint's existing
2xx/5xx + Telegram mapping in `_handle_moco_dispatch_webhook` applies:
  - a 4xx (revoked/expired refresh token) → `urlerror.HTTPError` → Telegram
    "bexio_error" alert + 200 ok=false (the signal to re-run the bootstrap);
  - a 5xx / unreachable IdP / lock contention → `urlerror.URLError` → 502 so
    Moco retries, and (deliberately) no Telegram — these self-heal.

Seed the initial blob with `scripts/bexio_oauth_bootstrap.py`.
"""

import json
import logging
import time
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

logger = logging.getLogger("moco_sync")

TOKEN_URL = "https://auth.bexio.com/realms/bexio/protocol/openid-connect/token"

OAUTH_KEY = "bexio:oauth"
LOCK_KEY = "bexio:refresh_lock"

# Refresh a little before actual expiry so an in-flight webhook never uses a
# token that lapses mid-request.
EXPIRY_SKEW_SECONDS = 60
DEFAULT_ACCESS_LIFETIME_SECONDS = 3600
LOCK_TTL_SECONDS = 15
# The loser of the refresh lock waits up to this long for the winner to publish
# a fresh token before giving up and letting Moco retry.
CONTENTION_WAIT_SECONDS = 6.0
CONTENTION_POLL_SECONDS = 0.5


class BexioTokenRefreshContended(urlerror.URLError):
    """Another instance holds the refresh lock and didn't publish in time.

    A `URLError` subclass on purpose: the endpoint's existing `except URLError`
    arm maps it to a 502 (transient — Moco retries) without a Telegram alert,
    which is exactly right for contention.
    """

    def __init__(self):
        super().__init__("bexio token refresh contended")


class BexioTokenProvider:
    HTTP_TIMEOUT_SECONDS = 15
    TOKEN_URL = TOKEN_URL

    def __init__(self, *, client_id: str, client_secret: str, kv,
                 sleep=time.sleep, now=time.time):
        self._client_id = client_id
        self._client_secret = client_secret
        self._kv = kv
        self._sleep = sleep
        self._now = now

    def get_access_token(self) -> str:
        cached = self._fresh_access_token(self._read_blob())
        if cached is not None:
            return cached
        return self._refresh()

    # --- internals -----------------------------------------------------------

    def _read_blob(self) -> dict:
        raw = self._kv.get(OAUTH_KEY)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("bexio oauth blob in KV is not valid JSON; ignoring")
            return {}

    def _fresh_access_token(self, blob: dict):
        access = blob.get("access_token")
        expires_at = blob.get("expires_at", 0)
        if access and expires_at - self._now() > EXPIRY_SKEW_SECONDS:
            return access
        return None

    def _refresh(self) -> str:
        if not self._kv.set_nx(LOCK_KEY, "1", ex=LOCK_TTL_SECONDS):
            return self._await_other_refresh()
        try:
            # Re-read under the lock: another instance may have refreshed
            # between our stale read and acquiring the lock.
            blob = self._read_blob()
            cached = self._fresh_access_token(blob)
            if cached is not None:
                return cached
            refresh_token = blob.get("refresh_token")
            if not refresh_token:
                raise RuntimeError(
                    "no bexio refresh_token in KV — "
                    "run scripts/bexio_oauth_bootstrap.py")
            tokens = self._exchange_refresh_token(refresh_token)
            self._persist(tokens, previous_refresh_token=refresh_token)
            return tokens["access_token"]
        finally:
            self._kv.delete(LOCK_KEY)

    def _await_other_refresh(self) -> str:
        waited = 0.0
        while waited < CONTENTION_WAIT_SECONDS:
            self._sleep(CONTENTION_POLL_SECONDS)
            waited += CONTENTION_POLL_SECONDS
            cached = self._fresh_access_token(self._read_blob())
            if cached is not None:
                return cached
        raise BexioTokenRefreshContended()

    def _exchange_refresh_token(self, refresh_token: str) -> dict:
        data = urlparse.urlencode({
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh_token,
        }).encode()
        req = urlrequest.Request(
            self.TOKEN_URL, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
        )
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def _persist(self, tokens: dict, *, previous_refresh_token: str) -> None:
        lifetime = tokens.get("expires_in", DEFAULT_ACCESS_LIFETIME_SECONDS)
        blob = {
            "access_token": tokens["access_token"],
            # Rotation: Bexio returns a new refresh token on every refresh. Fall
            # back to the previous one only if the IdP omits it (non-rotating
            # config) so we never blank out our only credential.
            "refresh_token": tokens.get("refresh_token") or previous_refresh_token,
            "expires_at": self._now() + lifetime,
        }
        self._kv.set(OAUTH_KEY, json.dumps(blob))
