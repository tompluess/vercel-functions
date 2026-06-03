"""BrevoAPI — typed wrapper around the Brevo (ex-Sendinblue) Contacts API.

Owns base-URL construction, `api-key` auth headers, and urllib transport. No
business logic. Lives behind `BrevoContactSyncService` so it can be unit-tested
with a `FakeBrevoAPI` without monkeypatching urlopen.

Brevo uses an `api-key` request header (not `Authorization: Bearer`). Contact
identifier in URL paths can be either the numeric id or the URL-encoded email.

Docs: https://developers.brevo.com/reference/getcontactinfo
"""

import json
import logging
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote

logger = logging.getLogger("moco_sync")


class BrevoAPI:
    HTTP_TIMEOUT_SECONDS = 15
    BASE_URL = "https://api.brevo.com"

    def __init__(self, *, api_key: str):
        self._auth_headers = {
            "api-key": api_key,
            "Accept": "application/json",
        }

    def get_contact(self, identifier: str) -> dict | None:
        """GET /v3/contacts/{identifier}. Returns the contact, or None on 404.

        404 (document_not_found) is the normal "contact does not exist yet"
        signal — surfacing it as a return value lets the caller branch on it
        without exception handling, mirroring the n8n "Lookup contact in Brevo"
        node's success/error split.
        """
        try:
            return self._get(f"/v3/contacts/{quote(identifier, safe='')}")
        except urlerror.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def create_contact(self, payload: dict) -> dict:
        """POST /v3/contacts — returns at least `{"id": <int>}` on success.

        Body shape: `{"email": "...", "attributes": {"VORNAME": ...}, ...}`.
        """
        return self._send_json("/v3/contacts", payload, method="POST")

    def update_contact(self, identifier: str, payload: dict) -> dict:
        """PUT /v3/contacts/{identifier} — returns `{}` (204 No Content)."""
        return self._send_json(
            f"/v3/contacts/{quote(identifier, safe='')}", payload, method="PUT",
        )

    def add_to_list(self, list_id: int, emails: list[str]) -> dict:
        """POST /v3/contacts/lists/{listId}/contacts/add.

        Bulk add. Brevo's API has an awkward quirk: when **all** supplied
        emails are already in the list (or do not exist as contacts), the
        endpoint returns **HTTP 400** with a body like
        `{"code":"invalid_parameter","message":"Contact already in list and/or
        doesn't exist"}` — not 200 with an empty success array. From our
        perspective the desired post-condition (contact-in-list) already holds,
        so we map that 400 to a successful idempotent return rather than
        propagating an exception that would log a noisy traceback on every
        re-sync of an already-known contact (seen in prod for
        ev.aschwanden@gmail.com).

        Any other 4xx/5xx is propagated so the endpoint can map it (a 4xx →
        Telegram alert + 200 ok=false; a 5xx → 502 so Moco retries).
        """
        try:
            return self._send_json(
                f"/v3/contacts/lists/{list_id}/contacts/add",
                {"emails": emails}, method="POST",
            )
        except urlerror.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            if e.code == 400 and "already" in body.lower():
                logger.info("brevo list add: already in list (idempotent) "
                            "list_id=%s emails=%s", list_id, emails)
                return {
                    "contacts": {"success": [], "failure": list(emails)},
                    "already_in_list": True,
                    "brevo_status": 400,
                    "brevo_body": body,
                }
            logger.warning("brevo list add: %s %s", e.code, body)
            raise

    # --- private transport helpers ------------------------------------------

    def _get(self, path: str):
        req = urlrequest.Request(f"{self.BASE_URL}{path}",
                                 headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    def _send_json(self, path: str, payload, *, method: str):
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        data = json.dumps(payload).encode() if payload is not None else b""
        req = urlrequest.Request(f"{self.BASE_URL}{path}",
                                 data=data, method=method, headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}
