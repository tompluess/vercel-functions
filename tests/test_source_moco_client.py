"""Unit tests for SourceMocoClient.search_suppliers.

The rest of SourceMocoClient (get_company / get_project / post_comment /
download_file) is exercised end-to-end via the Bexio and OCR endpoint tests
— they stub urlopen so the wrappers are trivially covered by call-site
assertions. The supplier search has non-trivial client-side logic
(case-insensitive exact matching, blank guard, 404 fallback) that deserves
focused unit tests."""

import json
from urllib import error as urlerror

import pytest

import api.source_moco_client as src_mod
from api.source_moco_client import SourceMocoClient
from tests.conftest import FakeUrlopenResponse


@pytest.fixture
def calls(monkeypatch):
    state: dict = {"calls": [], "next_response": b"{}", "next_status": 200}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        state["calls"].append({
            "url": url, "method": method,
            "headers": dict(req.header_items()),
        })
        if state["next_status"] >= 400:
            raise urlerror.HTTPError(url, state["next_status"], "err", {}, fp=None)
        return FakeUrlopenResponse(state["next_response"])

    monkeypatch.setattr(src_mod.urlrequest, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def client():
    return SourceMocoClient(subdomain="solar", api_key="test_source_key")


def test_search_suppliers_uses_type_and_term_query_params(client, calls):
    """Server-side narrowing via `term=` plus client-side exact
    case-insensitive filter: Moco's `term=` returns substring/prefix
    matches, but we only auto-link on a fully-qualified exact name
    (partial matches are too risky for auto-linking `company_id`)."""
    calls["next_response"] = json.dumps([
        {"id": 100, "name": "FLYERALARM"},
        {"id": 101, "name": "Flyeralarm GmbH"},   # substring hit, not exact
        {"id": 102, "name": "Other AG"},          # ignored entirely
    ]).encode()
    result = client.search_suppliers("flyeralarm")
    assert [c["id"] for c in result] == [100]
    call = calls["calls"][0]
    assert call["method"] == "GET"
    # Both `type` and `term` ride along; order is stable from urlencode().
    assert call["url"] == (
        "https://solar.mocoapp.com/api/v1/companies"
        "?type=supplier&term=flyeralarm"
    )
    headers = {k.lower(): v for k, v in call["headers"].items()}
    assert headers["authorization"] == "Token token=test_source_key"


def test_search_suppliers_url_encodes_special_chars_in_term(client, calls):
    """Real supplier names contain spaces, ampersands, umlauts. The term
    must be url-encoded so Moco parses the query string correctly."""
    calls["next_response"] = b"[]"
    client.search_suppliers("Müller & Co AG")
    call = calls["calls"][0]
    assert call["url"] == (
        "https://solar.mocoapp.com/api/v1/companies"
        "?type=supplier&term=M%C3%BCller+%26+Co+AG"
    )


def test_search_suppliers_strips_whitespace_before_querying(client, calls):
    """The OCR'd supplier_name often carries trailing whitespace; the
    stripped form is sent to Moco AND used for exact match locally."""
    calls["next_response"] = json.dumps([{"id": 100, "name": "FLYERALARM"}]).encode()
    result = client.search_suppliers("  FLYERALARM  ")
    assert result == [{"id": 100, "name": "FLYERALARM"}]
    call = calls["calls"][0]
    assert call["url"].endswith("term=FLYERALARM")


def test_search_suppliers_returns_empty_for_blank_input(client, calls):
    """Blank / None → no Moco call (avoids round-tripping when OCR
    couldn't extract a supplier name at all)."""
    assert client.search_suppliers("") == []
    assert client.search_suppliers("   ") == []
    assert calls["calls"] == []


def test_search_suppliers_returns_empty_on_404(client, calls):
    """A 404 from the list endpoint (account with no suppliers, or env
    quirk) becomes an empty result rather than an exception."""
    calls["next_status"] = 404
    assert client.search_suppliers("anything") == []


def test_search_suppliers_propagates_non_404_errors(client, calls):
    """A 5xx during company lookup must propagate so the handler can map
    it to 502 — silently treating it as "no match" would link no company
    on every retry, which is worse than failing loudly."""
    calls["next_status"] = 500
    with pytest.raises(urlerror.HTTPError):
        client.search_suppliers("anything")


def test_search_suppliers_handles_unexpected_response_shape(client, calls):
    """Defensive: if Moco's response shape ever changes (e.g. wraps results
    in a `data` key), don't crash — return empty so the OCR service falls
    back to "no company linked" rather than 500-ing on a schema drift."""
    calls["next_response"] = json.dumps({"data": []}).encode()
    assert client.search_suppliers("anything") == []
