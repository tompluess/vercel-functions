"""MocoClient — read-only/comment-only client for the attached Moco account.

`MocoAPI` is scoped to the *target* Moco account of the moco-sync replication
flow and only owns the endpoints needed to replicate activities. The Bexio
sync flows additionally need to read company data from the account, fetch a
webhook's attachment via a signed `file_url`, and post a comment back so the
Bexio link is visible to the user inside Moco. Kept as a separate
collaborator to keep each class single-purpose (see CLAUDE.md).
"""

import json
from urllib import parse as urlparse
from urllib import request as urlrequest


class MocoClient:
    HTTP_TIMEOUT_SECONDS = 30  # attachment download may dominate

    def __init__(self, *, subdomain: str, api_key: str):
        self._base_url = f"https://{subdomain}.mocoapp.com/api/v1"
        self._auth_headers = {
            "Authorization": f"Token token={api_key}",
            "Accept": "application/json",
        }

    def get_company(self, company_id: int) -> dict:
        req = urlrequest.Request(f"{self._base_url}/companies/{company_id}",
                                 headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def list_suppliers(self, *, limit: int = 1000) -> list[dict]:
        """GET /companies?type=supplier — full supplier list, paginated.

        Feeds `MocoSupplierMatcher`, whose substring tier needs companies
        whose name is a *substring* of the OCR'd supplier name — a
        server-side `term=<ocr name>` search can never return those, so
        the matching runs fully client-side against the complete list.

        Moco's `type` filter is singular per the API docs
        (https://docs.mocoapp.com/api/docs/v1#tag/companies/GET/companies);
        `type=suppliers` silently returns the full list (no filter
        applied), `type=supplier` is what actually narrows it — and it
        also keeps customers from accidentally being linked as suppliers.
        4xx/5xx propagate as HTTPError so the caller can map them to the
        right retry semantics.
        """
        return self._list_companies(company_type="supplier", limit=limit)

    def list_customers(self, *, limit: int = 1000) -> list[dict]:
        """GET /companies?type=customer — full customer list, paginated.

        Feeds `EnergyCreditNoteService.is_evu_tagged_customer`: an EVU's
        `Lokaler Energieversorger (EVU)` tag can live on its `type:
        "customer"` company record rather than (or in addition to) any
        `type: "supplier"` record — confirmed live for both CKW and BKW,
        since the relationship an energy-credit-note represents is
        PVcontracting selling production back to the EVU, i.e. the EVU
        acting as a customer, even though the document itself arrives in
        the Purchase::Draft inbox. Not used for the general purchase
        pipeline's own company-linking (that stays supplier-only by
        design — see `list_suppliers`).
        """
        return self._list_companies(company_type="customer", limit=limit)

    def _list_companies(self, *, company_type: str, limit: int) -> list[dict]:
        """Shared pagination loop for `list_suppliers`/`list_customers`.

        Paginates with `per_page=100` until the last page or `limit`.
        """
        companies: list[dict] = []
        page = 1
        per_page = 100
        while len(companies) < limit:
            params = urlparse.urlencode({"type": company_type,
                                         "per_page": per_page,
                                         "page": page})
            url = f"{self._base_url}/companies?{params}"
            req = urlrequest.Request(url, headers=self._auth_headers)
            with urlrequest.urlopen(req,
                                    timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
                batch = json.loads(resp.read())
            if not isinstance(batch, list) or not batch:
                break
            companies.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return companies[:limit]

    def get_project(self, project_id: int) -> dict:
        req = urlrequest.Request(f"{self._base_url}/projects/{project_id}",
                                 headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def list_projects(self, *, limit: int = 200) -> list[dict]:
        """GET /projects — list **active** projects on the Moco account.

        Used by the batch validation script to build a Kommission-index for
        `MocoProjectResolver`. Moco's listing is active-only by default; we
        do not pass `include_archived` (the spec restricts matching to
        active projects). Paginates with `per_page=100` until the last page
        or `limit` is reached.
        """
        projects: list[dict] = []
        page = 1
        per_page = 100
        while len(projects) < limit:
            url = (f"{self._base_url}/projects"
                   f"?per_page={per_page}&page={page}")
            req = urlrequest.Request(url, headers=self._auth_headers)
            with urlrequest.urlopen(req,
                                    timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
                batch = json.loads(resp.read())
            if not isinstance(batch, list) or not batch:
                break
            projects.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return projects[:limit]

    def create_project_expense(self, project_id: int, payload: dict) -> dict:
        """POST /projects/{id}/expenses — create one additional service.

        Used by the smart-me energy-bill flow to book the Netto-Betrag as
        a billable expense on the matched project. An attachment rides
        along JSON-embedded as `file: {filename, base64}` (same convention
        as POST /purchases — not multipart). Pure transport; the caller
        builds the payload. 4xx/5xx propagate as HTTPError so the endpoint
        can map them to the retry semantics.
        """
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        req = urlrequest.Request(
            f"{self._base_url}/projects/{project_id}/expenses",
            data=json.dumps(payload).encode(),
            method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def post_comment(self, *, commentable_id: int, commentable_type: str,
                     text: str) -> dict:
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        payload = {"commentable_id": commentable_id,
                   "commentable_type": commentable_type,
                   "text": text}
        req = urlrequest.Request(f"{self._base_url}/comments",
                                 data=json.dumps(payload).encode(),
                                 method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def download_file(self, signed_url: str) -> bytes:
        """Download an attachment from Moco's signed `file_url`.

        These URLs are pre-signed by Moco and require no auth header; passing
        one would actually cause a 403 against Moco's object storage.
        """
        req = urlrequest.Request(signed_url)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return resp.read()
