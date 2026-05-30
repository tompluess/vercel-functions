"""Unit tests for MocoSyncService — project/task resolution and payload shape.

These talk to a stubbed urlopen so we assert the outbound URL, method, and
JSON body the service constructs, given the JSON fixtures as input.
"""

from api.moco_sync_service import MocoSyncService
from tests.conftest import load_fixture


DEFAULTS = dict(
    target_subdomain="skyr",
    target_api_key="test_api_key",
    target_company_id="761404231",
    default_project_id=947156885,
    default_task_id=25339113,
    source_account_url="solar",
)


def test_matched_activity_resolves_to_named_project_and_task(stub_target_api):
    source = load_fixture("activity_create_matched.json")
    service = MocoSyncService(**DEFAULTS)

    result = service.sync_create(source)

    assert result == {"created_id": 99999999,
                      "project_id": 1234500001,
                      "task_id": 1234500002}

    get_call, post_call = stub_target_api["calls"]
    assert get_call[1] == "GET"
    assert get_call[0] == "https://skyr.mocoapp.com/api/v1/projects?company_id=761404231"

    assert post_call[1] == "POST"
    assert post_call[0] == "https://skyr.mocoapp.com/api/v1/activities"
    assert post_call[2] == {
        "date": "2025-01-10",
        "description": "Implement webhook receiver",
        "project_id": 1234500001,
        "task_id": 1234500002,
        "seconds": 900,
        "remote_service": "",
        "remote_id": "solar:1064823757",
        "remote_url": "https://github.com/example/repo/pull/42",
        "tag": "backend",
    }


def test_unmatched_activity_falls_back_to_defaults(stub_target_api):
    source = load_fixture("activity_create_unmatched.json")
    service = MocoSyncService(**DEFAULTS)

    result = service.sync_create(source)

    assert result["project_id"] == 947156885
    assert result["task_id"] == 25339113

    _, post_call = stub_target_api["calls"]
    assert post_call[2]["project_id"] == 947156885
    assert post_call[2]["task_id"] == 25339113
    # Source ID is namespaced into remote_id; remote_service is left empty
    # because Moco rejects non-enum values server-side.
    assert post_call[2]["remote_id"] == "solar:1064823758"
    assert post_call[2]["remote_service"] == ""
    # null remote_url is coerced to empty string.
    assert post_call[2]["remote_url"] == ""


def test_after_project_fallback_task_name_is_resolved_against_default_project(
    stub_target_api,
):
    """When the source project doesn't match by name, the service falls back
    to the default project — and *then* searches that project's task list for
    the source task name. A matching task wins over the default task id."""
    source = load_fixture("activity_create_unmatched.json")
    projects = load_fixture("target_projects.json")
    projects[0]["tasks"].append({"id": 77777777, "name": "Phantom Task",
                                 "billable": True, "active": True})
    stub_target_api["projects_response"] = projects

    service = MocoSyncService(**DEFAULTS)
    result = service.sync_create(source)

    assert result["project_id"] == 947156885
    assert result["task_id"] == 77777777


def test_payload_uses_empty_strings_for_missing_optional_fields(stub_target_api):
    """`tag`, `remote_url`, and `description` are coerced to "" when null/missing.
    `remote_id` is always our namespaced string; `remote_service` is always ""."""
    source = {
        "id": 12345,
        "date": "2025-03-01",
        "description": None,
        "seconds": 600,
        "project": {"name": "Webseite und IT Services"},
        "task": {"name": "Development"},
    }
    service = MocoSyncService(**DEFAULTS)
    service.sync_create(source)

    _, post_call = stub_target_api["calls"]
    assert post_call[2]["description"] == ""
    assert post_call[2]["tag"] == ""
    assert post_call[2]["remote_url"] == ""
    assert post_call[2]["remote_id"] == "solar:12345"
    assert post_call[2]["remote_service"] == ""


