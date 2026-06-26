"""MocoPurchaseClient — read draft purchases + create real purchases on the source Moco account.

The OCR webhook fires on Moco's `Purchase:create` (the draft that email-import
made), but drafts can't be patched: `PATCH /purchases/drafts/{id}` returns
404. So the flow is: read the draft (to grab its `file_url`), OCR the PDF,
then **create a new, real purchase** via `POST /purchases` with the
extracted fields, the PDF base64-encoded as an attachment, and the tags
`["OCR", "Review pending"]` so the human reviewer can find it in Moco's UI.

Endpoints used:
  - GET   /api/v1/purchases/drafts/{id}     — read the source draft
  - GET   /api/v1/vat_code_purchases        — list available VAT codes for
                                              purchase items (used by the
                                              OCR service to map `vat_rate`
                                              to `vat_code_id`)
  - POST  /api/v1/purchases                 — create the real purchase
  - POST  /api/v1/comments                  — comment with the OCR summary

Auth: `Authorization: Token token={MOCO_SOURCE_API_KEY}`.

Supplier company lookup (`GET /companies?type=supplier`) lives in
`SourceMocoClient.search_suppliers` rather than here — it's a generic
company-list operation on the source account that sits next to
`get_company(id)`, not something purchase-specific.

Kept separate from `SourceMocoClient` for one-class-per-file (CLAUDE.md)
and because the draft URL space + the JSON-base64 attachment format are
specific to the purchase-create flow.
"""

import json
import logging
from urllib import request as urlrequest

logger = logging.getLogger("moco_purchase_client")


class MocoPurchaseClient:
    HTTP_TIMEOUT_SECONDS = 30  # POST /purchases carries the base64 PDF, can be large

    def __init__(self, *, subdomain: str, api_key: str):
        self._base_url = f"https://{subdomain}.mocoapp.com/api/v1"
        self._auth_headers = {
            "Authorization": f"Token token={api_key}",
            "Accept": "application/json",
        }

    def list_purchase_drafts(self, *, limit: int = 100) -> list[dict]:
        """GET /purchases/drafts — list draft purchases.

        Used by the batch validation script (`scripts/batch_ocr_drafts.py`)
        to enumerate every pending draft and run the OCR pipeline across
        them. Production webhook flow does not need this — webhooks deliver
        one draft at a time.

        Paginates with `per_page=100` (Moco's typical max) until either the
        last page is reached or the caller-supplied `limit` is hit. Returned
        list is trimmed to `limit`; ordering is whatever Moco returns
        (callers that care about ordering sort client-side).
        """
        drafts: list[dict] = []
        page = 1
        per_page = 100
        while len(drafts) < limit:
            url = (f"{self._base_url}/purchases/drafts"
                   f"?per_page={per_page}&page={page}")
            req = urlrequest.Request(url, headers=self._auth_headers)
            with urlrequest.urlopen(req,
                                    timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
                batch = json.loads(resp.read())
            if not isinstance(batch, list) or not batch:
                break
            drafts.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return drafts[:limit]

    def get_purchase_draft(self, purchase_id: int) -> dict:
        """GET /purchases/drafts/{id} — read a draft purchase.

        The OCR service uses this only in the validation script (the
        webhook receives the draft body directly); the script reads the
        existing draft to show a before/after diff to the operator.
        """
        url = f"{self._base_url}/purchases/drafts/{purchase_id}"
        req = urlrequest.Request(url, headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def delete_purchase_draft(self, purchase_id: int) -> None:
        """DELETE /purchases/drafts/{id} — remove a draft purchase.

        The OCR service calls this after successfully creating a real
        purchase from the draft, so the operator doesn't have to clean
        up duplicates manually in Moco's UI. A 404 is treated as "already
        gone" (idempotent) and swallowed; other failures propagate so
        the caller can log + alert.
        """
        url = f"{self._base_url}/purchases/drafts/{purchase_id}"
        req = urlrequest.Request(url, method="DELETE", headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS):
            return

    def list_vat_codes(self) -> list[dict]:
        """GET /vat_code_purchases — list the VAT codes valid on items.

        Returns objects with at least `id` and `value` (the rate). The OCR
        service uses this to translate the extracted `vat_rate` into the
        `vat_code_id` Moco requires on every purchase item. Re-fetched per
        request: vat-code lists change rarely, but the project pattern is
        per-request collaborators (no module-level state).
        """
        url = f"{self._base_url}/vat_code_purchases"
        req = urlrequest.Request(url, headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, list) else []

    def list_categories(self) -> list[dict]:
        """GET /purchases/categories — the catalog of bookkeeping accounts.

        Returns objects with at least `id` and `credit_account` (a string
        like `"4000"`). The OCR service uses this to translate either a
        project's `Aufwandkonto` custom-property or the hardcoded
        Wareneinkauf default (`"4000"`) into the `category_id` Moco
        requires on each purchase item. Re-fetched per request to keep
        the resolver fresh; categories change rarely but adding one
        shouldn't require a redeploy.
        """
        url = f"{self._base_url}/purchases/categories"
        req = urlrequest.Request(url, headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, list) else []

    def create_purchase(self, payload: dict) -> dict:
        """POST /purchases — create a new (non-draft) purchase.

        Required fields per Moco docs: `date`, `currency`, `payment_method`,
        and `items` with at least one position containing `title`, `total`,
        and `vat_code_id`. File attachments are JSON-embedded as
        `{"file": {"filename": "...", "base64": "..."}}`. Tags are a JSON
        string array, e.g. `["OCR", "Review pending"]`.

        Caller is responsible for constructing the payload — this method
        is pure transport. Returns the created purchase dict (with the
        Moco-assigned `id`).
        """
        url = f"{self._base_url}/purchases"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        data = json.dumps(payload).encode()
        req = urlrequest.Request(url, data=data, method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def assign_item_to_project(self, purchase_id: int, item_id: int,
                                *, project_id: int,
                                notify_project_leader: bool,
                                billable: bool, budget_relevant: bool,
                                surcharge: bool,
                                expense_id: int | None = None) -> dict:
        """POST /purchases/{id}/assign_to_project — link one line item.

        Moco's docs require the assignment to be made per line item, so
        callers loop. When `expense_id` is omitted Moco creates a fresh
        expense on the project; passing one links to an existing expense
        (we don't need that path yet).

        Returns the assignment response (caller can ignore — the side
        effect is the project link).
        """
        url = f"{self._base_url}/purchases/{purchase_id}/assign_to_project"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        payload: dict = {
            "item_id": item_id,
            "project_id": project_id,
            "notify_project_leader": notify_project_leader,
            "billable": billable,
            "budget_relevant": budget_relevant,
            "surcharge": surcharge,
        }
        if expense_id is not None:
            payload["expense_id"] = expense_id
        data = json.dumps(payload).encode()
        req = urlrequest.Request(url, data=data, method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def post_comment(self, purchase_id: int, text: str) -> dict:
        """POST /comments — attach a comment to a purchase.

        commentable_type is "Purchase" — same as the Bexio expense flow's
        comments (Moco shares the polymorphism between drafts and real
        purchases here).
        """
        url = f"{self._base_url}/comments"
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        payload = {
            "commentable_id": purchase_id,
            "commentable_type": "Purchase",
            "text": text,
        }
        data = json.dumps(payload).encode()
        req = urlrequest.Request(url, data=data, method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}
