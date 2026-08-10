"""Unit tests for KVClient — Upstash REST command encoding + result parsing.

urlopen is patched so the tests exercise the real wrapper but touch no network.
"""

import json

import pytest

from api.kv_client import KVClient
from tests.conftest import FakeUrlopenResponse


@pytest.fixture
def kv_calls(monkeypatch):
    calls: list[dict] = []
    responses: dict[str, object] = {"result": "OK"}

    def fake_urlopen(req, timeout=None):
        calls.append({
            "method": req.get_method(),
            "url": req.full_url,
            "headers": dict(req.headers),
            "data": req.data,
        })
        return FakeUrlopenResponse(json.dumps(responses).encode())

    import api.kv_client as mod
    monkeypatch.setattr(mod.urlrequest, "urlopen", fake_urlopen)
    return {"calls": calls, "responses": responses}


def test_command_posts_json_array_with_bearer(kv_calls):
    KVClient(url="https://x.upstash.io", token="tok").set("k", "v")

    call = kv_calls["calls"][0]
    assert call["method"] == "POST"
    assert call["url"] == "https://x.upstash.io"
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert json.loads(call["data"]) == ["SET", "k", "v"]


def test_url_trailing_slash_is_stripped(kv_calls):
    KVClient(url="https://x.upstash.io/", token="tok").get("k")
    assert kv_calls["calls"][0]["url"] == "https://x.upstash.io"


def test_get_returns_result_value(kv_calls):
    kv_calls["responses"]["result"] = "hello"
    assert KVClient(url="https://x.upstash.io", token="t").get("k") == "hello"


def test_get_missing_key_returns_none(kv_calls):
    kv_calls["responses"]["result"] = None
    assert KVClient(url="https://x.upstash.io", token="t").get("nope") is None


def test_set_with_ex_appends_expiry(kv_calls):
    KVClient(url="https://x.upstash.io", token="t").set("k", "v", ex=15)
    assert json.loads(kv_calls["calls"][0]["data"]) == ["SET", "k", "v", "EX", "15"]


def test_set_nx_true_when_ok(kv_calls):
    kv_calls["responses"]["result"] = "OK"
    assert KVClient(url="https://x.upstash.io", token="t").set_nx("k", "1", ex=15) is True
    assert json.loads(kv_calls["calls"][0]["data"]) == ["SET", "k", "1", "NX", "EX", "15"]


def test_set_nx_false_when_null(kv_calls):
    kv_calls["responses"]["result"] = None
    assert KVClient(url="https://x.upstash.io", token="t").set_nx("k", "1", ex=15) is False


def test_delete_issues_del(kv_calls):
    KVClient(url="https://x.upstash.io", token="t").delete("k")
    assert json.loads(kv_calls["calls"][0]["data"]) == ["DEL", "k"]
