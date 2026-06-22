"""End-to-end tests for POST /api/supplier-invoice-ocr.

Patches `urlopen` in the four modules involved (anthropic ocr, moco purchase
client, source moco client, telegram notifier) so the full auth + dispatch
+ service pipeline runs without network. The shared patch is keyed by
hostname/path, like `test_bexio_endpoints.py`.
"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeUrlopenResponse, signed_headers


SAMPLE_OCR = {
    "supplier_name": "FLYERALARM",
    "supplier_address": "Alfred-Nobel-Str. 18, 97080 Würzburg",
    "invoice_date": "2026-05-12",
    "due_date": "2026-06-11",
    "invoice_number": "R-2026-042",
    "total_amount": 1234.50,
    "net_amount": 1142.00,
    "vat_amount": 92.50,
    "vat_rate": 0.081,
    "currency": "CHF",
    "iban": "CH4431999123000889012",  # QR-IBAN (IID 31999)
    "qr_reference": "210000000003139471430009017",
    "payment_purpose": "Rechnung Mai 2026",
    "description": "Solarmodule und Montage",
    "is_credit_note": False,
    "commission": None,
    "confidence": 0.92,
}

WEBHOOK_BODY = {
    "id": 3001069,
    "file_url": "https://data.mocoapp.com/objects/fake.pdf?sig=abc",
}


@pytest.fixture(autouse=True)
def _ocr_env(monkeypatch):
    """Endpoint needs ANTHROPIC_API_KEY on top of what TEST_ENV already
    provides. The VAT code is now resolved dynamically per invoice."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _anthropic_response(text: str) -> bytes:
    return json.dumps({
        "id": "msg_x", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-6", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }).encode()


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Routes outbound requests by hostname to in-memory responses."""
    state: dict = {
        "ocr_text": json.dumps(SAMPLE_OCR),
        "suppliers": [{"id": 555, "name": "FLYERALARM"}],
        "company_by_id": {555: {"id": 555, "name": "FLYERALARM",
                                "default_vat_code_purchase_id": 77}},
        "vat_codes": [
            {"id": 11, "tax": 8.1, "code": "1", "active": True, "default": False},
            {"id": 12, "tax": 2.6, "code": "2", "active": True, "default": True},
            {"id": 13, "tax": 0.0, "code": "0", "active": True, "default": False},
        ],
        "next_purchase_id": 4001234,
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

        # Telegram (success + failure paths both fire)
        if "api.telegram.org" in url:
            return FakeUrlopenResponse(
                json.dumps({"ok": True, "result": {"message_id": 1}}).encode()
            )

        # Anthropic OCR
        if "api.anthropic.com" in url:
            return FakeUrlopenResponse(_anthropic_response(state["ocr_text"]))

        # Source Moco — PDF download via signed url
        if "data.mocoapp.com" in url:
            return FakeUrlopenResponse(b"%PDF-1.4 fake-test-pdf")

        # Moco API
        if "mocoapp.com/api/v1" in url:
            if method == "GET" and url.endswith("/vat_code_purchases"):
                return FakeUrlopenResponse(json.dumps(state["vat_codes"]).encode())
            if method == "GET" and "/companies/" in url:
                # /companies/{id} — used by the vat-resolver supplier-default
                # fallback. Path tail is the id.
                cid = int(url.rsplit("/", 1)[-1])
                return FakeUrlopenResponse(
                    json.dumps(state["company_by_id"].get(cid, {})).encode()
                )
            if method == "GET" and "/companies" in url:
                # /companies?type=supplier — supplier search
                return FakeUrlopenResponse(json.dumps(state["suppliers"]).encode())
            if method == "POST" and url.endswith("/purchases"):
                # Confirm the base64'd PDF rode along — that's the whole point.
                assert isinstance(payload, dict) and "file" in payload
                assert payload["file"]["base64"]
                created = {
                    "id": state["next_purchase_id"],
                    "identifier": "E260042",
                    "tags": payload.get("tags", []),
                }
                return FakeUrlopenResponse(json.dumps(created).encode())
            if method == "POST" and url.endswith("/comments"):
                return FakeUrlopenResponse(json.dumps({"id": 1}).encode())
            if method == "DELETE" and "/purchases/drafts/" in url:
                # Service auto-deletes the draft after a successful create.
                return FakeUrlopenResponse(b"")
            raise AssertionError(f"unexpected Moco request: {method} {url}")

        raise AssertionError(f"unexpected request: {method} {url}")

    # Patch every module that imports urlrequest. Each `monkeypatch.setattr`
    # on `mod.urlrequest.urlopen` redirects through the shared urllib
    # module, but doing it per-module documents intent.
    import api.anthropic_ocr_client as ocr_mod
    import api.moco_purchase_client as mpc_mod
    import api.source_moco_client as src_mod
    import api.telegram_notifier as tg_mod
    for mod in (ocr_mod, mpc_mod, src_mod, tg_mod):
        monkeypatch.setattr(mod.urlrequest, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def client(set_env, stub_pipeline):
    from api.index import app
    return TestClient(app)


# --- happy path -------------------------------------------------------------

def test_happy_path_creates_real_purchase_with_attachment(client, stub_pipeline):
    raw = json.dumps(WEBHOOK_BODY).encode()
    headers = signed_headers(raw, target="Purchase::Draft",
                             event="create")
    resp = client.post("/api/supplier-invoice-ocr", content=raw,
                       headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["event"] == "create"
    assert body["draft_id"] == 3001069
    assert body["purchase_id"] == 4001234
    assert body["company_id"] == 555

    # POST /purchases was called with the OCR'd payload + base64 PDF.
    post_calls = [c for c in stub_pipeline["calls"]
                  if c[0] == "POST" and c[1].endswith("/api/v1/purchases")]
    assert len(post_calls) == 1
    payload = post_calls[0][2]
    assert payload["currency"] == "CHF"
    assert payload["payment_method"] == "bank_transfer_swiss_qr_esr"
    assert payload["tags"] == ["OCR", "Review pending"]
    assert payload["company_id"] == 555
    # SAMPLE_OCR carries vat_rate=0.081 → matched to vat_codes[id=11, value=8.1].
    assert payload["items"][0]["vat_code_id"] == 11
    # Base64-decoding the file blob recovers the original PDF bytes.
    decoded = base64.b64decode(payload["file"]["base64"])
    assert decoded == b"%PDF-1.4 fake-test-pdf"
    # The original draft is auto-deleted after the create succeeds.
    delete_calls = [c for c in stub_pipeline["calls"]
                    if c[0] == "DELETE"
                    and c[1].endswith("/api/v1/purchases/drafts/3001069")]
    assert len(delete_calls) == 1


def test_supplier_not_found_omits_company_id(client, stub_pipeline):
    stub_pipeline["suppliers"] = []
    raw = json.dumps(WEBHOOK_BODY).encode()
    headers = signed_headers(raw, target="Purchase::Draft",
                             event="create")
    resp = client.post("/api/supplier-invoice-ocr", content=raw,
                       headers=headers)
    assert resp.status_code == 200
    assert resp.json()["company_id"] is None
    post_calls = [c for c in stub_pipeline["calls"]
                  if c[0] == "POST" and c[1].endswith("/api/v1/purchases")]
    assert "company_id" not in post_calls[0][2]


# --- auth + dispatch gates --------------------------------------------------

def test_rejects_wrong_target(client):
    raw = json.dumps(WEBHOOK_BODY).encode()
    headers = signed_headers(raw, target="not-the-right-target", event="create")
    resp = client.post("/api/supplier-invoice-ocr", content=raw,
                       headers=headers)
    assert resp.status_code == 422
    assert "unexpected_target" in resp.text


def test_rejects_bad_event(client):
    raw = json.dumps(WEBHOOK_BODY).encode()
    headers = signed_headers(raw, target="Purchase::Draft",
                             event="delete")
    resp = client.post("/api/supplier-invoice-ocr", content=raw,
                       headers=headers)
    assert resp.status_code == 422


def test_rejects_bad_signature(client):
    raw = json.dumps(WEBHOOK_BODY).encode()
    headers = signed_headers(raw, target="Purchase::Draft",
                             event="create")
    headers["x-moco-signature"] = "deadbeef"
    resp = client.post("/api/supplier-invoice-ocr", content=raw,
                       headers=headers)
    assert resp.status_code == 401


# --- skip + error mapping ---------------------------------------------------

def test_update_event_skips_without_calling_ocr(client, stub_pipeline):
    """`update` passes the dispatcher's event gate but the service's
    own gate skips OCR — returns ok=true with skipped='event_not_create'."""
    raw = json.dumps(WEBHOOK_BODY).encode()
    headers = signed_headers(raw, target="Purchase::Draft",
                             event="update")
    resp = client.post("/api/supplier-invoice-ocr", content=raw,
                       headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "event": "update",
                            "skipped": "event_not_create"}
    # No anthropic / purchase calls.
    assert not any("api.anthropic.com" in c[1] for c in stub_pipeline["calls"])


def test_no_file_url_is_skipped_with_telegram_alert(client, stub_pipeline):
    raw = json.dumps({"id": 3001069}).encode()    # no file_url
    headers = signed_headers(raw, target="Purchase::Draft",
                             event="create")
    resp = client.post("/api/supplier-invoice-ocr", content=raw,
                       headers=headers)
    assert resp.status_code == 200
    assert resp.json()["skipped"] == "no_file_url"
    assert any("api.telegram.org" in c[1] for c in stub_pipeline["calls"])
    assert not any("api.anthropic.com" in c[1] for c in stub_pipeline["calls"])