def test_update_finds_existing_target_and_puts(stub_target_api):
    """Update path: GET /activities for the source date, find by
    (remote_service, remote_id), then PUT /activities/{id}."""
    source = load_fixture("activity_create_matched.json")
    stub_target_api["activities_for_date_response"] = load_fixture(
        "target_activities_for_date.json"
    )

    service = MocoSyncService(**DEFAULTS)
    result = service.sync_update(source)

    assert result == {"updated_id": 88888881,
                      "project_id": 1234500001,
                      "task_id": 1234500002}

    # Three calls: GET activities (lookup), GET projects (mapping), PUT activity.
    assert [c[1] for c in stub_target_api["calls"]] == ["GET", "GET", "PUT"]
    lookup, _, put_call = stub_target_api["calls"]
    assert lookup[0] == (
        "https://skyr.mocoapp.com/api/v1/activities"
        "?from=2025-01-10&to=2025-01-10"
    )
    assert put_call[0] == "https://skyr.mocoapp.com/api/v1/activities/88888881"
    # The updated payload carries the new description from the source.
    assert put_call[2]["description"] == "Implement webhook receiver"
    assert put_call[2]["remote_id"] == "solar:1064823757"
    assert put_call[2]["remote_service"] == ""


def test_update_upserts_when_no_target_activity_matches(stub_target_api):
    """If no existing target activity matches the source remote_id, fall
    through to POST (create) — the function is stateless and can't
    distinguish a missed create-webhook from a real new activity."""
    source = load_fixture("activity_create_matched.json")
    stub_target_api["activities_for_date_response"] = []

    service = MocoSyncService(**DEFAULTS)
    result = service.sync_update(source)

    assert result == {"created_id": 99999999,
                      "project_id": 1234500001,
                      "task_id": 1234500002,
                      "upserted": True}
    assert [c[1] for c in stub_target_api["calls"]] == ["GET", "GET", "POST"]


def test_update_ignores_activities_from_other_source_namespaces(stub_target_api):
    """Activities whose remote_id is namespaced to a different source account
    must NOT be matched — otherwise unrelated integrations would collide."""
    source = load_fixture("activity_create_matched.json")
    activities = load_fixture("target_activities_for_date.json")
    # Drop our "solar:..." entry so only the "different-account:..." entry
    # with the same numeric ID survives. Service should treat as not-found
    # and upsert.
    activities = [a for a in activities
                  if not str(a.get("remote_id") or "").startswith("solar:")]
    stub_target_api["activities_for_date_response"] = activities

    service = MocoSyncService(**DEFAULTS)
    result = service.sync_update(source)

    assert "created_id" in result
    assert result.get("upserted") is True


def test_delete_finds_existing_target_and_issues_delete(stub_target_api):
    """Delete path: GET /activities for the source date, find by
    (remote_service, remote_id), then DELETE /activities/{id}. No project
    mapping required."""
    source = load_fixture("activity_create_matched.json")
    stub_target_api["activities_for_date_response"] = load_fixture(
        "target_activities_for_date.json"
    )

    service = MocoSyncService(**DEFAULTS)
    result = service.sync_delete(source)

    assert result == {"deleted_id": 88888881}
    assert [c[1] for c in stub_target_api["calls"]] == ["GET", "DELETE"]
    delete_call = stub_target_api["calls"][1]
    assert delete_call[0] == "https://skyr.mocoapp.com/api/v1/activities/88888881"
    assert delete_call[2] is None  # no body on DELETE


def test_delete_is_idempotent_when_no_target_found(stub_target_api):
    """If the source activity was never synced (or already removed), DELETE
    skips with target_not_found — desired state already holds."""
    source = load_fixture("activity_create_matched.json")
    stub_target_api["activities_for_date_response"] = []

    service = MocoSyncService(**DEFAULTS)
    result = service.sync_delete(source)

    assert result == {"skipped": "target_not_found"}
    assert [c[1] for c in stub_target_api["calls"]] == ["GET"]


def test_delete_does_not_match_other_source_namespaces(stub_target_api):
    """Same numeric ID under a different source-namespace must not be deleted."""
    source = load_fixture("activity_create_matched.json")
    activities = load_fixture("target_activities_for_date.json")
    activities = [a for a in activities
                  if not str(a.get("remote_id") or "").startswith("solar:")]
    stub_target_api["activities_for_date_response"] = activities

    service = MocoSyncService(**DEFAULTS)
    result = service.sync_delete(source)

    assert result == {"skipped": "target_not_found"}
    assert [c[1] for c in stub_target_api["calls"]] == ["GET"]
