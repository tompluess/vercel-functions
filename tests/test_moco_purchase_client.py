"""Unit tests for MocoPurchaseClient — draft read, create purchase (with
base64 attachment), and comment posting. Supplier-company lookup lives on
SourceMocoClient — see `test_source_moco_client.py`."""

import json
from urllib import error as urlerror

import pytest

import api.moco_purchase_client as mpc_mod
from api.moco_purchase_client import MocoPurchaseClient
from tests.conftest import FakeUrlopenResponse


@pytest.fixture
def calls(monkeypatch):
    """Capture every outbound request; tests set the next response body
    (and optionally a non-2xx status to simulate Moco errors)."""
    state: dict = {"calls": [], "next_response": b"{}", "next_status": 200}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        data = req.data
        try:
            payload = json.loads(data) if data else None
        except (ValueError, UnicodeDecodeError):
            payload = data
        state["calls"].append({
            "url": url, "method": method, "payload": payload,
            "headers": dict(req.header_items()),
        })
        if state["next_status"] >= 400:
            raise urlerror.HTTPError(url, state["next_status"], "err", {}, fp=None)
        return FakeUrlopenResponse(state["next_response"])

    monkeypatch.setattr(mpc_mod.urlrequest, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def client():
    return MocoPurchaseClient(subdomain="solar", api_key="test_source_key")


# --- get_purchase_draft -----------------------------------------------------

def test_get_purchase_draft_hits_drafts_url_with_token_auth(client, calls):
    """The draft endpoint lives under /purchases/drafts/{id}; hitting
    /purchases/{id} returns 403 (observed in prod) so the URL prefix is
    load-bearing."""
    calls["next_response"] = json.dumps({"id": 3001069, "title": "draft"}).encode()
    result = client.get_purchase_draft(3001069)
    assert result == {"id": 3001069, "title": "draft"}
    call = calls["calls"][0]
    assert call["url"] == "https://solar.mocoapp.com/api/v1/purchases/drafts/3001069"
    assert call["method"] == "GET"
    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["authorization"] == "Token token=test_source_key"


def test_get_purchase_draft_propagates_http_errors(client, calls):
    """A 403 on the wrong URL space or a 404 must bubble — the validation
    script + the handler need to see the status to decide retry semantics."""
    calls["next_status"] = 403
    with pytest.raises(urlerror.HTTPError):
        client.get_purchase_draft(3001069)


# --- list_vat_codes ---------------------------------------------------------

def test_list_vat_codes_returns_array(client, calls):
    """GET /vat_code_purchases returns the available VAT codes (objects
    with at least `id` and `value`). The OCR service maps the OCR'd
    vat_rate to one of these `id`s."""
    calls["next_response"] = json.dumps([
        {"id": 11, "tax": 8.1, "code": "1", "active": True},
        {"id": 12, "tax": 2.6, "code": "2", "active": True},
        {"id": 13, "tax": 0.0, "code": "0", "active": True},
    ]).encode()
    result = client.list_vat_codes()
    assert [c["id"] for c in result] == [11, 12, 13]
    call = calls["calls"][0]
    assert call["url"] == "https://solar.mocoapp.com/api/v1/vat_code_purchases"
    assert call["method"] == "GET"


def test_list_vat_codes_handles_non_array_response(client, calls):
    """Defensive: if Moco wraps in `{"data": [...]}` someday or returns
    something unexpected, return [] rather than crash."""
    calls["next_response"] = json.dumps({"data": []}).encode()
    assert client.list_vat_codes() == []


def test_list_vat_codes_propagates_http_errors(client, calls):
    """A 5xx during vat-code lookup propagates so the handler returns 502
    (Moco retry) — silently treating it as empty would push a
    vat_code_id-less payload that Moco then 422s on POST /purchases."""
    calls["next_status"] = 500
    with pytest.raises(urlerror.HTTPError):
        client.list_vat_codes()


# --- create_purchase --------------------------------------------------------

def test_create_purchase_posts_to_purchases_endpoint_with_json(client, calls):
    """POST /purchases with the full payload as JSON body — the attachment
    is part of the body (base64), not multipart."""
    calls["next_response"] = json.dumps({"id": 4001234, "identifier": "E260042"}).encode()
    payload = {
        "date": "2026-05-12",
        "currency": "CHF",
        "payment_method": "bank_transfer_swiss_qr_esr",
        "tags": ["OCR", "Review pending"],
        "items": [{"title": "OCR import", "total": 1234.50,
                   "tax_included": True, "vat_code_id": 11}],
        "file": {"filename": "rechnung.pdf", "base64": "JVBERi0xLjQK"},
    }
    result = client.create_purchase(payload)
    assert result == {"id": 4001234, "identifier": "E260042"}
    call = calls["calls"][0]
    assert call["url"] == "https://solar.mocoapp.com/api/v1/purchases"
    assert call["method"] == "POST"
    assert call["payload"] == payload
    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["content-type"] == "application/json"
    assert headers["authorization"] == "Token token=test_source_key"


def test_create_purchase_returns_empty_dict_on_empty_response(client, calls):
    """Defensive: Moco may respond 201 No Content; don't crash on json.loads(b'')."""
    calls["next_response"] = b""
    assert client.create_purchase({"date": "2026-01-01"}) == {}


def test_create_purchase_propagates_422_for_invalid_payload(client, calls):
    """A 422 (missing vat_code_id, invalid currency, etc.) must surface so
    the handler maps it to an app error + Telegram alert — silent failure
    would create a purchase-shaped void in Moco."""
    calls["next_status"] = 422
    with pytest.raises(urlerror.HTTPError):
        client.create_purchase({"date": "2026-01-01"})


# --- post_comment -----------------------------------------------------------

def test_post_comment_uses_comments_endpoint_with_purchase_type(client, calls):
    calls["next_response"] = json.dumps({"id": 555}).encode()
    client.post_comment(4001234, "OCR run complete")
    call = calls["calls"][0]
    assert call["url"] == "https://solar.mocoapp.com/api/v1/comments"
    assert call["method"] == "POST"
    assert call["payload"] == {
        "commentable_id": 4001234,
        "commentable_type": "Purchase",
        "text": "OCR run complete",
    }


def test_post_comment_propagates_http_errors(client, calls):
    calls["next_status"] = 500
    with pytest.raises(urlerror.HTTPError):
        client.post_comment(4001234, "hi")


# --- construction -----------------------------------------------------------

def test_subdomain_is_used_in_base_url():
    c = MocoPurchaseClient(subdomain="staging-acct", api_key="k")
    assert "https://staging-acct.mocoapp.com/api/v1" in c._base_url
