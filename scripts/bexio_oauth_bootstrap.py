#!/usr/bin/env python3
"""One-time bootstrap of the Bexio OAuth2 offline session.

Runs the Authorization Code Flow against Bexio's IdP (Keycloak at
auth.bexio.com) locally, then seeds the resulting token state into KV (Upstash)
under `bexio:oauth`, which is where `BexioTokenProvider` reads and rotates it in
production. Run this once when first wiring up OAuth, and again only if the
offline session ever dies (e.g. after ~1 year idle, or a revoked refresh token —
you'll get a Telegram "bexio_error" alert in that case).

Prerequisites (registered in Bexio's developer portal):
  - an OAuth app → BEXIO_CLIENT_ID / BEXIO_CLIENT_SECRET
  - its redirect URI must equal BEXIO_OAUTH_REDIRECT_URI (default
    http://localhost:8737/callback)
  - the API scopes the app needs, in BEXIO_OAUTH_SCOPES (openid + offline_access
    are always added)

Config is read from the environment (source .env.local first, or export inline):
    set -a; source .env.local; set +a
    python scripts/bexio_oauth_bootstrap.py

Needs: BEXIO_CLIENT_ID, BEXIO_CLIENT_SECRET, KV_REST_API_URL, KV_REST_API_TOKEN.
Optional: BEXIO_OAUTH_REDIRECT_URI, BEXIO_OAUTH_SCOPES.
"""

import json
import logging
import os
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib import parse as urlparse
from urllib import request as urlrequest

# Allow running straight from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.bexio_token_provider import OAUTH_KEY, TOKEN_URL  # noqa: E402
from api.kv_client import KVClient  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bexio_oauth_bootstrap")

AUTH_URL = "https://auth.bexio.com/realms/bexio/protocol/openid-connect/auth"
DEFAULT_REDIRECT_URI = "http://localhost:8737/callback"


def main() -> int:
    client_id = os.environ.get("BEXIO_CLIENT_ID", "")
    client_secret = os.environ.get("BEXIO_CLIENT_SECRET", "")
    kv_url = os.environ.get("KV_REST_API_URL", "")
    kv_token = os.environ.get("KV_REST_API_TOKEN", "")
    redirect_uri = os.environ.get("BEXIO_OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    extra_scopes = os.environ.get("BEXIO_OAUTH_SCOPES", "").split()

    missing = [name for name, val in [
        ("BEXIO_CLIENT_ID", client_id), ("BEXIO_CLIENT_SECRET", client_secret),
        ("KV_REST_API_URL", kv_url), ("KV_REST_API_TOKEN", kv_token),
    ] if not val]
    if missing:
        print(f"Missing required env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    # openid + offline_access are mandatory (offline_access is what mints the
    # long-lived refresh token); the app's API scopes come from env.
    scopes = ["openid", "offline_access", *extra_scopes]
    state = secrets.token_urlsafe(16)

    parsed = urlparse.urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80

    auth_url = AUTH_URL + "?" + urlparse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    })

    print("Opening the Bexio login in your browser. If it doesn't open, visit:\n")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    code = _wait_for_code(host, port, expected_state=state)
    if not code:
        print("Did not receive an authorization code.", file=sys.stderr)
        return 1

    tokens = _exchange_code(code, redirect_uri=redirect_uri,
                            client_id=client_id, client_secret=client_secret)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("Token response had no refresh_token — was offline_access granted?\n"
              f"Response keys: {sorted(tokens)}", file=sys.stderr)
        return 1

    blob = {
        "access_token": tokens["access_token"],
        "refresh_token": refresh_token,
        "expires_at": time.time() + tokens.get("expires_in", 3600),
    }
    KVClient(url=kv_url, token=kv_token).set(OAUTH_KEY, json.dumps(blob))

    print(f"\n✅ Seeded '{OAUTH_KEY}' in KV. The Bexio integration is live.")
    print("Backup — store this refresh token somewhere safe if you like:\n")
    print(f"  {refresh_token}\n")
    return 0


def _wait_for_code(host: str, port: int, *, expected_state: str) -> str | None:
    """Run a one-shot local HTTP server that captures the OAuth redirect."""
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            query = urlparse.parse_qs(urlparse.urlparse(self.path).query)
            captured["code"] = query.get("code", [""])[0]
            captured["state"] = query.get("state", [""])[0]
            captured["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = ("Authorization received — you can close this tab."
                   if captured["code"] else
                   f"Authorization failed: {captured['error'] or 'no code'}")
            self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

        def log_message(self, *args):  # silence per-request stderr logging
            pass

    server = HTTPServer((host, port), Handler)
    print(f"Waiting for the redirect on {host}:{port} …")
    server.handle_request()  # blocks until exactly one request is served
    server.server_close()

    if captured.get("error"):
        print(f"IdP returned error: {captured['error']}", file=sys.stderr)
        return None
    if captured.get("state") != expected_state:
        print("State mismatch — aborting (possible CSRF).", file=sys.stderr)
        return None
    return captured.get("code") or None


def _exchange_code(code: str, *, redirect_uri: str, client_id: str,
                   client_secret: str) -> dict:
    data = urlparse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urlrequest.Request(
        TOKEN_URL, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
    )
    with urlrequest.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


if __name__ == "__main__":
    raise SystemExit(main())
