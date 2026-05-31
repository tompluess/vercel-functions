"""End-to-end tests for /api/brevo-contact-sync.

Patches urlopen in both `api.brevo_api` and `api.source_moco_client` so the
full request pipeline runs without network. Asserts the HMAC pipeline,
envelope unwrapping, target/event gating, and that Brevo receives the
expected calls in the right order.
"""

import json
from urllib import error as urlerror

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeUrlopenResponse, load_fixture, signed_headers


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Patches urlopen in api.brevo_api AND api.source_moco_client.

    Routes by hostname/path so the source-Moco `post_comment` call succeeds
    and Brevo returns realistic shapes. `state` lets individual tests override
    the lookup response (None vs. existing) and inspect every call.
    """
    state: dict = {
        "lookup_response": None,        # None = 404 / contact_not_found
        "create_response": {"id": 9001},
        "update_response": {},
        "list_add_response": {"contacts": {"success": [], "failure": []}},
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

        # --- Brevo ---
        if "api.brevo.com" in url:
            if "/v3/contacts/lists/" in url and url.endswith("/contacts/add"):
                return _resp(state["list_add_response"])
            if method == "POST" and url.endswith("/v3/contacts"):
                return _resp(state["create_response"])
            if method == "PUT" and "/v3/contacts/" in url:
                return _resp(state["update_response"])
            if method == "GET" and "/v3/contacts/" in url:
                if state["lookup_response"] is None:
                    raise urlerror.HTTPError(url, 404, "not found", {}, fp=None)
                return _resp(state["lookup_response"])
            raise AssertionError(f"unexpected brevo request: {method} {url}")

        # --- source Moco ---
        if "mocoapp.com" in url:
            if url.endswith("/comments"):
                return _resp({"id": 999})
            raise AssertionError(f"unexpected moco request: {method} {url}")

        raise AssertionError(f"unexpected request: {method} {url}")

    import api.brevo_api as brevo_mod
    import api.source_moco_client as src_mod
    monkeypatch.setattr(brevo_mod.urlrequest, "urlopen", fake_urlopen)
    monkeypatch.setattr(src_mod.urlrequest, "urlopen", fake_urlopen)
    return state


def _resp(body) -> FakeUrlopenResponse:
    return FakeUrlopenResponse(json.dumps(body).encode())


@pytest.fixture
def brevo_client(set_env, stub_pipeline):
    from api.index import app
    return TestClient(app), stub_pipeline


def _moco_envelope(body: dict) -> bytes:
    return json.dumps({"body": body}).encode()


def test_create_returns_200_and_creates_contact_in_brevo(brevo_client):
    client, state = brevo_client
    raw = _moco_envelope(load_fixture("contact_create.json"))

    r = client.post(
        "/api/brevo-contact-sync",
        content=raw,
        headers=signed_headers(raw, target="Contact", event="create"),
    )

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["event"] == "create"
    assert payload["action"] == "created"
    assert payload["brevo_id"] == 9001
    assert payload["email"] == "max@muster.example"

    # The Brevo call order must be: lookup -> create -> SMS update -> list add.
    brevo_urls = [(m, u) for m, u, _ in state["calls"]
                  if "api.brevo.com" in u]
    methods_in_order = [m for m, _ in brevo_urls]
    assert methods_in_order == ["GET", "POST", "PUT", "POST"]
    # The Moco comment-back was posted.
    assert any(u.endswith("/api/v1/comments") for _, u, _ in state["calls"])


def test_update_routes_to_put_not_post(brevo_client):
    """When lookup returns an existing contact, we must NOT call POST /contacts
    (which would 400 with `Contact already exist`). Update path only."""
    client, state = brevo_client
    state["lookup_response"] = {"id": 9001, "email": "max@muster.example"}
    raw = _moco_envelope(load_fixture("contact_update.json"))

    r = client.post(
        "/api/brevo-contact-sync",
        content=raw,
        headers=signed_headers(raw, target="Contact", event="update"),
    )

    assert r.status_code == 200
    assert r.json()["action"] == "updated"

    brevo_methods = [m for m, u, _ in state["calls"] if "api.brevo.com" in u]
    # GET (lookup), PUT (main attrs), PUT (SMS), POST (list add). No POST /contacts.
    assert brevo_methods == ["GET", "PUT", "PUT", "POST"]
    create_calls = [(m, u) for m, u, _ in state["calls"]
                    if m == "POST" and u.endswith("/v3/contacts")]
    assert create_calls == []
    # No Moco comment on update — only on create.
    assert not any(u.endswith("/api/v1/comments") for _, u, _ in state["calls"])


def test_skips_when_work_email_is_empty(brevo_client):
    client, state = brevo_client
    raw = _moco_envelope(load_fixture("contact_no_email.json"))

    r = client.post(
        "/api/brevo-contact-sync",
        content=raw,
        headers=signed_headers(raw, target="Contact", event="create"),
    )

    assert r.status_code == 200
    assert r.json()["skipped"] == "no_work_email"
    # No outbound calls at all.
    assert state["calls"] == []


def test_invalid_signature_returns_401(brevo_client):
    client, state = brevo_client
    raw = _moco_envelope(load_fixture("contact_create.json"))
    headers = signed_headers(raw, target="Contact", event="create")
    headers["x-moco-signature"] = "0" * 64

    r = client.post("/api/brevo-contact-sync", content=raw, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_signature"
    assert state["calls"] == []


def test_rejects_wrong_target(brevo_client):
    client, _ = brevo_client
    raw = _moco_envelope(load_fixture("contact_create.json"))
    r = client.post(
        "/api/brevo-contact-sync",
        content=raw,
        headers=signed_headers(raw, target="Purchase", event="create"),
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "unexpected_target: Purchase"


def test_rejects_delete_event(brevo_client):
    """We sync create+update only — delete is out of scope for this endpoint."""
    client, _ = brevo_client
    raw = _moco_envelope(load_fixture("contact_create.json"))
    r = client.post(
        "/api/brevo-contact-sync",
        content=raw,
        headers=signed_headers(raw, target="Contact", event="delete"),
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "event_not_handled: delete"


def test_supports_unwrapped_body(brevo_client):
    """Both `{"body": {...}}` envelope and top-level shapes must work."""
    client, _ = brevo_client
    raw = json.dumps(load_fixture("contact_create.json")).encode()

    r = client.post(
        "/api/brevo-contact-sync",
        content=raw,
        headers=signed_headers(raw, target="Contact", event="create"),
    )
    assert r.status_code == 200
    assert r.json()["action"] == "created"


def test_brevo_500_surfaces_as_502(set_env, monkeypatch):
    """If Brevo returns 5xx on the lookup, surface as 502 so Moco's delivery
    log makes the failure visible and Moco retries the webhook."""
    def boom(req, timeout=None):
        if "api.brevo.com" in req.full_url:
            raise urlerror.HTTPError(req.full_url, 500, "kaboom", {}, fp=None)
        return FakeUrlopenResponse(b"{}")

    import api.brevo_api as brevo_mod
    import api.source_moco_client as src_mod
    monkeypatch.setattr(brevo_mod.urlrequest, "urlopen", boom)
    monkeypatch.setattr(src_mod.urlrequest, "urlopen", boom)

    from api.index import app
    client = TestClient(app)
    raw = _moco_envelope(load_fixture("contact_create.json"))
    r = client.post(
        "/api/brevo-contact-sync",
        content=raw,
        headers=signed_headers(raw, target="Contact", event="create"),
    )
    assert r.status_code == 502
    assert "brevo_error: 500" in r.json()["detail"]


def test_already_in_list_400_does_not_fail_endpoint(brevo_client, monkeypatch):
    """Regression for prod log: list-add returns 400 "Contact already in list"
    on every re-sync of a known contact. Endpoint must still return 200 (the
    contact mutation already succeeded) AND no scary stack trace should be
    logged — the BrevoAPI handles it as an idempotent success."""
    import io

    client, state = brevo_client
    # Have the lookup HIT so we go through the update path (matches the
    # ev.aschwanden case where the contact already existed).
    state["lookup_response"] = {"id": 9001, "email": "max@muster.example"}

    # Wrap the existing stub so the list-add specifically returns 400.
    import api.brevo_api as brevo_mod
    real_urlopen = brevo_mod.urlrequest.urlopen

    def list_add_400(req, timeout=None):
        if (req.get_method() == "POST"
                and "/v3/contacts/lists/" in req.full_url
                and req.full_url.endswith("/contacts/add")):
            body = (b'{"code":"invalid_parameter","message":"Contact already '
                    b'in list and/or doesn\'t exist"}')
            raise urlerror.HTTPError(req.full_url, 400, "Bad Request", {},
                                     fp=io.BytesIO(body))
        return real_urlopen(req, timeout=timeout)

    monkeypatch.setattr(brevo_mod.urlrequest, "urlopen", list_add_400)

    raw = _moco_envelope(load_fixture("contact_update.json"))
    r = client.post(
        "/api/brevo-contact-sync",
        content=raw,
        headers=signed_headers(raw, target="Contact", event="update"),
    )
    assert r.status_code == 200
    assert r.json()["action"] == "updated"


def test_missing_brevo_key_returns_500(brevo_client, monkeypatch):
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    client, _ = brevo_client
    raw = _moco_envelope(load_fixture("contact_create.json"))
    r = client.post(
        "/api/brevo-contact-sync",
        content=raw,
        headers=signed_headers(raw, target="Contact", event="create"),
    )
    assert r.status_code == 500
    assert r.json()["detail"] == "server_misconfigured"
