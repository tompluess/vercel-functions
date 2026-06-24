# SPEC: Supplier Invoice OCR Automation

This document specifies two implementation approaches for automating supplier invoice ingestion
with OCR and automatic creation of Moco purchases (Ausgaben). It is written for Claude Code
to implement within the existing `vercel-functions` project.

Read `CLAUDE.md` first to understand project conventions, architecture, and the existing
collaborator structure before starting any implementation work.

---

## Context & Goal

Currently: supplier invoices arrive by email and are forwarded directly to Moco, where they
land as `Purchase` drafts. Fields are filled in manually.

Goal: parse incoming PDFs with OCR (Claude Vision via Anthropic API), extract structured
invoice data, and create/enrich Moco purchases automatically with zero manual field entry.
A Telegram notification is sent for human review before or after posting.

Two approaches are specified below. **Approach 2 is the recommended starting point** (minimal
change to existing process, reuses existing code). Approach 1 is the target architecture for
full automation.

---

## Approach 2 — Moco-Webhook Enrichment (Recommended First Step)

### Overview

Keep the existing mail → Moco-draft flow unchanged. Add a new webhook endpoint that fires on
`Purchase:create`, downloads the attached PDF, runs OCR, and patches the Moco purchase with
the extracted fields.

```
Supplier email
     │
     ▼
  Moco (email import → Purchase draft created)
     │
     │  Purchase:create webhook
     ▼
POST /api/supplier-invoice-ocr          ← new endpoint
     │
     ├── 1. Validate HMAC + timestamp (existing MocoWebhookValidator)
     ├── 2. Check x-moco-target header = "supplier-invoice-ocr"
     ├── 3. Download PDF from purchase.file_url (SourceMocoClient pattern)
     ├── 4. OCR via Anthropic Claude Vision API → InvoiceData
     ├── 5. PATCH /purchases/{id} with extracted fields
     ├── 6. POST comment to Moco purchase with OCR summary + confidence
     └── 7. Telegram notification (always, for human review)
```

### New Files to Create

```
api/supplier_invoice_ocr_service.py   — service class (one class per file)
api/anthropic_ocr_client.py           — Claude Vision API wrapper
api/moco_purchase_client.py           — Moco purchase read/write (GET + PATCH /purchases)
tests/test_supplier_invoice_ocr_service.py
tests/test_anthropic_ocr_client.py
tests/test_moco_purchase_client.py
tests/fixtures/purchase_create_webhook.json   — realistic Moco Purchase:create payload
tests/fixtures/ocr_response_sample.json       — sample structured OCR output
```

### Changes to Existing Files

`api/index.py`:
- Add `REQUIRED_ENV_SUPPLIER_INVOICE_OCR` list (see env vars below)
- Register `POST /api/supplier-invoice-ocr` route
- Instantiate collaborators and dispatch to `SupplierInvoiceOcrService.process()`
- Re-use `_handle_moco_dispatch_webhook` if it fits; otherwise inline the auth pipeline
  (the existing helper is tightly coupled to the three external-sink services — prefer
  clarity over forced reuse here)

`CLAUDE.md`:
- Add the new endpoint to the endpoint table
- Document the new env vars

### Collaborators

**`api/anthropic_ocr_client.py` — `AnthropicOcrClient`**

Wraps `POST https://api.anthropic.com/v1/messages` with `claude-sonnet-4-6` (vision-capable).
Auth: `x-api-key: {ANTHROPIC_API_KEY}` + `anthropic-version: 2023-06-01`.
Sends the PDF as base64-encoded `image/jpeg` or as a `document` block (use document type for
PDFs: `{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
"data": "<base64>"}}`).
Returns a typed `InvoiceData` dataclass (see below). Raises `AnthropicOcrError` on 4xx/5xx.
Use `urllib` only (no external HTTP client), consistent with project conventions.

System prompt for the OCR call:

```
You are an invoice data extraction assistant for a Swiss solar energy company (PVcontracting AG).
Extract structured data from the supplied invoice PDF. Respond ONLY with a JSON object — no
preamble, no markdown fences.

Required fields (null if not found):
{
  "supplier_name": "string — company or person name on the invoice",
  "supplier_address": "string — full address",
  "invoice_date": "string — ISO 8601 date (YYYY-MM-DD)",
  "due_date": "string — ISO 8601 date or null",
  "invoice_number": "string — invoice/Rechnungsnummer",
  "total_amount": "number — total including VAT, in CHF",
  "net_amount": "number — total excluding VAT or null",
  "vat_amount": "number — VAT amount or null",
  "vat_rate": "number — VAT rate as decimal (e.g. 0.081) or null",
  "currency": "string — ISO 4217 (usually CHF)",
  "iban": "string — IBAN without spaces or null",
  "qr_reference": "string — QR-Referenznummer (27 digits) or null",
  "payment_purpose": "string — Zahlungszweck/Mitteilung or null",
  "description": "string — brief description of goods/services (max 200 chars)",
  "confidence": "number — your overall extraction confidence 0.0–1.0"
}
```

