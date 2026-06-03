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


def test_wrong_user_is_rejected_with_422(client, stub_target_api):
    body = (FIXTURES_DIR / "activity_create_wrong_user.json").read_bytes()
    r = post(client, body, signed_headers(body))

    assert r.status_code == 422
    assert r.json()["detail"] == "user_filter: 555555555"
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


def test_delete_event_removes_target_activity(client, stub_target_api):
    from tests.conftest import load_fixture
    stub_target_api["activities_for_date_response"] = load_fixture(
        "target_activities_for_date.json"
    )
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    r = post(client, body, signed_headers(body, event="delete"))

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["event"] == "delete"
    assert payload["deleted_id"] == 88888881

    # GET activities (lookup) + DELETE activity. No projects mapping needed.
    methods = [c[1] for c in stub_target_api["calls"]]
    assert methods == ["GET", "DELETE"]
    delete_call = stub_target_api["calls"][1]
    assert delete_call[0] == "https://skyr.mocoapp.com/api/v1/activities/88888881"


def test_delete_handles_minimal_payload_from_moco(client, stub_target_api):
    """Moco's real Activity:delete webhook ships only `{id}` — no `user`,
    no `date`, no `project`. The user-id must come from x-moco-user-id, and
    the lookup must widen its date range when no date is in the body."""
    from tests.conftest import load_fixture
    stub_target_api["activities_for_date_response"] = load_fixture(
        "target_activities_for_date.json"
    )
    body = (FIXTURES_DIR / "activity_delete_minimal.json").read_bytes()
    r = post(client, body, signed_headers(body, event="delete"))

    assert r.status_code == 200
    payload = r.json()
    assert payload["event"] == "delete"
    assert payload["deleted_id"] == 88888881

    # GET /activities should have widened to a date range (not a single day).
    lookup_url = stub_target_api["calls"][0][0]
    assert "?from=" in lookup_url and "&to=" in lookup_url
    qs = lookup_url.split("?")[1]
    params = dict(p.split("=") for p in qs.split("&"))
    assert params["from"] != params["to"]  # widened, not single-day


def test_delete_target_missing_acks_200_and_notifies(client, stub_target_api):
    """If the corresponding target activity isn't found, the mismatch is an
    application error a retry can't fix: ACK with 200 (ok=false) so Moco stops
    retrying, and fire a Telegram alert instead of the old 404."""
    stub_target_api["activities_for_date_response"] = []
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    r = post(client, body, signed_headers(body, event="delete"))

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is False
    assert payload["error"] == "target_not_found: solar:1064823757"
    # The lookup happened and a Telegram alert was sent — no DELETE issued.
    assert [c[1] for c in stub_target_api["calls"]] == ["GET", "POST"]
    assert any("api.telegram.org" in c[0] for c in stub_target_api["calls"])


def test_unknown_event_is_rejected_with_422(client, stub_target_api):
    """Events outside {create, update, delete} are rejected with 422."""
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    headers = signed_headers(body, event="archive")

    r = post(client, body, headers)
    assert r.status_code == 422
    assert r.json()["detail"] == "event_not_handled: archive"
    assert stub_target_api["calls"] == []


def test_non_activity_target_is_rejected_with_422(client, stub_target_api):
    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    headers = signed_headers(body, target="Project")

    r = post(client, body, headers)
    assert r.status_code == 422
    assert r.json()["detail"] == "not_activity_target: Project"
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


def test_unexpected_exception_acks_200_notifies_and_logs_payload(
    client, stub_target_api, monkeypatch, caplog
):
    """An unexpected internal exception is an application error a retry can't
    fix: log 'Exception, Error on Request' with the parsed payload, fire a
    Telegram alert, and ACK with 200 (ok=false) so Moco stops retrying."""
    import logging
    import api.moco_sync_service as svc

    def boom(self, source):
        raise RuntimeError("unexpected internal failure")

    monkeypatch.setattr(svc.MocoSyncService, "sync_create", boom)
    caplog.set_level(logging.ERROR, logger="moco_sync")

    body = (FIXTURES_DIR / "activity_create_matched.json").read_bytes()
    r = post(client, body, signed_headers(body))

    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is False
    assert payload["error"] == "internal_error: unexpected internal failure"
    # A Telegram alert was sent.
    assert any("api.telegram.org" in c[0] for c in stub_target_api["calls"])
    assert "Error on request with payload" in caplog.text
    # The exception message AND the full parsed payload must appear in the log.
    assert "unexpected internal failure" in caplog.text
    assert "Implement webhook receiver" in caplog.text
