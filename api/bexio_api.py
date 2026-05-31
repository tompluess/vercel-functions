"""BexioAPI — typed wrapper around the Bexio REST endpoints used by the sync services.

Owns base-URL construction, Bearer auth headers, urllib transport, and
multipart file uploads. No business logic. Lives behind the two sync services
so they can be unit-tested with a `FakeBexioAPI` without monkeypatching urlopen.

Bexio exposes versioned namespaces; the two workflows we replicate touch /2.0,
/3.0 and /4.0 endpoints, so the version is part of each method's URL.
"""

import json
import logging
import mimetypes
import uuid
from urllib import request as urlrequest

logger = logging.getLogger("moco_sync")


class BexioAPI:
    HTTP_TIMEOUT_SECONDS = 30  # multipart upload of a PDF can take a few seconds
    BASE_URL = "https://api.bexio.com"

    def __init__(self, *, api_token: str):
        self._auth_headers = {
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
        }

    def search_contact_by_name(self, name: str) -> list[dict]:
        """POST /2.0/contact/search — `like` match on name_1.

        Returns an empty list when no contact matches (Bexio returns `[]`).
        """
        return self._post_json(
            "/2.0/contact/search",
            [{"field": "name_1", "value": name, "criteria": "like"}],
        )

    def create_contact(self, payload: dict) -> dict:
        """POST /2.0/contact — returns the created contact incl. its `id`."""
        return self._post_json("/2.0/contact", payload)

    def search_account_by_no(self, account_no: str) -> list[dict]:
        """POST /2.0/accounts/search — exact match on `account_no`.

        Used to map a Moco credit_account (e.g. "6600") onto a Bexio booking
        account id + its default tax_id.
        """
        return self._post_json(
            "/2.0/accounts/search",
            [{"field": "account_no", "value": str(account_no), "criteria": "="}],
        )

    def search_bills(self, *, vendor: str, vendor_ref: str | None = None) -> dict:
        """GET /4.0/purchase/bills — filter by vendor name (and optional vendor_ref).

        Returns the raw Bexio envelope `{"data": [...], "paging": {...}}`.
        """
        query = f"vendor={_urlencode(vendor)}"
        if vendor_ref:
            query += f"&vendor_ref={_urlencode(vendor_ref)}"
        return self._get(f"/4.0/purchase/bills?{query}")

    def get_bill(self, bill_id: int) -> dict:
        return self._get(f"/4.0/purchase/bills/{bill_id}")

    def create_bill(self, payload: dict) -> dict:
        return self._post_json("/4.0/purchase/bills", payload)

    def update_bill(self, bill_id: int, payload: dict) -> dict:
        return self._put_json(f"/4.0/purchase/bills/{bill_id}", payload)

    def create_invoice(self, payload: dict) -> dict:
        return self._post_json("/2.0/kb_invoice", payload)

    def issue_invoice(self, invoice_id: int) -> dict:
        return self._post_json(f"/2.0/kb_invoice/{invoice_id}/issue", None)

    def comment_invoice(self, invoice_id: int, payload: dict) -> dict:
        return self._post_json(f"/2.0/kb_invoice/{invoice_id}/comment", payload)

    def list_document_templates(self) -> list[dict]:
        return self._get("/3.0/document_templates")

    def upload_file(self, *, filename: str, content: bytes,
                    mime_type: str | None = None) -> dict:
        """POST /3.0/files — multipart/form-data upload.

        Returns the file record incl. its `uuid`, which is what bill payloads
        reference under `attachment_ids`.
        """
        mime = mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        boundary = f"----vercelfn-{uuid.uuid4().hex}"
        body = _multipart_body(boundary=boundary, fields={"name": filename},
                               file_field="attachment", filename=filename,
                               file_bytes=content, file_mime=mime)
        headers = {
            **self._auth_headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        }
        req = urlrequest.Request(f"{self.BASE_URL}/3.0/files",
                                 data=body, method="POST", headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    # --- private transport helpers ------------------------------------------------

    def _get(self, path: str):
        req = urlrequest.Request(f"{self.BASE_URL}{path}", headers=self._auth_headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read())

    def _post_json(self, path: str, payload):
        return self._send_json(path, payload, method="POST")

    def _put_json(self, path: str, payload):
        return self._send_json(path, payload, method="PUT")

    def _send_json(self, path: str, payload, *, method: str):
        headers = {**self._auth_headers, "Content-Type": "application/json"}
        data = json.dumps(payload).encode() if payload is not None else b""
        req = urlrequest.Request(f"{self.BASE_URL}{path}",
                                 data=data, method=method, headers=headers)
        with urlrequest.urlopen(req, timeout=self.HTTP_TIMEOUT_SECONDS) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}


def _urlencode(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _multipart_body(*, boundary: str, fields: dict[str, str], file_field: str,
                    filename: str, file_bytes: bytes, file_mime: str) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {file_mime}\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts)