**`InvoiceData` dataclass** (define in `anthropic_ocr_client.py` or a shared `models.py`):

```python
@dataclass
class InvoiceData:
    supplier_name: str | None
    supplier_address: str | None
    invoice_date: str | None          # ISO 8601
    due_date: str | None              # ISO 8601
    invoice_number: str | None
    total_amount: float | None
    net_amount: float | None
    vat_amount: float | None
    vat_rate: float | None
    currency: str | None
    iban: str | None                  # already stripped of spaces
    qr_reference: str | None
    payment_purpose: str | None
    description: str | None
    confidence: float                 # 0.0–1.0
```

**`api/moco_purchase_client.py` — `MocoPurchaseClient`**

Read/write client for the *source* Moco account (same account as `SourceMocoClient`).
Auth: `Authorization: Token token={MOCO_SOURCE_API_KEY}`.
Base URL: `https://{MOCO_SOURCE_SUBDOMAIN}.mocoapp.com/api/v1`.

Methods:
- `get_purchase(purchase_id: int) -> dict` — `GET /purchases/{id}`
- `patch_purchase(purchase_id: int, payload: dict) -> dict` — `PATCH /purchases/{id}`
- `post_comment(purchase_id: int, text: str) -> None` — `POST /comments` (reuse
  `SourceMocoClient.post_comment` pattern; commentable_type = `"Purchase"`)

`PATCH /purchases/{id}` payload fields to set (only include fields where OCR returned a value):

```python
{
    "date": invoice_data.invoice_date,           # "YYYY-MM-DD"
    "due_date": invoice_data.due_date,
    "title": invoice_data.description,
    "net_total": invoice_data.net_amount,        # float, CHF
    "gross_total": invoice_data.total_amount,    # float, CHF
    "currency": invoice_data.currency,
    "receipt_identifier": invoice_data.invoice_number,
    "iban": invoice_data.iban,
    "reference": invoice_data.qr_reference,
    "payment_note": invoice_data.payment_purpose,
    # Do NOT set "company_id" here — leave supplier matching to the human reviewer
    # unless confidence > 0.95 and a Moco company lookup is added later
}
```

**`api/supplier_invoice_ocr_service.py` — `SupplierInvoiceOcrService`**

Constructor: `__init__(self, source_moco: SourceMocoClient, purchase_client: MocoPurchaseClient, ocr: AnthropicOcrClient, telegram: TelegramNotifier | None = None)`

Main method: `process(event: str, payload: dict) -> dict`

Flow:
1. Gate on `event == "create"` — update/delete are no-ops (return `{"ok": True, "skipped": "event_not_create"}`)
2. Extract `purchase_id = payload["id"]` and `file_url = payload.get("file_url")`
3. If no `file_url`: Telegram alert + return `{"ok": True, "skipped": "no_file_url"}`
4. Download PDF bytes from `file_url` (pre-signed URL, no auth header needed — same pattern as `bexio_expense_sync_service.py` `_download_moco_file`)
5. Call `ocr.extract(pdf_bytes: bytes) -> InvoiceData`
6. Build PATCH payload from `InvoiceData` (skip null fields)
7. Call `purchase_client.patch_purchase(purchase_id, patch_payload)`
8. Post comment to Moco purchase:
   ```
   🤖 OCR-Extraktion (Konfidenz: {confidence:.0%})
   Lieferant: {supplier_name}
   Betrag: {currency} {total_amount:.2f}
   Datum: {invoice_date} / Fällig: {due_date}
   Rechnungs-Nr: {invoice_number}
   IBAN: {iban}
   QR-Ref: {qr_reference}
   ⚠️ Bitte Felder prüfen und Entwurf freigeben.
   ```
9. Telegram notification:
   - If `confidence >= 0.85`: `✅ OCR erfolgreich — {supplier_name} CHF {total_amount:.2f} — Moco Purchase #{purchase_id} aktualisiert. Bitte prüfen.`
   - If `confidence < 0.85`: `⚠️ OCR unsicher ({confidence:.0%}) — {supplier_name or "Unbekannt"} CHF {total_amount or "?"} — Moco Purchase #{purchase_id} bitte manuell prüfen.`
10. Return `{"ok": True, "purchase_id": purchase_id, "confidence": confidence}`

Error handling: follow the existing 2xx/5xx contract from `CLAUDE.md`. An `AnthropicOcrError`
from a 4xx is an application error (Telegram + 200 ok=false). A network failure reaching
Anthropic is infrastructure (502, no Telegram).

### Moco Webhook Configuration

