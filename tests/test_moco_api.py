"""Unit tests for MocoAPI — URL/header/payload shape over the urlopen stub."""

import logging

from api.moco_api import MocoAPI


DEFAULTS = dict(subdomain="skyr", api_key="test_api_key",
                company_id="761404231")


def test_list_projects_url_and_auth_headers(stub_target_api):
    api = MocoAPI(**DEFAULTS)
    api.list_projects()

    url, method, payload = stub_target_api["calls"][0]
    assert method == "GET"
    assert url == "https://skyr.mocoapp.com/api/v1/projects?company_id=761404231"
    assert payload is None


def test_list_activities_url_uses_date_range(stub_target_api):
    api = MocoAPI(**DEFAULTS)
    api.list_activities(date_from="2025-01-01", date_to="2025-01-10")

    url, method, _ = stub_target_api["calls"][0]
    assert method == "GET"
    assert url == ("https://skyr.mocoapp.com/api/v1/activities"
                   "?from=2025-01-01&to=2025-01-10")


def test_list_activities_logs_x_total_for_pagination_diagnostics(
    stub_target_api, caplog,
):
    """The lookup logs X-Total (and returned count) at INFO so pagination
    boundaries are visible — if X-Total > returned, matches on later pages
    will be missed by the single-page scan."""
    from tests.conftest import load_fixture
    stub_target_api["activities_for_date_response"] = load_fixture(
        "target_activities_for_date.json"
    )
    stub_target_api["activities_for_date_total"] = 247  # pretend many pages
    caplog.set_level(logging.INFO, logger="moco_sync")

    api = MocoAPI(**DEFAULTS)
    api.list_activities(date_from="2025-01-10", date_to="2025-01-10")

    assert "activities lookup" in caplog.text
    assert "X-Total=247" in caplog.text
    assert "returned=3" in caplog.text  # the fixture has 3 entries


def test_create_activity_posts_json_payload(stub_target_api):
    payload = {"date": "2025-01-10", "seconds": 900, "project_id": 1, "task_id": 2}
    api = MocoAPI(**DEFAULTS)
    result = api.create_activity(payload)

    assert result == {"id": 99999999}
    url, method, captured = stub_target_api["calls"][0]
    assert method == "POST"
    assert url == "https://skyr.mocoapp.com/api/v1/activities"
    assert captured == payload


def test_update_activity_puts_to_id_url(stub_target_api):
    payload = {"date": "2025-01-10", "seconds": 1200, "project_id": 1, "task_id": 2}
    api = MocoAPI(**DEFAULTS)
    result = api.update_activity(88888881, payload)

    assert result == {"id": 88888881}
    url, method, captured = stub_target_api["calls"][0]
    assert method == "PUT"
    assert url == "https://skyr.mocoapp.com/api/v1/activities/88888881"
    assert captured == payload


def test_delete_activity_issues_delete_with_no_body(stub_target_api):
    api = MocoAPI(**DEFAULTS)
    api.delete_activity(88888881)

    url, method, captured = stub_target_api["calls"][0]
    assert method == "DELETE"
    assert url == "https://skyr.mocoapp.com/api/v1/activities/88888881"
    assert captured is None
