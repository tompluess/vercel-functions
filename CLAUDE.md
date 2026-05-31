# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single Vercel-deployed FastAPI app exposing webhook endpoints. Three webhook receivers, all Moco-sourced:

- `POST /api/moco-sync` — replicates Moco Activity create/update/delete into a *target* Moco account.
- `POST /api/bexio-expense-sync` — turns a Moco `Purchase:create|update` webhook into a Bexio supplier bill (with attachment).
- `POST /api/bexio-invoice-sync` — turns a Moco `Invoice:update` (status=sent) webhook into a Bexio customer invoice.

The two Bexio endpoints replace the n8n workflows that previously sat between Moco and Bexio (see `reference/` for the original exports).

## Architecture

`api/index.py` is the Vercel Functions entrypoint and is intentionally thin: it owns request parsing, env-var loading, and the auth/filter pipeline, then dispatches to one of the service classes. The webhook pipeline runs in a fixed order — verify HMAC signature → check timestamp freshness → check source account URL → check target → check event → parse JSON → (for moco-sync) filter by user → before any outbound work. The auth/parse plumbing is shared between the two Bexio endpoints via `_handle_bexio_webhook`.

Collaborators (one class per file):

- `api/moco_webhook_validator.py` — `MocoWebhookValidator`: HMAC-SHA256 signature check, ±300s timestamp window, account-URL match. Pure, no I/O.
- `api/moco_api.py` — `MocoAPI`: thin typed wrapper around the **target** Moco REST endpoints used by moco-sync (`list_projects`, `list_activities`, `create_activity`, `update_activity`, `delete_activity`).
- `api/moco_sync_service.py` — `MocoSyncService`: resolves source project/task names against the target account (falls back to configured defaults when no match), builds the activity payload, and dispatches to `sync_create` / `sync_update` / `sync_delete`.
- `api/source_moco_client.py` — `SourceMocoClient`: reads from the **source** Moco account (`/companies/{id}`, `/projects/{id}`), posts comments back (`/comments`), and downloads signed `file_url` attachments. Used only by the Bexio sync services.
- `api/bexio_api.py` — `BexioAPI`: thin wrapper around the Bexio REST endpoints (`/2.0/contact[/search]`, `/2.0/accounts/search`, `/2.0/kb_invoice[/:id/issue|/comment]`, `/3.0/document_templates`, `/3.0/files` multipart upload, `/4.0/purchase/bills[/:id]`). Bearer auth.
- `api/bexio_config.py` — hardcoded non-secret Bexio numeric IDs (user_id, owner_id, bank_account_id, mwst/currency/language defaults) and the project-label → revenue-account mapping for the invoice flow. The one exception is the per-Moco-user manual-bill bank routing (`manual_bank_account_id`), which reads from the `BEXIO_MANUAL_BANK_MAP` env var to keep staff first names out of the public source.
- `api/bexio_expense_sync_service.py` — `BexioExpenseSyncService`: contact find-or-create → account lookup → bill find-or-create-or-update (idempotent via `vendor`+`vendor_ref` search; skips non-DRAFT bills) → attachment upload → comment back to Moco. Branches on whether the Moco purchase carries an IBAN (QR/IBAN payment vs MANUAL).
- `api/bexio_invoice_sync_service.py` — `BexioInvoiceSyncService`: gates on `status=sent`, fetches the source project, resolves the revenue account from project labels, creates the invoice, cross-comments both sides, and transitions Bexio state DRAFT → Open via `/issue`.

**moco-sync — stateless source↔target link**: the target activity's `remote_id` is set to `f"{MOCO_SOURCE_ACCOUNT_URL}:{source.id}"` (e.g. `"solar:1064823757"`). Update and delete lookups scan `/activities?from=DATE&to=DATE` and filter by exact `remote_id` match. Moco's `remote_service` field is server-side enum-validated (github / trello / jira / asana / …) — sending an arbitrary string returns `422 {"remote_service":["ist kein gültiger Wert"]}`. So we leave `remote_service` blank and pack the namespace into `remote_id` instead. If no matching target activity is found on update, `sync_update` falls through to POST (upsert) — a missed create-webhook self-heals on the next update. On delete, `sync_delete` raises `TargetNotFoundError` → **HTTP 404** so the mismatch is visible in Moco's webhook delivery log. The dateless lookup window for delete is intentionally tight (currently 14 days) to stay within a single Moco `/activities` page — we don't follow pagination.

