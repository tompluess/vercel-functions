"""SourceMocoClient — read-only/comment-only client for the *source* Moco account.

The existing `MocoAPI` is scoped to a *target* Moco account and only owns the
endpoints needed to replicate activities. The Bexio sync flows additionally
need to read company data from the source account, fetch a webhook's
attachment via a signed `file_url`, and post a comment back so the Bexio link
is visible to the user inside Moco. Kept as a separate collaborator to keep
each class single-purpose (see CLAUDE.md).
"""

import json
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


class SourceMocoClient:
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

    def search_suppliers(self, name: str) -> list[dict]:
        """GET /companies?type=supplier&term=<name> — find candidate suppliers.

        Server-side narrowing via `term`: Moco's company list supports a
        `term` query that filters by name substring. We additionally
        constrain `type=supplier` so customers can't accidentally be
        linked as suppliers, then apply a client-side case-insensitive
        **exact** match — `term` returns broader matches (substring /
        prefix) and we don't want to auto-link `company_id` on a fuzzy
        hit (a misassignment would silently skew supplier reporting).
        Ambiguity (multiple exact matches) is left for the human reviewer.

        Returns an empty list when nothing matches; 4xx/5xx propagate as
        HTTPError so the caller can map them to the right retry semantics
        (a 404 from `term=...` with no results is mapped to empty too).
        """
        if not name or not name.strip():
            return []
        term = name.strip()
        # Moco's `type` filter is singular per the API docs
        # (https://docs.mocoapp.com/api/docs/v1#tag/companies/GET/companies);
        # `type=suppliers` silently returns the full list (no filter
        # applied), `type=supplier` is what actually narrows it.
        params = urlparse.urlencode({"type": "supplier", "term": term})
        url = f"{self._base_url}/companies?{params}"
        req = urlrequest.Request(url, headers=self._auth_headers)
        try:
            with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read())
        except urlerror.HTTPError as e:
            if e.code == 404:
                return []
            raise
        if not isinstance(data, list):
            return []
        target = term.casefold()
        return [c for c in data
                if isinstance(c, dict)
                and (c.get("name") or "").strip().casefold() == target]

    def get_project(self, project_id: int) -> dict:
        req = urlrequest.Request(f"{self._base_url}/projects/{project_id}",
                                 headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

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
