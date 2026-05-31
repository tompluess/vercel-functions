"""Unit tests for BrevoAPI — verify URL construction, auth headers, payload
serialization, and the 404 -> None mapping on get_contact."""

import json
from urllib import error as urlerror

import pytest

import api.brevo_api as brevo_mod
from api.brevo_api import BrevoAPI
from tests.conftest import FakeUrlopenResponse


@pytest.fixture
def calls(monkeypatch):
    """Capture every outbound request; let each test set the next response."""
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
        if state["next_status"] == 404:
            raise urlerror.HTTPError(url, 404, "not found", {}, fp=None)
        return FakeUrlopenResponse(state["next_response"])

    monkeypatch.setattr(brevo_mod.urlrequest, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def brevo():
    return BrevoAPI(api_key="test_brevo_key")


def test_get_contact_url_encodes_email(brevo, calls):
    calls["next_response"] = json.dumps({"id": 42, "email": "a+b@example.com"}).encode()
    result = brevo.get_contact("a+b@example.com")
    assert result == {"id": 42, "email": "a+b@example.com"}
    # @ and + must be percent-encoded in the path so Brevo doesn't misparse.
    assert calls["calls"][0]["url"] == \
        "https://api.brevo.com/v3/contacts/a%2Bb%40example.com"
    assert calls["calls"][0]["method"] == "GET"
    # api-key auth header is sent (not Bearer).
    header_names = {k.lower() for k in calls["calls"][0]["headers"]}
    assert "api-key" in header_names


def test_get_contact_returns_none_on_404(brevo, calls):
    calls["next_status"] = 404
    assert brevo.get_contact("missing@example.com") is None


def test_get_contact_reraises_non_404_errors(brevo, monkeypatch):
    """500s, 401s etc. must NOT be swallowed — the caller (and the endpoint)
    needs to surface those as 502 so Moco retries the webhook."""
    def boom(req, timeout=None):
        raise urlerror.HTTPError(req.full_url, 500, "kaboom", {}, fp=None)
    monkeypatch.setattr(brevo_mod.urlrequest, "urlopen", boom)
    with pytest.raises(urlerror.HTTPError):
        brevo.get_contact("foo@example.com")


def test_create_contact_posts_json_body(brevo, calls):
    calls["next_response"] = json.dumps({"id": 100}).encode()
    payload = {"email": "x@y.example", "attributes": {"VORNAME": "X"}}
    result = brevo.create_contact(payload)
    assert result == {"id": 100}
    call = calls["calls"][0]
    assert call["url"] == "https://api.brevo.com/v3/contacts"
    assert call["method"] == "POST"
    assert call["payload"] == payload


def test_update_contact_uses_put(brevo, calls):
    brevo.update_contact("x@y.example", {"attributes": {"SMS": "+41771234567"}})
    call = calls["calls"][0]
    assert call["method"] == "PUT"
    assert call["url"].endswith("/v3/contacts/x%40y.example")
    assert call["payload"] == {"attributes": {"SMS": "+41771234567"}}


def test_add_to_list_posts_emails_array(brevo, calls):
    brevo.add_to_list(5, ["a@example.com", "b@example.com"])
    call = calls["calls"][0]
    assert call["url"] == "https://api.brevo.com/v3/contacts/lists/5/contacts/add"
    assert call["method"] == "POST"
    assert call["payload"] == {"emails": ["a@example.com", "b@example.com"]}


def test_add_to_list_treats_400_already_in_list_as_success(brevo, monkeypatch):
    """Regression: Brevo returns HTTP 400 with body
    `{"code":"invalid_parameter","message":"Contact already in list and/or
    doesn't exist"}` when the contact is already a list member. From our
    perspective the desired state already holds, so add_to_list must NOT
    raise — otherwise every re-sync of a known contact logs a stack trace
    (observed in prod for ev.aschwanden@gmail.com)."""
    import io

    body = (b'{"code":"invalid_parameter","message":"Contact already in list '
            b'and/or doesn\'t exist"}')

    def fake_urlopen(req, timeout=None):
        raise urlerror.HTTPError(req.full_url, 400, "Bad Request", {},
                                 fp=io.BytesIO(body))

    monkeypatch.setattr(brevo_mod.urlrequest, "urlopen", fake_urlopen)
    result = brevo.add_to_list(5, ["ev.aschwanden@gmail.com"])
    assert result["already_in_list"] is True
    assert result["brevo_status"] == 400
    assert result["contacts"]["failure"] == ["ev.aschwanden@gmail.com"]


def test_add_to_list_reraises_other_400s(brevo, monkeypatch):
    """A 400 without 'already' in the body (e.g. bad payload shape) must
    still propagate so it surfaces as a 502 upstream — silently swallowing
    every 400 would hide real bugs."""
    import io

    body = b'{"code":"invalid_parameter","message":"emails must be an array"}'

    def fake_urlopen(req, timeout=None):
        raise urlerror.HTTPError(req.full_url, 400, "Bad Request", {},
                                 fp=io.BytesIO(body))

    monkeypatch.setattr(brevo_mod.urlrequest, "urlopen", fake_urlopen)
    with pytest.raises(urlerror.HTTPError):
        brevo.add_to_list(5, ["x@y.example"])


def test_add_to_list_reraises_5xx(brevo, monkeypatch):
    """500s from Brevo (rate limit, outage) must NOT be treated as
    idempotent successes — the list membership is unknown after a 500."""
    import io

    def fake_urlopen(req, timeout=None):
        raise urlerror.HTTPError(req.full_url, 502, "Bad Gateway", {},
                                 fp=io.BytesIO(b"upstream"))

    monkeypatch.setattr(brevo_mod.urlrequest, "urlopen", fake_urlopen)
    with pytest.raises(urlerror.HTTPError):
        brevo.add_to_list(5, ["x@y.example"])
