"""SourceMocoClient — read-only/comment-only client for the *source* Moco account.

The existing `MocoAPI` is scoped to a *target* Moco account and only owns the
endpoints needed to replicate activities. The Bexio sync flows additionally
need to read company data from the source account, fetch a webhook's
attachment via a signed `file_url`, and post a comment back so the Bexio link
is visible to the user inside Moco. Kept as a separate collaborator to keep
each class single-purpose (see CLAUDE.md).
"""

import json
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
