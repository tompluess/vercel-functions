"""Unit tests for BexioAPI — URL construction, headers, JSON / multipart encoding.

urlopen is patched so the tests exercise the real wrapper but touch no network.
"""

import json
from urllib import request as urlrequest

import pytest

from api.bexio_api import BexioAPI
from tests.conftest import FakeUrlopenResponse


@pytest.fixture
def bexio_calls(monkeypatch):
    calls: list[dict] = []
    responses: dict[str, object] = {"default": {"ok": True}}

    def fake_urlopen(req, timeout=None):
        calls.append({
            "method": req.get_method(),
            "url": req.full_url,
            "headers": dict(req.headers),
            "data": req.data,
        })
        # Allow tests to pre-register per-URL-suffix responses.
        for suffix, body in responses.items():
            if suffix != "default" and req.full_url.endswith(suffix):
                return FakeUrlopenResponse(json.dumps(body).encode())
        return FakeUrlopenResponse(json.dumps(responses["default"]).encode())

    import api.bexio_api as mod
    monkeypatch.setattr(mod.urlrequest, "urlopen", fake_urlopen)
    return {"calls": calls, "responses": responses}


def test_authorization_header_is_bearer(bexio_calls):
    api = BexioAPI(api_token="abc123")
    api.search_contact_by_name("Foo")

    headers = bexio_calls["calls"][0]["headers"]
    # urllib normalizes header names to title-case.
    assert headers["Authorization"] == "Bearer abc123"
    assert headers["Accept"] == "application/json"


def test_token_provider_resolved_lazily_once(bexio_calls):
    """With a token_provider, the access token is fetched on the first request
    and reused (memoized) for subsequent ones — one resolution per instance."""
    class FakeProvider:
        def __init__(self):
            self.calls = 0

        def get_access_token(self):
            self.calls += 1
            return "oauth_access_token"

    provider = FakeProvider()
    api = BexioAPI(token_provider=provider)
    assert provider.calls == 0  # nothing resolved at construction

    api.search_contact_by_name("Foo")
    api.create_bill({"title": "X"})

    assert provider.calls == 1  # resolved once, then cached
    for call in bexio_calls["calls"]:
        assert call["headers"]["Authorization"] == "Bearer oauth_access_token"


def test_requires_a_token_or_provider():
    with pytest.raises(ValueError):
        BexioAPI()


def test_search_contact_posts_filter_array(bexio_calls):
    BexioAPI(api_token="t").search_contact_by_name("FLYERALARM")

    call = bexio_calls["calls"][0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.bexio.com/2.0/contact/search"
    assert json.loads(call["data"]) == [
        {"field": "name_1", "value": "FLYERALARM", "criteria": "like"}
    ]


def test_search_bills_url_quotes_query_values(bexio_calls):
    BexioAPI(api_token="t").search_bills(vendor="A&B GmbH",
                                         vendor_ref="X 1/2")

    url = bexio_calls["calls"][0]["url"]
    assert url.startswith("https://api.bexio.com/4.0/purchase/bills?")
    # & in vendor name and / in ref must be URL-encoded so they don't
    # silently terminate the query / segment.
    assert "vendor=A%26B%20GmbH" in url
    assert "vendor_ref=X%201%2F2" in url


def test_create_bill_posts_json(bexio_calls):
    BexioAPI(api_token="t").create_bill({"title": "X"})

    call = bexio_calls["calls"][0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.bexio.com/4.0/purchase/bills"
    assert json.loads(call["data"]) == {"title": "X"}
    assert call["headers"]["Content-type"] == "application/json"


def test_update_bill_uses_put(bexio_calls):
    BexioAPI(api_token="t").update_bill(42, {"title": "Y"})

    call = bexio_calls["calls"][0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/4.0/purchase/bills/42")


def test_issue_invoice_posts_with_empty_body(bexio_calls):
    BexioAPI(api_token="t").issue_invoice(100)

    call = bexio_calls["calls"][0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/2.0/kb_invoice/100/issue")
    assert call["data"] == b""


def test_book_bill_puts_to_bookings_booked(bexio_calls):
    BexioAPI(api_token="t").book_bill(9001)

    call = bexio_calls["calls"][0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/4.0/purchase/bills/9001/bookings/BOOKED")
    assert call["data"] == b""


def test_create_outgoing_payment_posts_json(bexio_calls):
    BexioAPI(api_token="t").create_outgoing_payment({"bill_id": "9001",
                                                     "amount": 67.43})

    call = bexio_calls["calls"][0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/4.0/payment/outgoing-payments")
    assert json.loads(call["data"]) == {"bill_id": "9001", "amount": 67.43}


def test_upload_file_sends_multipart_with_boundary_and_uuid(bexio_calls):
    bexio_calls["responses"]["/3.0/files"] = {"uuid": "the-uuid"}

    result = BexioAPI(api_token="t").upload_file(
        filename="receipt.pdf", content=b"%PDF-1.4 hello",
        mime_type="application/pdf",
    )

    assert result == {"uuid": "the-uuid"}
    call = bexio_calls["calls"][0]
    content_type = call["headers"]["Content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=")[1]
    body = call["data"]
    # Body must contain the file part with its filename, the mime, and the
    # raw bytes — and end with the closing boundary marker.
    assert f"--{boundary}".encode() in body
    assert b'filename="receipt.pdf"' in body
    assert b"Content-Type: application/pdf" in body
    assert b"%PDF-1.4 hello" in body
    assert body.endswith(f"--{boundary}--\r\n".encode())
