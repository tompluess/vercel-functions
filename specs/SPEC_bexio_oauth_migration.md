# SPEC: Bexio auth — personal access token → OAuth2 / OpenID Connect

Migrate the Bexio integration off the 60-day personal access token onto Bexio's
OAuth2 Authorization Code Flow with `offline_access` (its IdP is Keycloak at
`auth.bexio.com/realms/bexio`).

Read `CLAUDE.md` first for project conventions (one-class-per-file, zero runtime
deps beyond FastAPI, urllib-only outbound HTTP, the 2xx/5xx + Telegram error
contract, and the `_handle_moco_dispatch_webhook` pipeline).

---

## Context

Bexio personal access tokens (PATs) expire 60 days after creation. Today the whole
Bexio integration authenticates with a single static PAT in the `BEXIO_API_TOKEN`
env var, frozen into a `Bearer` header in `BexioAPI.__init__` (`api/bexio_api.py`).
Every ~60 days both Bexio endpoints (`/api/bexio-expense-sync`,
`/api/bexio-invoice-sync`) silently start 401ing until someone regenerates the PAT
by hand — an operational time-bomb.

Bexio's own recommendation for long-lived, more-scoped access is the OAuth2
Authorization Code Flow with `offline_access`. An offline session stays alive up to
~1 year of idle and is kept alive automatically by use, so it removes the 60-day
cliff.

**The hard part — refresh-token rotation.** Bexio's docs explicitly say to
*"replace refresh tokens with the new refresh tokens provided during the token
refresh instead of reusing"* — i.e. Keycloak "Revoke Refresh Token" is on, so each
refresh **invalidates the previous refresh token**. A static env var cannot hold a
rotating secret, and this app has **no persistence today** (pure `urllib`, one
runtime dep). So the migration needs (a) somewhere durable to keep the rotating
token, and (b) a small token-management layer that caches the ~1h access token,
refreshes under a lock, and persists the rotated refresh token. If two concurrent
refreshes race under strict revoke, Keycloak's reuse-detection can kill the whole
offline session — so serializing refreshes is a correctness requirement, not a
nicety.

**Decisions locked:**
- Storage: **Upstash Redis** (via Vercel Marketplace), talked to over its REST API
  with `urllib` so we add **no new pip dependency**.
- Bootstrap: a one-time **local operator script** runs the Authorization Code Flow
  and seeds the initial refresh token into Upstash.

---

## Token endpoint facts (Bexio / Keycloak)

- Auth URL:  `https://auth.bexio.com/realms/bexio/protocol/openid-connect/auth`
- Token URL: `https://auth.bexio.com/realms/bexio/protocol/openid-connect/token`
- Refresh:  form-encoded `POST`, body `grant_type=refresh_token` + `client_id` +
  `client_secret` + `refresh_token` → `{access_token, refresh_token(new),
  expires_in}`. Access token ~1h.
- Scopes: `openid offline_access` **plus** the Bexio API scopes the endpoints need
  (contacts / accounting / kb_invoice / kb_bill(purchase) / files / comments) — the
  exact scope strings come from the registered app in Bexio's developer portal.

---

## Approach

Keep `BexioAPI` a thin string-token wrapper; introduce a token provider that
resolves a fresh OAuth access token per request. Resolve the token **lazily inside
`BexioAPI`** (memoized per instance) so the refresh HTTP call lands *inside*
`service.sync()` — i.e. within the existing try/except in
`_handle_moco_dispatch_webhook` (`api/index.py:384-401`). That reuses the current
error contract for free: a `4xx` from the token endpoint (revoked/expired refresh
token) → `_app_error` → Telegram alert ("re-bootstrap Bexio OAuth") + HTTP 200
`ok=false`; a `5xx`/`URLError` → HTTP 502 retry, no Telegram spam. (`build_service`
at `api/index.py:383` runs *outside* the try, which is exactly why eager resolution
there would escape the mapping — hence lazy.)

### New files

- **`api/kv_client.py` — `KVClient`**: `urllib` wrapper around the Upstash REST API.
  One `command(*args)` method: `POST {KV_REST_API_URL}` with JSON body
  `["SET","key","val","EX","15","NX"]` etc. and `Authorization: Bearer
  {KV_REST_API_TOKEN}`; returns the parsed `.result`. Thin conveniences: `get(key)`,
  `set(key, val, *, ex=None)`, `set_nx(key, val, *, ex)`, `delete(key)`. One class
  per file. Reuses the `_send_json` urllib idiom from `bexio_api.py:138`.

- **`api/bexio_token_provider.py` — `BexioTokenProvider`**: constructed with
  `client_id`, `client_secret`, a `KVClient`, and `token_url` (module constant, not
  env). One public method `get_access_token() -> str`:
  1. Read the single JSON blob key `bexio:oauth` = `{access_token, refresh_token,
     expires_at}` (one `KVClient.get`).
  2. If `access_token` present and `expires_at - now > 60s` → return it (the common
     path — no network, no rotation).
  3. Else refresh under a lock: `set_nx("bexio:refresh_lock", ..., ex=15)`.
     - **Lock acquired**: form-encoded `POST` to the token endpoint with the stored
       refresh token; on success write back `{access_token, refresh_token(new),
       expires_at = now + expires_in - 60}` and delete the lock; return the access
       token. A `4xx`/`invalid_grant` propagates (→ Telegram re-bootstrap alert).
     - **Lock not acquired**: bounded wait-and-reread (a few short `time.sleep`s) for
       the winner's fresh token; if it appears, use it; if not, surface as a
       **transient/502** so Moco retries — contention must NOT Telegram-spam and must
       NOT trigger a second concurrent refresh (protects the offline session from
       reuse-detection revocation).

