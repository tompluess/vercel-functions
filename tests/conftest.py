"""Shared pytest fixtures: env vars, fixture JSON loader, and a FastAPI TestClient
with the target Moco HTTP calls stubbed via urlopen patching."""

import hashlib
import hmac
import io
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

FIXTURES_DIR = Path(__file__).parent / "fixtures"

TEST_ENV = {
    "MOCO_WEBHOOK_SECRET": "test_secret",
    "MOCO_SOURCE_ACCOUNT_URL": "solar",
    "MOCO_USER_ID_FILTER": "933719334",
    "MOCO_TARGET_SUBDOMAIN": "skyr",
    "MOCO_TARGET_API_KEY": "test_api_key",
    "MOCO_TARGET_COMPANY_ID": "761404231",
    "MOCO_TARGET_DEFAULT_PROJECT_ID": "947156885",
    "MOCO_TARGET_DEFAULT_TASK_ID": "25339113",
}


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text())


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, *, account_url: str = "solar",
                   event: str = "create", target: str = "Activity",
                   secret: str = "test_secret",
                   timestamp_ms: int | None = None) -> dict[str, str]:
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    return {
        "x-moco-signature": sign(secret, body),
        "x-moco-timestamp": str(ts),
        "x-moco-account-url": account_url,
        "x-moco-event": event,
        "x-moco-target": target,
        "content-type": "application/json",
    }


@pytest.fixture
def fixture_body():
    """Returns (raw_bytes, parsed_dict) for a given fixture name."""
    def _load(name: str) -> tuple[bytes, dict]:
        raw = (FIXTURES_DIR / name).read_bytes()
        return raw, json.loads(raw)
    return _load


@pytest.fixture
def set_env(monkeypatch):
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    return TEST_ENV


class FakeUrlopenResponse:
    """Minimal context-manager response object compatible with urlopen()."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


@pytest.fixture
def stub_target_api(monkeypatch):
    """Patches urlopen in api.moco_sync_service. Captures every outbound
    request as (url, method, payload-or-None) so tests can assert exact
    behavior. State dict lets individual tests override responses.
    """
    state = {
        "projects_response": load_fixture("target_projects.json"),
        "activities_for_date_response": [],
        "next_post_response": {"id": 99999999},
        "next_put_response": {"id": 88888881},
        "calls": [],
    }

    def fake_urlopen(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        data = req.data
        payload = json.loads(data) if data else None
        state["calls"].append((url, method, payload))

        if method == "GET" and "/projects" in url:
            return FakeUrlopenResponse(
                json.dumps(state["projects_response"]).encode()
            )
        if method == "GET" and "/activities" in url:
            return FakeUrlopenResponse(
                json.dumps(state["activities_for_date_response"]).encode()
            )
        if method == "POST" and url.endswith("/activities"):
            return FakeUrlopenResponse(
                json.dumps(state["next_post_response"]).encode()
            )
        if method == "PUT" and "/activities/" in url:
            return FakeUrlopenResponse(
                json.dumps(state["next_put_response"]).encode()
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    import api.moco_sync_service as svc
    monkeypatch.setattr(svc.urlrequest, "urlopen", fake_urlopen)
    return state


@pytest.fixture
def client(set_env, stub_target_api):
    from api.index import app
    return TestClient(app)
