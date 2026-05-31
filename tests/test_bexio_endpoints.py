"""End-to-end tests for /api/bexio-expense-sync and /api/bexio-invoice-sync.

Patches urlopen in both `api.bexio_api` and `api.source_moco_client` so the
full request pipeline runs without network. Asserts the HMAC pipeline and
that Bexio receives the expected calls.
"""

import json
from urllib import request as urlrequest

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (FIXTURES_DIR, FakeUrlopenResponse, load_fixture,
                            signed_headers)


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Patches urlopen in api.bexio_api AND api.source_moco_client.

    Routes by hostname/path so tests get realistic Bexio responses and the
    source-Moco calls (companies, projects, comments, file downloads) succeed.
    `state` lets tests override individual responses and inspect every call.
    """
    state: dict = {
        "contact_search": [{"id": 6000}],
        "account_search": [{"id": 7000, "tax_id": 11}],
        "bills_search": {"data": [], "paging": {"item_count": 0}},
        "bill_by_id": {},
        "create_bill": {"id": 9100},
        "update_bill": {"id": 9100},
        "create_invoice": {"id": 9200},
        "create_contact": {"id": 6100},
        "templates": [{"template_slug": "default-de"}],
        "company": {"id": 1, "name": "X", "address": "X\nFoo 1\n8000 Bar"},
        "project": {"customer": {"id": 1, "name": "Muster AG"},
                    "labels": ["Stromproduktion"],
                    "billing_address": "Muster AG\nMusterstrasse 123\n8000 Zürich"},
        "calls": [],
    }

    def fake_urlopen(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        data = req.data
        try:
            payload = json.loads(data) if data else None
        except (ValueError, UnicodeDecodeError):
            payload = "<binary>"
        state["calls"].append((method, url, payload))

        # --- Bexio ---
        if "api.bexio.com" in url:
            if url.endswith("/2.0/contact/search"):
                return _resp(state["contact_search"])
            if url.endswith("/2.0/contact") and method == "POST":
                return _resp(state["create_contact"])
            if url.endswith("/2.0/accounts/search"):
                return _resp(state["account_search"])
            if "/4.0/purchase/bills" in url and method == "GET" and "?" in url:
                return _resp(state["bills_search"])
            if "/4.0/purchase/bills/" in url and method == "GET":
                bill_id = int(url.rsplit("/", 1)[-1])
                return _resp(state["bill_by_id"][bill_id])
            if url.endswith("/4.0/purchase/bills") and method == "POST":
                return _resp(state["create_bill"])
            if "/4.0/purchase/bills/" in url and method == "PUT":
                return _resp(state["update_bill"])
            if url.endswith("/3.0/files"):
                return _resp({"uuid": "uploaded-uuid"})
            if url.endswith("/3.0/document_templates"):
                return _resp(state["templates"])
            if url.endswith("/2.0/kb_invoice") and method == "POST":
                return _resp(state["create_invoice"])
            if "/2.0/kb_invoice/" in url and url.endswith("/issue"):
                return _resp({})
            if "/2.0/kb_invoice/" in url and url.endswith("/comment"):
                return _resp({})
            raise AssertionError(f"unexpected bexio request: {method} {url}")

        # --- source Moco ---
        if "mocoapp.com" in url:
            if "/companies/" in url:
                return _resp(state["company"])
            if "/projects/" in url:
                return _resp(state["project"])
            if url.endswith("/comments"):
                return _resp({"id": 999})
            # Signed file download (no auth header) — return raw bytes.
            return FakeUrlopenResponse(b"%PDF-1.4 fake")

        raise AssertionError(f"unexpected request: {method} {url}")

    import api.bexio_api as bexio_mod
    import api.source_moco_client as src_mod
    monkeypatch.setattr(bexio_mod.urlrequest, "urlopen", fake_urlopen)
    monkeypatch.setattr(src_mod.urlrequest, "urlopen", fake_urlopen)
    return state


def _resp(body) -> FakeUrlopenResponse:
    return FakeUrlopenResponse(json.dumps(body).encode())


@pytest.fixture
def bexio_client(set_env, stub_pipeline):
    from api.index import app
    return TestClient(app), stub_pipeline


def _moco_envelope(body: dict) -> bytes:
    """Production Moco webhooks wrap the entity inside a `body` key (matches
    the n8n workflows' "Extract Purchase" code node). Endpoint must unwrap."""
    return json.dumps({"body": body}).encode()


# --- expense endpoint -------------------------------------------------------

def test_expense_create_returns_200_and_creates_bill(bexio_client):
    client, state = bexio_client
    purchase = load_fixture("purchase_with_iban.json")
    raw = _moco_envelope(purchase)

    r = client.post(
        "/api/bexio-expense-sync",
        content=raw,
        headers=signed_headers(raw, target="Purchase", event="create"),
    )

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["event"] == "create"
    assert payload["action"] == "created"
    assert payload["bill_id"] == 9100
    # POST /4.0/purchase/bills happened.
    methods = [(c[0], c[1]) for c in state["calls"]]
    assert any(m == "POST" and u.endswith("/4.0/purchase/bills")
               for m, u in methods)


def test_expense_invalid_signature_returns_401(bexio_client):
    client, state = bexio_client
    raw = _moco_envelope(load_fixture("purchase_with_iban.json"))
    headers = signed_headers(raw, target="Purchase", event="create")
    headers["x-moco-signature"] = "0" * 64

    r = client.post("/api/bexio-expense-sync", content=raw, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_signature"
    # No outbound calls happened — pipeline rejected before service ran.
    assert state["calls"] == []


def test_expense_rejects_wrong_target(bexio_client):
    """The expense endpoint only handles x-moco-target=Purchase. Sending
    Activity (which belongs to /api/moco-sync) returns 422."""
    client, _ = bexio_client
    raw = _moco_envelope(load_fixture("purchase_with_iban.json"))
    r = client.post(
        "/api/bexio-expense-sync",
        content=raw,
        headers=signed_headers(raw, target="Activity", event="create"),
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "unexpected_target: Activity"


def test_expense_supports_unwrapped_body(bexio_client):
    """Webhook bodies sometimes arrive without the {"body": ...} envelope
    (e.g. when the user has stripped n8n's extract step). Both shapes must
    work so the endpoint isn't brittle to upstream changes."""
    client, _ = bexio_client
    purchase = load_fixture("purchase_with_iban.json")
    raw = json.dumps(purchase).encode()  # top-level, no envelope

    r = client.post(
        "/api/bexio-expense-sync",
        content=raw,
        headers=signed_headers(raw, target="Purchase", event="create"),
    )
    assert r.status_code == 200
    assert r.json()["bill_id"] == 9100


def test_expense_unknown_event_returns_422(bexio_client):
    client, _ = bexio_client
    raw = _moco_envelope(load_fixture("purchase_with_iban.json"))
    r = client.post(
        "/api/bexio-expense-sync",
        content=raw,
        headers=signed_headers(raw, target="Purchase", event="archive"),
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "event_not_handled: archive"


# --- invoice endpoint -------------------------------------------------------

def test_invoice_sent_creates_in_bexio_and_issues(bexio_client):
    client, state = bexio_client
    state["account_search"] = [{"id": 4400, "tax_id": 21}]
    invoice = load_fixture("invoice_sent.json")
    raw = _moco_envelope(invoice)

    r = client.post(
        "/api/bexio-invoice-sync",
        content=raw,
        headers=signed_headers(raw, target="Invoice", event="update"),
    )

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["action"] == "created"
    assert payload["invoice_id"] == 9200
    # /issue is called to move the invoice from DRAFT -> Open.
    urls = [c[1] for c in state["calls"]]
    assert any(u.endswith("/2.0/kb_invoice/9200/issue") for u in urls)


def test_invoice_draft_returns_200_but_skips(bexio_client):
    """status != "sent" must short-circuit without touching Bexio (so we
    don't churn drafts on every edit) and still return 200 — Moco's webhook
    log uses non-2xx to flag deliveries for retry."""
    client, state = bexio_client
    invoice = load_fixture("invoice_draft.json")
    raw = _moco_envelope(invoice)

    r = client.post(
        "/api/bexio-invoice-sync",
        content=raw,
        headers=signed_headers(raw, target="Invoice", event="update"),
    )
    assert r.status_code == 200
    assert r.json()["skipped"] == "status_not_sent"
    # No HTTP calls to anyone — gated upfront on status.
    assert state["calls"] == []


def test_invoice_rejects_wrong_target(bexio_client):
    client, _ = bexio_client
    raw = _moco_envelope(load_fixture("invoice_sent.json"))
    r = client.post(
        "/api/bexio-invoice-sync",
        content=raw,
        headers=signed_headers(raw, target="Purchase", event="update"),
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "unexpected_target: Purchase"


def test_invoice_bexio_502_surfaces_as_502(bexio_client, monkeypatch):
    """If Bexio returns a 4xx/5xx, the endpoint surfaces a 502 so Moco's
    delivery log makes the failure visible (and Moco retries the webhook)."""
    from urllib import error as urlerror

    def boom(req, timeout=None):
        url = req.full_url
        if "api.bexio.com" in url:
            raise urlerror.HTTPError(url, 500, "bexio kaboom", {}, fp=None)
        # Source-Moco project fetch happens first; it must succeed so the
        # service reaches the failing Bexio call.
        if "/projects/" in url:
            return FakeUrlopenResponse(json.dumps({
                "customer": {"id": 1, "name": "Muster AG"},
                "labels": [], "billing_address": "",
            }).encode())
        return FakeUrlopenResponse(b"{}")

    import api.bexio_api as bexio_mod
    monkeypatch.setattr(bexio_mod.urlrequest, "urlopen", boom)

    client, _ = bexio_client
    raw = _moco_envelope(load_fixture("invoice_sent.json"))
    r = client.post(
        "/api/bexio-invoice-sync",
        content=raw,
        headers=signed_headers(raw, target="Invoice", event="update"),
    )
    assert r.status_code == 502
    assert "bexio_error: 500" in r.json()["detail"]


def test_missing_bexio_token_returns_500(bexio_client, monkeypatch):
    monkeypatch.delenv("BEXIO_API_TOKEN", raising=False)
    client, _ = bexio_client
    raw = _moco_envelope(load_fixture("invoice_sent.json"))
    r = client.post(
        "/api/bexio-invoice-sync",
        content=raw,
        headers=signed_headers(raw, target="Invoice", event="update"),
    )
    assert r.status_code == 500
    assert r.json()["detail"] == "server_misconfigured"