- **`scripts/bexio_oauth_bootstrap.py`**: one-time local operator CLI (sits next to
  `batch_ocr_drafts.py`). Reads `BEXIO_CLIENT_ID`/`BEXIO_CLIENT_SECRET` + KV creds
  from `.env.local`; builds the auth URL (`scope=openid offline_access <api
  scopes>`, a localhost `redirect_uri`), `webbrowser.open`s it, catches the `?code=`
  on a tiny local `http.server`, exchanges `grant_type=authorization_code` at the
  token endpoint, and writes the initial `bexio:oauth` blob into Upstash via
  `KVClient` (also prints the refresh token as a backup). Re-run if the offline
  session ever dies.

### Modified files

- **`api/bexio_api.py`**: add an optional `token_provider` constructor arg alongside
  the existing `api_token=` (keep the string path for back-compat + existing
  `test_bexio_api.py`). Replace the frozen `_auth_headers` dict with a memoized
  `_auth()` that, when a provider is set, calls `token_provider.get_access_token()`
  once on first request and caches the header for the instance's life; used by
  `_get`, `_send_json`, and the inline `upload_file` multipart request.

- **`api/index.py`**:
  - `REQUIRED_ENV_BEXIO_SYNC` (L57): drop `BEXIO_API_TOKEN`; add `BEXIO_CLIENT_ID`,
    `BEXIO_CLIENT_SECRET`, `KV_REST_API_URL`, `KV_REST_API_TOKEN`.
  - Both `build_service` lambdas (L174, L193): build
    `BexioTokenProvider(client_id=cfg["BEXIO_CLIENT_ID"], client_secret=…,
    kv=KVClient(url=cfg["KV_REST_API_URL"], token=cfg["KV_REST_API_TOKEN"]))` and
    pass it as `BexioAPI(token_provider=provider)`.

### Tests

- New `tests/test_kv_client.py` — stub `urlopen`; assert command encoding, Bearer
  header, `.result` parsing, NX-miss returning `None`.
- New `tests/test_bexio_token_provider.py` — fake `KVClient` + stubbed token
  endpoint: (a) unexpired cache → returns without any HTTP; (b) expired → refreshes,
  **persists the rotated refresh token**, returns the new access token; (c)
  `invalid_grant` 400 propagates as `HTTPError`; (d) lock-contention path.
- `tests/conftest.py:27` — swap `BEXIO_API_TOKEN` for the four new env keys.
- `tests/test_bexio_endpoints.py` — extend the hostname-routed `stub_pipeline` to
  answer `auth.bexio.com` and the KV host; seed a fake `bexio:oauth` blob with an
  **unexpired** access token so the happy-path endpoint tests need no refresh call.
- `tests/test_bexio_api.py` — unchanged for the `api_token=` cases; add a couple
  asserting the `token_provider=` lazy-resolution path (provider called once, header
  carries its token).

### Docs

- `CLAUDE.md`: update the `bexio_api.py` bullet, add `kv_client.py` /
  `bexio_token_provider.py` collaborators, revise the env section
  (`REQUIRED_ENV_BEXIO_SYNC`), and add a short "Bexio OAuth token lifecycle"
  paragraph (rotation, Upstash storage, bootstrap script, re-bootstrap-on-revoke).
- `README.md:232` env table: replace `BEXIO_API_TOKEN` with the new vars.

---

## Operator prerequisites (outside the code)

1. Register an OAuth app in Bexio's developer portal → `client_id`, `client_secret`,
   redirect URI = the bootstrap script's localhost callback; note the API scopes.
2. Provision Upstash Redis via the Vercel Marketplace; confirm the injected env var
   names (assumed `KV_REST_API_URL` / `KV_REST_API_TOKEN` — adjust the names above
   if the integration uses `UPSTASH_REDIS_REST_*`).
3. Put `BEXIO_CLIENT_ID`/`SECRET` + KV creds in `.env.local` (KV token is likely a
   secret-type var → `vercel env pull` returns it empty, so paste it manually).

---

## Verification (end-to-end)

1. `.venv/bin/pytest -v` — full suite green (unit + endpoint).
2. Run `scripts/bexio_oauth_bootstrap.py` locally → browser login → confirms
   `bexio:oauth` seeded in Upstash (script prints the refresh token).
3. Add the new env vars to the Vercel project (all environments) and remove
   `BEXIO_API_TOKEN`; `vercel deploy` a preview.
4. Trigger a real expense + invoice sync through the operator scripts; watch
   `vercel logs` and Telegram — the bill/invoice lands in Bexio.
5. **Rotation proof**: expire the cached access token in Upstash (delete
   `access_token`/`expires_at`, or wait ~1h) and re-trigger → the provider refreshes,
   the `refresh_token` in `bexio:oauth` visibly changes, and the following sync still
   succeeds with the new token.

Because this changes webhook-facing env vars and adds a Marketplace integration, a
prod deploy is warranted here. Stop after push + prod verification and leave the
merge to Tom.
