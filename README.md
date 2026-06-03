# vercel-functions

Serverless webhook handlers deployed to [Vercel](https://vercel.com), written in Python with [FastAPI](https://fastapi.tiangolo.com).

## What's in here

Four webhook receivers, all triggered by [Moco webhooks](https://github.com/hundertzehn/mocoapp-api-docs):

| Endpoint | Trigger | Sink |
| --- | --- | --- |
| `POST /api/moco-sync` | `Activity:create / update / delete` | Target Moco account |
| `POST /api/bexio-expense-sync` | `Purchase:create / update` | [Bexio](https://www.bexio.com) supplier bill |
| `POST /api/bexio-invoice-sync` | `Invoice:update` (status=sent) | Bexio customer invoice |
| `POST /api/brevo-contact-sync` | `Contact:create / update` | [Brevo](https://www.brevo.com) contact (+ list) |

### `POST /api/moco-sync`

Receives [Activity webhooks](https://github.com/hundertzehn/mocoapp-api-docs) (`create`, `update`, `delete`) from a source [Moco](https://www.mocoapp.com) account and replicates them into a target Moco account.

The flow:

```
Source Moco                           Vercel Function                          Target Moco
─────────────                         ───────────────                          ─────────────
Activity created  ──webhook──▶  ┌─────────────────────────┐
updated, or deleted             │ 1. verify HMAC          │
                                │ 2. check timestamp      │
                                │ 3. check account        │
                                │ 4. accept event in      │
                                │    {create,update,      │
                                │     delete}             │
                                │ 5. filter by user       │
                                └────────────┬────────────┘
                                             │
                          create ────────────┤            GET /projects ──▶
                                             │            ◀── project list
                                             │            POST /activity ─▶
                                             │
                          update ────────────┤            GET /activities ▶  (for date)
                                             │            ◀── activities
                                             │            GET /projects ──▶
                                             │            ◀── project list
                                             │            PUT /activity/{id} or POST
                                             │              (upsert if not found)
                                             │
                          delete ────────────┤            GET /activities ▶  (14-day window)
                                             │            ◀── activities
                                             ▼            DELETE /activity/{id}
                                                            (200 ok=false + Telegram
                                                             alert if not found)
```

Source project and task are mapped onto the target account **by name**. If no match is found, configured defaults are used. The link between source and target activity is tracked **statelessly** by writing a namespaced `remote_id` (`{source-account}:{source-id}`) on the target activity — so updates and deletes can be located without any external database.

Useful when two related companies share an employee and time entries logged on one side need to mirror to the other.

### `POST /api/bexio-expense-sync`

Receives Moco `Purchase` webhooks and creates a matching supplier bill in [Bexio](https://www.bexio.com). Replaces the n8n workflow `Sync expenses from Moco to Bexio.json`.

- Looks up (or creates) the Bexio contact by company name.
- Looks up the Bexio booking account by the Moco category's `credit_account`.
- Downloads the Moco-signed `file_url` and uploads it to Bexio as an attachment.
- Idempotent: searches `/4.0/purchase/bills?vendor=…&vendor_ref=…` — updates DRAFT bills in-place, creates new ones otherwise, and skips bills that are no longer DRAFT.
- Branches on `iban`: present → QR or IBAN payment block on the default bank account; absent → MANUAL payment routed to the per-user bank account from `BEXIO_MANUAL_BANK_MAP`.
- Posts a comment back to the Moco Purchase with the Bexio bill URL.
- Skip branches (no company, no booking account, bill no longer DRAFT) send a Telegram notification with entity context, mirroring the n8n `…Notification to Telegram` nodes.

### `POST /api/bexio-invoice-sync`

Receives Moco `Invoice` webhooks and creates a matching customer invoice in Bexio. Replaces the n8n workflow `Sync invoices from Moco to Bexio.json`.

- Gates on `status == "sent"` — drafts are ignored so Moco edit-loops don't churn Bexio.
- Fetches the source project to read its labels and customer.
- Resolves the revenue account from project labels (e.g. `Stromproduktion → 3010`, `Wartung → 3450`); see `INVOICE_REVENUE_ACCOUNT_BY_LABEL` in [`api/bexio_config.py`](api/bexio_config.py) for the full mapping.
- Creates the invoice with `api_reference` set to the Moco identifier, then calls `/issue` to transition the invoice to Open (awaiting payment).
- Cross-comments: a Bexio comment with the Moco URL, and a Moco comment with the Bexio URL.
- The `no_customer` skip (a sent invoice whose project has no resolvable customer) sends a Telegram notification with the Moco invoice link. The `status != "sent"` gate stays silent (it fires on every draft edit).

### `POST /api/brevo-contact-sync`

Receives Moco `Contact` webhooks (`create` + `update`) and mirrors the contact into [Brevo](https://www.brevo.com). Replaces the n8n workflow `Add Moco contacts to Brevo.json`.

- Skips when the Moco contact has no `work_email` (Brevo identifies contacts by email).
- Lookup `GET /v3/contacts/{email}`. Branches on result:
  - **Not found**: `POST /v3/contacts` with VORNAME, NACHNAME, ADDITIONAL_INFO (today + Moco URL). Posts a comment back to the Moco contact with the new Brevo URL.
  - **Found**: `PUT /v3/contacts/{email}` updating VORNAME, NACHNAME, RESPONSIBLE_PERSON (Moco owner's full name), JOB_TITLE.
- Both branches converge to: normalize and set the SMS attribute from `mobile_phone` (matches the n8n JS: keeps `+`/`00` prefixes, drops a single leading `0`, strips whitespace), and add the contact to the configured Brevo list (idempotent).
- Failures on the SMS update or list-add are logged but do not fail the sync — the contact mutation is the authoritative outcome.

## Architecture

- [`api/index.py`](api/index.py) — FastAPI entrypoint. Parses the request, runs the auth pipeline, dispatches to the appropriate service. The three external-sink endpoints (two Bexio, one Brevo) share `_handle_moco_dispatch_webhook` so the auth/parse/error plumbing isn't duplicated.
- [`api/moco_webhook_validator.py`](api/moco_webhook_validator.py) — `MocoWebhookValidator`: HMAC-SHA256 signature check, ±300s timestamp window, source-account allowlist. Pure, no I/O.
- [`api/moco_api.py`](api/moco_api.py) / [`api/moco_sync_service.py`](api/moco_sync_service.py) — target-Moco transport and the Activity replication logic.
- [`api/source_moco_client.py`](api/source_moco_client.py) — read-only/comment-only client for the *source* Moco account (companies, projects, comments, signed file downloads).
- [`api/bexio_api.py`](api/bexio_api.py) — Bearer-auth Bexio REST wrapper covering contacts, accounts, bills, invoices (incl. state transitions), files (multipart upload), document templates.
- [`api/bexio_config.py`](api/bexio_config.py) — non-secret Bexio numeric IDs (user, owner, bank, defaults) and the label → revenue-account mapping. Hardcoded since they change rarely and aren't credentials.
- [`api/bexio_expense_sync_service.py`](api/bexio_expense_sync_service.py) / [`api/bexio_invoice_sync_service.py`](api/bexio_invoice_sync_service.py) — pure business logic; all HTTP transport delegated to the two collaborators above.
- [`api/brevo_api.py`](api/brevo_api.py) — `api-key`-auth Brevo (ex-Sendinblue) REST wrapper covering contact lookup/create/update and list-add. Maps 404 from the contact lookup to `None` so the service can branch without exceptions.
- [`api/brevo_contact_sync_service.py`](api/brevo_contact_sync_service.py) — pure business logic for the Brevo flow (lookup → create-or-update → SMS → list add → optional cross-comment to Moco).
- [`api/telegram_notifier.py`](api/telegram_notifier.py) — `TelegramNotifier`: best-effort `sendMessage` to a Telegram chat. Used for error/skip notifications; never raises (a Telegram outage won't change the HTTP response).

Everything uses `urllib` — no external HTTP client dependency.

### Error notifications & the 2xx/5xx contract

Errors are split by a single question: **can a webhook retry fix this?** (ports the n8n `Error Trigger → Send Error to Telegram` handler).

- **Application errors** — a retry can't help: the upstream rejected our request (a 4xx), an unexpected internal error, or a source↔target mismatch. These fire a Telegram alert (endpoint, event, source ID, error detail) and **return HTTP 200 `{"ok": false, "error": …}`** so Moco stops retrying. Telegram carries the visibility.
- **Infrastructure failures** — transient and retry-worthy: the upstream is unreachable or returns a 5xx. These return **HTTP 502** so Moco retries, and are **not** notified (a flapping upstream would otherwise spam the chat on every retry).
- **Auth/validation rejections** (bad signature, stale timestamp, wrong target, user-filter) are rejected up front with 401/422 and never notified — they're webhook noise.

The Telegram send is best-effort: it never masks or changes the HTTP response Moco receives.

## Tech

- Python 3.12+, FastAPI
- [Vercel Fluid Compute](https://vercel.com/docs/fluid-compute) (Python runtime)
- Pytest with patched `urlopen` — tests touch no network
- GitHub Actions CI on push to `main` and on pull requests

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -v
```

## Deploy

Deployments are managed with the [Vercel CLI](https://vercel.com/docs/cli). The `main` branch auto-deploys to production.

```bash
vercel deploy        # preview deployment
vercel --prod        # production deployment from the current branch
vercel logs <url>    # tail logs for a given deployment
```

Required environment variables (configure in the Vercel project, then `vercel env pull` for local use):

**Shared:**

| Variable | Purpose |
| --- | --- |
| `MOCO_WEBHOOK_SECRET` | Shared secret used by the source Moco account to sign webhook bodies |
| `MOCO_SOURCE_ACCOUNT_URL` | Expected `x-moco-account-url` header value (e.g. `solar`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (the `…/bot<token>/sendMessage` path) for error/skip notifications |
| `TELEGRAM_CHAT_ID` | Target Telegram chat/group ID (e.g. `-1002342319319`); per-environment so dev/staging/prod can notify different chats |

**Used by `/api/moco-sync`:**

| Variable | Purpose |
| --- | --- |
| `MOCO_USER_ID_FILTER` | Only sync activities for this Moco user ID |
| `MOCO_TARGET_SUBDOMAIN` | `{subdomain}.mocoapp.com` of the target account |
| `MOCO_TARGET_API_KEY` | API token for the target Moco account |
| `MOCO_TARGET_COMPANY_ID` | Target company ID used to scope the `/projects` lookup |
| `MOCO_TARGET_DEFAULT_PROJECT_ID` | Fallback project ID when no project name matches |
| `MOCO_TARGET_DEFAULT_TASK_ID` | Fallback task ID when no task name matches |

**Used by `/api/bexio-expense-sync` and `/api/bexio-invoice-sync`:**

| Variable | Purpose |
| --- | --- |
| `MOCO_SOURCE_API_KEY` | API token for the **source** Moco account (used to fetch companies/projects and post comments back) |
| `BEXIO_API_TOKEN` | Bexio API v3 token (sent as `Authorization: Bearer …`) |
| `BEXIO_MANUAL_BANK_MAP` | *Optional.* JSON map of Moco user first name → Bexio `bank_account_id` for non-IBAN bills. Example: `{"default": 3, "Alice": 5, "Bob": 4}`. Falls back to `bexio_config.BANK_ACCOUNT_ID` when missing. |

**Used by `/api/brevo-contact-sync`:**

| Variable | Purpose |
| --- | --- |
| `MOCO_SOURCE_API_KEY` | API token for the **source** Moco account (used to post the cross-link comment back) |
| `BREVO_API_KEY` | Brevo API v3 key (sent as `api-key: …`) |
| `BREVO_LIST_ID` | Numeric ID of the Brevo list every synced contact is added to |

## Author

[@tompluess](https://github.com/tompluess)
