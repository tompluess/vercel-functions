# vercel-functions

Serverless webhook handlers deployed to [Vercel](https://vercel.com), written in Python with [FastAPI](https://fastapi.tiangolo.com).

## What's in here

Five webhook receivers, all triggered by [Moco webhooks](https://github.com/hundertzehn/mocoapp-api-docs):

| Endpoint | Trigger | Sink |
| --- | --- | --- |
| `POST /api/moco-sync` | `Activity:create / update / delete` | Target Moco account |
| `POST /api/bexio-expense-sync` | `Purchase:create / update` | [Bexio](https://www.bexio.com) supplier bill |
| `POST /api/bexio-invoice-sync` | `Invoice:update` (status=sent) | Bexio customer invoice |
| `POST /api/brevo-contact-sync` | `Contact:create / update` | [Brevo](https://www.brevo.com) contact (+ list) |
| `POST /api/supplier-invoice-ocr` | `Purchase::Draft:create` | Real Moco purchase with OCR'd fields |

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
- After create/update the bill is auto-booked (`/4.0/purchase/bills/{id}/bookings/BOOKED`) and an outgoing payment is created (`/4.0/payment/outgoing-payments`), then a second Moco comment is posted noting the booking + payment date. Sender (own-company) details come from `BEXIO_OUTGOING_PAYMENT_SENDER`. MANUAL bills (no IBAN) skip this step silently since Bexio rejects MANUAL outgoing-payment payloads. Failures are soft — log + Telegram alert with both URLs, the sync still returns `ok=true`.
- Skip branches (no company, no booking account, bill no longer DRAFT) send a Telegram notification with entity context, mirroring the n8n `…Notification to Telegram` nodes.

### `POST /api/bexio-invoice-sync`

Receives Moco `Invoice` webhooks and creates a matching customer invoice in Bexio. Replaces the n8n workflow `Sync invoices from Moco to Bexio.json`.

- Gates on `status == "sent"` — drafts are ignored so Moco edit-loops don't churn Bexio.
- Fetches the Moco project to read its labels and customer.
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

### `POST /api/supplier-invoice-ocr`

Receives Moco `Purchase::Draft:create` webhooks (drafts from Moco's email-import) and turns each one into a fully-populated **real** Moco purchase pre-filled with fields OCR'd from the attached PDF via [Claude Sonnet 4.6 Vision](https://docs.anthropic.com/en/docs/about-claude/models/overview). Moco drafts can't be PATCHed via the API, so the flow creates a fresh purchase and auto-deletes the original draft.

- Downloads the PDF from the draft's signed `file_url` and runs OCR via `AnthropicOcrClient` → typed `InvoiceData` (date, due_date, supplier name/address, IBAN, QR-Referenz, totals, VAT rate, payment_purpose, description, commission, Lieferadresse, Gutschrift flag, confidence).
- Looks up the supplier against the full `GET /companies?type=supplier` list via `MocoSupplierMatcher` (three tiers: exact → substring → normalized token-set) and links `company_id` only on a unique match at the winning tier. Ambiguity is left for the reviewer. On a match the full company record is fetched once via `GET /companies/{id}` and shared by the VAT and category chains.
- VAT-code resolved per invoice from `GET /vat_code_purchases` (active codes only). Priority: OCR `vat_rate` match → supplier company's default → account-wide default → omit + Moco 422.
- IBAN safety: Moco's email-import populates `iban` / `reference` fields on the draft from its own QR-bill parser. The service prefers those over OCR (vision-OCR has been observed to mangle alphanumeric Swiss QR-IBANs like `CH22 3000 00DE …`). OCR results are mod-97 checksum-validated and nulled on failure. QR-IBAN check (IID `30000`–`31999`) gates the `bank_transfer_swiss_qr_esr` payment method — regular IBANs with a QR-reference fall back to `bank_transfer` and drop the reference (avoids Moco's `"ist keine QR-IBAN"` 422).
- Resolves the OCR'd `commission` ("Kommission" / "Objekt" / "Bauvorhaben") to a Moco project via `MocoProjectResolver` — three matching tiers over aggressive `[\W_]+`-stripped + case-folded keys: **exact** → **substring** (either side contains the other) → **token-overlap** (shared token of length ≥ 6). Projects are indexed by their `Kommission` custom-property, falling back to `project.name` when the field is empty. Ambiguous matches (more than one project at the winning tier) are left unresolved.
- Picks the per-line-item `category_id` (Buchhaltungs-Konto) via `MocoCategoryResolver`: resolved project with an `Aufwandkonto` custom-property → that project's category; otherwise a matched supplier company with an `Aufwandkonto` custom-property → that category (either override omits on a catalog miss — silent fallback would mis-route); otherwise card-paid receipts → omit (operator decides per receipt); otherwise the SKR default `4000` (Wareneinkauf). `POST /purchases` is called with `category_id` baked into each line item.
- `POST /purchases` with the OCR'd fields, the PDF base64-embedded as `file: {filename, base64}`, and tags `["OCR", "Review pending"]` (plus `"Gutschrift"` for credit notes — the line-item total is also negated). `due_date` defaults to `invoice_date + 30 days` and weekend dates roll back to Friday.
- When a project was resolved: `POST /purchases/{id}/assign_to_project` is called per line item with `notify_project_leader=false`, `billable=true`, `budget_relevant=true`, `surcharge=true`. Per-item failures are soft (logged + appended to the Telegram OCR-outcome alert) — the purchase exists, so the operator finishes the assignment manually.
- Posts **two** Moco comments on the new purchase: 📧 `Email-Quelle` (sender + body when the draft carries `email_from` / `email_body`; whitespace-normalized; HTML emails sanitized to Moco's allowed tag subset) and 🤖 `OCR-Extraktion` (extracted fields + confidence). HTML-formatted, html-escaped values, capped at Moco's allowed tag subset (`div, strong, em, u, pre, ul, ol, li, br`).
- Deletes the original draft after the create succeeds (`DELETE /purchases/drafts/{id}`). 404 is treated as idempotent; other failures alert via Telegram but don't roll the create back.
- Confidence-routed Telegram alert: ✅ when ≥ 85%, ⚠️ when below threshold, ⚠️ Gutschrift warning that overrides the confidence path (the reviewer must check the sign).
- **Bexio-sync interlock:** while the `Review pending` tag is on the purchase, `/api/bexio-expense-sync` silently skips it. Once the operator strips the tag in Moco's UI, the next `Purchase:update` webhook syncs to Bexio normally.
- Moco 4xx is converted to a silent skip + Telegram alert (e.g. `POST /purchases 422 receipt_identifier: ist bereits vergeben` on a webhook replay) — the response stays 200 ok=true so Moco's delivery log doesn't go red on unrecoverable rejections.

Operator scripts for validating the OCR pipeline against real Moco drafts live under [`scripts/`](scripts/) — see [Scripts](#scripts) below.

## Scripts

Two CLIs drive the OCR pipeline directly against the Moco account so you can validate behaviour without going through the webhook. Both default to **dry-run** (no Moco writes); pass `--apply` to actually create purchases. Both load env from `.env.local` (use `vercel env pull .env.local` first) and need `MOCO_SUBDOMAIN`, `MOCO_API_KEY`, and `ANTHROPIC_API_KEY`.

### `scripts/test_ocr_create_purchase.py` — single draft

Runs the full pipeline against one specific draft id and prints a detailed step-by-step view: the draft fields, the OCR'd `InvoiceData`, the supplier-lookup outcome, the resolved VAT code, the Kommission → project resolution (status + tier + candidate count), the chosen category (with reasoning), the exact `POST /purchases` payload (with the PDF base64 elided) and rendered comment bodies, plus a preview of the `POST /purchases/{id}/assign_to_project` body. Useful when iterating on the prompt or chasing a single weird invoice.

```bash
.venv/bin/python scripts/test_ocr_create_purchase.py 3001069                 # dry-run
.venv/bin/python scripts/test_ocr_create_purchase.py 3001069 --apply         # create the real purchase
.venv/bin/python scripts/test_ocr_create_purchase.py 3001069 --apply --notify  # + Telegram alert
.venv/bin/python scripts/test_ocr_create_purchase.py 3001069 --model claude-sonnet-4-6  # model override
```

Flags: `--apply` (POST + comments + delete draft), `--notify` (Telegram on confidence/Gutschrift), `--model` (override the Claude model), `--env-file` (alternative dotenv path).

Exit codes: `0` ok, `1` OCR error, `2` missing env / bad args, `3` Moco fetch error, `4` `POST /purchases` error.

### `scripts/batch_ocr_drafts.py` — all drafts

Lists `GET /purchases/drafts` (newest first), runs the same in-process pipeline against each draft, and prints a per-draft live log followed by a summary table.

```bash
.venv/bin/python scripts/batch_ocr_drafts.py --max 5            # dry-run, 5 newest
.venv/bin/python scripts/batch_ocr_drafts.py --max 5 --apply    # actually create
.venv/bin/python scripts/batch_ocr_drafts.py --max 20           # larger sweep
```

Per-draft live log (one block per draft) shows PDF size + OCR latency, confidence + Gutschrift flag, supplier lookup outcome (id + matched/ambiguous/no-match), VAT-code resolution tier (matched OCR rate / supplier default / account default / unresolved), the Kommission → project resolution (project id + tier or `no_match` / `ambiguous`), the chosen `category_id` with reasoning, and chosen payment method + IBAN tail (with `(QR-IBAN)` marker).

Summary table columns: `DRAFT ID | PURCHASE ID | LIEFERANT | BETRAG | KOMMISSION | KATEGORIE | RESULT`. The `LIEFERANT` column carries a leading `✓` when the supplier was uniquely matched in Moco's company list; the `KOMMISSION` column shows the raw OCR'd value plus a `✓` when it resolved to exactly one project (or `✗ ambiguous (N)` when it didn't); the `KATEGORIE` column shows the resolved account and its source — `✓ 4500 (project)` / `✓ 6510 (supplier)` / `✓ 4000 (default)` on a hit, `✗ 4999 (project)` when an `Aufwandkonto` override names an account missing from the catalog (field omitted), `- paid` for card receipts without an override. The footer counts `created / dry-run / skipped / failed / supplier-matched`.

Flags: `--max N` (cap at N newest drafts, default 10), `--apply`, `--model`, `--env-file`.

Telegram is intentionally **not** wired into the batch script (one alert per row would spam the chat); the table is the audit surface. Production webhook traffic still notifies as usual.

## Architecture

- [`api/index.py`](api/index.py) — FastAPI entrypoint. Parses the request, runs the auth pipeline, dispatches to the appropriate service. The three external-sink endpoints (two Bexio, one Brevo) share `_handle_moco_dispatch_webhook` so the auth/parse/error plumbing isn't duplicated.
- [`api/moco_webhook_validator.py`](api/moco_webhook_validator.py) — `MocoWebhookValidator`: HMAC-SHA256 signature check, ±300s timestamp window, account allowlist. Pure, no I/O.
- [`api/moco_api.py`](api/moco_api.py) / [`api/moco_sync_service.py`](api/moco_sync_service.py) — target-Moco transport and the Activity replication logic.
- [`api/moco_client.py`](api/moco_client.py) — read-only/comment-only client for the attached Moco account (companies, projects, comments, signed file downloads).
- [`api/bexio_api.py`](api/bexio_api.py) — Bearer-auth Bexio REST wrapper covering contacts, accounts, bills, invoices (incl. state transitions), files (multipart upload), document templates. The bearer token is a short-lived OAuth2 access token from `BexioTokenProvider`, resolved lazily per request.
- [`api/bexio_token_provider.py`](api/bexio_token_provider.py) / [`api/kv_client.py`](api/kv_client.py) — OAuth2 token management. Bexio rotates the refresh token on every refresh, so the token state lives in a Redis blob (`bexio:oauth`, via the native `REDIS_URL` — `KVClient` speaks RESP over a socket, no `redis-py` dep); the provider caches the access token, refreshes under a KV lock, and persists the rotated refresh token. Seed the initial token once with [`scripts/bexio_oauth_bootstrap.py`](scripts/bexio_oauth_bootstrap.py).
- [`api/bexio_config.py`](api/bexio_config.py) — non-secret Bexio numeric IDs (user, owner, bank, defaults) and the label → revenue-account mapping. Hardcoded since they change rarely and aren't credentials.
- [`api/bexio_expense_sync_service.py`](api/bexio_expense_sync_service.py) / [`api/bexio_invoice_sync_service.py`](api/bexio_invoice_sync_service.py) — pure business logic; all HTTP transport delegated to the two collaborators above.
- [`api/brevo_api.py`](api/brevo_api.py) — `api-key`-auth Brevo (ex-Sendinblue) REST wrapper covering contact lookup/create/update and list-add. Maps 404 from the contact lookup to `None` so the service can branch without exceptions.
- [`api/brevo_contact_sync_service.py`](api/brevo_contact_sync_service.py) — pure business logic for the Brevo flow (lookup → create-or-update → SMS → list add → optional cross-comment to Moco).
- [`api/telegram_notifier.py`](api/telegram_notifier.py) — `TelegramNotifier`: best-effort `sendMessage` to a Telegram chat. Used for error/skip notifications; never raises (a Telegram outage won't change the HTTP response).
- [`api/anthropic_ocr_client.py`](api/anthropic_ocr_client.py) — `AnthropicOcrClient`: thin wrapper around Anthropic's `POST /v1/messages` (Claude Sonnet 4.6 Vision). Sends a PDF as a base64 `document` content block, parses the JSON response into an `InvoiceData` dataclass. Robust parser tolerates `<think>`-style preamble (Sonnet sometimes reasons out loud when length-checked prompts fire); IBAN mod-97 validated; QR-Referenz strict 27-digit check.
- [`api/moco_purchase_client.py`](api/moco_purchase_client.py) — `MocoPurchaseClient`: draft read + create real purchase + delete draft + list vat-code-purchases + list purchases categories + assign-item-to-project + comment on the Moco account. Note the `drafts/` URL space — drafts live at `GET /purchases/drafts/{id}`, distinct from confirmed `GET /purchases/{id}`.
- [`api/moco_project_resolver.py`](api/moco_project_resolver.py) — `MocoProjectResolver`: indexes Moco projects by their `Kommission` custom-property (falling back to `project.name`) and resolves an OCR'd commission string in three tiers (exact → substring → token-overlap), reporting `matched` / `ambiguous` / `no_match` / `empty` with the winning tier. Pure, no I/O.
- [`api/moco_category_resolver.py`](api/moco_category_resolver.py) — `MocoCategoryResolver`: maps a `(project, supplier, already_paid)` triple to a `category_id` from the `GET /purchases/categories` catalog via `credit_account`. Prefers the project's `Aufwandkonto` custom-property, then the supplier company's, omits on card-paid receipts without an override, falls back to SKR `4000` (Wareneinkauf). An override naming an unmapped account omits instead of falling through. Pure, no I/O.
- [`api/supplier_invoice_ocr_service.py`](api/supplier_invoice_ocr_service.py) — pure business logic for the OCR flow; orchestrates download → OCR → supplier lookup → VAT resolve → project resolve → category resolve → create → two comments → assign-to-project per item → delete draft → confidence-routed Telegram.

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
| `MOCO_WEBHOOK_SECRET` | Shared secret used by the Moco account to sign webhook bodies |
| `MOCO_SUBDOMAIN` | Subdomain of the attached Moco account (`{subdomain}.mocoapp.com`, e.g. `solar`); also the expected `x-moco-account-url` header value |
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
| `MOCO_API_KEY` | API token for the Moco account (used to fetch companies/projects and post comments back) |
| `BEXIO_CLIENT_ID` | Bexio OAuth2 app client id (Authorization Code Flow with `offline_access`). |
| `BEXIO_CLIENT_SECRET` | Bexio OAuth2 app client secret. |
| `REDIS_URL` | Native Redis connection string (`rediss://default:<token>@host:6379`) from the Vercel Marketplace Redis integration. Durable store for the rotating Bexio OAuth token (`bexio:oauth`). |
| `BEXIO_MANUAL_BANK_MAP` | *Optional.* JSON map of Moco user first name → Bexio `bank_account_id` for non-IBAN bills. Example: `{"default": 3, "Alice": 5, "Bob": 4}`. Falls back to `bexio_config.BANK_ACCOUNT_ID` when missing. |
| `BEXIO_OUTGOING_PAYMENT_SENDER` | JSON object with the own-company sender fields embedded in every Bexio outgoing payment created by the expense flow. Expected keys: `name`, `iban`, `bank_name`, `bc_no`, `street`, `house_no`, `postcode`, `city`, `country_code`, `bank_account_id` (int). IBAN must be contiguous (no spaces). When missing/malformed, the book+pay step is skipped and a Telegram alert fires — the bill itself is still created. Not used by the invoice flow. |

**Used by `/api/brevo-contact-sync`:**

| Variable | Purpose |
| --- | --- |
| `MOCO_API_KEY` | API token for the Moco account (used to post the cross-link comment back) |
| `BREVO_API_KEY` | Brevo API v3 key (sent as `api-key: …`) |
| `BREVO_LIST_ID` | Numeric ID of the Brevo list every synced contact is added to |

**Used by `/api/supplier-invoice-ocr`:**

| Variable | Purpose |
| --- | --- |
| `MOCO_API_KEY` | API token for the Moco account (read draft, list vat codes + suppliers, create the real purchase, post comments, delete draft) |
| `ANTHROPIC_API_KEY` | Anthropic API key for the Claude Sonnet 4.6 Vision OCR call (`x-api-key` header) |

The VAT code is resolved dynamically per invoice (OCR `vat_rate` → supplier company default → account-wide `default: true` flag in `/vat_code_purchases`), so there's no env-var default to configure.

## Author

[@tompluess](https://github.com/tompluess)