**bexio-expense-sync — idempotency**: identifies an existing bill by `GET /4.0/purchase/bills?vendor={company.name}&vendor_ref={receipt_identifier}`. If the first result is DRAFT, fetch the full bill via `GET /4.0/purchase/bills/{id}` to preserve `document_no` and `split_into_line_items`, then PUT the new payload. If status is anything other than DRAFT, skip with `{"skipped": "bill_not_draft"}` (n8n's "bill closed" branch). If no bill matches, POST. The IBAN branch sets `payment.type = QR` (when a reference is also present) or `IBAN` and routes to `bexio_config.BANK_ACCOUNT_ID`; the MANUAL branch routes via `manual_bank_account_id(user.firstname)` from `BEXIO_MANUAL_BANK_MAP`. Attachments: download the Moco `file_url` (a pre-signed URL — no auth header), upload to `POST /3.0/files`, attach the returned UUID to the bill. Skipped when the existing bill already carries an attachment_id. Bookkeeping account is looked up by `account_no` from `items[0].category.credit_account` (falls back to `supplier_credit_number`, then to `bexio_config.DEFAULT_BOOKING_ACCOUNT_NO`).

**bexio-invoice-sync — idempotency + state transition**: Bexio's `api_reference` field carries the Moco invoice identifier; the create payload sets it so duplicates can be reasoned about in Bexio's UI. After `POST /2.0/kb_invoice`, the service calls `/issue` to move the invoice DRAFT → Open (pending payment). `/issue` failures are logged but do not fail the sync, since the invoice is already created in Bexio and Moco would otherwise retry and duplicate. (`/set_pending` is intentionally NOT called — Bexio 404s it once the invoice is already issued.)

**Comments back to Moco**: both Bexio services POST to `/api/v1/comments` on the *source* Moco account so the Bexio URL is discoverable from inside Moco. Comment failures are logged and swallowed — the Bexio mutation is the authoritative outcome.

Required env vars are listed in `REQUIRED_ENV_MOCO_SYNC` / `REQUIRED_ENV_BEXIO_SYNC` in `api/index.py` — the handler fails fast with a 500 if any are missing. All collaborators are instantiated per-request inside `index.py`; they don't share state across invocations.

Vercel routing: `vercel.json` is empty (`{}`); Vercel's Python builder auto-detects the FastAPI `app` in `api/index.py` and routes all paths through it. Do not add a `vercel.json` `routes`/`rewrites` block unless you also restructure the entrypoint.

## Commands

```bash
# Install dev deps (FastAPI + pytest + httpx) into a local venv
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Run the test suite
.venv/bin/pytest -v
.venv/bin/pytest tests/test_endpoint.py::test_happy_path_creates_target_activity  # single test

# Deploy preview
vercel deploy

# Deploy to production (do this for feature branches BEFORE merging to main; see auto-memory)
vercel --prod

# Tail production logs
vercel logs <deployment-url>

# Manage env vars (all REQUIRED_ENV keys must exist in the Vercel project)
vercel env ls
vercel env add <NAME>
vercel env pull   # writes .env.local
```

Tests live in `tests/`; JSON fixtures under `tests/fixtures/` mirror real Moco Activity / Purchase / Invoice webhook bodies and a few target-Moco / Bexio responses. Service tests (`test_sync_service.py`, `test_bexio_expense_sync_service.py`, `test_bexio_invoice_sync_service.py`) inject in-memory fakes (`FakeMocoAPI`, `FakeBexioAPI`, `FakeSourceMoco`) directly so no HTTP is involved. Wrapper tests (`test_moco_api.py`, `test_bexio_api.py`) and endpoint tests (`test_endpoint.py`, `test_bexio_endpoints.py`) exercise the real classes with `urllib.request.urlopen` stubbed — no network is touched. Note: `monkeypatch.setattr(mod.urlrequest, "urlopen", …)` patches the *shared* `urllib.request.urlopen`, so stubs leak across modules; the `stub_pipeline` fixture in `test_bexio_endpoints.py` routes by hostname to handle both Bexio and source-Moco calls in one place. CI runs the same `pytest` command on push and PR via `.github/workflows/test.yml`.

`requirements.txt` is empty — runtime deps are declared in `pyproject.toml`. Vercel installs from `requirements.txt` if present, otherwise from `pyproject.toml`.

## Conventions

- One class per file; the entrypoint just wires imports together (see auto-memory `feedback_one_class_per_file`).
- Stick to `urllib` for outbound HTTP unless there's a reason to add a dependency — the service currently has zero runtime deps beyond FastAPI.
- The `reference/` directory contains n8n workflow JSON exports used as behavior references for the Moco integrations being ported here. It is gitignored and should not be edited or committed.
