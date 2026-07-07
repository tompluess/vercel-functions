"""Unit tests for MocoClient.list_suppliers.

The rest of MocoClient (get_company / get_project / post_comment /
download_file) is exercised end-to-end via the Bexio and OCR endpoint tests
— they stub urlopen so the wrappers are trivially covered by call-site
assertions. The supplier listing has non-trivial client-side logic
(pagination, defensive shape handling) that deserves focused unit tests.
The name matching itself lives in MocoSupplierMatcher (own test module).
"""

import json
from urllib import error as urlerror

import pytest

import api.moco_client as src_mod
from api.moco_client import MocoClient
from tests.conftest import FakeUrlopenResponse


@pytest.fixture
def calls(monkeypatch):
    state: dict = {"calls": [], "responses": [], "next_status": 200}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        state["calls"].append({
            "url": url, "method": method,
            "headers": dict(req.header_items()),
        })
        if state["next_status"] >= 400:
            raise urlerror.HTTPError(url, state["next_status"], "err", {}, fp=None)
        # Responses are consumed in order; the last one repeats so a
        # single-page test doesn't have to queue terminators.
        if len(state["responses"]) > 1:
            return FakeUrlopenResponse(state["responses"].pop(0))
        return FakeUrlopenResponse(state["responses"][0]
                                   if state["responses"] else b"[]")

    monkeypatch.setattr(src_mod.urlrequest, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def client():
    return MocoClient(subdomain="solar", api_key="test_source_key")


def test_list_suppliers_uses_type_supplier_and_pagination_params(client, calls):
    """`type=supplier` narrows server-side (customers must never be
    linkable as purchase suppliers); per_page/page drive pagination."""
    calls["responses"] = [json.dumps([
        {"id": 100, "name": "FLYERALARM"},
        {"id": 102, "name": "Other AG"},
    ]).encode()]
    result = client.list_suppliers()
    assert [c["id"] for c in result] == [100, 102]
    call = calls["calls"][0]
    assert call["method"] == "GET"
    assert call["url"] == (
        "https://solar.mocoapp.com/api/v1/companies"
        "?type=supplier&per_page=100&page=1"
    )
    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["authorization"] == "Token token=test_source_key"


def test_list_suppliers_stops_after_a_short_page(client, calls):
    """A page shorter than per_page is the last one — no extra request."""
    calls["responses"] = [json.dumps([{"id": 1, "name": "A"}]).encode()]
    client.list_suppliers()
    assert len(calls["calls"]) == 1


def test_list_suppliers_follows_pagination_until_short_page(client, calls):
    """A full page (100 entries) means there may be more — fetch page 2."""
    page1 = [{"id": i, "name": f"S{i}"} for i in range(100)]
    page2 = [{"id": 100, "name": "S100"}]
    calls["responses"] = [json.dumps(page1).encode(),
                          json.dumps(page2).encode()]
    result = client.list_suppliers()
    assert len(result) == 101
    assert [c["url"].endswith(f"page={n}")
            for c, n in zip(calls["calls"], (1, 2))] == [True, True]


def test_list_suppliers_respects_limit(client, calls):
    """`limit` caps the result even when Moco would keep paginating."""
    page = [{"id": i, "name": f"S{i}"} for i in range(100)]
    calls["responses"] = [json.dumps(page).encode()]
    result = client.list_suppliers(limit=50)
    assert len(result) == 50
    assert len(calls["calls"]) == 1


def test_list_suppliers_propagates_http_errors(client, calls):
    """A 5xx during the company listing must propagate so the handler can
    map it to 502 — silently treating it as "no suppliers" would link no
    company on every retry, which is worse than failing loudly."""
    calls["next_status"] = 500
    with pytest.raises(urlerror.HTTPError):
        client.list_suppliers()


def test_list_suppliers_handles_unexpected_response_shape(client, calls):
    """Defensive: if Moco's response shape ever changes (e.g. wraps results
    in a `data` key), don't crash — return empty so the OCR service falls
    back to "no company linked" rather than 500-ing on a schema drift."""
    calls["responses"] = [json.dumps({"data": []}).encode()]
    assert client.list_suppliers() == []