In the source Moco account, create a new webhook:
- Event: `Purchase:create`
- URL: `https://{vercel-deployment-url}/api/supplier-invoice-ocr`
- Target header: `x-moco-target: supplier-invoice-ocr`
- Secret: same `MOCO_WEBHOOK_SECRET` or a new dedicated secret

### New Environment Variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude Vision |
| `MOCO_SOURCE_SUBDOMAIN` | Subdomain of the source Moco account (e.g. `pvcontracting`) |
| `MOCO_SOURCE_API_KEY` | Already exists for the Bexio flows — reuse |
| `TELEGRAM_BOT_TOKEN` | Already exists — reuse |
| `TELEGRAM_CHAT_ID` | Already exists — reuse |

Add to `REQUIRED_ENV_SUPPLIER_INVOICE_OCR` in `index.py`:
```python
REQUIRED_ENV_SUPPLIER_INVOICE_OCR = [
    *REQUIRED_ENV_TELEGRAM,
    "ANTHROPIC_API_KEY",
    "MOCO_SOURCE_API_KEY",
    "MOCO_SOURCE_SUBDOMAIN",
    "MOCO_WEBHOOK_SECRET",
    "MOCO_SOURCE_ACCOUNT_URL",
]
```

### Testing

Follow existing patterns: service tests inject in-memory fakes (`FakeAnthropicOcrClient`,
`FakeMocoPurchaseClient`, `FakeTelegram`). Endpoint tests stub `urllib.request.urlopen`
routing by hostname. Add a fixture `tests/fixtures/purchase_create_webhook.json` mirroring
a real Moco Purchase:create payload (with `file_url`, `id`, `company`, `items` fields).

---

## Approach 1 — Email-Ingest via Parse Webhook (Full Automation)

### Overview

Rechnungen werden nicht mehr direkt an Moco weitergeleitet. Stattdessen landen sie bei einem
Mail-Parse-Dienst (Mailgun oder Postmark), der einen Webhook mit dem PDF-Anhang an Vercel
schickt. Die Vercel Function läuft OCR, legt eine neue Moco Purchase an, und sendet eine
Telegram-Benachrichtigung.

```
Supplier email
     │
     ▼  (mail forwarding rule or MX record)
  Mailgun / Postmark inbound parse
     │
     │  Multipart POST with PDF attachment as base64
     ▼
POST /api/supplier-invoice-ingest       ← new endpoint
     │
     ├── 1. Validate parse-service HMAC (Mailgun) or shared secret (Postmark)
     ├── 2. Extract PDF attachment from multipart body
     ├── 3. OCR via AnthropicOcrClient → InvoiceData (same as Approach 2)
     ├── 4. POST /purchases (new Moco purchase, status = draft)
     ├── 5. Upload PDF to Moco purchase attachment
     ├── 6. POST comment with OCR summary
     └── 7. Telegram notification
```

This approach requires:
- Changing the mail forwarding rule (no longer forward to Moco directly)
- Setting up a Mailgun or Postmark inbound parse route
- Or: setting up a dedicated mailbox with an email-to-webhook bridge

### Recommended Parse Service

**Mailgun Inbound Routes** or **Postmark Inbound Webhooks** both work. Postmark is simpler
(no DNS changes if you use their inbound address), Mailgun gives you more control.

Postmark: create an inbound email address (e.g. `abc123@inbound.postmarkapp.com`), forward
supplier invoices there, and set the webhook URL to `/api/supplier-invoice-ingest`.
Postmark delivers a JSON body with `Attachments[].Content` as base64 and `Attachments[].ContentType`.

Mailgun: set up an inbound route matching the recipient address, configure the webhook URL.
Mailgun delivers multipart/form-data with `attachment-1` etc. as file fields.

**For implementation, choose Postmark** — the JSON body is easier to parse with `urllib` +
`json` than Mailgun's multipart, and no additional DNS setup is required for testing.

### New Files to Create (in addition to those from Approach 2)

```
api/supplier_invoice_ingest_service.py  — service class for the email-ingest flow
api/postmark_webhook_validator.py       — validates the Postmark inbound webhook token
api/moco_purchase_creator.py            — wraps POST /purchases + attachment upload
tests/test_supplier_invoice_ingest_service.py
tests/test_postmark_webhook_validator.py
tests/test_moco_purchase_creator.py
tests/fixtures/postmark_inbound_webhook.json  — sample Postmark inbound payload
```

### New Endpoint Details

`POST /api/supplier-invoice-ingest`

Auth: Postmark sets a configurable inbound token. Validate via a shared secret checked
against a custom header (e.g. `X-Postmark-Inbound-Token`), or accept from a fixed source IP.
Simpler approach: validate a `?token=...` query param against `POSTMARK_INBOUND_TOKEN` env var.

