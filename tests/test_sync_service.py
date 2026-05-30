"""Unit tests for MocoSyncService — project/task resolution and payload shape.

These talk to a stubbed urlopen so we assert the outbound URL, method, and
JSON body the service constructs, given the JSON fixtures as input.
"""

import json

from api.moco_sync_service import MocoSyncService
from tests.conftest import load_fixture


DEFAULTS = dict(
    target_subdomain="skyr",
    target_api_key="test_api_key",
    target_company_id="761404231",
    default_project_id=947156885,
    default_task_id=25339113,
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
        "remote_service": "github",
        "remote_id": "PR-42",
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
    # None-valued source fields are coerced to empty strings in the payload.
    assert post_call[2]["remote_id"] == ""
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
    """`tag`, `remote_*`, and `description` are coerced to "" when null/missing."""
    source = {
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
    assert post_call[2]["remote_service"] == ""
    assert post_call[2]["remote_id"] == ""
    assert post_call[2]["remote_url"] == ""
