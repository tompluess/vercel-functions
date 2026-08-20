"""MocoInvoiceClient — create Moco sales invoices + attachments.

Used by `EnergyCreditNoteService` to turn a billed project expense (created
via `MocoClient.create_project_expense`) into a real Moco invoice. Kept
separate from `MocoClient` (one-class-per-file, CLAUDE.md) since invoice
creation is its own endpoint family (`/invoices`) distinct from the
project/company/comment endpoints `MocoClient` owns.

Endpoints used (confirmed against the Moco OpenAPI spec,
docs.mocoapp.com/api/docs/v1/openapi.json):
  - GET  /api/v1/vat_code_sales               — VAT codes valid on invoice items
  - POST /api/v1/invoices                     — create an invoice
  - POST /api/v1/invoices/{id}/attachments    — attach a file (base64, JSON body)

Auth: `Authorization: Token token={MOCO_API_KEY}`.

No `update_status` method: the created invoice is deliberately left in
Moco's default `status: "created"` — sending is a manual step the operator
performs later in the Moco UI (see `EnergyCreditNoteService`).
"""

import json
from urllib import request as urlrequest


class MocoInvoiceClient:
    HTTP_TIMEOUT_SECONDS = 30  # attachment upload carries the base64 PDF

    def __init__(self, *, subdomain: str, api_key: str):
        self._base_url = f"https://{subdomain}.mocoapp.com/api/v1"
        self._auth_headers = {
            "Authorization": f"Token token={api_key}",
            "Accept": "application/json",
        }

    def list_vat_code_sales(self) -> list[dict]:
        """GET /vat_code_sales — VAT codes valid on invoice (sales) items.

        Distinct from `MocoPurchaseClient.list_vat_codes`
        (`/vat_code_purchases`) — Moco keeps separate purchase-side and
        sales-side VAT code catalogs.
        """
        url = f"{self._base_url}/vat_code_sales"
        req = urlrequest.Request(url, headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, list) else []

    def create_invoice(self, payload: dict) -> dict:
        """POST /invoices — create a new invoice.

        Required per Moco docs: `customer_id`, `recipient_address`, `date`,
        `due_date`, `title`, `currency`, `items`. Either `vat_code_id` or
        the deprecated `tax` must be provided. Caller builds the payload —
        this method is pure transport. Returns the created invoice dict
        (with the Moco-assigned `id`).
        """
        url = f"{self._base_url}/invoices"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        data = json.dumps(payload).encode()
        req = urlrequest.Request(url, data=data, method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def add_attachment(self, invoice_id: int, *, filename: str,
                        base64_content: str) -> dict:
        """POST /invoices/{id}/attachments — upload a file to an invoice."""
        url = f"{self._base_url}/invoices/{invoice_id}/attachments"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        payload = {"attachment": {"filename": filename,
                                  "base64": base64_content}}
        data = json.dumps(payload).encode()
        req = urlrequest.Request(url, data=data, method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}
