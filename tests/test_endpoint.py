"""End-to-end tests for the FastAPI /api/moco-sync endpoint.

Uses the FastAPI TestClient with a stubbed target Moco HTTP API. Each test
posts a fixture JSON and asserts the auth pipeline, filters, and target-call
behavior end-to-end.
"""

import json

from tests.conftest import FIXTURES_DIR, sign, signed_headers


def post(client, body: bytes, headers: dict[str, str]):
    return client.post("/api/moco-sync", content=body, headers=headers)


def test_health_check(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.json() == {"message": "Hello, World!"}


def test_happy_path_creates_target_activity(client, stub_target_api):
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    r = post(client, body, signed_headers(body))

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["event"] == "create"
    assert payload["created_id"] == 99999999
    assert payload["project_id"] == 1234500001
    assert payload["task_id"] == 1234500002

    # GET projects + POST activity were both made.
    methods = [call[1] for call in stub_target_api["calls"]]
    assert methods == ["GET", "POST"]


def test_happy_path_updates_existing_target_activity(client, stub_target_api):
    from tests.conftest import load_fixture
    stub_target_api["activities_for_date_response"] = load_fixture(
        "target_activities_for_date.json"
    )
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    r = post(client, body, signed_headers(body, event="update"))

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["event"] == "update"
    assert payload["updated_id"] == 88888881
    assert "created_id" not in payload

    # GET activities (lookup) + GET projects (mapping) + PUT activity.
    assert [c[1] for c in stub_target_api["calls"]] == ["GET", "GET", "PUT"]


def test_update_upserts_when_target_missing(client, stub_target_api):
    """Update for an unknown source activity creates instead — function is
    stateless, so a missed create webhook self-heals on the next update."""
    stub_target_api["activities_for_date_response"] = []
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    r = post(client, body, signed_headers(body, event="update"))

    assert r.status_code == 200
    payload = r.json()
    assert payload["event"] == "update"
    assert payload["upserted"] is True
    assert payload["created_id"] == 99999999


def test_unmatched_falls_back_to_defaults_end_to_end(client, stub_target_api):
    body = (FIXTURES_DIR / "activity_create_unmatched.json").read_bytes()
    r = post(client, body, signed_headers(body))

    assert r.status_code == 200
    payload = r.json()
    assert payload["project_id"] == 947156885
    assert payload["task_id"] == 25339113


def test_wrong_user_is_skipped_without_calling_target(client, stub_target_api):
    body = (FIXTURES_DIR / "activity_create_wrong_user.json").read_bytes()
    r = post(client, body, signed_headers(body))

    assert r.status_code == 200
    assert r.json() == {"skipped": "user_filter"}
    assert stub_target_api["calls"] == []  # never reached the target API


def test_invalid_signature_returns_401(client, stub_target_api):
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    headers = signed_headers(body)
    headers["x-moco-signature"] = "0" * 64

    r = post(client, body, headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_signature"
    assert stub_target_api["calls"] == []


def test_stale_timestamp_returns_401(client, stub_target_api):
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    # 10 minutes in the past — outside the 300s window.
    import time
    stale_ms = int(time.time() * 1000) - 600_000
    headers = signed_headers(body, timestamp_ms=stale_ms)

    r = post(client, body, headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "timestamp_out_of_window"


def test_wrong_source_account_returns_401(client, stub_target_api):
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    headers = signed_headers(body, account_url="not-solar")

    r = post(client, body, headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "wrong_source_account"


def test_delete_event_is_skipped(client, stub_target_api):
    """Only create and update are handled; delete and other events are skipped."""
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    headers = signed_headers(body, event="delete")

    r = post(client, body, headers)
    assert r.status_code == 200
    assert r.json() == {"skipped": "event_not_handled", "event": "delete"}
    assert stub_target_api["calls"] == []


def test_non_activity_target_is_skipped(client, stub_target_api):
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    headers = signed_headers(body, target="Project")

    r = post(client, body, headers)
    assert r.status_code == 200
    assert r.json() == {"skipped": "not_activity_target"}
    assert stub_target_api["calls"] == []


def test_oversized_body_returns_413(client, stub_target_api):
    body = b"x" * (64 * 1024 + 1)
    headers = signed_headers(body)

    r = post(client, body, headers)
    assert r.status_code == 413
    assert stub_target_api["calls"] == []


def test_invalid_json_after_passing_auth_returns_400(client, stub_target_api):
    body = b"not-json-at-all"
    headers = signed_headers(body)

    r = post(client, body, headers)
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_json"


def test_missing_env_returns_500(client, monkeypatch, stub_target_api):
    monkeypatch.delenv("MOCO_WEBHOOK_SECRET", raising=False)
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    r = post(client, body, signed_headers(body))
    assert r.status_code == 500
    assert r.json()["detail"] == "server_misconfigured"