Postmark payload structure (relevant fields):
```json
{
  "From": "lieferant@example.ch",
  "Subject": "Rechnung 2024-042",
  "TextBody": "...",
  "Attachments": [
    {
      "Name": "rechnung.pdf",
      "Content": "<base64>",
      "ContentType": "application/pdf",
      "ContentLength": 123456
    }
  ]
}
```

Service flow:
1. Validate token
2. Find first attachment with `ContentType == "application/pdf"` (skip if none; Telegram alert)
3. Decode base64 → `pdf_bytes`
4. `AnthropicOcrClient.extract(pdf_bytes)` → `InvoiceData`
5. `MocoPurchaseCreator.create_purchase(invoice_data, sender_email, subject)` → `purchase_id`
6. `MocoPurchaseCreator.upload_attachment(purchase_id, pdf_bytes, filename)` → sets the PDF on the purchase
7. `MocoPurchaseCreator.post_comment(purchase_id, ocr_summary_text)`
8. Telegram notification (same confidence-based logic as Approach 2)

**`api/moco_purchase_creator.py` — `MocoPurchaseCreator`**

Wraps the source Moco API for creating new purchases.
Auth: `Authorization: Token token={MOCO_SOURCE_API_KEY}`.

`create_purchase(invoice_data: InvoiceData, sender_email: str, subject: str) -> int`:
```python
POST /purchases
{
    "date": invoice_data.invoice_date or today(),
    "due_date": invoice_data.due_date,
    "currency": invoice_data.currency or "CHF",
    "net_total": invoice_data.net_amount or invoice_data.total_amount,
    "gross_total": invoice_data.total_amount,
    "title": invoice_data.description or subject,
    "receipt_identifier": invoice_data.invoice_number,
    "iban": invoice_data.iban,
    "reference": invoice_data.qr_reference,
    "payment_note": invoice_data.payment_purpose,
    "tag": "ocr-import",   # useful for filtering in Moco UI
    # "company_id": resolved if supplier_name matches a Moco company — see note below
}
```
Returns the new `purchase["id"]`.

Company resolution (optional, implement as a second pass): call
`GET /companies?term={supplier_name}&type=supplier` and if exactly one match is returned
with high confidence, set `company_id`. Otherwise leave unset — the reviewer will assign.

`upload_attachment(purchase_id: int, pdf_bytes: bytes, filename: str) -> None`:
Moco attachment upload: `POST /purchases/{id}/attachments` with multipart/form-data,
field name `file`. Content-Type: `application/pdf`.

### New Environment Variables (additional to Approach 2)

| Variable | Purpose |
|---|---|
| `POSTMARK_INBOUND_TOKEN` | Shared secret to validate inbound webhook requests |

### Mailbox / Routing Setup Steps

1. Create a Postmark account and add an inbound email address.
2. Set the webhook URL to `https://{vercel-deployment-url}/api/supplier-invoice-ingest?token={POSTMARK_INBOUND_TOKEN}`.
3. Change the mail rule: instead of forwarding invoices to Moco's mail-import address,
   forward them to the Postmark inbound address.
4. Test with a real invoice email; check Telegram and Moco.
5. Once stable, remove the old Moco mail-import forwarding rule.

---

## Implementation Order

Recommended sequence:

1. **Implement `AnthropicOcrClient`** with tests — this is the shared core of both approaches.
   Validate the OCR quality against 3–5 real PVcontracting supplier invoices before proceeding.

2. **Implement Approach 2** (`supplier-invoice-ocr` endpoint) end-to-end.
   This is low-risk: existing Moco draft flow stays intact, the webhook only enriches.
   Deploy and run in parallel for 2–4 weeks.

3. **Evaluate OCR quality** from real-world data (Telegram notifications will show confidence
   scores). Adjust the system prompt if needed.

4. **Implement Approach 1** (`supplier-invoice-ingest` endpoint) once OCR quality is validated.
   Switch mail routing after a test period with a few forwarded invoices.

---

## Code Conventions to Follow

From `CLAUDE.md`:
- One class per file.
- `urllib` only for outbound HTTP — no `requests`, `httpx`, or other external HTTP clients.
- Service classes receive all collaborators via constructor injection.
- Tests use in-memory fakes, never hit the network.
- `TelegramNotifier` is optional on services (`telegram: TelegramNotifier | None = None`) so
  unit tests can omit it.
- Error split: 4xx upstream → application error (Telegram + 200 ok=false). Network/5xx → 502.
- Auth/validation rejections (bad token, no PDF, unsupported event) → 401/422, no Telegram.
- Skips with business-rule context (no PDF attached, OCR below threshold) → 200 ok=true with
  `skipped` key + Telegram alert.

The `AnthropicOcrClient` is the only new external dependency. It should be pure (no I/O side
effects beyond the API call) and raise typed exceptions (`AnthropicOcrError`) rather than
returning None on failure, so the service can distinguish application errors from skips.
